# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Native window entry point: ``rubato-waveform-app`` / ``python -m rubato_waveform.app``.

pywebview is imported only inside the functions that open a window, so the CLI, the tests and
Linux CI never need it. The session token lives only in the URL handed to the window: it is never
logged, printed in normal mode or passed as a process argument.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import wave
from pathlib import Path
from types import SimpleNamespace

from . import __version__, ffmpeg_tools
from .catalog import default_look_dict
from .server import AppServer
from .session import ALLOWED_EXTENSIONS, Session, SessionError, reveal_in_file_manager

APP_NAME = "Rubato Waveform Generator"
WINDOW_SIZE = (1400, 900)
WINDOW_MIN_SIZE = (1040, 680)
BACKGROUND = "#111114"
AUDIO_FILTER = "Audio and video files (" + ";".join("*" + ext for ext in sorted(ALLOWED_EXTENSIONS)) + ")"
MOV_FILTER = "QuickTime movie (*.mov)"
_DROP_JS = ("document.addEventListener('drop', function (e) { e.preventDefault(); e.stopPropagation();"
            " window.pywebview._jsApiCallback('pywebviewEventHandler',"
            " { event: e, nodeId: 'document' }, 'eventHandler'); });")
REFRESH_JS = "window.__rubatoRefresh && window.__rubatoRefresh()"      # fixed: nothing is ever interpolated
LOAD_TIMEOUT = 60.0
SMOKE_STEP_TIMEOUT = 120.0
_AUDIO_TYPES = {"wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4", "flac": "audio/flac",
                "aiff": "audio/aiff", "ogg": "audio/ogg", "opus": "audio/ogg; codecs=opus"}

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ logging

def log_path() -> Path:
    """The per-OS log file location."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / f"{APP_NAME}.log"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME / "app.log"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "rubato-waveform-generator" / "app.log"


def setup_logging() -> None:
    """Rotating file log; also makes a missing stdout/stderr (a windowed frozen app) safe."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))   # noqa: SIM115 - lives for the process
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return                                  # no log file is better than no app
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    logging.getLogger("webview").setLevel(logging.WARNING)


# ------------------------------------------------------------------ dialogs

def _first_path(result: object) -> str | None:
    """A file dialog returns a tuple, a list, a str or None depending on platform and dialog type."""
    if isinstance(result, (list, tuple)):
        result = result[0] if result else None
    return result if isinstance(result, str) and result else None


class WebviewDialogs:
    """Native dialogs for the session. ``window`` is set once the window exists."""

    def __init__(self) -> None:
        self.window = None
        self._busy = threading.Lock()           # one dialog at a time

    def pick_audio(self, start_dir: str | None) -> str | None:
        import webview
        return self._run(dialog_type=webview.FileDialog.OPEN, directory=start_dir or "",
                         file_types=(AUDIO_FILTER,))

    def pick_output(self, start_dir: str | None, suggested_name: str) -> str | None:
        import webview
        return self._run(dialog_type=webview.FileDialog.SAVE, directory=start_dir or "",
                         save_filename=suggested_name, file_types=(MOV_FILTER,))

    def reveal(self, path: str) -> None:
        reveal_in_file_manager(path)

    def _run(self, **kwargs) -> str | None:
        if self.window is None or not self._busy.acquire(blocking=False):
            return None
        try:
            return _first_path(self.window.create_file_dialog(**kwargs))
        finally:
            self._busy.release()


class _StubDialogs:
    """Dialogs that pick nothing and reveal nothing; the dev browser and the smoke test build on it."""

    def pick_audio(self, start_dir):
        return None

    def pick_output(self, start_dir, suggested_name):
        return None

    def reveal(self, path):
        pass


class _NoDialogs(_StubDialogs):
    """For ``--dev-browser``: nothing to pick with, nothing to reveal.

    The dev-only routes can queue one save path for the next ``pick_output`` and read back the
    paths that would have been shown in the file manager.
    """

    def __init__(self) -> None:
        self.next_output: str | None = None
        self.reveals: list[str] = []

    def pick_output(self, start_dir, suggested_name):
        chosen, self.next_output = self.next_output, None
        return chosen

    def reveal(self, path):
        self.reveals.append(path)


# ------------------------------------------------------------- window events

class _NativePaths(list):
    """The platform's record of dropped files, which also remembers what pywebview took from it.

    pywebview fills in ``pywebviewFullPath`` only for a name it finds here, and removes the record
    when it does; a file it cannot match keeps whatever path the page sent. Remembering the removed
    records is how the drop handler tells the two apart.
    """

    def __init__(self, items=()) -> None:
        super().__init__(items)
        self.claimed: set[str] = set()

    def remove(self, item) -> None:
        super().remove(item)
        self.claimed.add(urllib.parse.unquote(item[1]))


def make_drop_handler(session: Session, window, native: _NativePaths):
    """The callback for a dropped file: only the first file is used, and its path stays on this side.

    Only a path that came from the native record is accepted; one the page made up has none.
    """
    def on_drop(event) -> None:
        try:
            path = event["dataTransfer"]["files"][0]["pywebviewFullPath"]
        except (KeyError, IndexError, TypeError):
            return
        if not isinstance(path, str):
            return
        known = path in native.claimed
        native.claimed.clear()
        if not known:
            log.warning("a dropped file had no native path; ignored")
            return
        try:
            session.select_audio(path)
        except SessionError as exc:
            session.report_input_error(exc)
        try:
            window.run_js(REFRESH_JS)
        except Exception:
            log.debug("could not refresh the page after a drop", exc_info=True)
    return on_drop


def _register_drop(session: Session, window) -> None:
    """Hook native file drops up to ``make_drop_handler``.

    pywebview's DOM API (``window.dom.document.events.drop += ...``) cannot be used here: it runs its
    scripts through ``evaluate_js``, which wraps them in ``eval`` and the server's CSP forbids that.
    ``run_js`` does not use ``eval``, so this does by hand what ``Element.on`` does: it adds the page
    listener and fills the registries that pywebview's own event dispatcher reads. The script is
    a fixed string. The page's own dragenter/dragover handlers must call preventDefault, or the
    browser never fires "drop".
    """
    from webview.dom import _dnd_state

    if not isinstance(_dnd_state["paths"], _NativePaths):
        _dnd_state["paths"] = _NativePaths(_dnd_state["paths"])
    # The dispatcher only reads ``_event_handlers`` from the registered element; a real Element would
    # build itself through ``evaluate_js`` and fail the same way.
    window.dom._elements["document"] = SimpleNamespace(_event_handlers={"drop": [make_drop_handler(session, window, _dnd_state["paths"])]})
    _dnd_state["num_listeners"] += 1          # without a listener the native side does not record paths
    window.run_js(_DROP_JS)


def _install_close_guard(session: Session, window) -> None:
    """Ask before closing during an export.

    The closing handler runs on the UI thread, and on macOS ``create_confirmation_dialog`` queues its
    work onto that same thread and waits, which would deadlock. So the handler only cancels the close
    and a worker thread asks; on "OK" the window is destroyed with a flag that lets it through.
    """
    state = {"confirmed": False, "asking": False}

    def ask() -> None:
        try:
            if window.create_confirmation_dialog(APP_NAME, "An export is still running. Quit and discard it?"):
                state["confirmed"] = True
                window.destroy()
        finally:
            state["asking"] = False

    def on_closing():
        if state["confirmed"] or not session.is_exporting():
            return None
        if not state["asking"]:
            state["asking"] = True
            threading.Thread(target=ask, name="rubato-close-confirm", daemon=True).start()
        return False                                  # False cancels the close

    window.events.closing += on_closing


def _set_dock_icon() -> None:
    """Best effort, from source only: a frozen build gets its icon from the bundle."""
    if sys.platform != "darwin" or getattr(sys, "frozen", False):
        return
    try:
        from AppKit import NSApplication, NSImage
        icon = Path(__file__).resolve().parent.parent / "assets" / "brand" / "icon-1024.png"
        if icon.is_file():
            image = NSImage.alloc().initWithContentsOfFile_(str(icon))
            if image is not None:
                NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:  # cosmetic only
        log.debug("could not set the dock icon", exc_info=True)


def _create_window(server: AppServer, dialogs: WebviewDialogs):
    import webview
    window = webview.create_window(APP_NAME, url=server.url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
                                   min_size=WINDOW_MIN_SIZE, background_color=BACKGROUND)
    dialogs.window = window
    return window


def _run_window(session: Session, server: AppServer, dialogs: WebviewDialogs, worker=None) -> None:
    import webview
    window = _create_window(server, dialogs)
    _install_close_guard(session, window)
    window.events.loaded += lambda: _register_drop(session, window)
    _set_dock_icon()
    if worker is None:
        webview.start()
    else:
        webview.start(worker, (window,))


def run_app(worker=None) -> None:
    log.info("%s %s started; FFmpeg: %s", APP_NAME, __version__, ffmpeg_tools.find_ffmpeg())
    dialogs = WebviewDialogs()
    session = Session(dialogs)
    server = AppServer(session)
    try:
        server.start()
        _run_window(session, server, dialogs, worker)
    finally:
        session.shutdown()
        server.stop()


# ------------------------------------------------------------- dev browser

def _write_port_file(path: Path, url: str, port: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"url": url, "port": port}, handle)
    os.replace(tmp, path)


def run_dev_browser(port_file: str | None) -> int:
    """No window: the server alone, for browser-driven tests. ``dev=True`` comes only from this flag."""
    dialogs = _NoDialogs()
    session = Session(dialogs)
    server = AppServer(session, dev=True, dev_dialogs=dialogs)
    stop = threading.Event()
    parent = os.getppid()
    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, lambda *_: stop.set())
    try:
        server.start()
        if port_file:
            _write_port_file(Path(port_file), server.url, server.port)
        else:
            print(server.url, flush=True)
        while not stop.wait(0.2):
            if sys.platform != "win32" and os.getppid() != parent:
                break                                   # the test run that started us is gone
    finally:
        session.shutdown()
        server.stop()
    return 0


# ------------------------------------------------------------- smoke test

_PROBE_JS = """(function () {
  var audio = document.createElement('audio'), types = __TYPES__, can = {};
  for (var k in types) { can[k] = audio.canPlayType(types[k]); }
  var canvas = document.createElement('canvas'), input = document.createElement('input');
  input.type = 'color';
  var h1 = document.querySelector('h1.brand-title');
  return JSON.stringify({
    can_play_type: can,
    canvas_2d: !!(canvas.getContext && canvas.getContext('2d')),
    input_color: input.type === 'color',
    user_agent: navigator.userAgent,
    page_heading: h1 ? h1.textContent : null,
    fonts_check: document.fonts.check("16px 'Instrument Sans'")
  });
})()""".replace("__TYPES__", json.dumps(_AUDIO_TYPES))

_FONT_JS = ("window.__smokeFont = 'pending';"
            "document.fonts.load(\"16px 'Instrument Sans'\").then("
            "function (f) { window.__smokeFont = f.length > 0 ? 'loaded' : 'none'; },"
            "function () { window.__smokeFont = 'error'; }); 0")


_SMOKE_DROP_JS = ("var dt = new DataTransfer(); dt.items.add(new File(['x'], 'smoke.wav'));"
                  " document.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true})); 0")


def _write_test_wav(path: Path) -> None:
    import math
    import struct
    rate = 44100
    frames = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * n / rate)
                                            * (0.3 + 0.7 * (n / rate)))) for n in range(rate))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)


class _SmokeDialogs(_StubDialogs):
    def __init__(self, output: Path) -> None:
        self.output = output

    def pick_output(self, start_dir, suggested_name):
        return str(self.output)


def _wait(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {what}")


def _probe_output(output: Path) -> dict:
    ffprobe = ffmpeg_tools.find_ffprobe()
    if ffprobe is None:
        raise RuntimeError("ffprobe not found")
    out = subprocess.run(
        [str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,pix_fmt",
         "-of", "json", str(output)],
        capture_output=True, text=True, check=True, timeout=60, stdin=subprocess.DEVNULL,
        creationflags=ffmpeg_tools.subprocess_flags()).stdout
    stream = json.loads(out)["streams"][0]
    pix = stream.get("pix_fmt", "")
    alpha = any(token in pix for token in ("yuva", "rgba", "argb", "bgra", "abgr", "gbrap", "ya"))
    return {"codec": stream.get("codec_name"), "pix_fmt": pix, "has_alpha": alpha}


def _smoke_steps(window, session: Session, scratch: Path, result: dict) -> None:
    if not window.events.loaded.wait(LOAD_TIMEOUT):
        raise TimeoutError("the page did not finish loading")
    window.run_js(_FONT_JS)
    _wait(lambda: window.run_js("window.__smokeFont") != "pending", 10.0, "the font to load")
    result["font_load"] = window.run_js("window.__smokeFont")
    result["webview"] = json.loads(window.run_js(_PROBE_JS))
    result["fonts_ok"] = result["font_load"] == "loaded"

    status = ffmpeg_tools.check()
    result["ffmpeg"] = {"ok": status.ok, "problem": status.problem, "has_prores": status.has_prores,
                        "ffmpeg": status.ffmpeg.name if status.ffmpeg else None,
                        "ffprobe": status.ffprobe.name if status.ffprobe else None}
    if not status.ok:
        raise RuntimeError(status.problem or "FFmpeg is not usable")

    wav = scratch / "smoke.wav"
    _write_test_wav(wav)
    # A native drag cannot be scripted, so this stands in for it: record the path the way the platform's
    # drop handler does, then fire a page "drop" event, which goes through the real bridge and callback.
    from webview.dom import _dnd_state
    _dnd_state["paths"].append((wav.name, str(wav)))
    window.run_js(_SMOKE_DROP_JS)
    _wait(lambda: session.state()["audio"] is not None, 10.0, "the simulated drop to select the file")
    _wait(lambda: "smoke.wav" in str(window.run_js("document.getElementById('source-name').textContent")), 10.0,
          "the page to refresh after the drop")
    result["drop_chain"] = True
    _wait(lambda: session.state()["analysis"]["state"] in ("ready", "failed"), SMOKE_STEP_TIMEOUT, "the analysis")
    state = session.state()
    if state["analysis"]["state"] != "ready":
        raise RuntimeError("analysis failed: " + str(state["analysis"]["error"]))
    width, height, rgba = session.render_preview(default_look_dict(), 0.5, 540, 90)
    result["preview"] = {"width": width, "height": height, "bytes": len(rgba)}
    if len(rgba) != width * height * 4:
        raise RuntimeError("preview has the wrong size")

    output = scratch / "smoke.mov"                                # where _SmokeDialogs sends the export
    if not session.start_export(default_look_dict()):
        raise RuntimeError("the export did not start")
    _wait(lambda: session.state()["export"]["state"] in ("done", "failed", "cancelled"),
          SMOKE_STEP_TIMEOUT, "the export")
    export = session.state()["export"]
    if export["state"] != "done":
        raise RuntimeError("export " + export["state"] + ": " + str(export["error"]))
    result["output"] = _probe_output(output)
    if result["output"]["codec"] != "prores" or not result["output"]["has_alpha"]:
        raise RuntimeError("the output is not ProRes with alpha")


def run_smoke_test(out_path: str) -> int:
    """Real window, real server, real export. Returns the process exit code."""
    result: dict = {"ok": False, "error": None, "version": __version__}
    scratch = Path(tempfile.mkdtemp(prefix="rubato-smoke-"))
    dialogs = _SmokeDialogs(scratch / "smoke.mov")
    session = Session(dialogs, settings_dir=scratch / "settings")      # never the user's own settings
    server = AppServer(session)

    def worker(window) -> None:
        try:
            _smoke_steps(window, session, scratch, result)
            result["ok"] = True
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            log.exception("smoke test failed")
        finally:
            try:
                window.destroy()
            except Exception:
                log.debug("could not close the smoke window", exc_info=True)

    try:
        server.start()
        webview_dialogs = WebviewDialogs()           # the window needs it only for its own handlers
        _run_window(session, server, webview_dialogs, worker)
    except Exception as exc:
        result["error"] = result["error"] or f"{type(exc).__name__}: {exc}"
        log.exception("smoke test could not run")
    finally:
        session.shutdown()
        server.stop()
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)
        Path(out_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1


# --------------------------------------------------------------------- main

def run(argv: list[str] | None = None) -> int:
    """The work of main(); returns the exit code, so tests can call it in-process."""
    parser = argparse.ArgumentParser(prog="rubato-waveform-app", description=f"{APP_NAME} (window).")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # Developer flags: hidden from --help, still accepted.
    parser.add_argument("--dev-browser", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--port-file", metavar="FILE", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-test", metavar="OUT.json", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.port_file and not args.dev_browser:
        parser.error("--port-file needs --dev-browser")
    if args.dev_browser and args.smoke_test:
        parser.error("--dev-browser and --smoke-test cannot be combined")
    setup_logging()
    if args.dev_browser:
        return run_dev_browser(args.port_file)
    if args.smoke_test:
        return run_smoke_test(args.smoke_test)
    run_app()
    return 0


def main(argv: list[str] | None = None) -> int:
    code = run(argv)
    # Daemon threads (HTTP handlers) must not meet interpreter finalization: end the process here.
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError, AttributeError):
            pass
    os._exit(code)


if __name__ == "__main__":
    main()
