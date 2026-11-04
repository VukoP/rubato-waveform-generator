# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""The inspector in real browsers: catalog-driven controls, layout rules, what reaches the API,
persistence, fit at 288 px and the keyboard."""

import json
import os
import signal
import subprocess
import sys
import urllib.request
from types import SimpleNamespace

import pytest

from rubato_waveform import settings as app_settings
from rubato_waveform.catalog import format_value

from .conftest import (
    _SCREENSHOT_STYLE,
    ROOT,
    SHOTS,
    SIZES,
    _wait_for,
    api,
    mock_state,
    open_app,
    select_audio,
    wait_for_frame,
)

expect = pytest.importorskip("playwright.sync_api").expect

DEFAULT_LOOK_PATH = "/api/catalog"
FRAME_TIMEOUT = 15000

# layout: (playhead, baseline fraction or None, Motion enabled, Style enabled, voice sliders, format)
RULES = {
    "Scroll": (True, "0.5", True, True, False, "1080 × 180"),
    "Center-out": (True, "0.5", True, True, False, "1080 × 180"),
    "Pulse": (False, "0.5", False, True, False, "1080 × 180"),
    "Flow": (True, "0.5", True, True, False, "1080 × 180"),
    "Ripple": (False, "0.5", False, True, False, "1080 × 180"),
    "Neon": (True, "0.5", False, False, False, "1080 × 180"),
    "Reflection": (True, "0.62", False, True, False, "1080 × 180"),
    "Peak hold": (False, None, False, True, False, "1080 × 180"),
    "Orbit": (False, None, True, True, False, "1080 × 1080"),
    "Voice line": (False, "0.5", False, True, True, "1080 × 180"),
}


def get_json(server, path):
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", headers={"X-Rubato-Token": server.token})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


@pytest.fixture(scope="module")
def catalog(server):
    return get_json(server, DEFAULT_LOOK_PATH)


@pytest.fixture(scope="module", autouse=True)
def real_settings_untouched():
    """The tests' servers run in a throw-away home; the user's own settings file must not change."""
    path = app_settings.config_dir() / app_settings.FILE_NAME
    before = path.read_bytes() if path.exists() else None
    yield
    assert (path.read_bytes() if path.exists() else None) == before, "a test wrote the real settings"


@pytest.fixture(autouse=True)
def fresh_settings(server, catalog):
    """The module's server keeps what the pages save; every test starts from the defaults."""
    defaults = catalog["defaults"]
    api(server, "/api/settings", {"look": defaults["look"], "theme": defaults["theme"], "gradient": defaults["gradient"]})


@pytest.fixture
def app(page, server, demo_wav):
    select_audio(server, demo_wav)
    open_app(page)
    wait_for_frame(page)
    return page


# ------------------------------------------------------------------ helpers

def radio(page, group, option):
    return page.get_by_role("radiogroup", name=group, exact=True).get_by_role("radio", name=option, exact=True)


def slider(page, name):
    return page.get_by_role("slider", name=name, exact=True)


def hex_field(page, letter):
    return page.get_by_label(f"Color {letter} hex", exact=True)


def picker(page, letter):
    return page.get_by_label(f"Color {letter}", exact=True)


def footer_format(page, label):
    return page.locator("#formats button", has_text=label)


def changed(page, action, **expected):
    """Runs the action and returns the Look of the first /api/frame request that carries the values."""
    def wanted(request):
        return request.url.endswith("/api/frame") and all(
            request.post_data_json["look"].get(k) == v for k, v in expected.items())
    with page.expect_request(wanted, timeout=FRAME_TIMEOUT) as info:
        action()
    return info.value.post_data_json["look"]


def theme(catalog, name):
    return next(t for t in catalog["themes"] if t["name"] == name)


# -------------------------------------------------------------------- rules

@pytest.mark.parametrize("name", list(RULES))
def test_layout_rules(app, name):
    playhead, baseline, motion, style, voice, fmt = RULES[name]
    radio(app, "Layout", name).click()
    expect(radio(app, "Layout", name)).to_have_attribute("aria-checked", "true")
    expect(app.locator("#playhead")).to_have_attribute("data-on", str(playhead).lower())
    if playhead:
        expect(app.locator("#playhead")).to_be_visible()
    else:
        expect(app.locator("#playhead")).to_be_hidden()
    expect(app.locator("#baseline")).to_have_attribute("data-at", baseline or "none")
    if baseline:
        expect(app.locator("#baseline")).to_be_visible()
    else:
        expect(app.locator("#baseline")).to_be_hidden()
    for group, enabled in (("Motion", motion), ("Style", style)):
        buttons = app.get_by_role("radiogroup", name=group, exact=True).get_by_role("radio")
        for i in range(buttons.count()):
            (expect(buttons.nth(i)).to_be_enabled if enabled else expect(buttons.nth(i)).to_be_disabled)()
    for field in ("Motion", "Style"):
        group = app.get_by_role("radiogroup", name=field, exact=True)
        (expect(group).to_be_hidden if voice else expect(group).to_be_visible)()
    for field in ("Reactivity", "Drift", "Thickness"):
        (expect(slider(app, field)).to_be_visible if voice else expect(slider(app, field)).to_be_hidden)()
    expect(app.locator("#formats button[aria-pressed=true]")).to_have_text(fmt)
    expect(app.locator("#size-label")).to_have_text(f"{fmt} · 30 FPS")


def test_reflection_baseline_sits_at_62_percent_of_the_frame(app):
    radio(app, "Layout", "Reflection").click()
    expect(app.locator("#baseline")).to_have_attribute("data-at", "0.62")
    fraction = app.evaluate("""() => {
      const c = document.getElementById('preview'), b = document.getElementById('baseline');
      return (b.offsetTop - c.offsetTop) / c.offsetHeight;
    }""")
    assert fraction == pytest.approx(0.62, abs=0.01)


def test_stored_motion_choice_survives_a_disabled_layout(app):
    radio(app, "Motion", "Static").click()
    radio(app, "Layout", "Pulse").click()
    expect(radio(app, "Motion", "Static")).to_be_disabled()
    expect(radio(app, "Motion", "Static")).to_have_attribute("aria-checked", "true")
    radio(app, "Layout", "Scroll").click()
    expect(radio(app, "Motion", "Static")).to_be_enabled()
    expect(radio(app, "Motion", "Static")).to_have_attribute("aria-checked", "true")


def test_orbit_restores_the_previous_format(app):
    footer_format(app, "1920 × 240").click()
    radio(app, "Layout", "Orbit").click()
    expect(app.locator("#status")).to_have_text("Orbit needs a square frame. Switched to 1080 × 1080.")
    expect(app.locator("#formats button[aria-pressed=true]")).to_have_text("1080 × 1080")
    radio(app, "Layout", "Scroll").click()
    expect(app.locator("#formats button[aria-pressed=true]")).to_have_text("1920 × 240")
    expect(app.locator("#status")).to_have_text("Restored 1920 × 240.")


def test_manual_format_on_orbit_is_kept_after_leaving(app):
    radio(app, "Layout", "Orbit").click()
    expect(app.locator("#size-label")).to_have_text("1080 × 1080 · 30 FPS")
    footer_format(app, "3840 × 360").click()
    radio(app, "Layout", "Scroll").click()
    expect(radio(app, "Layout", "Scroll")).to_have_attribute("aria-checked", "true")
    expect(app.locator("#formats button[aria-pressed=true]")).to_have_text("3840 × 360")
    expect(app.locator("#status")).not_to_contain_text("Restored")


def test_orbit_in_an_empty_app_still_works_and_controls_stay_usable(page):
    mock_state(page)
    open_app(page)
    assert page.locator("#empty").is_visible()
    radio(page, "Layout", "Orbit").click()
    expect(page.locator("#status")).to_have_text("Orbit needs a square frame. Switched to 1080 × 1080.")
    slider(page, "Sensitivity").fill("1.5")
    expect(slider(page, "Sensitivity")).to_have_attribute("aria-valuetext", "1.50×")


# ------------------------------------------------------- what reaches the API

SLIDERS = [
    ("Sensitivity", "1.5", {"gain": 1.5}), ("Contrast", "1.2", {"contrast": 1.2}),
    ("Smoothing", "0.5", {"smoothing": 0.5}), ("Time window", "2", {"window_seconds": 2.0}),
    ("Density", "100", {"bars": 100}), ("Height", "0.5", {"max_height_ratio": 0.5}),
    ("Opacity", "100", {"opacity": 100}),
]
VOICE_SLIDERS = [
    ("Reactivity", "0.3", {"reactivity": 0.3}), ("Drift", "1.5", {"drift": 1.5}),
    ("Thickness", "3", {"line_width": 3.0}),
]


@pytest.mark.parametrize(("name", "value", "expected"), SLIDERS)
def test_slider_maps_to_its_field(app, catalog, name, value, expected):
    look = changed(app, lambda: slider(app, name).fill(value), **expected)
    assert set(look) == set(catalog["defaults"]["look"])        # always the full look


@pytest.mark.parametrize(("name", "value", "expected"), VOICE_SLIDERS)
def test_voice_slider_maps_to_its_field(app, name, value, expected):
    radio(app, "Layout", "Voice line").click()
    changed(app, lambda: slider(app, name).fill(value), layout="voiceline", **expected)


CONTROLS = [
    ("metric", lambda p, c: radio(p, "Measure", "Loudness").click(), {"metric": "rms"}),
    ("layout", lambda p, c: radio(p, "Layout", "Scroll").click(), {"layout": "scroll"}),
    ("style", lambda p, c: radio(p, "Style", "Line").click(), {"style": "line"}),
    ("motion", lambda p, c: radio(p, "Motion", "Static").click(), {"stationary": True}),
    ("theme", lambda p, c: radio(p, "Theme", "Coral").click(),
     {"color": "COLOR", "color2": "COLOR2"}),
    ("solid", lambda p, c: radio(p, "Fill", "Solid").click(), {"color2": None}),
    ("custom-a", lambda p, c: hex_field(p, "A").fill("#12ab34"), {"color": "#12AB34"}),
    ("custom-b", lambda p, c: hex_field(p, "B").fill("#34AB12"), {"color2": "#34AB12"}),
    ("format", lambda p, c: footer_format(p, "1920 × 240").click(), {"width": 1920, "height": 240}),
]


@pytest.mark.parametrize(("label", "action", "expected"), CONTROLS, ids=[c[0] for c in CONTROLS])
def test_control_maps_to_its_field(app, catalog, label, action, expected):
    coral = theme(catalog, "Coral")
    expected = {k: {"COLOR": coral["color"], "COLOR2": coral["color2"]}.get(v, v) if isinstance(v, str) else v
                for k, v in expected.items()}
    changed(app, lambda: action(app, catalog), **expected)


def test_gradient_brings_color_b_back(app):
    changed(app, lambda: hex_field(app, "B").fill("#34AB12"), color2="#34AB12")
    changed(app, lambda: radio(app, "Fill", "Solid").click(), color2=None)
    changed(app, lambda: radio(app, "Fill", "Gradient").click(), color2="#34AB12")


@pytest.mark.parametrize("key", ["calm", "balanced", "energetic"])
def test_presets_apply_the_catalog_values_and_move_the_sliders(app, catalog, key):
    preset = next(p for p in catalog["presets"] if p["key"] == key)
    other = "Calm" if key != "calm" else "Energetic"
    app.get_by_role("button", name=other, exact=True).click()
    app.get_by_role("button", name=preset["label"], exact=True).click()
    expect(app.get_by_role("button", name=preset["label"], exact=True)).to_have_attribute("aria-pressed", "true")
    by_field = {s["field"]: s for s in catalog["sliders"]}
    for field, value in preset["values"].items():
        spec = by_field[field]
        expect(slider(app, spec["label"])).to_have_attribute("aria-valuetext", format_value(spec, value))


@pytest.mark.parametrize("key", ["calm", "balanced", "energetic"])
def test_preset_request_carries_exact_values(app, catalog, key):
    preset = next(p for p in catalog["presets"] if p["key"] == key)
    other = "Calm" if key != "calm" else "Energetic"
    app.get_by_role("button", name=other, exact=True).click()
    changed(app, lambda: app.get_by_role("button", name=preset["label"], exact=True).click(), **preset["values"])


# ---------------------------------------------------------------- formatting

DEFAULT_TEXT = {
    "Sensitivity": "1.00×", "Contrast": "1.00", "Smoothing": "25%", "Time window": "1.05 s",
    "Density": "61", "Height": "72%", "Opacity": "92%",
}


def value_of(page, name):
    return page.locator(".slider-row", has=slider(page, name)).locator(".val")


def test_default_values_read_exactly(app):
    for name, text in DEFAULT_TEXT.items():
        expect(value_of(app, name)).to_have_text(text)
        expect(slider(app, name)).to_have_attribute("aria-valuetext", text)
    radio(app, "Layout", "Voice line").click()
    for name, text in {"Reactivity": "70%", "Drift": "1.00×", "Thickness": "2.0 px"}.items():
        expect(value_of(app, name)).to_have_text(text)


def test_every_slider_matches_the_catalog_format_at_its_extremes(app, catalog):
    radio(app, "Layout", "Voice line").click()
    for spec in catalog["sliders"]:
        for end in ("min", "max"):
            raw = spec[end]
            slider(app, spec["label"]).fill(f"{raw:g}")
            expected = format_value(spec, float(raw))
            expect(value_of(app, spec["label"])).to_have_text(expected)
            expect(slider(app, spec["label"])).to_have_attribute("aria-valuetext", expected)


def test_density_and_opacity_move_in_whole_steps(app):
    for name in ("Density", "Opacity"):
        assert app.get_by_role("slider", name=name, exact=True).get_attribute("step") == "1"


# -------------------------------------------------------------- custom colors

def test_valid_hex_applies_and_the_label_reads_custom(app):
    expect(app.locator("#theme-name")).to_have_text("Violet")
    changed(app, lambda: hex_field(app, "A").fill("#ff8800"), color="#FF8800")
    expect(app.locator("#theme-name")).to_have_text("Custom")
    expect(picker(app, "A")).to_have_value("#ff8800")
    expect(hex_field(app, "A")).not_to_have_attribute("aria-invalid", "true")
    expect(app.get_by_role("radiogroup", name="Theme", exact=True).locator("[aria-checked=true]")).to_have_count(0)
    radio(app, "Theme", "Violet").click()
    expect(app.locator("#theme-name")).to_have_text("Violet")


def test_invalid_hex_is_flagged_and_not_applied(app):
    hex_field(app, "A").fill("#12")
    expect(hex_field(app, "A")).to_have_attribute("aria-invalid", "true")
    expect(app.locator("#theme-name")).to_have_text("Violet")
    expect(picker(app, "A")).to_have_value("#8b4dff")
    hex_field(app, "A").fill("#12zz45")
    expect(hex_field(app, "A")).to_have_attribute("aria-invalid", "true")
    expect(radio(app, "Theme", "Violet")).to_have_attribute("aria-checked", "true")
    hex_field(app, "A").blur()                         # leaving the field puts the applied color back
    expect(hex_field(app, "A")).to_have_value("#8B4DFF")
    expect(hex_field(app, "A")).not_to_have_attribute("aria-invalid", "true")


def test_picker_and_text_field_stay_in_sync(app):
    changed(app, lambda: picker(app, "B").fill("#00ff66"), color2="#00FF66")
    expect(hex_field(app, "B")).to_have_value("#00FF66")
    expect(app.locator("#theme-name")).to_have_text("Custom")
    hex_field(app, "A").fill("#aa00bb")
    expect(picker(app, "A")).to_have_value("#aa00bb")


def test_color_b_is_disabled_in_solid(app):
    radio(app, "Fill", "Solid").click()
    expect(hex_field(app, "B")).to_be_disabled()
    expect(picker(app, "B")).to_be_disabled()
    expect(hex_field(app, "A")).to_be_enabled()
    radio(app, "Fill", "Gradient").click()
    expect(hex_field(app, "B")).to_be_enabled()


def test_swatches_are_named_buttons_with_a_diagonal_split(app, catalog):
    swatches = app.get_by_role("radiogroup", name="Theme", exact=True).get_by_role("radio")
    assert swatches.count() == len(catalog["themes"]) == 18
    for i, spec in enumerate(catalog["themes"]):
        swatch = swatches.nth(i)
        assert swatch.get_attribute("title") == swatch.get_attribute("aria-label") == spec["name"]
    first = swatches.first
    assert "linear-gradient" in first.evaluate("n => getComputedStyle(n).backgroundImage")
    assert first.evaluate("n => getComputedStyle(n).borderRadius") == "6px"
    columns = app.locator(".swatches").evaluate("n => getComputedStyle(n).gridTemplateColumns.split(' ').length")
    assert columns == 9


# -------------------------------------------------------------- persistence

def settings_look(server):
    return get_json(server, "/api/settings")


def restored_look(page):
    """What the controls show, as plain data."""
    return {
        "layout": page.get_by_role("radiogroup", name="Layout", exact=True).locator("[aria-checked=true]").text_content(),
        "gain": slider(page, "Sensitivity").get_attribute("aria-valuetext"),
        "bars": slider(page, "Density").get_attribute("aria-valuetext"),
        "theme": page.locator("#theme-name").text_content(),
        "hex_b": hex_field(page, "B").input_value(),
        "format": page.locator("#formats button[aria-pressed=true]").text_content(),
    }


def make_a_look(page):
    radio(page, "Layout", "Flow").click()
    slider(page, "Sensitivity").fill("1.5")
    slider(page, "Density").fill("90")
    radio(page, "Theme", "Cyan").click()
    hex_field(page, "B").fill("#102030")
    footer_format(page, "1920 × 240").click()


def saved(server):
    data = settings_look(server)
    return data["look"]["layout"] == "flow" and data["look"]["width"] == 1920 and data["look"]["color2"] == "#102030" \
        and data["look"]["bars"] == 90 and data["look"]["gain"] == 1.5


WANTED = {"layout": "Flow", "gain": "1.50×", "bars": "90", "theme": "Custom", "hex_b": "#102030", "format": "1920 × 240"}


def test_settings_survive_a_reload(app, server):
    make_a_look(app)
    _wait_for(lambda: saved(server), 10, "the settings were not saved")
    open_app(app)                                      # the page forgets its token on purpose: a reload is a fresh start
    expect(radio(app, "Layout", "Flow")).to_have_attribute("aria-checked", "true")
    assert restored_look(app) == WANTED


class OwnServer:
    """A dev-browser server with a settings directory of its own (the environment redirects it: the
    app derives its config directory from HOME / XDG_CONFIG_HOME / APPDATA)."""

    def __init__(self, home):
        self.home = home
        self.proc = None
        self.url = self.port = self.token = None

    def start(self):
        port_file = self.home / "port.json"
        port_file.unlink(missing_ok=True)
        env = {**os.environ, "HOME": str(self.home), "XDG_STATE_HOME": str(self.home / "state"),
               "XDG_CONFIG_HOME": str(self.home / "config"), "APPDATA": str(self.home / "appdata"),
               "LOCALAPPDATA": str(self.home / "local"), "USERPROFILE": str(self.home)}
        self.proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "rubato_waveform.app", "--dev-browser", "--port-file", str(port_file)],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        _wait_for(lambda: port_file.exists() or self.proc.poll() is not None, 60, "the dev server did not start")
        if self.proc.poll() is not None:
            raise RuntimeError("the dev server exited: " + self.proc.stderr.read())
        info = json.loads(port_file.read_text())
        self.url, self.port, self.token = info["url"], info["port"], info["url"].split("token=")[1]

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.communicate()


@pytest.fixture
def own_server(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    own = OwnServer(home)
    own.start()
    yield own
    own.stop()


def test_settings_survive_a_server_restart(page, own_server):
    page.server = SimpleNamespace(url=own_server.url, port=own_server.port, token=own_server.token)
    open_app(page)
    make_a_look(page)
    _wait_for(lambda: saved(own_server), 10, "the settings were not saved")
    page.goto("about:blank")                           # nothing polls a server that is about to stop
    own_server.stop()
    own_server.start()
    page.server = SimpleNamespace(url=own_server.url, port=own_server.port, token=own_server.token)
    open_app(page)
    expect(radio(page, "Layout", "Flow")).to_have_attribute("aria-checked", "true")
    assert restored_look(page) == WANTED
    files = list(own_server.home.rglob(app_settings.FILE_NAME))
    assert len(files) == 1, files                      # the settings went to the test's own directory


def test_orbit_still_restores_the_previous_format_after_a_server_restart(page, own_server):
    page.server = SimpleNamespace(url=own_server.url, port=own_server.port, token=own_server.token)
    open_app(page)
    footer_format(page, "1920 × 240").click()
    radio(page, "Layout", "Orbit").click()
    expect(page.locator("#formats button[aria-pressed=true]")).to_have_text("1080 × 1080")
    _wait_for(lambda: settings_look(own_server)["auto_format"] == {"w": 1920, "h": 240}, 10, "the format was not saved")
    page.goto("about:blank")
    own_server.stop()
    own_server.start()
    page.server = SimpleNamespace(url=own_server.url, port=own_server.port, token=own_server.token)
    open_app(page)
    expect(radio(page, "Layout", "Orbit")).to_have_attribute("aria-checked", "true")
    radio(page, "Layout", "Scroll").click()
    expect(page.locator("#formats button[aria-pressed=true]")).to_have_text("1920 × 240")
    expect(page.locator("#status")).to_have_text("Restored 1920 × 240.")


def test_a_manual_square_format_with_orbit_stays_after_a_server_restart(page, own_server):
    page.server = SimpleNamespace(url=own_server.url, port=own_server.port, token=own_server.token)
    open_app(page)
    radio(page, "Layout", "Orbit").click()
    footer_format(page, "1920 × 240").click()
    footer_format(page, "1080 × 1080").click()                 # chosen by hand: nothing to restore
    _wait_for(lambda: settings_look(own_server)["auto_format"] is None
              and settings_look(own_server)["look"]["layout"] == "radial", 10, "not saved")
    page.goto("about:blank")
    own_server.stop()
    own_server.start()
    page.server = SimpleNamespace(url=own_server.url, port=own_server.port, token=own_server.token)
    open_app(page)
    radio(page, "Layout", "Scroll").click()
    expect(page.locator("#formats button[aria-pressed=true]")).to_have_text("1080 × 1080")


# ----------------------------------------------------------------------- fit

LABELS_JS = """() => {
  const out = [];
  const inspector = document.getElementById('inspector');
  if (inspector.scrollWidth > inspector.clientWidth) out.push('inspector overflows: ' + inspector.scrollWidth + ' > ' + inspector.clientWidth);
  const edge = inspector.getBoundingClientRect().left + inspector.clientLeft + inspector.clientWidth + 0.5;
  for (const n of inspector.querySelectorAll('.field-label, .slider-row label, .val, .seg-ctl button, .layout-grid button, .chip, .theme-name, h2, p')) {
    if (n.offsetParent === null) continue;
    const range = document.createRange();
    range.selectNodeContents(n);
    if (n.scrollWidth > n.clientWidth) out.push('clipped: ' + n.textContent);
    if (range.getBoundingClientRect().height > parseFloat(getComputedStyle(n).fontSize) * 1.8 && !n.matches('.sec-head p')) {
      out.push('wraps: ' + n.textContent);
    }
  }
  for (const n of inspector.querySelectorAll('*')) {
    if (n.offsetParent === null) continue;
    const box = n.getBoundingClientRect();
    if (box.width && box.right > edge) out.push('past the edge: ' + (n.className || n.tagName) + ' ' + box.right);
  }
  return out;
}"""


@pytest.mark.parametrize("layout", ["Scroll", "Voice line"])
def test_inspector_never_overflows_and_labels_stay_on_one_line(app, layout):
    radio(app, "Layout", layout).click()
    expect(radio(app, "Layout", layout)).to_have_attribute("aria-checked", "true")
    assert app.evaluate(LABELS_JS) == []
    width = app.evaluate("() => document.querySelector('#color-body').getBoundingClientRect().width")
    assert 279 <= width <= 288      # the 320 px column less border and padding, and a classic scrollbar where shown


def test_the_inspector_scrolls_and_the_footer_stays_put(app):
    before = app.locator(".app-foot").bounding_box()
    scrolls = app.evaluate("() => { const i = document.getElementById('inspector'); return i.scrollHeight > i.clientHeight; }")
    if app.size_name == "1040x640":
        assert scrolls
    app.evaluate("() => { document.getElementById('inspector').scrollTop = 100000; }")
    assert app.locator(".app-foot").bounding_box() == before
    assert app.evaluate("() => document.documentElement.scrollTop") == 0
    assert app.evaluate("() => document.documentElement.scrollHeight <= document.documentElement.clientHeight")


# ------------------------------------------------------------------ keyboard

def test_arrow_keys_move_the_selection_in_a_segmented_control(app):
    radio(app, "Style", "Bars").focus()                   # a mouse click gives the focus up (Space plays)
    app.keyboard.press("ArrowRight")
    expect(radio(app, "Style", "Line")).to_have_attribute("aria-checked", "true")
    expect(radio(app, "Style", "Line")).to_be_focused()
    expect(radio(app, "Style", "Line")).to_have_attribute("tabindex", "0")
    expect(radio(app, "Style", "Bars")).to_have_attribute("tabindex", "-1")
    app.keyboard.press("End")
    expect(radio(app, "Style", "Dots")).to_have_attribute("aria-checked", "true")
    app.keyboard.press("ArrowRight")                      # wraps
    expect(radio(app, "Style", "Bars")).to_have_attribute("aria-checked", "true")
    app.keyboard.press("ArrowLeft")
    expect(radio(app, "Style", "Dots")).to_have_attribute("aria-checked", "true")


def test_arrow_keys_move_through_the_layouts_and_the_themes(app):
    radio(app, "Layout", "Center-out").focus()
    app.keyboard.press("ArrowRight")
    expect(radio(app, "Layout", "Pulse")).to_have_attribute("aria-checked", "true")
    radio(app, "Theme", "Violet").focus()
    app.keyboard.press("ArrowRight")
    expect(radio(app, "Theme", "Lavender")).to_have_attribute("aria-checked", "true")
    expect(app.locator("#theme-name")).to_have_text("Lavender")


def test_disabled_options_are_skipped_by_the_arrows(app):
    radio(app, "Layout", "Neon").click()
    for name in ("Bars", "Line", "Area", "Dots"):
        expect(radio(app, "Style", name)).to_be_disabled()
    radio(app, "Layout", "Scroll").click()
    radio(app, "Motion", "Moving").click()
    radio(app, "Motion", "Moving").focus()
    app.keyboard.press("ArrowRight")
    expect(radio(app, "Motion", "Static")).to_have_attribute("aria-checked", "true")


def test_range_keys_change_the_value_and_the_valuetext(app):
    sensitivity = slider(app, "Sensitivity")
    sensitivity.focus()
    app.keyboard.press("ArrowRight")
    expect(sensitivity).to_have_attribute("aria-valuetext", "1.01×")
    app.keyboard.press("End")
    expect(sensitivity).to_have_attribute("aria-valuetext", "2.50×")
    app.keyboard.press("Home")
    expect(sensitivity).to_have_attribute("aria-valuetext", "0.30×")
    density = slider(app, "Density")
    density.focus()
    app.keyboard.press("ArrowRight")
    expect(density).to_have_attribute("aria-valuetext", "62")


# --------------------------------------------------------------- screenshots

def inspector_shot(page, label):
    """The whole inspector, scrolled or not: the window grows tall for the capture and goes back."""
    width, height = SIZES[page.size_name]
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"inspector-{label}-{page.browser_name}-{page.size_name}.png"
    page.set_viewport_size({"width": width, "height": 1400})
    seen = len(page.problems)
    csp = page.evaluate("window.__csp.length")
    page.locator("#inspector").screenshot(path=str(path))
    page.problems[seen:] = [p for p in page.problems[seen:] if not any(m in p for m in _SCREENSHOT_STYLE)]
    page.evaluate("n => { window.__csp = window.__csp.filter((v, i) => i < n || !v.startsWith('style-src-elem')); }", csp)
    page.set_viewport_size({"width": width, "height": height})
    return path


@pytest.mark.parametrize("layout", ["Scroll", "Voice line"])
def test_screenshots_of_the_full_inspector(app, browser, layout, shot):
    app.browser_name = browser.browser_name
    radio(app, "Layout", layout).click()
    expect(radio(app, "Layout", layout)).to_have_attribute("aria-checked", "true")
    assert inspector_shot(app, layout.lower().replace(" ", "-")).stat().st_size > 5000
    shot(layout.lower().replace(" ", "-"))
