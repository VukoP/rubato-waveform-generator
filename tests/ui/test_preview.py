# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""The live preview in real browsers: one request in flight, what reaches the canvas, and the fit
of the canvas (and its guides) to the stage."""

import time

import pytest

from .conftest import (
    SHOTS,
    allow_failed_loads,
    mock_state,
    open_app,
    reset_look,
    select_audio,
    wait_for_frame,
)

expect = pytest.importorskip("playwright.sync_api").expect

# Counts the /api/frame requests that are on their way at once.
COUNT_FRAMES = """
window.__frames = { now: 0, max: 0 };
const realFetch = window.fetch;
window.fetch = async (...args) => {
  const counted = String(args[0]).includes('/api/frame');
  if (counted) { window.__frames.now += 1; window.__frames.max = Math.max(window.__frames.max, window.__frames.now); }
  try { return await realFetch(...args); } finally { if (counted) window.__frames.now -= 1; }
};
"""

DRIVE = """async (steps) => {
  const scrub = document.getElementById('scrub');
  for (let i = 1; i <= steps; i++) {
    scrub.value = String(i * 200);
    scrub.dispatchEvent(new Event('input', { bubbles: true }));
    if (i % 2) await new Promise((resolve) => requestAnimationFrame(resolve));
  }
}"""


@pytest.fixture(autouse=True)
def fresh_settings(server):
    reset_look(server)


@pytest.fixture
def app(page, server, demo_wav):
    select_audio(server, demo_wav)
    return page


def blank_frame(width, height):
    def serve(route):
        route.fulfill(status=200, body=bytes(width * height * 4),
                      headers={"Content-Type": "application/octet-stream", "X-W": str(width), "X-H": str(height)})
    return serve


def settle(page):
    """Waits for the resize observer and the layout that follows it: two animation frames."""
    page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")


# ----------------------------------------------------------- the request loop

def test_one_request_at_a_time_and_the_last_time_wins(app):
    bodies = []

    def slow(route):
        bodies.append(route.request.post_data_json)
        time.sleep(0.03)                         # the latency of a slow machine; nothing waits on it
        blank_frame(120, 20)(route)

    app.add_init_script(COUNT_FRAMES)
    app.route("**/api/frame", slow)
    open_app(app)
    wait_for_frame(app)
    before = len(bodies)
    app.evaluate(DRIVE, 40)
    expect(app.locator("#preview")).to_have_attribute("data-t", "16")        # 40 * 200 / 10000 of 20 s
    assert bodies[-1]["t"] == 16
    assert 1 < len(bodies) - before < 40, "intermediate times should be dropped"
    assert app.evaluate("window.__frames.max") == 1
    assert app.evaluate("window.__frames.now") == 0


def test_a_failed_frame_is_reported_once_and_does_not_loop(app):
    attempts = []

    def broken(route):
        attempts.append(route.request.post_data_json["t"])
        route.fulfill(status=500, json={"error": "The preview broke."})

    app.route("**/api/frame", broken)
    open_app(app)
    expect(app.locator("#status")).to_have_text("Preview unavailable: The preview broke.")
    app.evaluate(DRIVE, 10)
    settle(app)
    assert len(attempts) <= 2, "a failing server must not be asked again at once"
    allow_failed_loads(app, "/api/frame", 500)


def test_no_analysis_means_no_frame(page):
    requests = []
    mock_state(page)
    page.on("request", lambda r: r.url.endswith("/api/frame") and requests.append(r))
    open_app(page)
    assert page.locator("#preview").is_hidden()
    assert page.evaluate("document.getElementById('preview').dataset.frame") is None
    assert requests == []


# -------------------------------------------------------------- what is drawn

def test_corners_of_a_real_frame_are_transparent(app):
    open_app(app)
    wait_for_frame(app)
    alphas = app.evaluate("""() => {
      const c = document.getElementById('preview'), g = c.getContext('2d');
      return [[0, 0], [c.width - 1, 0], [0, c.height - 1], [c.width - 1, c.height - 1]]
        .map(([x, y]) => g.getImageData(x, y, 1, 1).data[3]);
    }""")
    assert alphas == [0, 0, 0, 0]


def test_a_semi_transparent_pixel_is_not_premultiplied_twice(app):
    pixels = [(200, 100, 50, 128), (255, 0, 0, 255), (0, 0, 0, 0), (10, 200, 30, 64)]
    body = b"".join(bytes(p) for p in pixels)
    app.route("**/api/frame", lambda route: route.fulfill(
        status=200, body=body, headers={"Content-Type": "application/octet-stream", "X-W": "4", "X-H": "1"}))
    open_app(app)
    expect(app.locator("#preview")).to_have_attribute("data-frame", "4x1")
    read, over_white = app.evaluate("""() => {
      const c = document.getElementById('preview');
      const own = [...c.getContext('2d').getImageData(0, 0, 4, 1).data];
      const flat = document.createElement('canvas');
      flat.width = 4; flat.height = 1;
      const g = flat.getContext('2d');
      g.fillStyle = '#fff'; g.fillRect(0, 0, 4, 1); g.drawImage(c, 0, 0);
      return [own, [...g.getImageData(0, 0, 4, 1).data]];
    }""")
    for i, (r, g, b, a) in enumerate(pixels):
        got = read[4 * i:4 * i + 4]
        slack = 2 + (255 // a if a else 0)             # premultiplied storage loses a little at low alpha
        assert got[3] == a and all(abs(x - y) <= slack for x, y in zip(got[:3], (r, g, b), strict=True)), (i, got)
        white = [round(c * a / 255 + 255 * (1 - a / 255)) for c in (r, g, b)]
        assert all(abs(x - y) <= 2 for x, y in zip(over_white[4 * i:4 * i + 3], white, strict=True)), (i, over_white)


# ------------------------------------------------------------------------ fit

SHAPES = [(1080, 180), (3840, 360), (1080, 1080)]

RECTS = """() => {
  const box = (n) => { const r = n.getBoundingClientRect(); return { l: r.left, t: r.top, w: r.width, h: r.height }; };
  const stage = document.getElementById('stage');
  return { stage: box(stage), canvas: box(document.getElementById('preview')),
           room: [stage.clientWidth - 32, stage.clientHeight - 32] };
}"""


@pytest.mark.parametrize(("width", "height"), SHAPES)
def test_the_canvas_fits_the_stage(app, width, height):
    bodies = []

    def serve(route):
        bodies.append(route.request.post_data_json)
        blank_frame(width, height)(route)

    app.route("**/api/frame", serve)
    open_app(app)
    expect(app.locator("#preview")).to_have_attribute("data-frame", f"{width}x{height}")
    rects = app.evaluate(RECTS)
    stage, canvas, (room_w, room_h) = rects["stage"], rects["canvas"], rects["room"]
    assert abs(canvas["w"] / canvas["h"] - width / height) / (width / height) < 0.02
    # inside the stage, one margin away from its inner edge (the 1 px border is not part of it)
    assert canvas["l"] >= stage["l"] + 1 + 15.5 and canvas["t"] >= stage["t"] + 1 + 15.5
    assert canvas["l"] + canvas["w"] <= stage["l"] + stage["w"] - 1 - 15.5
    assert canvas["t"] + canvas["h"] <= stage["t"] + stage["h"] - 1 - 15.5
    assert max(canvas["w"] / room_w, canvas["h"] / room_h) >= 0.98 and canvas["w"] <= room_w + 1 and canvas["h"] <= room_h + 1
    assert (bodies[-1]["max_w"], bodies[-1]["max_h"]) == (int(room_w), int(room_h))   # dpr 1 in these contexts


def test_a_small_frame_is_scaled_up_to_the_stage(app):
    app.route("**/api/frame", blank_frame(120, 20))
    open_app(app)
    expect(app.locator("#preview")).to_have_attribute("data-frame", "120x20")
    rects = app.evaluate(RECTS)
    assert rects["canvas"]["w"] > 400 and abs(rects["canvas"]["w"] / rects["canvas"]["h"] - 6) < 0.15


def test_guides_follow_the_drawn_rectangle_after_a_resize(app):
    app.route("**/api/frame", blank_frame(1080, 180))
    open_app(app)
    wait_for_frame(app)
    for width, height in ((1500, 900), (1040, 640), (1920, 1200)):
        app.set_viewport_size({"width": width, "height": height})
        settle(app)
        guides = app.evaluate("""() => {
          const box = (id) => { const r = document.getElementById(id).getBoundingClientRect();
                                return { l: r.left, t: r.top, w: r.width, h: r.height }; };
          return { canvas: box('preview'), playhead: box('playhead'), baseline: box('baseline') };
        }""")
        c, p, b = guides["canvas"], guides["playhead"], guides["baseline"]
        assert abs(p["l"] - (c["l"] + c["w"] / 2)) <= 1 and abs(p["t"] - (c["t"] + 0.05 * c["h"])) <= 1.5
        assert abs(p["h"] - 0.9 * c["h"]) <= 1.5
        assert abs(b["l"] - c["l"]) <= 1 and abs(b["w"] - c["w"]) <= 1 and abs(b["t"] - (c["t"] + 0.5 * c["h"])) <= 1.5


# ---------------------------------------------------------------- screenshots

@pytest.mark.parametrize("layout", ["default", "Orbit"])
def test_fitted_preview_screenshot(app, shot, layout):
    open_app(app)
    wait_for_frame(app)
    if layout != "default":
        app.get_by_role("radiogroup", name="Layout", exact=True).get_by_role("radio", name=layout, exact=True).click()
        expect(app.locator("#size-label")).to_contain_text("1080 × 1080")
    app.locator("#scrub").fill("3000")
    expect(app.locator("#preview")).to_have_attribute("data-t", "6")
    settle(app)
    SHOTS.mkdir(parents=True, exist_ok=True)
    shot(layout.lower())
