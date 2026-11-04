# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Feature coverage of every control, on WebKit only (the other UI files cover Chromium).

Each control of the window is driven through the page. The look the page then sends to /api/frame must
carry the change, the real frame the server draws for it must differ from the frame before, and the
look sent to /api/export must be that same look, so the export honours what the preview shows. Then
there are the transport items (sync, scrub, progress, reveal), one export checked with ffprobe, and
the screenshots that README media is cut from (build/ui-shots/parity/)."""

import json
import os
import re
import subprocess
import sys
import urllib.request

import pytest
from PIL import Image, ImageChops

from rubato_waveform.catalog import catalog as get_catalog

from .conftest import (
    _SCREENSHOT_STYLE,
    ROOT,
    SHOTS,
    api,
    get_json,
    mock_state,
    open_app,
    reset_look,
    select_audio,
    wait_for_frame,
)
from .test_flows import RENDER_TIMEOUT_MS, REVEAL_LABEL, probe
from .test_shell import ARMED
from .test_transport import play

expect = pytest.importorskip("playwright.sync_api").expect

CATALOG = get_catalog()
LAYOUTS = [entry["key"] for entry in CATALOG["layouts"]]
STYLES = [entry["key"] for entry in CATALOG["styles"]]
THEMES = CATALOG["themes"]
SLIDERS = CATALOG["sliders"]
DEFAULTS = CATALOG["defaults"]["look"]
FORMATS = CATALOG["formats"]
PARITY = SHOTS / "parity"
MIN_PIXELS = 50                      # a difference must be visible, not one stray pixel


# ----------------------------------------------------------------- fixtures

@pytest.fixture(scope="module", params=["webkit"])
def browser(request, playwright_driver):
    allowed = os.environ.get("RUBATO_UI_BROWSERS")
    if allowed and request.param not in allowed.split(","):
        pytest.skip(f"{request.param} is not enabled by RUBATO_UI_BROWSERS={allowed}")
    from playwright.sync_api import Error as PlaywrightError
    try:
        instance = getattr(playwright_driver, request.param).launch()
    except PlaywrightError as exc:
        pytest.skip(f"{request.param} is not available: {str(exc).splitlines()[0]}")
    instance.browser_name = request.param
    yield instance
    instance.close()


@pytest.fixture
def size():
    return "1040x640"                # the screenshot tests choose their own


@pytest.fixture(autouse=True)
def fresh_settings(server):
    reset_look(server)


@pytest.fixture
def armed(server, demo_wav):
    state = get_json(server, "/api/state")        # selecting again also clears an earlier export's status
    if state["audio"] is None or state["audio"]["name"] != demo_wav.name or state["export"]["state"] != "idle":
        select_audio(server, demo_wav)


@pytest.fixture(scope="session")
def short_wav(tmp_path_factory):
    path = tmp_path_factory.mktemp("short") / "short.wav"
    subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "make_demo_audio.py"), "--out", str(path),
                    "--seconds", "6"], check=True, capture_output=True, timeout=120)
    return path


def frame(server, look, t):
    """The real frame for a look, as (width, height, RGBA bytes)."""
    body = json.dumps({"look": look, "t": t, "max_w": 1080, "max_h": 1080}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.port}/api/frame", data=body,
        headers={"X-Rubato-Token": server.token, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return int(response.headers["X-W"]), int(response.headers["X-H"]), response.read()


def visible(shot):
    width, height, rgba = shot
    return sum(Image.frombytes("RGBA", (width, height), rgba).getchannel("A").histogram()[1:])


@pytest.fixture
def signal_t(server, armed):
    """A time at which the demo audio is loud: the frame with the most visible pixels."""
    return max((float(t) for t in range(1, 19)), key=lambda t: visible(frame(server, DEFAULTS, t)))


def differing_pixels(a, b):
    if a[:2] != b[:2]:
        return max(a[0] * a[1], b[0] * b[1])
    diff = ImageChops.difference(Image.frombytes("RGBA", a[:2], a[2]), Image.frombytes("RGBA", b[:2], b[2]))
    return sum(diff.convert("L").histogram()[1:])


def close(value, wanted):
    if isinstance(wanted, float) and isinstance(value, (int, float)):
        return abs(value - wanted) < 1e-6
    return value == wanted


@pytest.fixture
def app(page, server, signal_t):
    """The page open on the demo audio, with the preview parked at a time with real signal."""
    open_app(page)
    wait_for_frame(page)
    duration = get_json(server, "/api/state")["audio"]["duration"]
    position = round(signal_t / duration * 10000)
    wanted = position / 10000 * duration
    with page.expect_response(lambda r: r.url.endswith("/api/frame") and abs(r.request.post_data_json["t"] - wanted) < 1e-3) as seen:
        page.locator("#scrub").fill(str(position))
    page.last_body = seen.value.request.post_data_json
    page.app_t = wanted
    return page


def drive(page, act, at=None, **wanted):
    """Runs a UI action and returns the /api/frame request body that carries its result.

    When the page is already showing what the action would produce, nothing is done and the body that
    showed it is returned: the picture is then the one before, which is just what a comparison needs.
    """
    def satisfied(sent):
        return (at is None or abs(sent["t"] - at) <= 1e-3) and all(
            close(sent["look"].get(field), value) for field, value in wanted.items())

    if satisfied(page.last_body):
        return page.last_body
    with page.expect_response(lambda r: r.url.endswith("/api/frame") and satisfied(r.request.post_data_json)) as seen:
        act()
    seen.value.finished()
    page.last_body = seen.value.request.post_data_json
    return page.last_body


def exported_look(page):
    """The look the Export button sends. The dev dialog gives no path, so nothing is rendered."""
    with page.expect_request(lambda r: r.url.endswith("/api/export")) as seen:
        page.locator("#export-btn").click()
    return seen.value.post_data_json["look"]


def check_changes_the_picture(page, server, before, after):
    """The two looks, rendered for real at the same time, must differ; the export gets the second."""
    assert before["t"] == after["t"] == page.app_t
    assert before["look"] != after["look"]
    shots = [frame(server, body["look"], body["t"]) for body in (before, after)]
    assert visible(shots[0]) > 0 and visible(shots[1]) > 0, "a blank frame proves nothing"
    assert differing_pixels(*shots) >= MIN_PIXELS
    assert exported_look(page) == after["look"]


def layout_button(page, key):
    return page.locator(f'.layout-grid button[data-value="{key}"]')


def seg_button(page, caption, value):
    """A button of the segmented control that sits under a given caption (Style, Motion, Measure, Fill)."""
    field = page.locator(".field").filter(has=page.locator(".field-label", has_text=re.compile(f"^{caption}$")))
    return field.locator(f'button[data-value="{value}"]')


# ------------------------------------------------------------------ the list

@pytest.mark.parametrize("key", LAYOUTS)
def test_layout(app, server, key):
    other = "scroll" if key == "mirror" else "mirror"
    before = drive(app, layout_button(app, other).click, layout=other)
    after = drive(app, layout_button(app, key).click, layout=key)
    if key == "radial":
        assert (after["look"]["width"], after["look"]["height"]) == (1080, 1080)
    check_changes_the_picture(app, server, before, after)


@pytest.mark.parametrize("key", STYLES)
def test_style(app, server, key):
    other = "line" if key == "bars" else "bars"
    before = drive(app, seg_button(app, "Style", other).click, style=other)
    after = drive(app, seg_button(app, "Style", key).click, style=key)
    check_changes_the_picture(app, server, before, after)


@pytest.mark.parametrize("metric", ["peak", "rms"])
def test_metric(app, server, metric):
    other = "rms" if metric == "peak" else "peak"
    before = drive(app, seg_button(app, "Measure", other).click, metric=other)
    after = drive(app, seg_button(app, "Measure", metric).click, metric=metric)
    check_changes_the_picture(app, server, before, after)


@pytest.mark.parametrize("static", [False, True], ids=["moving", "static"])
def test_motion(app, server, static):
    before = drive(app, seg_button(app, "Motion", str(not static).lower()).click, stationary=not static)
    after = drive(app, seg_button(app, "Motion", str(static).lower()).click, stationary=static)
    check_changes_the_picture(app, server, before, after)


@pytest.mark.parametrize("preset", [p["key"] for p in CATALOG["presets"]])
def test_preset(app, server, preset):
    entries = {p["key"]: p for p in CATALOG["presets"]}
    other = "energetic" if preset == "calm" else "calm"
    chip = lambda key: app.locator(".chip", has_text=re.compile(f"^{entries[key]['label']}$"))
    before = drive(app, chip(other).click, **entries[other]["values"])
    after = drive(app, chip(preset).click, **entries[preset]["values"])
    check_changes_the_picture(app, server, before, after)


@pytest.mark.parametrize("index", range(len(THEMES)), ids=[t["name"] for t in THEMES])
def test_theme(app, server, index):
    theme, other = THEMES[index], THEMES[(index + 1) % len(THEMES)]
    swatch = lambda t: app.locator(f'#color-body button[data-value="{t["name"]}"]')
    before = drive(app, swatch(other).click, color=other["color"], color2=other["color2"])
    after = drive(app, swatch(theme).click, color=theme["color"], color2=theme["color2"])
    check_changes_the_picture(app, server, before, after)
    expect(app.locator("#theme-name")).to_have_text(theme["name"])


def test_solid(app, server):
    drive(app, seg_button(app, "Fill", "false").click, color2=None)
    gradient = drive(app, seg_button(app, "Fill", "true").click, color2=DEFAULTS["color2"])
    solid = drive(app, seg_button(app, "Fill", "false").click, color2=None)
    check_changes_the_picture(app, server, gradient, solid)


def test_gradient(app, server):
    solid = drive(app, seg_button(app, "Fill", "false").click, color2=None)
    gradient = drive(app, seg_button(app, "Fill", "true").click, color2=DEFAULTS["color2"])
    check_changes_the_picture(app, server, solid, gradient)


@pytest.mark.parametrize("letter, field, first, second", [
    ("A", "color", "#00FF88", "#FF3366"), ("B", "color2", "#FFD400", "#00B7FF")])
def test_custom_colour(app, server, letter, field, first, second):
    box = app.get_by_label(f"Color {letter} hex")
    before = drive(app, lambda: box.fill(first), **{field: first})
    after = drive(app, lambda: box.fill(second), **{field: second})
    expect(app.locator("#theme-name")).to_have_text("Custom")
    check_changes_the_picture(app, server, before, after)


@pytest.mark.parametrize("slider", [s["key"] for s in SLIDERS])
def test_slider(app, server, slider):
    entry = next(s for s in SLIDERS if s["key"] == slider)
    if entry["voice_only"]:
        layout_button(app, "voiceline").click()
        expect(app.locator("#voice-sliders")).to_be_visible()
    control = app.get_by_label(entry["label"], exact=True)
    low, high = float(entry["min"]), float(entry["max"])
    before = drive(app, lambda: control.fill(f"{low:g}"), **{entry["field"]: low})
    after = drive(app, lambda: control.fill(f"{high:g}"), **{entry["field"]: high})
    check_changes_the_picture(app, server, before, after)


@pytest.mark.parametrize("index", range(len(FORMATS)), ids=[f["label"] for f in FORMATS])
def test_export_format(app, server, index):
    chosen, other = FORMATS[index], FORMATS[(index + 1) % len(FORMATS)]
    button = lambda f: app.locator("#formats button").nth(FORMATS.index(f))
    before = drive(app, button(other).click, width=other["w"], height=other["h"])
    after = drive(app, button(chosen).click, width=chosen["w"], height=chosen["h"])
    check_changes_the_picture(app, server, before, after)
    shot = frame(server, after["look"], after["t"])
    assert shot[0] / shot[1] == pytest.approx(chosen["w"] / chosen["h"], rel=0.02)
    expect(app.locator("#size-label")).to_contain_text(f"{chosen['w']} × {chosen['h']}")


def test_orbit_switches_to_a_square_frame_and_back(app, server):
    orbit = drive(app, layout_button(app, "radial").click, layout="radial")
    assert (orbit["look"]["width"], orbit["look"]["height"]) == (1080, 1080)      # never an Orbit frame at 1080 x 180
    expect(app.locator("#status")).to_have_text("Orbit needs a square frame. Switched to 1080 × 1080.")
    expect(app.locator("#formats button").nth(3)).to_have_attribute("aria-pressed", "true")
    assert frame(server, orbit["look"], orbit["t"])[:2] == (1080, 1080)
    assert exported_look(app) == orbit["look"]
    back = drive(app, layout_button(app, "mirror").click, layout="mirror")
    assert (back["look"]["width"], back["look"]["height"]) == (1080, 180)
    expect(app.locator("#status")).to_have_text("Restored 1080 × 180.")


# ----------------------------------------------------------------- transport

WAIT_PAINTED_AFTER = """({ at, timeout }) => new Promise((resolve, reject) => {
  const canvas = document.getElementById('preview');
  const check = () => Number(canvas.dataset.t) > at;
  if (check()) { resolve(Number(canvas.dataset.t)); return; }
  const watch = new MutationObserver(() => { if (check()) { watch.disconnect(); resolve(Number(canvas.dataset.t)); } });
  watch.observe(canvas, { attributes: true, attributeFilter: ['data-t'] });
  setTimeout(() => reject(new Error('the preview did not follow the audio')), timeout);
})"""


def test_synced_playback(app):
    start = app.app_t
    play(app)
    painted = app.evaluate(WAIT_PAINTED_AFTER, {"at": start + 0.5, "timeout": 15000})
    current = app.evaluate("document.querySelector('audio').currentTime")
    assert painted > start + 0.5
    assert 0 <= current - painted < 1.5, f"the picture ({painted}) lags the sound ({current})"


def test_timeline_scrub(app, server):
    duration = get_json(server, "/api/state")["audio"]["duration"]
    start = drive(app, lambda: app.locator("#scrub").fill("0"), at=0.0)
    middle = drive(app, lambda: app.locator("#scrub").fill("5000"), at=duration / 2)
    assert start["look"] == middle["look"]
    assert differing_pixels(frame(server, start["look"], start["t"]), frame(server, middle["look"], middle["t"])) >= MIN_PIXELS
    expect(app.locator("#time-now")).to_have_text(f"{int(duration / 2) // 60:02d}:{int(duration / 2) % 60:02d}")


def test_progress_bar_tracks_the_frames(page):
    state = mock_state(page, **{**ARMED, "step": 3,
                                "export": {"state": "rendering", "current": 1500, "total": 6000}})
    open_app(page)
    expect(page.locator("#status")).to_have_text("Rendering 1,500 / 6,000 frames")
    assert page.evaluate("document.getElementById('progress-bar').style.width") == "25%"
    state["export"]["current"] = 3000
    expect(page.locator("#status")).to_have_text("Rendering 3,000 / 6,000 frames")
    assert page.evaluate("document.getElementById('progress-bar').style.width") == "50%"


def test_reveal_in_file_manager(page, server, short_wav, tmp_path):
    select_audio(server, short_wav)
    open_app(page)
    wait_for_frame(page)
    out = tmp_path / "shown.mov"
    api(server, "/api/dev/next-output", {"path": str(out)})
    seen = len(api(server, "/api/dev/reveals")["paths"])
    page.locator("#export-btn").click()
    expect(page.locator("#status")).to_have_text("Saved shown.mov", timeout=RENDER_TIMEOUT_MS)
    expect(page.locator("#reveal-btn")).to_have_text(REVEAL_LABEL)
    page.locator("#reveal-btn").click()
    page.locator("#status").click()
    assert api(server, "/api/dev/reveals")["paths"][seen:] == [str(out)]


def test_end_to_end_export_of_a_changed_look(app, server, tmp_path):
    drive(app, layout_button(app, "scroll").click, layout="scroll")
    drive(app, seg_button(app, "Style", "area").click, style="area")
    drive(app, seg_button(app, "Fill", "false").click, color2=None)
    look = drive(app, app.locator("#formats button").nth(1).click, width=1920, height=240)["look"]
    assert look["layout"] == "scroll" and look["style"] == "area" and look["color2"] is None
    out = tmp_path / "parity.mov"
    api(server, "/api/dev/next-output", {"path": str(out)})
    app.locator("#export-btn").click()
    expect(app.locator("#status")).to_have_text(re.compile(r"^Rendering [\d,]+ / 600 frames$"))
    width = app.evaluate("document.getElementById('progress-bar').style.width")
    assert width.endswith("%") and 0 <= float(width[:-1]) <= 100
    expect(app.locator("#status")).to_have_text("Saved parity.mov", timeout=RENDER_TIMEOUT_MS)
    stream = probe(out)
    assert (stream["codec_name"], int(stream["width"]), int(stream["height"]), int(stream["nb_read_frames"])) == (
        "prores", 1920, 240, 600)


# --------------------------------------------------------------- screenshots

def settled(page, response=None):
    if response is not None:
        response.finished()
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")


def take(page, name):
    PARITY.mkdir(parents=True, exist_ok=True)
    path = PARITY / f"{name}.png"
    seen, csp = len(page.problems), page.evaluate("window.__csp.length")
    page.screenshot(path=str(path))
    # WebKit's screenshot injects an inline style the CSP refuses: the tool's doing, not the page's.
    page.problems[seen:] = [p for p in page.problems[seen:] if not any(m in p for m in _SCREENSHOT_STYLE)]
    page.evaluate("n => { window.__csp = window.__csp.filter((v, i) => i < n || !v.startsWith('style-src-elem')); }", csp)
    return path


@pytest.mark.parametrize("size", ["1040x640", "1920x1200"])
@pytest.mark.parametrize("key", LAYOUTS)
def test_screenshot_layout(app, key, size):
    other = "scroll" if key == "mirror" else "mirror"
    drive(app, layout_button(app, other).click, layout=other)
    drive(app, layout_button(app, key).click, layout=key)
    settled(app)
    assert take(app, f"layout-{key}-{size}").stat().st_size > 5000


@pytest.mark.parametrize("size", ["1920x1200"])
@pytest.mark.parametrize("index", range(len(FORMATS)), ids=[f["label"] for f in FORMATS])
def test_screenshot_export_format(app, index, size):
    chosen, other = FORMATS[index], FORMATS[(index + 1) % len(FORMATS)]
    button = lambda f: app.locator("#formats button").nth(FORMATS.index(f))
    drive(app, button(other).click, width=other["w"], height=other["h"])
    drive(app, button(chosen).click, width=chosen["w"], height=chosen["h"])
    settled(app)
    assert take(app, f"format-{chosen['w']}x{chosen['h']}-{size}").stat().st_size > 5000
