# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""The application shell in real browsers: states, layout that never overflows, hostile input."""

import shutil
import sys

import pytest

from .conftest import mock_state, open_app, select_audio, wait_for_frame

LONG_NAME = ("a long recording name " * 12)[:196] + ".wav"
LONG_ERROR = ("The file could not be analysed because something unexpected happened. " * 6)[:300]
LONG_OUTPUT = ("final render of the interview " * 8)[:196] + ".mov"
ARMED = {"audio": {"name": "interview.wav", "extension": "wav", "duration": 204.0, "has_spectrum": True},
         "analysis": {"state": "ready", "progress": 1.0, "error": None}, "step": 2, "audio_version": 1}

STATES = {
    "empty": {},
    "loaded": ARMED,
    "analysing": {"audio": ARMED["audio"] | {"duration": None}, "step": 1,
                  "analysis": {"state": "analyzing", "progress": 0.42, "error": None}},
    "rendering": {**ARMED, "step": 3, "export": {"state": "rendering", "current": 1234, "total": 6120}},
    "finalizing": {**ARMED, "step": 3, "export": {"state": "finalizing", "current": 6120, "total": 6120}},
    "failure": {"audio": None, "step": 0, "analysis": {"state": "failed", "progress": 0.0, "error": "x"},
                "error": "The audio could not be read."},
    "done": {**ARMED, "step": 3, "export": {"state": "done", "output_name": "interview.mov"}},
    "long-name": {**ARMED, "audio": ARMED["audio"] | {"name": LONG_NAME}},
    "long-error": {**ARMED, "error": LONG_ERROR},
    "long-progress": {**ARMED, "step": 3, "audio": ARMED["audio"] | {"name": LONG_NAME},
                      "export": {"state": "rendering", "current": 123456789012, "total": 987654321098}},
    "long-output": {**ARMED, "step": 3, "export": {"state": "done", "output_name": LONG_OUTPUT}},
}

OVERFLOW_JS = """() => {
  const root = document.documentElement, out = [];
  if (root.scrollWidth > root.clientWidth) out.push('page scrolls horizontally');
  if (root.scrollHeight > root.clientHeight) out.push('page scrolls vertically');
  for (const area of document.querySelectorAll('.app-head, .app-foot, .app-main')) {
    for (const node of area.querySelectorAll('*')) {
      const box = node.getBoundingClientRect(), parent = node.parentElement.getBoundingClientRect();
      if (box.width && (box.left < parent.left - 0.5 || box.right > parent.right + 0.5)) {
        out.push(node.className + ' leaves its parent: ' + box.left + '..' + box.right + ' in ' + parent.left + '..' + parent.right);
      }
    }
  }
  for (const [selector, height] of [['.app-head', 56], ['.app-foot', 56]]) {
    const box = document.querySelector(selector).getBoundingClientRect();
    if (box.height !== height) out.push(selector + ' is ' + box.height + ' px tall');
  }
  const stage = document.getElementById('stage').getBoundingClientRect();
  const transport = document.querySelector('.transport').getBoundingClientRect();
  if (transport.height !== 50) out.push('transport is ' + transport.height + ' px tall');
  if (stage.height < 150) out.push('stage collapsed to ' + stage.height);
  return out;
}"""


def assert_fits(page):
    assert page.evaluate(OVERFLOW_JS) == []


def load_state(page, name):
    mock_state(page, **STATES[name])
    open_app(page)
    if STATES[name].get("analysis", {}).get("state") == "ready":
        wait_for_frame(page)


def text(page, selector):
    return page.locator(selector).text_content()


def test_empty_state(page, shot):
    load_state(page, "empty")
    assert page.locator("#empty").is_visible()
    assert page.locator("#source").is_hidden()
    assert page.locator("#export-btn").is_disabled()
    assert page.locator("#play-btn").is_disabled() and page.locator("#scrub").is_disabled()
    assert page.locator(".step[aria-current='step']").get_attribute("data-step") == "0"
    assert page.locator(".sec h2").all_text_contents() == ["Signal", "Form", "Color"]
    assert "Drop an audio file" in text(page, ".empty-title")
    assert_fits(page)
    shot()


def test_loaded_state_with_a_real_frame(page, server, demo_wav, shot):
    select_audio(server, demo_wav)
    open_app(page)
    wait_for_frame(page)
    assert text(page, "#source-name") == "demo.wav"
    assert text(page, "#source-meta") == "00:20 · WAV"
    assert text(page, "#time-total") == "00:20"
    assert text(page, "#size-label") == "1080 × 180 · 30 FPS"
    states = page.eval_on_selector_all(".step", "nodes => nodes.map(n => n.dataset.state)")
    assert states == ["done", "done", "current", "todo"]
    assert page.locator("#empty").is_hidden() and page.locator("#export-btn").is_enabled()
    assert page.locator("#preview").evaluate("c => c.getBoundingClientRect().width <= c.parentElement.clientWidth")
    assert_fits(page)
    shot()


def test_analysing(page, shot):
    load_state(page, "analysing")
    assert text(page, "#status") == "Analyzing audio · 42%"
    assert page.locator("#progress.is-active").count() == 1
    assert page.evaluate("document.getElementById('progress-bar').style.width") == "42%"
    assert page.locator(".step[aria-current='step']").get_attribute("data-step") == "1"
    assert_fits(page)
    shot()


def test_rendering_shows_cancel_and_progress(page, shot):
    load_state(page, "rendering")
    assert text(page, "#status") == "Rendering 1,234 / 6,120 frames"
    assert page.locator("#cancel-btn").is_visible() and page.locator("#export-btn").is_hidden()
    assert page.locator("#formats button").first.is_disabled()
    assert page.evaluate("document.getElementById('progress-bar').style.width") == "20.2%"
    assert_fits(page)
    shot()


def test_finalizing(page):
    load_state(page, "finalizing")
    assert text(page, "#status") == "Finalizing file…"
    assert page.evaluate("document.getElementById('progress-bar').style.width") == "100%"
    assert page.locator("#cancel-btn").is_hidden()      # cancel_export refuses at this stage
    assert_fits(page)


def test_failure_is_shown_in_the_danger_colour(page, shot):
    load_state(page, "failure")
    assert text(page, "#status") == "The audio could not be read."
    assert page.locator("#status.is-error").count() == 1
    assert page.locator("#status").evaluate("n => getComputedStyle(n).color") == "rgb(240, 100, 126)"
    assert page.locator("#progress.is-active").count() == 0
    assert_fits(page)
    shot()


@pytest.mark.parametrize("name", ["done", "long-name", "long-error", "long-progress", "long-output"])
def test_longest_states_never_overflow_or_push_controls(page, name, shot):
    load_state(page, name)
    assert_fits(page)
    before = page.locator("#formats").bounding_box()
    full = page.locator("#status").text_content()
    assert page.locator("#status").get_attribute("title") == full
    if name == "long-name":
        assert page.locator("#source-name").get_attribute("title") == LONG_NAME
        assert page.locator("#change-btn").is_visible()
    # the controls on the right keep the room they need whatever the status says
    assert before["x"] + before["width"] <= page.viewport_size["width"]
    if name.startswith("long-"):
        shot(name)


def test_hostile_file_name_is_literal_text(page, server, demo_wav, tmp_path):
    name = "<img src=x onerror=alert(1)>.wav"
    hostile = tmp_path / name
    try:
        shutil.copy(demo_wav, hostile)
    except OSError:
        pytest.skip("this file system cannot hold the hostile name")
    select_audio(server, hostile)
    open_app(page)
    wait_for_frame(page)
    assert text(page, "#source-name") == name
    assert page.locator("#source-name").get_attribute("title") == name
    sources = page.eval_on_selector_all("img", "nodes => nodes.map(n => n.getAttribute('src'))")
    assert all(src.endswith("mark.svg") for src in sources)
    assert page.locator("#source-name *").count() == 0


def test_token_is_not_kept_anywhere(page, server, demo_wav):
    select_audio(server, demo_wav)
    open_app(page)
    wait_for_frame(page)
    assert "token" not in page.url and page.evaluate("location.search") == ""
    assert page.evaluate("[localStorage.length, sessionStorage.length]") == [0, 0]
    assert page.evaluate("document.cookie") == ""


def test_fonts_load(page):
    load_state(page, "empty")
    result = page.evaluate("""async () => {
      const faces = await Promise.all([
        document.fonts.load("600 14px 'Instrument Sans'"), document.fonts.load("13px 'JetBrains Mono'")]);
      return [faces.map(f => f.length), document.fonts.check("14px 'Instrument Sans'"),
              document.fonts.check("13px 'JetBrains Mono'")];
    }""")
    assert result == [[1, 1], True, True]


def test_keyboard_reaches_controls_with_a_visible_ring(page, browser, server, demo_wav):
    select_audio(server, demo_wav)
    open_app(page)
    wait_for_frame(page)
    reached = {}
    # Safari's own setting skips buttons on Tab; Option+Tab walks every control, as with Full Keyboard Access.
    tab = "Alt+Tab" if browser.browser_name == "webkit" and sys.platform == "darwin" else "Tab"
    for _ in range(40):                    # the inspector's controls sit between the transport and the footer
        page.keyboard.press(tab)
        info = page.evaluate("""() => {
          const n = document.activeElement, s = getComputedStyle(n);
          if (n === document.body) return null;
          return { id: n.id || n.textContent.trim(), ring: n.matches(':focus-visible') && s.outlineStyle !== 'none'
                   && parseFloat(s.outlineWidth) >= 2, color: s.outlineColor };
        }""")
        if info:
            reached[info["id"]] = info
    for wanted in ("about-btn", "change-btn", "play-btn", "scrub", "1080 × 180", "export-btn"):
        assert wanted in reached, f"Tab never reached {wanted}: {sorted(reached)}"
    assert all(info["ring"] for info in reached.values())
    assert {info["color"] for info in reached.values()} == {"rgb(167, 139, 250)"}


def test_drag_over_is_marked_and_cleared(page, shot):
    load_state(page, "empty")
    fire = """(type) => {
      const dt = new DataTransfer();
      dt.items.add(new File(['x'], 'a.wav'));
      document.dispatchEvent(new DragEvent(type, { dataTransfer: dt, bubbles: true, cancelable: true }));
    }"""
    page.evaluate(fire, "dragenter")
    assert page.locator("#stage.drag").count() == 1
    shot("drag")
    page.evaluate(fire, "dragleave")
    assert page.locator("#stage.drag").count() == 0
