# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Helpers shared by the server tests: a fake Dialogs, a running server and a raw-Host HTTP client."""

import http.client
import json
from dataclasses import dataclass, field
from pathlib import Path

from conftest import make_synthetic_analysis, wait_until

from rubato_waveform import session as session_mod
from rubato_waveform.server import AppServer
from rubato_waveform.session import Session

REQUIRED_HEADERS = {
    "x-content-type-options": "nosniff",
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
}


class FakeDialogs:
    def __init__(self) -> None:
        self.audio: str | None = None
        self.output: str | None = None

    def pick_audio(self, start_dir):
        return self.audio

    def pick_output(self, start_dir, suggested_name):
        return self.output

    def reveal(self, path):
        pass


@dataclass
class Resp:
    status: int
    headers: dict[str, str]
    body: bytes
    raw_headers: list = field(default_factory=list)

    def json(self):
        return json.loads(self.body)


def assert_no_cors(resp: Resp) -> None:
    assert not [name for name, _ in resp.raw_headers if name.lower().startswith("access-control-")]


def request(srv: AppServer, method: str, path: str, *, headers: dict | None = None, body: bytes | None = None,
            host: str | None = "default", token: str | None = "default", json_body: object = None,
            content_type: str | None = "default") -> Resp:
    """One request on a fresh connection with full control over Host and the other headers.

    ``host="default"`` sends the right Host, ``None`` omits it. ``token="default"`` sends the real
    token header. Every response is checked for the absence of Access-Control-* headers.
    """
    if json_body is not None:
        body = json.dumps(json_body).encode()
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=20)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host == "default":
            conn.putheader("Host", srv.host)
        elif host is not None:
            conn.putheader("Host", host)
        if token == "default":
            conn.putheader("X-Rubato-Token", srv.token)
        elif token is not None:
            conn.putheader("X-Rubato-Token", token)
        if method == "POST" and content_type == "default":
            conn.putheader("Content-Type", "application/json")
        elif content_type not in (None, "default"):
            conn.putheader("Content-Type", content_type)
        if body is not None and "Content-Length" not in (headers or {}):
            conn.putheader("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        conn.endheaders(body if body else None)
        response = conn.getresponse()
        data = response.read() if method != "HEAD" else b""
        resp = Resp(response.status, {k.lower(): v for k, v in response.getheaders()}, data, response.getheaders())
    finally:
        conn.close()
    assert_no_cors(resp)
    return resp


def post(srv: AppServer, path: str, obj: object = None, **kwargs) -> Resp:
    return request(srv, "POST", path, json_body={} if obj is None else obj, **kwargs)


def get(srv: AppServer, path: str, **kwargs) -> Resp:
    return request(srv, "GET", path, **kwargs)


def make_server(tmp_path: Path, monkeypatch, *, dev: bool = False, ui: bool = True, dev_dialogs=None):
    """A started AppServer over a real Session; returns (server, session, dialogs)."""
    monkeypatch.setattr(session_mod, "analyze_audio",
                        lambda path, progress=None, cancel=None: make_synthetic_analysis())
    dialogs = dev_dialogs or FakeDialogs()
    session = Session(dialogs, tmp_path / "cfg")
    ui_dir = tmp_path / "site" / "ui"
    if ui:
        (ui_dir / "css").mkdir(parents=True)
        (ui_dir / "index.html").write_text("<!doctype html><title>t</title>")
        (ui_dir / "css" / "a.css").write_text("body{}")
        (ui_dir / "notes.exe").write_bytes(b"MZ")
        (tmp_path / "site" / "secret.txt").write_text("TOP SECRET")
    srv = AppServer(session, dev=dev, ui_dir=ui_dir, dev_dialogs=dev_dialogs)
    srv.start()
    return srv, session, dialogs


def select_audio(session: Session, path: Path) -> None:
    session.select_audio(path)
    assert wait_until(lambda: session.state()["analysis"]["state"] == "ready")
