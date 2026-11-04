# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""The audio transport in real browsers: real audio over real HTTP, the conversion fallback, and the
keyboard. Headless engines may not be able to play sound at all; such tests skip with what was seen."""

import subprocess
import sys

import pytest

from rubato_waveform import ffmpeg_tools

from .conftest import (
    ROOT,
    allow_failed_loads,
    get_json,
    mock_state,
    open_app,
    reset_look,
    select_audio,
    wait_for_frame,
)

expect = pytest.importorskip("playwright.sync_api").expect

LOAD_TIMEOUT_MS = 20000

# Resolves when the audio element holds metadata of version `minVersion` or later.
WAIT_LOADED = """({ minVersion, timeout }) => new Promise((resolve, reject) => {
  const a = document.querySelector('audio');
  const check = () => {
    const v = Number(new URL(a.currentSrc || 'http://x/?v=-1').searchParams.get('v'));
    if (a.readyState >= 1 && v >= minVersion) { resolve({ version: v, duration: a.duration }); return true; }
    return false;
  };
  if (check()) return;
  a.addEventListener('loadedmetadata', check);
  setTimeout(() => reject(new Error('the audio did not load')), timeout);
})"""

# Resolves when currentTime passes `at`; rejects with what the element looked like if it never does.
WAIT_PLAYING = """({ at, timeout }) => new Promise((resolve, reject) => {
  const a = document.querySelector('audio');
  const done = () => { if (a.currentTime > at) { resolve(a.currentTime); return true; } return false; };
  if (done()) return;
  a.addEventListener('timeupdate', done);
  setTimeout(() => reject(new Error(
    `paused=${a.paused} readyState=${a.readyState} currentTime=${a.currentTime} error=${a.error && a.error.code}`)), timeout);
})"""

WATCH_KEYS = """() => {
  window.__keys = [];
  document.addEventListener('keydown', (e) => window.__keys.push([e.key, e.defaultPrevented]));
}"""


@pytest.fixture(autouse=True)
def fresh_settings(server):
    reset_look(server)


@pytest.fixture
def selected(page, server, demo_wav):
    """A page for tests that set up routes first: the audio is selected, the page not yet open."""
    select_audio(server, demo_wav)
    return page


@pytest.fixture
def app(selected):
    open_app(selected)
    wait_for_frame(selected)
    return selected


@pytest.fixture(scope="session")
def short_wav(tmp_path_factory):
    path = tmp_path_factory.mktemp("short") / "short.wav"
    subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "make_demo_audio.py"), "--out", str(path),
                    "--seconds", "6"], check=True, capture_output=True, timeout=120)
    return path


@pytest.fixture(scope="session")
def demo_aiff(demo_wav, tmp_path_factory):
    ffmpeg = ffmpeg_tools.find_ffmpeg()
    if ffmpeg is None:
        pytest.skip("FFmpeg is not available to make an AIFF file")
    path = tmp_path_factory.mktemp("aiff") / "demo.aiff"
    subprocess.run([str(ffmpeg), "-v", "error", "-i", str(demo_wav), "-c:a", "pcm_s16be", str(path)],
                   check=True, capture_output=True, timeout=120)
    return path


def audio_loaded(page, min_version=0):
    return page.evaluate(WAIT_LOADED, {"minVersion": min_version, "timeout": LOAD_TIMEOUT_MS})


def play(page):
    """Presses Play and waits until the sound is really moving; skips if this engine cannot play."""
    page.locator("#play-btn").click()
    try:
        page.evaluate(WAIT_PLAYING, {"at": 0.2, "timeout": 10000})
    except Exception as exc:                      # noqa: BLE001 - whatever the engine did is the skip reason
        pytest.skip("this headless engine did not play the audio: " + str(exc).splitlines()[0])


def count_calls(page, pattern, answer):
    calls = []

    def handle(route):
        calls.append(route.request.url)
        route.fulfill(status=200, json=answer)
    page.route(pattern, handle)
    return calls


# ----------------------------------------------------------------- real audio

def test_the_audio_element_loads_the_wav_with_range_requests(selected, browser):
    app = selected
    seen = []
    app.on("response", lambda r: "/api/audio?" in r.url and seen.append(
        (r.request.headers.get("range"), r.status, r.headers.get("content-range"))))
    open_app(app)
    wait_for_frame(app)
    info = audio_loaded(app)
    assert abs(info["duration"] - 20) < 0.1
    assert seen, "the audio element never asked for the file"
    print(f"\n[{browser.browser_name}] audio requests (Range, status, Content-Range): {seen}")
    for requested, status, content_range in seen:
        assert status in (200, 206)
        if requested:
            assert status == 206 and content_range, (requested, status, content_range)


def test_play_moves_the_time_and_pause_stops_it(app):
    play(app)
    expect(app.locator("#play-btn")).to_have_attribute("aria-label", "Pause")
    expect(app.locator("#stage")).to_have_class("stage playing")
    expect(app.locator("#badge-text")).to_have_text("Playing")
    assert app.locator(".badge-dot").evaluate("n => getComputedStyle(n).display") == "block"
    expect(app.locator("#time-now")).to_have_text("00:01", timeout=15000)
    now = app.evaluate("document.querySelector('audio').currentTime")
    assert 0.9 < now < 6                                      # not an exact value: the clock is the audio
    app.locator("#play-btn").click()
    expect(app.locator("#play-btn")).to_have_attribute("aria-label", "Play")
    expect(app.locator("#badge-text")).to_have_text("Transparent")
    assert app.evaluate("document.querySelector('audio').paused")
    assert app.locator(".badge-dot").evaluate("n => getComputedStyle(n).display") == "none"


def test_the_preview_follows_the_playing_audio(app):
    bodies = []
    app.on("request", lambda r: r.url.endswith("/api/frame") and bodies.append(r.post_data_json["t"]))
    play(app)
    expect(app.locator("#time-now")).to_have_text("00:01", timeout=15000)
    assert max(bodies) > 0.8


def test_scrub_moves_t_in_the_frame_requests(app):
    with app.expect_request(lambda r: r.url.endswith("/api/frame") and r.post_data_json["t"] == 10):
        app.locator("#scrub").fill("5000")
    expect(app.locator("#time-now")).to_have_text("00:10")
    expect(app.locator("#preview")).to_have_attribute("data-t", "10")


def test_scrub_while_playing_seeks_the_audio(app):
    play(app)
    app.locator("#scrub").fill("5000")
    expect(app.locator("#time-now")).to_have_text("00:10")
    assert 9.9 < app.evaluate("document.querySelector('audio').currentTime") < 14


def test_a_blocked_play_says_so_in_plain_words(app):
    app.evaluate("""() => {
      document.querySelector('audio').play = () => Promise.reject(new DOMException('no', 'NotAllowedError'));
    }""")
    app.locator("#play-btn").click()
    expect(app.locator("#status")).to_have_text("Playback was blocked by the system. Press Play again.")


def test_the_end_stops_and_the_next_play_starts_over(app):
    app.locator("#scrub").fill("9900")                        # 19.8 s of 20
    play(app)
    expect(app.locator("#play-btn")).to_have_attribute("aria-label", "Play", timeout=15000)
    expect(app.locator("#time-now")).to_have_text("00:20")
    expect(app.locator("#scrub")).to_have_value("10000")
    app.locator("#play-btn").click()
    expect(app.locator("#play-btn")).to_have_attribute("aria-label", "Pause")
    expect(app.locator("#time-now")).to_have_text("00:00")
    assert app.evaluate("document.querySelector('audio').currentTime") < 3


def test_selecting_a_new_file_stops_playback(app, server, short_wav):
    play(app)
    select_audio(server, short_wav)
    expect(app.locator("#play-btn")).to_have_attribute("aria-label", "Play", timeout=15000)
    expect(app.locator("#time-total")).to_have_text("00:06")
    expect(app.locator("#time-now")).to_have_text("00:00")
    assert app.evaluate("document.querySelector('audio').paused")
    audio_loaded(app, get_json(server, "/api/state")["audio_version"])
    # A frame asked for while the page still showed the old file may get one 409; the page then stops asking.
    allow_failed_loads(app, "/api/frame", 409)


def test_the_play_button_is_full_white_when_enabled(app):
    button = app.locator("#play-btn")
    style = "n => { const s = getComputedStyle(n); return [s.opacity, s.filter, s.backgroundColor]; }"
    ink = ["1", "none", "rgb(237, 237, 240)"]
    assert button.evaluate(style) == ink
    button.hover()
    assert button.evaluate(style) == ink                        # the mouse resting on it changes nothing
    app.mouse.move(5, 5)
    assert button.evaluate(style) == ink


def test_the_play_button_is_dimmed_when_disabled(page):
    mock_state(page)
    open_app(page)
    assert page.locator("#play-btn").evaluate("n => getComputedStyle(n).opacity") == "0.4"


# --------------------------------------------------------------------- fallback

def test_forced_decode_failure_converts_once_and_becomes_playable(selected, server):
    app = selected
    version = get_json(server, "/api/state")["audio_version"]
    app.route(f"**/api/audio?*v={version}", lambda route: route.fulfill(status=500, body="no"))
    calls = []
    app.on("request", lambda r: r.url.endswith("/api/audio/fallback") and calls.append(r))
    open_app(app)
    wait_for_frame(app)
    info = audio_loaded(app, version + 1)
    assert info["version"] == version + 1 and abs(info["duration"] - 20) < 0.1
    assert len(calls) == 1
    expect(app.locator("#play-btn")).to_be_enabled()
    assert get_json(server, "/api/state")["playback"]["mode"] == "fallback"
    allow_failed_loads(app, "/api/audio", 500)


def test_aiff_is_converted_only_where_the_engine_cannot_decode_it(page, server, demo_aiff, browser):
    select_audio(server, demo_aiff)
    version = get_json(server, "/api/state")["audio_version"]
    calls = []
    page.on("request", lambda r: r.url.endswith("/api/audio/fallback") and calls.append(r))
    open_app(page)
    wait_for_frame(page)
    info = page.evaluate(WAIT_LOADED, {"minVersion": 0, "timeout": LOAD_TIMEOUT_MS})
    if len(calls) == 0 and info["version"] == version:
        native = True
    else:
        info = audio_loaded(page, version + 1)
        native = False
    print(f"\n[{browser.browser_name}] AIFF decoded natively: {native}; fallback calls: {len(calls)}")
    assert len(calls) == (0 if native else 1)
    assert abs(info["duration"] - 20) < 0.1
    expect(page.locator("#play-btn")).to_be_enabled()


def test_when_even_the_converted_file_fails_play_stays_disabled(selected):
    app = selected
    app.route("**/api/audio?*", lambda route: route.fulfill(status=500, body="no"))
    open_app(app)
    expect(app.locator("#status")).to_have_text(
        "Playback is unavailable for this file. Preview and export still work.", timeout=20000)
    expect(app.locator("#play-btn")).to_be_disabled()
    expect(app.locator("#preview[data-frame]")).to_be_visible()
    app.locator("#scrub").fill("5000")
    expect(app.locator("#preview")).to_have_attribute("data-t", "10")      # frames keep working
    assert app.locator("#export-btn").is_enabled()
    allow_failed_loads(app, "/api/audio", 500)


def test_a_failing_conversion_request_leaves_play_disabled(selected, server):
    app = selected
    version = get_json(server, "/api/state")["audio_version"]
    app.route(f"**/api/audio?*v={version}", lambda route: route.fulfill(status=500, body="no"))
    app.route("**/api/audio/fallback", lambda route: route.fulfill(status=500, json={"error": "No converter."}))
    open_app(app)
    expect(app.locator("#play-btn")).to_be_disabled()
    expect(app.locator("#status")).to_contain_text("Playback is unavailable")
    expect(app.locator("#preview[data-frame]")).to_be_visible()
    allow_failed_loads(app, "/api/audio", 500)
    allow_failed_loads(app, "/api/audio/fallback", 500)


def test_play_is_disabled_while_the_audio_is_prepared(page):
    mock_state(page, audio={"name": "a.flac", "extension": "flac", "duration": 20.0, "has_spectrum": True},
               analysis={"state": "ready", "progress": 1.0, "error": None}, step=2, audio_version=3,
               playback={"mode": "transcoding", "error": None})
    open_app(page)
    wait_for_frame(page)
    expect(page.locator("#play-btn")).to_be_disabled()
    expect(page.locator("#status")).to_have_text("Preparing audio for playback…")


# --------------------------------------------------------------------- keyboard

def active_is_body(page):
    return page.evaluate("document.activeElement === document.body")


def slider_value(slider):
    return slider.evaluate("n => Number(n.value)")


def test_space_plays_after_a_mouse_click_on_a_button(app):
    app.get_by_role("radiogroup", name="Layout", exact=True).get_by_role("radio", name="Neon", exact=True).click()
    expect(app.locator("#size-label")).to_contain_text("1080 × 180")
    assert active_is_body(app)
    app.keyboard.press("Space")
    expect(app.locator("#play-btn")).to_have_attribute("aria-label", "Pause")
    app.keyboard.press("Space")
    expect(app.locator("#play-btn")).to_have_attribute("aria-label", "Play")


@pytest.mark.parametrize("target", ["scrub", "sensitivity"])
def test_space_plays_after_a_mouse_click_on_a_slider(app, target):
    slider = app.locator("#scrub") if target == "scrub" else app.get_by_role("slider", name="Sensitivity", exact=True)
    slider.click()
    expect(slider).to_be_focused()                            # a click keeps the focus on a slider
    app.keyboard.press("Space")
    expect(app.locator("#play-btn")).to_have_attribute("aria-label", "Pause")
    app.keyboard.press("Space")
    expect(app.locator("#play-btn")).to_have_attribute("aria-label", "Play")


@pytest.mark.parametrize("target", ["scrub", "sensitivity"])
def test_arrow_keys_nudge_a_slider_after_a_mouse_click(app, target):
    slider = app.locator("#scrub") if target == "scrub" else app.get_by_role("slider", name="Sensitivity", exact=True)
    slider.click()
    before = slider_value(slider)
    app.keyboard.press("ArrowRight")
    assert slider_value(slider) > before
    app.keyboard.press("ArrowLeft")
    app.keyboard.press("ArrowLeft")
    assert slider_value(slider) < before


def test_the_scrub_bar_says_where_it_is(app):
    scrub = app.locator("#scrub")
    expect(scrub).to_have_attribute("aria-valuetext", "00:00 of 00:20")
    scrub.fill("5000")
    expect(scrub).to_have_attribute("aria-valuetext", "00:10 of 00:20")


def test_the_scrub_bar_has_no_text_without_audio(page):
    mock_state(page)
    open_app(page)
    assert page.locator("#scrub").get_attribute("aria-valuetext") is None


def test_a_conflict_from_the_frame_route_stops_the_requests_until_the_state_changes(selected):
    page = selected
    requests = []

    def conflict(route):
        requests.append(route.request.url)
        route.fulfill(status=409, json={"error": "There is no analysed audio to preview."})
    page.route("**/api/frame", conflict)
    open_app(page)
    expect(page.locator("#status")).to_have_text("Ready to export")
    page.locator("#scrub").fill("5000")
    page.locator("#scrub").fill("2000")
    page.wait_for_timeout(1500)                                # more than the retry delay
    assert len(requests) == 1
    expect(page.locator("#status")).not_to_contain_text("Preview unavailable")
    allow_failed_loads(page, "/api/frame", 409)


def test_space_on_a_tab_focused_button_activates_that_button(app):
    opened = count_calls(app, "**/api/open", {"opened": False})
    app.locator("#change-btn").focus()
    app.keyboard.press("Space")
    expect(app.locator("#change-btn")).to_be_focused()
    assert len(opened) == 1
    expect(app.locator("#play-btn")).to_have_attribute("aria-label", "Play")


def test_space_in_the_hex_field_is_not_a_shortcut(app):
    field = app.get_by_label("Color A hex", exact=True)
    field.click()
    before = field.input_value()
    app.keyboard.press("Space")
    expect(app.locator("#play-btn")).to_have_attribute("aria-label", "Play")
    assert field.input_value().strip() == before
    assert app.evaluate("window.__csp.length") == 0


def test_the_open_shortcut_asks_for_a_file(app):
    opened = count_calls(app, "**/api/open", {"opened": False})
    app.evaluate(WATCH_KEYS)
    app.keyboard.press("ControlOrMeta+o")
    expect(app.locator("#status")).not_to_have_text("Loading")
    assert app.evaluate("window.__keys.filter(([k]) => k === 'o').map(([, d]) => d)") == [True]
    assert len(opened) == 1


def test_the_export_shortcut_is_taken_from_the_browser(app):
    app.evaluate(WATCH_KEYS)
    app.keyboard.press("ControlOrMeta+e")
    assert app.evaluate("window.__keys.filter(([k]) => k === 'e').map(([, d]) => d)") == [True]


def test_shortcuts_are_ignored_while_a_modal_is_open(app):
    opened = count_calls(app, "**/api/open", {"opened": False})
    app.evaluate("() => { const m = document.createElement('div'); m.id = 'fake-modal'; m.setAttribute('aria-modal', 'true'); document.body.append(m); }")
    app.keyboard.press("Space")
    app.keyboard.press("ControlOrMeta+o")
    expect(app.locator("#play-btn")).to_have_attribute("aria-label", "Play")
    assert opened == []
    app.evaluate("document.getElementById('fake-modal').remove()")
    app.keyboard.press("Space")
    expect(app.locator("#play-btn")).to_have_attribute("aria-label", "Pause")
