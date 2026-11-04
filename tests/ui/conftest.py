# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Browser harness for the UI tests: a real dev-browser server per module, Playwright pages in
Chromium and WebKit, and a teardown that fails on any console error, page error, CSP violation or
request that leaves 127.0.0.1."""

import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "build" / "ui-shots"
SIZES = {"1040x640": (1040, 640), "1920x1200": (1920, 1200)}

BASE_STATE = {
    "audio": None, "step": 0,
    "analysis": {"state": "idle", "progress": 0.0, "error": None},
    "export": {"state": "idle", "current": 0, "total": 0, "error": None, "output_name": None,
               "can_reveal": False},
    "audio_version": 0, "playback": {"mode": "native", "error": None}, "error": None, "ffmpeg": {},
}


def pytest_collection_modifyitems(items):
    for item in items:
        if Path(str(item.fspath)).is_relative_to(Path(__file__).parent):
            item.add_marker(pytest.mark.ui)


# ------------------------------------------------------------------ server

def _wait_for(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)               # polling a child process, not a page: the page never sleeps
    raise TimeoutError(what)


@pytest.fixture(scope="session")
def demo_wav(tmp_path_factory):
    path = tmp_path_factory.mktemp("demo") / "demo.wav"
    subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "make_demo_audio.py"), "--out", str(path),
                    "--seconds", "20"], check=True, capture_output=True, timeout=120)
    return path


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    port_file = home / "port.json"
    env = {**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / "state"),
           "XDG_CONFIG_HOME": str(home / "config"), "APPDATA": str(home / "appdata"),
           "LOCALAPPDATA": str(home / "local")}
    errors = home / "stderr.txt"                      # never an unread pipe: a full one blocks the server
    with errors.open("w") as err_file:
        proc = subprocess.Popen([sys.executable, "-B", "-m", "rubato_waveform.app", "--dev-browser", "--port-file",
                                 str(port_file)], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=err_file, text=True)
    try:
        _wait_for(lambda: port_file.exists() or proc.poll() is not None, 60, "the dev server did not start")
        if proc.poll() is not None:
            raise RuntimeError("the dev server exited: " + errors.read_text())
        info = json.loads(port_file.read_text())
        yield SimpleNamespace(url=info["url"], port=info["port"], token=info["url"].split("token=")[1])
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0, f"the dev server ended with {proc.returncode}: {errors.read_text()[-2000:]}"


def api(server, path, body=None):
    """A JSON POST to the dev server, for setting the app up before a page looks at it."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.port}{path}", data=json.dumps(body or {}).encode(),
        headers={"X-Rubato-Token": server.token, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def get_json(server, path):
    """A JSON GET to the dev server."""
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", headers={"X-Rubato-Token": server.token})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def reset_look(server):
    """Puts the saved look back to the catalog defaults, for modules whose tests change it."""
    defaults = get_json(server, "/api/catalog")["defaults"]
    api(server, "/api/settings", {"look": defaults["look"], "theme": defaults["theme"], "gradient": defaults["gradient"]})


def select_audio(server, path):
    """Selects audio through the dev endpoint and waits until its analysis is done."""
    api(server, "/api/dev/select", {"path": str(path)})
    state_request = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/state",
                                           headers={"X-Rubato-Token": server.token})

    def ready():
        with urllib.request.urlopen(state_request, timeout=30) as response:
            return json.load(response)["analysis"]["state"] in ("ready", "failed")

    _wait_for(ready, 60, "the analysis did not finish")


# ----------------------------------------------------------------- browsers

@pytest.fixture(scope="session")
def playwright_driver():
    sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    driver = sync_api.sync_playwright().start()
    yield driver
    driver.stop()


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def browser(request, playwright_driver):
    allowed = os.environ.get("RUBATO_UI_BROWSERS")
    if allowed and request.param not in allowed.split(","):
        pytest.skip(f"{request.param} is not enabled by RUBATO_UI_BROWSERS={allowed}")
    from playwright.sync_api import Error as PlaywrightError
    try:
        instance = getattr(playwright_driver, request.param).launch()
    except PlaywrightError as exc:
        pytest.skip(f"{request.param} is not available (run `playwright install {request.param}`): "
                    f"{str(exc).splitlines()[0]}")
    instance.browser_name = request.param
    yield instance
    instance.close()


@pytest.fixture(params=list(SIZES))
def size(request):
    return request.param


_CSP_LISTENER = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', (e) =>
  window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));
"""


@pytest.fixture
def page(browser, size, server):
    from playwright.sync_api import Error as PlaywrightError
    width, height = SIZES[size]
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    page.problems = []
    page.add_init_script(_CSP_LISTENER)
    page.on("console", lambda m: m.type == "error" and page.problems.append(
        f"console error: {m.text} [{m.location.get('url', '')}]"))
    page.on("pageerror", lambda e: page.problems.append("page error: " + str(e)))
    page.on("dialog", lambda d: (page.problems.append("dialog: " + d.message), d.dismiss()))

    def check_request(req):
        url = urlsplit(req.url)
        if url.scheme in ("http", "https", "ws", "wss") and url.hostname != "127.0.0.1":
            page.problems.append("request left the machine: " + req.url)
    page.on("request", check_request)

    page.size_name = size
    page.server = server
    try:
        yield page
        try:
            violations = page.evaluate("window.__csp || []")
        except PlaywrightError:                 # the page is already gone
            violations = []
        page.problems.extend("CSP violation: " + v for v in violations)
        # Leaving the page cancels what is still on its way; WebKit reports those as page errors.
        mark = len(page.problems)
        try:
            page.goto("about:blank")
        except PlaywrightError:
            pass
        del page.problems[mark:]
    finally:
        context.close()
    if page.problems:
        pytest.fail("browser problems:\n" + "\n".join(page.problems), pytrace=False)


# ----------------------------------------------------------------- helpers

def _merge(base, patch):
    out = dict(base)
    for key, value in patch.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


_FRAME_W, _FRAME_H = 120, 20
_FRAME = b"".join(bytes([139, 108, 246, 255]) if x % 10 < 6 else bytes(4) for x in range(_FRAME_W)) * _FRAME_H


def _tiny_frame(route):
    route.fulfill(status=200, body=_FRAME, headers={
        "Content-Type": "application/octet-stream", "X-W": str(_FRAME_W), "X-H": str(_FRAME_H)})


def _silent_wav(seconds=1, rate=8000):
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(2 * rate * seconds))
    return out.getvalue()


_SILENCE = _silent_wav()


def serve_silence(route):
    """Answers an /api/audio request with a short silent WAV, Range included, as the real route does."""
    size = len(_SILENCE)
    match = re.match(r"bytes=(\d*)-(\d*)$", route.request.headers.get("range", ""))
    headers = {"Content-Type": "audio/wav", "Accept-Ranges": "bytes"}
    if not match:
        route.fulfill(status=200, body=_SILENCE, headers=headers)
        return
    start = int(match.group(1) or 0)
    end = min(int(match.group(2)) if match.group(2) else size - 1, size - 1)
    route.fulfill(status=206, body=_SILENCE[start:end + 1],
                  headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"})


def allow_failed_loads(page, path, status):
    """Forgives the browser's own log line for one deliberate failure: this URL path, this status.

    Anything else that fails to load (a font, a script, another route) is still a problem. A status of
    None means a request that never got an answer (an aborted one).
    """
    def expected(problem):
        text, _, url = problem.rpartition(" [")
        return ("Failed to load resource" in text and path in url
                and (("status of " not in text) if status is None else f"status of {status} " in text))
    page.problems[:] = [p for p in page.problems if not expected(p)]


def mock_state(page, **patch):
    """Overrides /api/state (and answers /api/frame and /api/audio) for states that are hard to reach for real."""
    state = _merge(BASE_STATE, patch)
    page.route("**/api/state", lambda route: route.fulfill(
        status=200, json=state, headers={"Cache-Control": "no-store"}))
    page.route("**/api/frame", _tiny_frame)
    page.route("**/api/audio?*", serve_silence)
    return state


def open_app(page):
    from playwright.sync_api import expect
    page.goto(page.server.url)
    expect(page.locator("#status")).not_to_have_text("Loading")


def wait_for_frame(page):
    """Waits until the page reports that the preview canvas holds a painted frame.

    Playwright's wait_for_function evaluates a string, which the app's CSP rightly forbids, so the
    page marks the canvas (data-frame) and the test waits on that attribute instead.
    """
    from playwright.sync_api import expect
    expect(page.locator("#preview[data-frame]")).to_be_visible()


_SCREENSHOT_STYLE = ("Refused to apply a stylesheet", "style-src-elem inline")


@pytest.fixture
def shot(request, page, browser):
    def take(label=""):
        SHOTS.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]", "-", request.node.originalname + (("-" + label) if label else ""))
        path = SHOTS / f"{name}-{browser.browser_name}-{page.size_name}.png"
        seen, csp = len(page.problems), page.evaluate("window.__csp.length")
        page.screenshot(path=str(path))
        # WebKit's screenshot injects an inline style of its own; the app's CSP refuses it, which is
        # the tool talking, not the page. Only that exact report, only while the screenshot ran.
        page.problems[seen:] = [p for p in page.problems[seen:] if not any(m in p for m in _SCREENSHOT_STYLE)]
        page.evaluate("n => { window.__csp = window.__csp.filter((v, i) => i < n || !v.startsWith('style-src-elem')); }", csp)
        return path
    return take
