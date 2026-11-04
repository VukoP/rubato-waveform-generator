# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""The flows in real browsers: export, cancel and failure against the real engine, the FFmpeg panel,
the About dialog, hostile text and the keyboard shortcut."""

import json
import re
import subprocess
import sys

import pytest

import rubato_waveform
from rubato_waveform import ffmpeg_tools

from .conftest import (
    ROOT,
    allow_failed_loads,
    api,
    get_json,
    mock_state,
    open_app,
    reset_look,
    select_audio,
    wait_for_frame,
)
from .test_shell import ARMED, LONG_ERROR, LONG_NAME, LONG_OUTPUT, assert_fits

expect = pytest.importorskip("playwright.sync_api").expect

HOSTILE = "<img src=x onerror=alert(1)>"
MISSING = {"ok": False, "has_prores": False, "problem": "FFmpeg was not found.", "install_command": "brew install ffmpeg",
           "install_alternative": None}
SHORTCUT = "Meta+E" if sys.platform == "darwin" else "Control+E"
REVEAL_LABEL = {"darwin": "Show in Finder", "win32": "Show in Explorer"}.get(sys.platform, "Show in folder")
RENDER_TIMEOUT_MS = 240000


@pytest.fixture(params=["1040x640"])
def size(request):
    return request.param


@pytest.fixture(autouse=True)
def fresh_settings(server):
    reset_look(server)


@pytest.fixture(scope="session")
def long_wav(tmp_path_factory):
    path = tmp_path_factory.mktemp("long") / "long.wav"
    subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "make_demo_audio.py"), "--out", str(path),
                    "--seconds", "240"], check=True, capture_output=True, timeout=300)
    return path


@pytest.fixture
def ready(page, server, demo_wav):
    """The app open on the analysed demo audio, with a frame painted."""
    select_audio(server, demo_wav)
    open_app(page)
    wait_for_frame(page)
    return page


def probe(path):
    exe = ffmpeg_tools.find_ffprobe()
    if exe is None:
        pytest.skip("ffprobe not found")
    out = subprocess.run([str(exe), "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                          "stream=codec_name,profile,pix_fmt,width,height,nb_read_frames", "-of", "json", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)["streams"][0]


def open_app_without_waiting(page):
    page.goto(page.server.url)


def preview_still_works(page):
    """A look change must still bring a new frame from the server."""
    with page.expect_response(lambda r: r.url.endswith("/api/frame")) as seen:
        page.locator('#form-body button[data-value="line"]').click()
    assert seen.value.ok
    expect(page.locator("#preview[data-frame]")).to_be_visible()


# ------------------------------------------------------------------ export

def test_real_export_through_the_ui(ready, server, tmp_path):
    out = tmp_path / "clip.mov"
    api(server, "/api/dev/next-output", {"path": str(out)})
    revealed = len(api(server, "/api/dev/reveals")["paths"])
    ready.locator("#export-btn").click()

    expect(ready.locator("#cancel-btn")).to_be_visible()                       # rendering was observed
    expect(ready.locator("#status")).to_have_text(re.compile(r"^Rendering [\d,]+ / 600 frames$"))
    expect(ready.locator("#progress.is-active")).to_have_count(1)
    expect(ready.locator("#export-btn")).to_be_hidden()

    expect(ready.locator("#status")).to_have_text("Saved clip.mov", timeout=RENDER_TIMEOUT_MS)
    expect(ready.locator("#export-btn")).to_be_visible()
    expect(ready.locator("#export-btn")).to_be_enabled()
    expect(ready.locator("#cancel-btn")).to_be_hidden()
    stream = probe(out)
    assert stream["codec_name"] == "prores" and "4444" in stream["profile"]
    assert stream["pix_fmt"].startswith("yuva444p")
    assert (int(stream["width"]), int(stream["height"]), int(stream["nb_read_frames"])) == (1080, 180, 600)

    reveal = ready.locator("#reveal-btn")
    expect(reveal).to_have_text(REVEAL_LABEL)
    reveal.click()
    ready.locator("#status").click()
    assert api(server, "/api/dev/reveals")["paths"][revealed:] == [str(out)]


def test_cancel_mid_render_leaves_no_trace(page, server, long_wav, tmp_path):
    ready = page
    select_audio(server, long_wav)
    open_app(ready)
    wait_for_frame(ready)
    out = tmp_path / "keep.mov"
    out.write_bytes(b"an earlier export, not a real movie")
    before = out.read_bytes()
    api(server, "/api/dev/next-output", {"path": str(out)})

    ready.locator("#export-btn").click()
    expect(ready.locator("#cancel-btn")).to_be_visible()
    expect(ready.locator("#status")).to_have_text(re.compile(r"^Rendering [\d,]+ / 7,200 frames$"))
    assert get_json(server, "/api/state")["export"]["state"] == "rendering"
    ready.locator("#cancel-btn").click()

    expect(ready.locator("#status")).to_have_text("Export cancelled")
    expect(ready.locator("#export-btn")).to_be_enabled()
    assert get_json(server, "/api/state")["export"]["state"] == "cancelled"
    assert list(tmp_path.glob("*.part-*")) == []
    assert out.read_bytes() == before
    assert get_json(server, "/api/state")["analysis"]["state"] == "ready"
    preview_still_works(ready)


def test_real_failure_keeps_everything_else_working(ready, server, tmp_path):
    blocker = tmp_path / "plain-file"
    blocker.write_text("not a folder")
    api(server, "/api/dev/next-output", {"path": str(blocker / "clip.mov")})
    ready.locator("#export-btn").click()

    status = ready.locator("#status")
    expect(status.and_(ready.locator(".is-error"))).to_be_visible()
    assert str(tmp_path) not in (status.text_content() or "")
    assert status.evaluate("n => getComputedStyle(n).color") == "rgb(240, 100, 126)"
    assert get_json(server, "/api/state")["export"]["state"] == "failed"
    assert get_json(server, "/api/state")["analysis"]["state"] == "ready"
    expect(ready.locator("#export-btn")).to_be_visible()
    expect(ready.locator("#export-btn")).to_be_enabled()
    preview_still_works(ready)


def test_api_refusal_shows_in_the_status_line(ready, server, tmp_path):
    (tmp_path / "taken.mov").write_bytes(b"x")
    api(server, "/api/dev/next-output", {"path": str(tmp_path / "taken")})
    ready.locator("#export-btn").click()
    expect(ready.locator("#status")).to_contain_text("taken.mov already exists")
    expect(ready.locator("#status.is-error")).to_have_count(1)
    expect(ready.locator("#export-btn")).to_be_enabled()
    allow_failed_loads(ready, "/api/export", 400)


def test_cancelled_dialog_changes_nothing(ready, server):
    expect(ready.locator("#status")).to_have_text("Ready to export")
    with ready.expect_response(lambda r: r.url.endswith("/api/export")) as seen:
        ready.locator("#export-btn").click()
    assert seen.value.json() == {"started": False}
    expect(ready.locator("#export-btn")).to_be_enabled()
    expect(ready.locator("#status")).to_have_text("Ready to export")
    expect(ready.locator("#status.is-error")).to_have_count(0)
    assert get_json(server, "/api/state")["export"]["state"] == "idle"


def test_finalizing_has_no_cancel(page):
    mock_state(page, **{**ARMED, "step": 3, "export": {"state": "finalizing", "current": 600, "total": 600}})
    open_app(page)
    expect(page.locator("#status")).to_have_text("Finalizing file…")
    expect(page.locator("#cancel-btn")).to_be_hidden()
    expect(page.locator("#export-btn")).to_be_disabled()
    expect(page.locator("#progress.is-quiet")).to_have_count(1)


@pytest.mark.parametrize("patch", [
    {},
    {"audio": ARMED["audio"] | {"duration": None}, "step": 1,
     "analysis": {"state": "analyzing", "progress": 0.3, "error": None}},
], ids=["no-audio", "analysing"])
def test_export_is_off_without_analysis_and_while_analysing(page, patch):
    mock_state(page, **patch)
    open_app(page)
    expect(page.locator("#export-btn")).to_be_disabled()


# ------------------------------------------------------------------ FFmpeg

def stub_clipboard(page, deny):
    page.add_init_script(f"""
      window.__copied = null;
      Object.defineProperty(navigator, 'clipboard', {{ configurable: true, value: {{
        writeText: (text) => {'Promise.reject(new DOMException("denied", "NotAllowedError"))' if deny
                              else '(window.__copied = text, Promise.resolve())'} }} }});
    """)


def test_ffmpeg_panel_shows_the_hint_and_copy_works(page):
    stub_clipboard(page, deny=False)
    mock_state(page, ffmpeg=MISSING)
    open_app(page)
    expect(page.locator("#ffmpeg-panel")).to_be_visible()
    expect(page.locator("#drop-prompt")).to_be_hidden()
    expect(page.locator("#ffmpeg-panel h2")).to_have_text("FFmpeg is required")
    expect(page.locator("#ffmpeg-cmd")).to_have_text("brew install ffmpeg")
    expect(page.locator("#ffmpeg-text")).to_have_text("FFmpeg was not found.")
    page.locator("#ffmpeg-copy").click()
    expect(page.locator("#ffmpeg-copy")).to_have_text("Copied")
    assert page.evaluate("window.__copied") == "brew install ffmpeg"
    expect(page.locator("#ffmpeg-copy")).to_have_text("Copy")           # and back again


def test_linux_copies_the_apt_command_alone_and_shows_the_other_distribution_apart(page):
    stub_clipboard(page, deny=False)
    mock_state(page, ffmpeg={**MISSING, "install_command": ffmpeg_tools.install_command("linux"),
                             "install_alternative": ffmpeg_tools.install_alternative("linux")})
    open_app(page)
    expect(page.locator("#ffmpeg-cmd")).to_have_text("sudo apt install ffmpeg")
    expect(page.locator("#ffmpeg-alt")).to_have_text("Fedora: enable RPM Fusion, then sudo dnf install ffmpeg")
    page.locator("#ffmpeg-copy").click()
    expect(page.locator("#ffmpeg-copy")).to_have_text("Copied")
    assert page.evaluate("window.__copied") == "sudo apt install ffmpeg"


def test_the_footer_says_ffmpeg_is_required_instead_of_asking_for_a_file(page):
    mock_state(page, ffmpeg=MISSING)
    open_app(page)
    expect(page.locator("#status")).to_have_text("FFmpeg is required")


def test_copy_falls_back_to_selecting_when_the_clipboard_is_denied(page):
    stub_clipboard(page, deny=True)
    mock_state(page, ffmpeg=MISSING)
    open_app(page)
    page.locator("#ffmpeg-copy").click()
    expect(page.locator("#ffmpeg-copy")).to_have_text(re.compile(r"^(Copied|Selected)$"))
    assert page.evaluate("window.getSelection().toString()") == "brew install ffmpeg"


def test_missing_prores_shows_the_problem_text(page):
    problem = "This FFmpeg build has no prores_ks (ProRes) encoder."
    mock_state(page, ffmpeg={**MISSING, "problem": problem})
    open_app(page)
    expect(page.locator("#ffmpeg-text")).to_have_text(problem)


def test_check_again_clears_the_panel_when_found(page):
    state = mock_state(page, ffmpeg=MISSING)
    answers = []

    def recheck(route):
        found = {"ok": bool(answers), "has_prores": bool(answers), "problem": None if answers else "FFmpeg was not found.",
                 "install_command": None, "install_alternative": None}
        answers.append(found)
        if found["ok"]:
            state["ffmpeg"] = found
        route.fulfill(status=200, json=found)
    page.route("**/api/ffmpeg/recheck", recheck)
    open_app(page)
    page.locator("#ffmpeg-recheck").click()
    expect(page.locator("#ffmpeg-note")).to_have_text("FFmpeg was not found yet.")
    expect(page.locator("#ffmpeg-panel")).to_be_visible()
    page.locator("#ffmpeg-recheck").click()
    expect(page.locator("#ffmpeg-panel")).to_be_hidden()
    expect(page.locator("#drop-prompt")).to_be_visible()


def test_about_stays_available_while_ffmpeg_is_missing(page):
    mock_state(page, ffmpeg=MISSING)
    open_app(page)
    page.locator("#about-btn").click()
    expect(page.locator("#about")).to_be_visible()


# ------------------------------------------------------------------- About

def test_about_content_and_escape(page):
    open_app(page)
    page.locator("#about-btn").click()
    about = page.locator("#about")
    expect(about).to_be_visible()
    expect(page.locator("#about-version")).to_have_text("Version " + rubato_waveform.__version__)
    expect(about).to_contain_text("Rubato Waveform Generator")
    expect(about).to_contain_text("Copyright 2026 VukoP")
    expect(about).to_contain_text("Apache License 2.0")
    expect(about).to_contain_text("name and logo are not licensed")
    expect(about).to_contain_text("SIL Open Font License")
    expect(about).to_contain_text("FFmpeg is a separate install")
    assert page.evaluate("document.querySelector('dialog').matches(':modal')")
    page.keyboard.press("Escape")
    expect(about).to_be_hidden()


@pytest.mark.parametrize("close", ["button", "escape", "backdrop"])
def test_focus_returns_to_the_about_button_whichever_way_it_closes(page, close):
    open_app(page)
    page.locator("#about-btn").click()
    expect(page.locator("#about")).to_be_visible()
    if close == "button":
        page.locator("#about-close").click()                      # with the mouse
    elif close == "escape":
        page.keyboard.press("Escape")
    else:
        page.mouse.click(5, 5)
    expect(page.locator("#about")).to_be_hidden()
    expect(page.locator("#about-btn")).to_be_focused()


def test_about_link_opens_outside_the_app_window(page):
    open_app(page)
    link = page.locator("#about-link")
    assert link.get_attribute("href") == "https://github.com/VukoP/rubato-waveform-generator"
    assert link.get_attribute("target") == "_blank"
    assert set(link.get_attribute("rel").split()) == {"noopener", "noreferrer"}


def test_space_does_not_play_while_about_is_open(ready):
    ready.evaluate("""() => {
      window.__keys = [];
      document.addEventListener('keydown', (e) => window.__keys.push([e.code, e.defaultPrevented]));
    }""")
    ready.locator("#about-btn").click()
    expect(ready.locator("#about")).to_be_visible()
    ready.keyboard.press("Space")
    assert ready.evaluate("window.__keys") == [["Space", False]]       # the app did not claim the key
    expect(ready.locator("#stage.playing")).to_have_count(0)
    assert ready.evaluate("document.querySelector('audio').paused")


# ------------------------------------------------------- stale messages and a11y

def test_a_running_export_is_never_hidden_by_an_old_refusal(page):
    state = mock_state(page, **{**ARMED, "error": "This file type is not supported."})
    open_app(page)
    expect(page.locator("#status")).to_have_text("This file type is not supported.")
    state["export"] = {"state": "rendering", "current": 10, "total": 600, "error": None, "output_name": None,
                       "can_reveal": False}
    state["step"] = 3
    expect(page.locator("#status")).to_have_text("Rendering 10 / 600 frames")
    expect(page.locator("#status.is-error")).to_have_count(0)
    state["export"] = {**state["export"], "state": "finalizing"}
    expect(page.locator("#status")).to_have_text("Finalizing file…")
    state["export"] = {**state["export"], "state": "done", "output_name": "clip.mov"}
    state["error"] = None                                           # the server cleared it when the export ended
    expect(page.locator("#status")).to_have_text("Saved clip.mov")


def test_a_refused_input_goes_away_when_the_server_clears_it(page):
    state = mock_state(page, **{**ARMED, "error": "This file type is not supported."})
    open_app(page)
    expect(page.locator("#status.is-error")).to_have_count(1)
    state["error"] = None
    expect(page.locator("#status")).to_have_text("Ready to export")


def test_an_export_refusal_expires_and_a_new_file_clears_it(ready, server, tmp_path):
    (tmp_path / "taken.mov").write_bytes(b"x")
    api(server, "/api/dev/next-output", {"path": str(tmp_path / "taken")})
    ready.clock.install()
    ready.locator("#export-btn").click()
    expect(ready.locator("#status")).to_contain_text("taken.mov already exists")
    ready.clock.run_for(9000)                                        # the timeout is about eight seconds
    expect(ready.locator("#status")).to_have_text("Ready to export")
    allow_failed_loads(ready, "/api/export", 400)


def test_a_new_audio_version_clears_an_export_refusal(page, server, demo_wav, tmp_path):
    select_audio(server, demo_wav)
    open_app(page)
    wait_for_frame(page)
    (tmp_path / "taken.mov").write_bytes(b"x")
    api(server, "/api/dev/next-output", {"path": str(tmp_path / "taken")})
    page.locator("#export-btn").click()
    expect(page.locator("#status")).to_contain_text("taken.mov already exists")
    other = tmp_path / "other.wav"
    other.write_bytes(demo_wav.read_bytes())
    select_audio(server, other)
    expect(page.locator("#status")).not_to_contain_text("already exists", timeout=5000)
    allow_failed_loads(page, "/api/export", 400)
    allow_failed_loads(page, "/api/frame", 409)


def test_progress_announcements_are_state_changes_only(page):
    state = mock_state(page, **{**ARMED, "step": 3, "export": {"state": "rendering", "current": 10, "total": 600}})
    open_app(page)
    live = page.locator("[role=status]")
    expect(live).to_have_count(2)                                    # the announcer and the FFmpeg note
    expect(page.locator("#announce")).to_have_text("Rendering started")
    expect(page.locator("#status")).not_to_have_attribute("role", "status")
    bar = page.locator("#progress")
    assert bar.get_attribute("role") == "progressbar"
    expect(bar).to_have_attribute("aria-valuenow", "2")
    state["export"] = {"state": "rendering", "current": 300, "total": 600}
    expect(page.locator("#status")).to_have_text("Rendering 300 / 600 frames")
    expect(bar).to_have_attribute("aria-valuenow", "50")
    expect(page.locator("#announce")).to_have_text("Rendering started")          # unchanged: nothing to read out
    state["export"] = {"state": "done", "output_name": "clip.mov", "can_reveal": False}
    expect(page.locator("#announce")).to_have_text("Saved clip.mov")


def test_completed_steps_say_so(page):
    mock_state(page, **{**ARMED, "step": 3, "export": {"state": "done", "output_name": "clip.mov"}})
    open_app(page)
    read = """nodes => nodes.map(n => n.querySelector('.step-label').textContent + ' ' + n.querySelector('.step-done').textContent)"""
    assert page.eval_on_selector_all(".step", read) == ["Audio completed", "Analysis completed", "Style completed",
                                                         "Export completed"]
    mock_state(page)
    open_app(page)
    assert "completed" not in page.locator("#steps").text_content()


def test_the_pulse_is_off_when_motion_is_reduced(page):
    page.emulate_media(reduced_motion="reduce")
    mock_state(page, **{**ARMED, "step": 3, "export": {"state": "finalizing", "current": 600, "total": 600}})
    open_app(page)
    expect(page.locator("#progress.is-quiet")).to_have_count(1)
    assert page.locator("#progress-bar").evaluate("n => getComputedStyle(n).animationName") == "none"


def test_error_text_can_be_selected_and_disabled_controls_stay_readable(page):
    mock_state(page, **{**ARMED, "error": "Something went wrong."})
    open_app(page)
    assert page.locator("#status").evaluate("n => getComputedStyle(n).userSelect") == "text"
    mock_state(page)
    open_app(page)
    expect(page.locator("#export-btn")).to_have_css("opacity", "0.55")
    expect(page.locator("#formats button").first).to_have_css("opacity", "1")
    mock_state(page, **{**ARMED, "step": 3, "export": {"state": "finalizing", "current": 6, "total": 6}})
    open_app(page)
    expect(page.locator("#formats button").first).to_have_css("opacity", "0.55")


# ----------------------------------------------------------------- connection

def test_a_lost_connection_switches_off_export_and_choose_file(page):
    mock_state(page, **ARMED)
    open_app(page)
    expect(page.locator("#export-btn")).to_be_enabled()
    page.route("**/api/state", lambda route: route.abort())
    expect(page.locator("#status")).to_have_text("Cannot reach the app")
    expect(page.locator("#status")).to_have_text("Lost connection to the app. Please restart it.", timeout=10000)
    expect(page.locator("#status.is-error")).to_have_count(1)
    expect(page.locator("#export-btn")).to_be_disabled()
    expect(page.locator("#change-btn")).to_be_disabled()
    allow_failed_loads(page, "/api/state", None)


def test_a_page_without_its_token_ends_up_in_the_same_state(page):
    page.goto(page.server.url.split("?")[0])                         # a reload: the token is gone on purpose
    expect(page.locator("#status")).to_have_text("Lost connection to the app. Please restart it.", timeout=10000)
    expect(page.locator("#choose-btn")).to_be_disabled()
    allow_failed_loads(page, "/api/catalog", 401)
    allow_failed_loads(page, "/api/settings", 401)
    allow_failed_loads(page, "/api/state", 401)


def test_a_bug_in_the_page_is_a_console_error_not_an_outage(page):
    mock_state(page, **{**ARMED, "analysis": None})                  # a state the page cannot draw
    open_app_without_waiting(page)
    page.wait_for_timeout(1500)
    assert any("Could not draw the app state" in p for p in page.problems), page.problems
    assert page.locator("#status").text_content() != "Cannot reach the app"
    page.problems.clear()


# ----------------------------------------------------------- hostile text

def no_markup_from(page):
    assert page.locator("img[src='x']").count() == 0
    assert page.locator("[onerror]").count() == 0


def test_hostile_file_name_is_text_everywhere(page, server, demo_wav, tmp_path):
    source = tmp_path / (HOSTILE + ".wav")
    source.write_bytes(demo_wav.read_bytes())
    select_audio(server, source)
    open_app(page)
    wait_for_frame(page)
    expect(page.locator("#source-name")).to_have_text(HOSTILE + ".wav")
    assert page.locator("#source-name").get_attribute("title") == HOSTILE + ".wav"

    (tmp_path / (HOSTILE + ".mov")).write_bytes(b"x")            # the refusal names the file
    api(server, "/api/dev/next-output", {"path": str(tmp_path / HOSTILE)})
    page.locator("#export-btn").click()
    expect(page.locator("#status")).to_have_text(HOSTILE + ".mov already exists. Choose another name, "
                                                 "or end the name with .mov to replace it.")
    allow_failed_loads(page, "/api/export", 400)
    no_markup_from(page)

    api(server, "/api/dev/next-output", {"path": str(tmp_path / (HOSTILE + "-new.mov"))})
    page.locator("#export-btn").click()
    expect(page.locator("#status")).to_have_text("Saved " + HOSTILE + "-new.mov", timeout=RENDER_TIMEOUT_MS)
    no_markup_from(page)


def test_hostile_text_in_a_server_error_is_text(page):
    mock_state(page, **{**ARMED, "error": HOSTILE, "audio": ARMED["audio"] | {"name": HOSTILE}})
    open_app(page)
    expect(page.locator("#status")).to_have_text(HOSTILE)
    expect(page.locator("#source-name")).to_have_text(HOSTILE)
    no_markup_from(page)


def test_hostile_text_in_the_ffmpeg_panel_is_text(page):
    mock_state(page, ffmpeg={**MISSING, "problem": HOSTILE, "install_command": HOSTILE, "install_alternative": HOSTILE})
    open_app(page)
    expect(page.locator("#ffmpeg-text")).to_have_text(HOSTILE)
    expect(page.locator("#ffmpeg-cmd")).to_have_text(HOSTILE)
    expect(page.locator("#ffmpeg-alt")).to_have_text(HOSTILE)
    no_markup_from(page)


# ---------------------------------------------------------------- shortcut

def count_exports(page):
    seen = []
    page.on("request", lambda r: seen.append(r.url) if r.url.endswith("/api/export") else None)
    return seen


def test_shortcut_starts_an_export(ready):
    seen = count_exports(ready)
    with ready.expect_response(lambda r: r.url.endswith("/api/export")):
        ready.keyboard.press(SHORTCUT)
    assert len(seen) == 1


def test_shortcut_is_ignored_in_a_text_field(ready):
    seen = count_exports(ready)
    ready.locator(".hex").first.click()
    ready.keyboard.press(SHORTCUT)
    ready.locator("#status").click()
    with ready.expect_response(lambda r: r.url.endswith("/api/export")):
        ready.keyboard.press(SHORTCUT)
    assert len(seen) == 1                       # only the second press, outside the field, got through


def test_shortcut_is_ignored_while_a_modal_is_open(ready):
    seen = count_exports(ready)
    ready.locator("#about-btn").click()
    expect(ready.locator("#about")).to_be_visible()
    ready.keyboard.press(SHORTCUT)
    ready.keyboard.press("Escape")
    expect(ready.locator("#about")).to_be_hidden()
    with ready.expect_response(lambda r: r.url.endswith("/api/export")):
        ready.keyboard.press(SHORTCUT)
    assert len(seen) == 1


# ------------------------------------------------------------------ layout

@pytest.mark.parametrize("name", ["export-done", "error", "name", "ffmpeg", "ffmpeg-linux"])
def test_longest_strings_do_not_overflow(page, name):
    ffmpeg_long = {**MISSING, "problem": ("This FFmpeg build has no prores_ks encoder. " * 8)[:200]}
    linux = ffmpeg_long | {"install_command": ffmpeg_tools.install_command("linux"),
                           "install_alternative": ffmpeg_tools.install_alternative("linux")}
    states = {
        "export-done": {**ARMED, "step": 3, "audio": ARMED["audio"] | {"name": HOSTILE + LONG_NAME},
                        "export": {"state": "done", "output_name": LONG_OUTPUT, "can_reveal": True}},
        "error": {**ARMED, "error": LONG_ERROR},
        "name": {**ARMED, "audio": ARMED["audio"] | {"name": LONG_NAME}},
        "ffmpeg": {"ffmpeg": ffmpeg_long},
        "ffmpeg-linux": {"ffmpeg": linux},
    }
    mock_state(page, **states[name])
    open_app(page)
    if name == "export-done":
        expect(page.locator("#reveal-btn")).to_be_visible()
    if name.startswith("ffmpeg"):
        expect(page.locator("#ffmpeg-panel")).to_be_visible()
    assert_fits(page)


def test_about_fits_the_smallest_window(page):
    open_app(page)
    page.locator("#about-btn").click()
    box = page.locator("#about").bounding_box()
    assert box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= 1040 and box["y"] + box["height"] <= 640
    assert page.locator("#about").evaluate("n => n.scrollHeight <= n.clientHeight && n.scrollWidth <= n.clientWidth")
