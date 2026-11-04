# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""The native window entry point, as far as it can be tested without a window."""

import inspect
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import wait_until

from rubato_waveform import app
from rubato_waveform.session import ALLOWED_EXTENSIONS, Session

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX signals and file modes")


def write_wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\0\0" * 8000)
    return path


def run_module(*args: str, home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / "state"), "LOCALAPPDATA": str(home)}
    return subprocess.run([sys.executable, "-B", "-m", "rubato_waveform.app", *args], capture_output=True, text=True,
                          check=False, timeout=60, env=env, stdin=subprocess.DEVNULL)


def test_import_does_not_import_webview():
    code = "import sys, rubato_waveform.app; sys.exit(1 if 'webview' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-B", "-c", code], check=False, timeout=60).returncode == 0


def test_audio_filter_covers_every_allowed_extension():
    assert app.AUDIO_FILTER.startswith("Audio and video files (")
    listed = app.AUDIO_FILTER.split("(", 1)[1].rstrip(")").split(";")
    assert set(listed) == {"*" + ext for ext in ALLOWED_EXTENSIONS}


def test_first_path_handles_every_return_shape():
    assert app._first_path(None) is None
    assert app._first_path(()) is None
    assert app._first_path("a.wav") == "a.wav"
    assert app._first_path(("a.wav", "b.wav")) == "a.wav"
    assert app._first_path(["a.wav"]) == "a.wav"
    assert app._first_path(42) is None


def test_second_concurrent_dialog_returns_none():
    dialogs = app.WebviewDialogs()

    class Window:
        def create_file_dialog(self, **kwargs):
            assert dialogs._run() is None          # a nested call finds the dialog busy
            return ("chosen.wav",)

    dialogs.window = Window()
    assert dialogs._run() == "chosen.wav"
    assert dialogs._busy.acquire(blocking=False)   # released again afterwards


def test_version(tmp_path):
    proc = run_module("--version", home=tmp_path)
    assert proc.returncode == 0 and "rubato-waveform-app" in proc.stdout


# ------------------------------------------------------------ drop callback

class FakeWindow:
    def __init__(self) -> None:
        self.scripts: list[str] = []

    def run_js(self, script):
        self.scripts.append(script)


def native(*paths) -> app._NativePaths:
    """What the platform's drop code has recorded and pywebview has already taken (it fills the path in)."""
    record = app._NativePaths()
    record.claimed.update(str(p) for p in paths)
    return record


@pytest.fixture
def session(tmp_path):
    sess = Session(app._NoDialogs(), settings_dir=tmp_path / "settings")
    yield sess
    sess.shutdown()


def drop_event(*paths):
    return {"type": "drop", "dataTransfer": {"files": [{"name": Path(p).name, "pywebviewFullPath": str(p)} for p in paths]}}


def test_drop_selects_only_the_first_file_and_refreshes_with_a_fixed_string(session, tmp_path):
    first, second = write_wav(tmp_path / "first one.wav"), write_wav(tmp_path / "second.wav")
    window = FakeWindow()
    app.make_drop_handler(session, window, native(first, second))(drop_event(first, second))
    assert session.state()["audio"]["name"] == "first one.wav"
    assert window.scripts == [app.REFRESH_JS]
    assert "first" not in app.REFRESH_JS


def test_drop_of_a_refused_file_surfaces_in_the_error_state(session, tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("x")
    window = FakeWindow()
    app.make_drop_handler(session, window, native(bad))(drop_event(bad))
    state = session.state()
    assert state["audio"] is None and "not supported" in state["error"]
    assert str(tmp_path) not in state["error"]
    assert window.scripts == [app.REFRESH_JS]
    ok = write_wav(tmp_path / "ok.wav")
    app.make_drop_handler(session, window, native(ok))(drop_event(ok))
    assert session.state()["error"] is None


@pytest.mark.parametrize("event", [{}, {"dataTransfer": {}}, {"dataTransfer": {"files": []}},
                                   {"dataTransfer": {"files": [{"name": "x.wav"}]}},
                                   {"dataTransfer": {"files": [{"pywebviewFullPath": 5}]}}, None])
def test_drop_without_a_native_path_does_nothing(session, event):
    window = FakeWindow()
    app.make_drop_handler(session, window, native())(event)
    assert session.state()["audio"] is None and window.scripts == []


def test_a_path_the_page_made_up_is_refused(session, tmp_path):
    forged = write_wav(tmp_path / "forged.wav")
    window = FakeWindow()
    record = native()                                        # no native record for it
    app.make_drop_handler(session, window, record)(drop_event(forged))
    assert session.state()["audio"] is None and window.scripts == []


def test_a_native_path_is_good_for_one_drop_only(session, tmp_path):
    first, second = write_wav(tmp_path / "first.wav"), write_wav(tmp_path / "second.wav")
    window = FakeWindow()
    handler = app.make_drop_handler(session, window, native(first))
    handler(drop_event(second))                              # not the one the platform recorded
    assert session.state()["audio"] is None
    record = native(first, second)
    handler = app.make_drop_handler(session, window, record)
    handler(drop_event(first))
    assert session.state()["audio"]["name"] == "first.wav" and not record.claimed
    handler(drop_event(first))                               # replayed: the record is used up
    assert window.scripts == [app.REFRESH_JS]


# ----------------------------------------------- pywebview internals the drop relies on

webview_dom = pytest.importorskip("webview.dom", reason="pywebview is not installed")


class DomWindow(FakeWindow):
    def __init__(self) -> None:
        super().__init__()
        self.dom = SimpleNamespace(_elements={})


@pytest.fixture
def dnd_state(monkeypatch):
    monkeypatch.setitem(webview_dom._dnd_state, "paths", [])
    monkeypatch.setitem(webview_dom._dnd_state, "num_listeners", 0)
    return webview_dom._dnd_state


def test_register_drop_fills_the_registries_pywebview_reads(session, dnd_state):
    window = DomWindow()
    app._register_drop(session, window)
    element = window.dom._elements["document"]
    assert len(element._event_handlers["drop"]) == 1
    assert dnd_state["num_listeners"] == 1 and isinstance(dnd_state["paths"], app._NativePaths)
    assert window.scripts == [app._DROP_JS]


def test_a_native_drop_goes_through_pywebviews_own_dispatcher(session, dnd_state, tmp_path):
    """The real ``js_bridge_call`` fills in the path and takes the record; a version that stops doing
    either breaks this test instead of silently breaking drops."""
    from webview.util import js_bridge_call
    window = DomWindow()
    app._register_drop(session, window)
    wav = write_wav(tmp_path / "dropped.wav")
    forged = write_wav(tmp_path / "forged.wav")
    dnd_state["paths"].append((wav.name, str(wav)))          # what the platform code records
    event = {"type": "drop", "dataTransfer": {"files": [
        {"name": wav.name, "pywebviewFullPath": "ignored"}]}}
    js_bridge_call(window, "pywebviewEventHandler", {"event": event, "nodeId": "document"}, "eventHandler")
    assert wait_until(lambda: app.REFRESH_JS in window.scripts)      # it comes after the audio is selected
    assert session.state()["audio"]["name"] == "dropped.wav" and dnd_state["paths"] == []

    session.shutdown()
    event = {"type": "drop", "dataTransfer": {"files": [{"name": forged.name, "pywebviewFullPath": str(forged)}]}}
    window.scripts.clear()
    js_bridge_call(window, "pywebviewEventHandler", {"event": event, "nodeId": "document"}, "eventHandler")
    assert not wait_until(lambda: window.scripts, 0.5)       # no native record: nothing happened


def test_pywebview_still_has_what_the_drop_code_touches():
    from webview.dom.dom import DOM
    from webview.util import js_bridge_call
    assert set(webview_dom._dnd_state) >= {"num_listeners", "paths"}
    assert DOM(SimpleNamespace(events=SimpleNamespace(loaded=FakeEvent())))._elements == {}
    source = inspect.getsource(js_bridge_call)               # the dispatcher: names the page script calls it by
    for name in ("pywebviewEventHandler", "_dnd_state['paths']", "pywebviewFullPath", "dom._elements",
                 "_event_handlers", "dataTransfer"):
        assert name in source, name


# ------------------------------------------------------------------ close guard

class FakeEvent:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class GuardWindow:
    def __init__(self, answer: bool) -> None:
        self.events = SimpleNamespace(closing=FakeEvent())
        self.answer = answer
        self.asked_from: list[threading.Thread] = []
        self.destroyed = 0
        self.release = threading.Event()
        self.release.set()

    def create_confirmation_dialog(self, title, message):
        self.asked_from.append(threading.current_thread())
        self.release.wait(10)
        return self.answer

    def destroy(self):
        self.destroyed += 1


def guarded(exporting: bool, answer: bool):
    window = GuardWindow(answer)
    session = SimpleNamespace(is_exporting=lambda: exporting)
    app._install_close_guard(session, window)
    (on_closing,) = window.events.closing.handlers
    return window, on_closing


def test_close_passes_when_nothing_is_exporting():
    window, on_closing = guarded(exporting=False, answer=True)
    assert on_closing() is None and window.asked_from == []


def test_close_during_an_export_asks_on_a_worker_thread_and_confirm_destroys():
    window, on_closing = guarded(exporting=True, answer=True)
    assert on_closing() is False
    assert wait_until(lambda: window.destroyed == 1)
    assert window.asked_from[0] is not threading.main_thread()
    assert on_closing() is None                              # the second close, after confirming, goes through


def test_declining_keeps_the_window_and_the_next_close_asks_again():
    window, on_closing = guarded(exporting=True, answer=False)
    assert on_closing() is False
    assert wait_until(lambda: len(window.asked_from) == 1)
    assert wait_until(lambda: on_closing() is False and len(window.asked_from) == 2)
    assert window.destroyed == 0


def test_repeated_closes_do_not_stack_dialogs():
    window, on_closing = guarded(exporting=True, answer=True)
    window.release.clear()                                   # the dialog stays open
    assert [on_closing() for _ in range(5)] == [False] * 5
    assert wait_until(lambda: len(window.asked_from) == 1)
    window.release.set()
    assert wait_until(lambda: window.destroyed == 1)
    assert len(window.asked_from) == 1


# -------------------------------------------------------------- dev browser

@pytest.fixture
def dev_browser(tmp_path):
    port_file = tmp_path / "port.json"
    env = {**os.environ, "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")}
    errors = tmp_path / "stderr.txt"                 # never an unread pipe: a full one blocks the server
    with errors.open("w") as err_file:
        proc = subprocess.Popen([sys.executable, "-B", "-m", "rubato_waveform.app", "--dev-browser", "--port-file",
                                 str(port_file)], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=err_file, text=True)
    proc.stderr_path = errors
    try:
        assert wait_until(lambda: port_file.exists() or proc.poll() is not None, 30)
        assert proc.poll() is None, errors.read_text()
        yield proc, port_file, json.loads(port_file.read_text())
    finally:
        exited = proc.poll() is not None
        if not exited:
            proc.kill()
        proc.communicate(timeout=30)
    assert not exited or proc.returncode == 0, f"the server ended with {proc.returncode}: {errors.read_text()}"


def call(info, path, *, body=None, token=True):
    headers = {"X-Rubato-Token": info["url"].split("token=")[1]} if token else {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}", headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    return urllib.request.urlopen(req, timeout=20)


@posix_only
def test_dev_browser_serves_the_page_and_stops_on_sigterm(dev_browser, tmp_path):
    proc, port_file, info = dev_browser
    assert port_file.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("port.json.tmp"))
    page = urllib.request.urlopen(info["url"], timeout=20)
    assert page.status == 200 and b"<h1" in page.read()
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    with pytest.raises(urllib.error.HTTPError) as err:
        call(info, "/api/state", token=False)
    assert err.value.code == 401

    wav = write_wav(tmp_path / "dev.wav")
    assert json.load(call(info, "/api/dev/select", body={"path": str(wav)})) == {"ok": True}
    assert wait_until(lambda: json.load(call(info, "/api/state"))["audio"] is not None, 20)

    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0
    token = info["url"].split("token=")[1]
    assert token not in out and token not in proc.stderr_path.read_text()


def test_dev_browser_without_a_port_file_prints_the_url(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")}
    with (tmp_path / "stderr.txt").open("w") as err_file:
        proc = subprocess.Popen([sys.executable, "-B", "-m", "rubato_waveform.app", "--dev-browser"], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err_file, text=True)
    try:
        line = proc.stdout.readline().strip()
        assert line.startswith("http://127.0.0.1:") and "?token=" in line
    finally:
        proc.kill()
        proc.communicate(timeout=30)


def test_port_file_needs_dev_browser(tmp_path):
    assert run_module("--port-file", str(tmp_path / "p.json"), home=tmp_path).returncode == 2


@posix_only
def test_aborted_requests_never_block_or_crash_the_server(tmp_path):
    """Handler threads must not print into a pipe nobody reads: when it fills they block with the
    stderr lock held, and finalization at SIGTERM aborts the process (exit -6, a crash dialog)."""
    port_file = tmp_path / "port.json"
    env = {**os.environ, "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")}
    proc = subprocess.Popen([sys.executable, "-B", "-m", "rubato_waveform.app", "--dev-browser", "--port-file",
                             str(port_file)], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)               # deliberately never read
    try:
        assert wait_until(lambda: port_file.exists() or proc.poll() is not None, 30)
        info = json.loads(port_file.read_text())
        wav = write_wav(tmp_path / "abort.wav")
        call(info, "/api/dev/select", body={"path": str(wav)})
        assert wait_until(lambda: json.load(call(info, "/api/state"))["analysis"]["state"] == "ready", 30)
        token = info["url"].split("token=")[1]
        body = json.dumps({"look": {}, "t": 0.1, "max_w": 1080, "max_h": 180}).encode()
        head = (f"POST /api/frame HTTP/1.1\r\nHost: 127.0.0.1:{info['port']}\r\nX-Rubato-Token: {token}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode()
        for _ in range(400):
            try:
                sock = socket.create_connection(("127.0.0.1", info["port"]), timeout=10)
                sock.sendall(head + body)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                sock.close()
            except OSError:
                pass                                              # the server was quicker than this test
        def serving():
            try:
                return json.load(call(info, "/api/state"))["analysis"]["state"] == "ready"
            except OSError:
                return False                                      # still draining the burst

        assert wait_until(serving, 30)
        proc.send_signal(signal.SIGTERM)
        assert wait_until(lambda: proc.poll() is not None, 30)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)
        proc.stderr.close()
