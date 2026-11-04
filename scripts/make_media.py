# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Generate the README and website media (docs/media/) from synthetic audio only.

Run from anywhere: ``python scripts/make_media.py``. Needs Pillow, numpy and FFmpeg; the two
screenshots also need Playwright with WebKit. The animated WebP files are byte-identical between
runs; the PNG screenshots may differ slightly, because the browser renders them.

Engine frames are premultiplied RGBA, so every composite here is ``rgb + (1 - a) * background``.
"""

from __future__ import annotations

import io
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rubato_waveform import catalog, engine

MEDIA = ROOT / "docs" / "media"
APP_BG = (17, 17, 20)
FPS = 20

# Hero: 7 s of the demo audio that cross from speech into music.
HERO_SIZE = (1280, 640)
HERO_START = 8.0
HERO_SECONDS = 7.0
HERO_BAR = (1120, 180)
HERO_BAR_POS = (80, 428)

# Layouts: 3 s each.
LAYOUT_SECONDS = 3.0

# key -> (colour, colour2); Scroll and Flow share colours, their gradient modes tell them apart.
LAYOUT_COLORS = {
    "scroll": ("#22D3EE", "#E040A0"),
    "mirror": ("#8B4DFF", "#D55ECF"),
    "pulse": ("#FF7A18", "#FFE29A"),
    "flow": ("#22D3EE", "#E040A0"),
    "ripple": ("#3B82F6", "#99F6E4"),
    "neon": ("#B4F04A", "#22D3EE"),
    "mirror-reflect": ("#A78BFA", "#F0ABFC"),
    "peakhold": ("#FF3B5C", "#FFB84D"),
    "radial": ("#2DD4BF", "#A78BFA"),
    "voiceline": ("#FF5C8A", "#FFB3C6"),
}


# Per layout: (start second in the demo audio, sensitivity), chosen so the shape varies and
# nothing sits at the maximum. Speech-like audio runs to 11 s, music-like follows; the three
# spectrum-driven layouts use stretches of speech where the voice is continuous.
LAYOUT_SECTION = {
    "scroll": (12.0, 1.0), "mirror": (12.0, 1.0), "pulse": (12.0, 2.2), "flow": (12.0, 1.0),
    "ripple": (12.0, 2.0), "neon": (1.4, 1.0), "mirror-reflect": (2.15, 1.1),
    "peakhold": (4.5, 1.0), "radial": (12.0, 1.5), "voiceline": (12.0, 1.0),
}
# Neon's line is a fixed fraction of the height, so it stays at the full 1080x180 to remain visible.
LAYOUT_EXTRA = {"neon": {"opacity": 255}, "voiceline": {"line_width": 3.5}}
# Neon at 1080x180 is too big lossless; a high-quality lossy encode keeps the thin line crisp.
LAYOUT_QUALITY = {"neon": 95}
LAYOUT_SIZE = {"neon": (1080, 180), "radial": (300, 300)}


def over(frame: Image.Image, background: np.ndarray) -> np.ndarray:
    """Composite a premultiplied RGBA frame over float RGB: rgb + (1 - a) * background."""
    px = np.asarray(frame.convert("RGBA"), np.float64)
    return px[..., :3] + (1.0 - px[..., 3:] / 255.0) * background


def to_image(rgb: np.ndarray) -> Image.Image:
    return Image.fromarray(np.rint(np.clip(rgb, 0, 255)).astype(np.uint8), "RGB")


def save_webp(frames: list[Image.Image], path: Path, *, quality: int | None) -> None:
    """Looping animated WebP; ``quality=None`` is lossless. No metadata is written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    options = {"lossless": True, "quality": 100} if quality is None else {"quality": quality}
    frames[0].save(path, "WEBP", save_all=True, append_images=frames[1:], duration=1000 // FPS,
                   loop=0, method=4 if quality is None else 6, **options)


def analyse(wav: Path) -> tuple[engine.Analysis, engine.Spectrum | None]:
    analysis = engine.analyze_audio(wav)
    spectrum = (engine.Spectrum(analysis.spectrum, analysis.spectrum_rate, analysis.spectrum_bands)
                if analysis.spectrum is not None else None)
    return analysis, spectrum


# ------------------------------------------------------------------ hero

def _blob(xs: np.ndarray, ys: np.ndarray, cx: float, cy: float, radius: float, hard: float) -> np.ndarray:
    """A soft disc: ``hard`` 0 is a gaussian haze, towards 1 a bokeh disc with a bright rim."""
    d = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2) / radius
    haze = np.exp(-(d ** 2) * 2.2)
    disc = 1.0 / (1.0 + np.exp((d - 1.0) * 9.0))
    return (1 - hard) * haze + hard * disc * (0.75 + 0.25 * d)


# (x, y, radius, drift x, drift y, phase, hardness, strength, colour)
_BLOBS = (
    (0.18, 0.30, 0.34, 0.05, 0.04, 0.0, 0.0, 0.55, (116, 80, 190)),
    (0.78, 0.22, 0.30, 0.04, 0.05, 1.7, 0.0, 0.45, (140, 98, 206)),
    (0.55, 0.78, 0.40, 0.06, 0.03, 3.1, 0.0, 0.40, (88, 62, 150)),
    (0.90, 0.70, 0.22, 0.03, 0.04, 4.4, 0.0, 0.35, (124, 88, 180)),
    (0.30, 0.12, 0.085, 0.03, 0.02, 0.9, 0.9, 0.26, (170, 140, 220)),
    (0.66, 0.40, 0.065, 0.02, 0.03, 2.4, 0.9, 0.22, (150, 120, 205)),
    (0.45, 0.30, 0.050, 0.03, 0.02, 5.2, 0.9, 0.20, (185, 150, 225)),
    (0.84, 0.46, 0.075, 0.02, 0.02, 3.8, 0.9, 0.20, (140, 110, 200)),
    (0.10, 0.62, 0.060, 0.02, 0.03, 1.3, 0.9, 0.18, (160, 128, 210)),
)


def hero_background(t: float) -> np.ndarray:
    """An abstract blurred-studio backdrop: graphite with drifting muted-violet haze and bokeh."""
    w, h = 320, 160                                   # computed small, then upscaled: smooth, cheap
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
    xs, ys = xs / h, ys / h                           # both axes in units of the height
    aspect = w / h
    top, bottom = np.array((30, 28, 38), float), np.array((13, 12, 18), float)
    rgb = top + (bottom - top) * np.clip(ys[..., None], 0, 1)
    swing = 2 * math.pi * t / HERO_SECONDS            # one slow drift cycle across the clip
    for x, y, radius, dx, dy, phase, hard, strength, colour in _BLOBS:
        cx = (x + dx * math.sin(swing + phase)) * aspect
        cy = y + dy * math.cos(swing + phase * 1.3)
        m = _blob(xs, ys, cx, cy, radius, hard)[..., None] * strength
        rgb = rgb + (np.array(colour, float) - rgb) * np.clip(m, 0, 1)
    vignette = 1.0 - 0.38 * (((xs / aspect - 0.5) * 1.5) ** 2 + ((ys - 0.5) * 1.1) ** 2)[..., None]
    rgb = rgb * np.clip(vignette, 0, 1)
    big = Image.fromarray(np.rint(np.clip(rgb, 0, 255)).astype(np.uint8), "RGB").resize(HERO_SIZE, Image.BICUBIC)
    return np.asarray(big, np.float64)


def make_hero(analysis: engine.Analysis, spectrum: engine.Spectrum | None) -> None:
    look = engine.Look(width=HERO_BAR[0], height=HERO_BAR[1], layout="mirror", bars=86,
                       max_height_ratio=0.95, opacity=255).with_theme("Lavender")
    track = engine.shape_track(analysis, look)
    x0, y0 = HERO_BAR_POS
    frames = []
    for i in range(int(HERO_SECONDS * FPS)):
        bg = hero_background(i / FPS)
        bar = engine.make_frame(track, analysis.slot_rate, HERO_START + i / FPS, look, spectrum=spectrum)
        bg[y0:y0 + HERO_BAR[1], x0:x0 + HERO_BAR[0]] = over(bar, bg[y0:y0 + HERO_BAR[1], x0:x0 + HERO_BAR[0]])
        frames.append(to_image(bg))
    save_webp(frames, MEDIA / "hero.webp", quality=80)


# --------------------------------------------------------------- layouts

def make_layouts(analysis: engine.Analysis, spectrum: engine.Spectrum | None) -> None:
    for key, _label in catalog.LAYOUT_LABELS:
        data = catalog.default_look_dict()
        color, color2 = LAYOUT_COLORS[key]
        start, gain = LAYOUT_SECTION[key]
        data.update(layout=key, color=color, color2=color2, gain=gain, **LAYOUT_EXTRA.get(key, {}))
        if key in catalog.SQUARE_LAYOUTS:
            data.update(width=1080, height=1080)
        look = catalog.look_from_dict(data)
        size = LAYOUT_SIZE.get(key, (540, 90))
        track = engine.shape_track(analysis, look)
        bg = np.empty((size[1], size[0], 3), np.float64)
        bg[:] = APP_BG
        frames = []
        for i in range(int(LAYOUT_SECONDS * FPS)):
            frame = engine.make_frame(track, analysis.slot_rate, start + i / FPS, look, spectrum=spectrum)
            frames.append(to_image(over(engine.downscale_premultiplied(frame, size), bg)))
        save_webp(frames, MEDIA / "layouts" / f"{key}.webp", quality=LAYOUT_QUALITY.get(key))


# ----------------------------------------------------------- screenshots

def _optimise_png(raw: bytes, path: Path) -> None:
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    if buf.tell() > 900_000:                          # fall back to a palette, still crisp UI
        buf = io.BytesIO()
        img.quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(buf, "PNG", optimize=True)
    path.write_bytes(buf.getvalue())


def _api(port: int, token: str, route: str, body: dict) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}", data=json.dumps(body).encode(),
        headers={"X-Rubato-Token": token, "Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=30).read()


def _wait_for(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise TimeoutError(what)


def make_screenshots(wav: Path) -> None:
    from playwright.sync_api import expect, sync_playwright  # lazy: only the screenshots need it

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        port_file = home / "port.json"
        env = {**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / "state"),
               "XDG_CONFIG_HOME": str(home / "config"), "APPDATA": str(home / "appdata"),
               "LOCALAPPDATA": str(home / "local")}
        with (home / "stderr.txt").open("w") as err_file:   # a file, never an unread pipe
            proc = subprocess.Popen([sys.executable, "-B", "-m", "rubato_waveform.app", "--dev-browser",
                                     "--port-file", str(port_file)], cwd=ROOT, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err_file)
        try:
            _wait_for(lambda: port_file.exists() or proc.poll() is not None, 60, "the dev server did not start")
            if proc.poll() is not None:
                raise RuntimeError("the dev server exited: " + (home / "stderr.txt").read_text())
            info = json.loads(port_file.read_text())
            port, token = info["port"], info["url"].split("token=")[1]
            with sync_playwright() as driver:
                browser = driver.webkit.launch()
                try:
                    context = browser.new_context(viewport={"width": 1400, "height": 900}, device_scale_factor=2)
                    page = context.new_page()
                    page.goto(info["url"])
                    expect(page.locator("#status")).not_to_have_text("Loading")
                    _shot(page, MEDIA / "app-empty.png")
                    _api(port, token, "/api/dev/select", {"path": str(wav)})
                    expect(page.locator("#export-btn")).to_be_enabled(timeout=60_000)
                    page.locator("#scrub").fill("2700")
                    expect(page.locator("#preview[data-frame]")).to_be_visible()
                    _shot(page, MEDIA / "app-main.png")
                    context.close()
                finally:
                    browser.close()
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _shot(page, path: Path) -> None:
    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    page.mouse.move(700, 880)                          # a spot with no control under it
    page.wait_for_timeout(400)                         # let hover transitions and the frame settle
    _optimise_png(page.screenshot(), path)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "demo.wav"
        subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "make_demo_audio.py"), "--out", str(wav)],
                       check=True, capture_output=True)
        analysis, spectrum = analyse(wav)
        make_hero(analysis, spectrum)
        make_layouts(analysis, spectrum)
        make_screenshots(wav)


if __name__ == "__main__":
    main()
