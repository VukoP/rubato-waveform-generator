# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""The app's local HTTP server: JSON API, static UI files, preview frames and audio with Range.

This is the application's attack surface, so it is deliberately small and strict. It binds to
127.0.0.1 on a random port and every request must pass, in this order: exact ``Host`` (stops
DNS rebinding), exact ``Origin`` if one is sent, then the per-run secret token, then routing.
No CORS header is ever sent and POST bodies must be ``application/json`` (which forces a
preflight on cross-site requests). Static files come from a dict built once at startup, so no
request data ever reaches a filesystem call; the audio file is whatever the session says, never
a path from the request.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import os
import re
import secrets
import socket
import stat
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .catalog import catalog
from .session import NoAnalysisError, Session, SessionError, _clean_message

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
CHUNK = 64 * 1024
CONNECTION_TIMEOUT = 30.0      # seconds an idle or stalled connection may hold a thread
MAX_CONNECTIONS = 64           # concurrent connections; more are closed at once, without a thread
TOKEN_HEADER = "X-Rubato-Token"

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; media-src 'self'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")

_STATIC_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}
_CSP_MIME = ("text/html", "image/svg+xml")

_RANGE_RE = re.compile(r"bytes=([0-9]{1,100})?-([0-9]{1,100})?")


class _Reject(Exception):
    """Ends a request with an error status and a short, safe message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _Binary:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self.body = body
        self.headers = headers


def parse_range(header: str | None, size: int) -> tuple[str, int, int]:
    """Interprets a Range header against a file size.

    Returns ``("full", 0, size - 1)`` when there is no usable single range (RFC 9110 allows
    ignoring a malformed or multi-range header), ``("range", start, end)`` with an inclusive
    clamped end, or ``("unsatisfiable", 0, 0)``.
    """
    full = ("full", 0, size - 1)
    if header is None:
        return full
    match = _RANGE_RE.fullmatch(header.strip())
    if match is None or (match[1] is None and match[2] is None):
        return full
    if match[1] is None:                       # suffix: the last n bytes
        count = int(match[2])
        if count == 0 or size == 0:
            return ("unsatisfiable", 0, 0)
        return ("range", max(size - count, 0), size - 1)
    start = int(match[1])
    end = int(match[2]) if match[2] is not None else None
    if end is not None and end < start:
        return full
    if start >= size:
        return ("unsatisfiable", 0, 0)
    end = size - 1 if end is None else end
    return ("range", start, min(end, size - 1))


def _load_static(ui_dir: Path) -> dict[str, tuple[bytes, str]]:
    files: dict[str, tuple[bytes, str]] = {}
    root = str(ui_dir)
    for folder, _dirs, names in os.walk(root):
        for name in names:
            full = os.path.join(folder, name)
            mime = _STATIC_MIME.get(os.path.splitext(name)[1].lower())
            if mime is None or os.path.islink(full):
                continue
            try:
                data = Path(full).read_bytes()
            except OSError:
                continue
            relative = os.path.relpath(full, root).replace(os.sep, "/")
            files["/" + relative] = (data, mime)
    return files


def _object(data: object, *names: str) -> tuple:
    """The values of exactly the fields ``names`` from a request object (strict: no extras)."""
    if not isinstance(data, dict) or set(data) != set(names):
        raise ValueError("expected exactly the fields: " + ", ".join(names) if names else "expected an object")
    return tuple(data[name] for name in names)


def _reject_constant(_name: str):
    raise ValueError("NaN and Infinity are not allowed")


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("number out of range")
    return value


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False    # on Windows SO_REUSEADDR would let another process share the port
    app: AppServer

    def __init__(self, address, handler) -> None:
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, handler)

    def server_bind(self) -> None:
        if sys.platform == "win32":
            # Without this, another local process can bind the same port and receive our requests.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()      # no thread took the connection over
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address) -> None:
        """Never writes to stderr (the default prints a traceback): a client that leaves is normal."""
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionError, TimeoutError)):
            return
        log.exception("unhandled error on a connection")


class AppServer:
    def __init__(self, session: Session, *, dev: bool = False, ui_dir: Path | None = None,
                 dev_dialogs: object | None = None) -> None:
        self.session = session
        self.dev = dev
        self._dev_dialogs = dev_dialogs
        ui = Path(__file__).parent / "ui" if ui_dir is None else Path(ui_dir)
        self._static = _load_static(ui) if ui.is_dir() else {}
        self.token = secrets.token_urlsafe(32)
        self._token_bytes = self.token.encode("ascii")
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.app = self
        self.port: int = self._httpd.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.host}"
        self.url = f"{self.origin}/?token={self.token}"
        self._thread: threading.Thread | None = None
        self._routes: dict[str, dict[str, Callable]] = {
            "/api/catalog": {"GET": self._catalog},
            "/api/state": {"GET": lambda _d: session.state()},
            "/api/settings": {"GET": lambda _d: session.get_settings(), "POST": session.save_settings},
            "/api/open": {"POST": lambda _d: {"opened": session.open_audio()}},
            "/api/export": {"POST": self._export},
            "/api/export/cancel": {"POST": self._cancel},
            "/api/reveal": {"POST": lambda _d: {"revealed": session.reveal_last()}},
            "/api/ffmpeg/recheck": {"POST": lambda _d: session.recheck_ffmpeg()},
            "/api/audio/fallback": {"POST": self._fallback},
            "/api/frame": {"POST": self._frame},
        }
        if dev:
            self._routes["/api/dev/select"] = {"POST": self._dev_select}
            if dev_dialogs is not None:
                self._routes["/api/dev/next-output"] = {"POST": self._dev_next_output}
                self._routes["/api/dev/reveals"] = {"POST": lambda _d: {"paths": list(dev_dialogs.reveals)}}

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                            name="rubato-http", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            self._httpd.shutdown()
            thread.join(5.0)
        self._httpd.server_close()

    def token_ok(self, supplied: str | None) -> bool:
        if supplied is None:
            return False
        # Bytes, never str: compare_digest raises TypeError on non-ASCII str.
        return hmac.compare_digest(supplied.encode("utf-8", "replace"), self._token_bytes)

    def route(self, path: str) -> dict[str, Callable] | None:
        return self._routes.get(path)

    def static_file(self, path: str) -> tuple[bytes, str] | None:
        return self._static.get("/index.html" if path == "/" else path)

    # -------------------------------------------------------------- handlers

    @staticmethod
    def _catalog(_data) -> dict:
        return catalog()

    def _export(self, data) -> dict:
        (look,) = _object(data, "look")
        return {"started": self.session.start_export(look)}

    def _cancel(self, _data) -> dict:
        return {"ok": self.session.cancel_export()}

    def _fallback(self, _data) -> dict:
        self.session.ensure_playable()
        return {"ok": True}

    def _dev_select(self, data) -> dict:
        (path,) = _object(data, "path")
        self.session.select_audio(path)
        return {"ok": True}

    def _dev_next_output(self, data) -> dict:
        (path,) = _object(data, "path")
        if type(path) is not str:
            raise ValueError("path: expected a string")
        self._dev_dialogs.next_output = path
        return {"ok": True}

    def _frame(self, data) -> _Binary:
        look, t, max_w, max_h = _object(data, "look", "t", "max_w", "max_h")
        width, height, rgba = self.session.render_preview(look, t, max_w, max_h)
        return _Binary(rgba, {"X-W": str(width), "X-H": str(height)})


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = CONNECTION_TIMEOUT
    disable_nagle_algorithm = True
    server: _Server

    # ------------------------------------------------------------ plumbing

    def version_string(self) -> str:
        return "Rubato"

    def parse_request(self) -> bool:
        ok = super().parse_request()
        if ok and self.request_version == "HTTP/0.9":      # a bare "GET /path": no headers to check
            self.send_error(400, "Bad request.")
            return False
        return ok

    def log_message(self, format, *args) -> None:
        """Silent: stderr is None in a windowed app, and request lines would contain the token."""

    def send_error(self, code, message=None, explain=None) -> None:
        self._send_json(code, {"error": message or "Request refused."})

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None,
              length: int | None = None) -> None:
        self._started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if content_type.split(";")[0] in _CSP_MIME:
            self.send_header("Content-Security-Policy", CSP)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        if status >= 400:
            self.send_header("Connection", "close")    # a body we did not read must not be parsed as a request
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _send_json(self, status: int, obj: object, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(obj, allow_nan=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _dispatch(self, method: str) -> None:
        try:
            self._dispatch_request(method)
        except OSError:                                # also when the client left during an error reply
            self.close_connection = True

    def _dispatch_request(self, method: str) -> None:
        self._started = False
        try:
            self._handle(method)
        except _Reject as exc:
            self._send_json(exc.status, {"error": exc.message})
        except NoAnalysisError as exc:
            self._send_json(409, {"error": _clean_message(exc)})
        except (SessionError, ValueError) as exc:
            self._send_json(400, {"error": _clean_message(exc)})
        except (BrokenPipeError, ConnectionError, TimeoutError):
            self.close_connection = True               # the client left or stalled: normal, not an error
        except Exception:
            log.exception("unhandled error while serving a request")
            self.close_connection = True
            if not self._started:
                try:
                    self._send_json(500, {"error": "Internal error."})
                except OSError:
                    pass

    do_GET = lambda self: self._dispatch("GET")
    do_HEAD = lambda self: self._dispatch("HEAD")
    do_POST = lambda self: self._dispatch("POST")
    do_OPTIONS = lambda self: self._dispatch("OPTIONS")
    do_PUT = lambda self: self._dispatch("PUT")
    do_DELETE = lambda self: self._dispatch("DELETE")
    do_PATCH = lambda self: self._dispatch("PATCH")

    # ------------------------------------------------------------- routing

    def _handle(self, method: str) -> None:
        app = self.server.app
        if self.headers.get_all("Host") != [app.host]:
            raise _Reject(403, "Forbidden.")
        origins = self.headers.get_all("Origin")
        if origins is not None and origins != [app.origin]:
            raise _Reject(403, "Forbidden.")
        if method not in ("GET", "HEAD", "POST"):
            raise _Reject(405, "Method not allowed.")
        if method != "POST" and (self.headers.get("Content-Length") or self.headers.get("Transfer-Encoding")):
            self.close_connection = True               # an unexpected body must not turn into a second request
        path, _, query = self.path.partition("?")
        if path == "/api" or path.startswith("/api/"):
            self._api(method, path, query)
        else:
            self._serve_static(method, path)

    def _api(self, method: str, path: str, query: str) -> None:
        app = self.server.app
        is_audio = path == "/api/audio" and method in ("GET", "HEAD")
        if is_audio:
            params = self._params(query)
            supplied = params.get("token", [None])[0]
        else:
            supplied = self.headers.get(TOKEN_HEADER)
        if not app.token_ok(supplied):
            raise _Reject(401, "Unauthorized.")
        if is_audio:
            self._serve_audio(params)
            return
        methods = app.route(path)
        if methods is None:
            raise _Reject(404, "Not found.")
        handler = methods.get(method)
        if handler is None:
            raise _Reject(405, "Method not allowed.")
        data = self._read_json() if method == "POST" else None
        result = handler(data)
        if isinstance(result, _Binary):
            self._send(200, result.body, "application/octet-stream", result.headers)
        else:
            self._send_json(200, result)

    @staticmethod
    def _params(query: str) -> dict[str, list[str]]:
        try:
            return parse_qs(query, keep_blank_values=True, max_num_fields=10)
        except ValueError:
            return {}

    def _serve_static(self, method: str, path: str) -> None:
        if method == "POST":
            raise _Reject(405, "Method not allowed.")
        found = self.server.app.static_file(path)
        if found is None:
            raise _Reject(404, "Not found.")
        self._send(200, found[0], found[1])

    # ---------------------------------------------------------------- body

    def _read_json(self) -> dict:
        parts = [p.strip() for p in (self.headers.get("Content-Type") or "").split(";")]
        if parts[0].lower() != "application/json" or any(
                p.replace(" ", "").lower() != "charset=utf-8" for p in parts[1:]):
            raise _Reject(415, "Content-Type must be application/json.")
        if self.headers.get("Transfer-Encoding") is not None:
            raise _Reject(400, "Chunked bodies are not accepted.")
        lengths = self.headers.get_all("Content-Length")
        if lengths is None:
            raise _Reject(411, "Content-Length is required.")
        text = lengths[0].strip()
        if len(lengths) != 1 or not text.isascii() or not text.isdigit() or len(text) > 12:
            raise _Reject(400, "Invalid Content-Length.")
        length = int(text)
        if length > MAX_BODY:
            raise _Reject(413, "Request body too large.")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise _Reject(400, "Incomplete request body.")
        try:
            data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, parse_float=_parse_float)
        except (ValueError, RecursionError):               # includes JSONDecodeError and UnicodeDecodeError
            raise _Reject(400, "Invalid JSON.") from None
        if not isinstance(data, dict):
            raise _Reject(400, "Expected a JSON object.")
        return data

    # --------------------------------------------------------------- audio

    def _serve_audio(self, params: dict[str, list[str]]) -> None:
        session = self.server.app.session
        info = session.audio_file()
        if info is None or params.get("v", [None])[0] != str(info[2]):
            raise _Reject(404, "Not found.")
        path, mime, _version = info
        try:
            handle = open(path, "rb")  # noqa: SIM115 - closed below
        except OSError:
            raise _Reject(404, "Not found.") from None
        with handle:
            status = os.fstat(handle.fileno())
            if not stat.S_ISREG(status.st_mode):
                raise _Reject(404, "Not found.")
            size = status.st_size
            kind, start, end = parse_range(self.headers.get("Range"), size)
            headers = {"Accept-Ranges": "bytes"}
            if kind == "unsatisfiable":
                headers["Content-Range"] = f"bytes */{size}"
                self._send_json(416, {"error": "Range not satisfiable."}, headers)
                return
            code = 200
            if kind == "range":
                code = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            length = end - start + 1 if size else 0
            self._send(code, b"", mime, headers, length=length)
            if self.command == "HEAD" or length == 0:
                return
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(CHUNK, remaining))
                if not chunk:                              # the file shrank: do not pretend we sent it all
                    self.close_connection = True
                    return
                self.wfile.write(chunk)
                remaining -= len(chunk)


__all__ = ["AppServer", "parse_range"]
