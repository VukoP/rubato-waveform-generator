# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Generate the brand assets (icons, favicons, mark, social preview) from one place.

Run from anywhere: ``python scripts/make_brand.py``. Needs Pillow, numpy, fontTools
(with brotli, only to read the WOFF2 font) and FFmpeg (the social preview shows a real
frame rendered by the engine). Output is byte-identical between runs.
"""

from __future__ import annotations

import io
import math
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from fontTools.ttLib import TTFont
from PIL import Image, ImageChops, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UPPER = (167, 139, 250)
LOWER = (116, 85, 240)
TILE_TOP = (31, 31, 36)
TILE_BOTTOM = (20, 20, 24)
BG = (17, 17, 20)
TEXT = (242, 240, 248)
MUTED = (154, 151, 168)

# The mark in a 64x64 box: a disc split in two by a wave-shaped, transparent gap.
DISC_RADIUS = 30
WAVE_START = (2, 32)
WAVE = ((6, 32, 8, 25, 15, 25), (22, 25, 20, 40, 27, 40), (34, 40, 32, 22, 39, 22),
        (46, 22, 44, 38, 51, 38), (58, 38, 58, 32, 62, 32))
GAP = 7.0
GAP_TINY = 9.0              # for a disc of 28 px or less, where a thinner gap closes up
DISC_IN_TILE = 0.72         # the disc's diameter on the macOS icon's tile


def wave_path() -> str:
    """The wave as SVG path data, from the left edge of the disc to the right edge."""
    curves = "".join("C{:g} {:g} {:g} {:g} {:g} {:g}".format(*curve) for curve in WAVE)
    return f"M{WAVE_START[0]:g} {WAVE_START[1]:g}{curves}"


def wave_points(steps: int = 48) -> list[tuple[float, float]]:
    """The wave as a polyline in the 64x64 box."""
    points = [WAVE_START]
    x0, y0 = WAVE_START
    for x1, y1, x2, y2, x3, y3 in WAVE:
        for i in range(1, steps + 1):
            t = i / steps
            u = 1 - t
            points.append((u ** 3 * x0 + 3 * u ** 2 * t * x1 + 3 * u * t ** 2 * x2 + t ** 3 * x3,
                           u ** 3 * y0 + 3 * u ** 2 * t * y1 + 3 * u * t ** 2 * y2 + t ** 3 * y3))
        x0, y0 = x3, y3
    return points


def supersampling(size: int) -> int:
    return 4 if size >= 256 else 16 if size <= 32 else 8


def squircle_mask(size: int, margin: float, ss: int) -> Image.Image:
    """Superellipse (exponent 5, a continuous corner) as an L mask, supersampled."""
    big = size * ss
    half = big * (0.5 - margin)
    pts = []
    for i in range(720):
        a = 2 * math.pi * i / 720
        c, s = math.cos(a), math.sin(a)
        pts.append((big / 2 + half * math.copysign(abs(c) ** 0.4, c),
                    big / 2 + half * math.copysign(abs(s) ** 0.4, s)))
    img = Image.new("L", (big, big), 0)
    ImageDraw.Draw(img).polygon(pts, fill=255)
    return img.reduce(ss)


def disc_halves(size: int, inset: float) -> tuple[np.ndarray, np.ndarray]:
    """Coverage (0..1) of the disc's upper and lower half. ``inset`` is the 64x64 box's margin."""
    ss = supersampling(size)
    big = size * ss
    unit = big * (1 - 2 * inset) / 64
    origin = big * inset
    points = [(origin + x * unit, origin + y * unit) for x, y in wave_points()]
    gap = GAP_TINY if 2 * DISC_RADIUS * unit / ss <= 28 else GAP
    left, right = points[0], points[-1]

    disc = Image.new("L", (big, big), 0)
    centre, radius = origin + 32 * unit, DISC_RADIUS * unit
    ImageDraw.Draw(disc).ellipse((centre - radius, centre - radius, centre + radius, centre + radius), fill=255)
    above = Image.new("L", (big, big), 0)
    ImageDraw.Draw(above).polygon([(0, 0), (0, left[1]), *points, (big, right[1]), (big, 0)], fill=255)
    kept = Image.new("L", (big, big), 255)
    draw = ImageDraw.Draw(kept)
    half_gap = gap * unit / 2
    draw.rectangle((0, left[1] - half_gap, left[0], left[1] + half_gap), fill=0)
    draw.rectangle((right[0], right[1] - half_gap, big, right[1] + half_gap), fill=0)
    for x, y in points:     # overlapping circles: a wide ImageDraw line leaves a rough edge on curves
        draw.ellipse((x - half_gap, y - half_gap, x + half_gap, y + half_gap), fill=0)

    whole = ImageChops.multiply(disc, kept)
    upper = ImageChops.multiply(whole, above).reduce(ss)
    lower = ImageChops.multiply(whole, ImageChops.invert(above)).reduce(ss)
    return np.asarray(upper, float)[..., None] / 255, np.asarray(lower, float)[..., None] / 255


def render_mark(size: int, inset: float = 0.0) -> Image.Image:
    """The bare mark. The gap and the area around the disc are transparent."""
    upper, lower = disc_halves(size, inset)
    total = upper + lower
    rgb = (upper * np.array(UPPER, float) + lower * np.array(LOWER, float)) / np.where(total > 0, total, 1)
    out = np.concatenate([np.rint(rgb), np.rint(total * 255)], axis=2).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def render_icon(size: int) -> Image.Image:
    """The macOS app icon: the mark on a graphite tile, which also shows through the gap."""
    margin = 0.098 if size >= 128 else 0.03
    tile = squircle_mask(size, margin, supersampling(size))
    box = (1 - 2 * margin) * DISC_IN_TILE * 32 / DISC_RADIUS
    upper, lower = disc_halves(size, (1 - box) / 2)
    ys = ((np.arange(size) + 0.5) / size)[:, None, None]
    top, bottom = np.array(TILE_TOP, float), np.array(TILE_BOTTOM, float)
    rgb = np.broadcast_to(top + (bottom - top) * ys, (size, size, 3))
    for coverage, colour in ((upper, UPPER), (lower, LOWER)):
        rgb = rgb + (np.array(colour, float) - rgb) * coverage
    alpha = np.asarray(tile, float)[..., None]
    out = np.concatenate([np.rint(rgb), alpha], axis=2).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def write_icns(path: Path, icons: dict[int, Image.Image]) -> None:
    # (type, pixel size): PNG payloads, 16..1024 incl. the @2x variants.
    kinds = (("icp4", 16), ("icp5", 32), ("ic11", 32), ("ic12", 64), ("ic07", 128),
             ("ic13", 256), ("ic08", 256), ("ic14", 512), ("ic09", 512), ("ic10", 1024))
    body = b""
    for kind, px in kinds:
        data = png_bytes(icons[px])
        body += kind.encode() + struct.pack(">I", 8 + len(data)) + data
    path.write_bytes(b"icns" + struct.pack(">I", 8 + len(body)) + body)


def write_ico(path: Path, icons: dict[int, Image.Image]) -> None:
    sizes = (16, 24, 32, 48, 64, 128, 256)
    blobs = [png_bytes(icons[s]) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, blob in zip(sizes, blobs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    path.write_bytes(head + entries + b"".join(blobs))


def mark_svg() -> str:
    wave = wave_path()
    x, y = WAVE_START
    arc = f"A{DISC_RADIUS} {DISC_RADIUS} 0 0"
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">\n'
            '<mask id="gap"><rect width="64" height="64" fill="#fff"/>'
            f'<path d="M-4 {y}H{x}{wave[wave.index("C"):]}H68" '
            f'fill="none" stroke="#000" stroke-width="{GAP:g}"/></mask>\n'
            '<g mask="url(#gap)">\n'
            f'<path d="{wave}{arc} 0 {x} {y}Z" fill="#a78bfa"/>\n'
            f'<path d="{wave}{arc} 1 {x} {y}Z" fill="#7455f0"/>\n'
            '</g>\n</svg>\n')


def instrument_sans(px: int, weight: int = 600) -> ImageFont.FreeTypeFont:
    font = TTFont(ROOT / "rubato_waveform/ui/fonts/InstrumentSans-latin.woff2")
    font.flavor = None
    buf = io.BytesIO()
    font.save(buf)
    buf.seek(0)
    face = ImageFont.truetype(buf, px)
    face.set_variation_by_axes([weight])
    return face


def engine_frame(width: int, height: int) -> Image.Image:
    """A real engine frame (premultiplied RGBA) from the synthetic demo audio."""
    from rubato_waveform import engine

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "demo.wav"
        subprocess.run([sys.executable, "-B", str(ROOT / "scripts/make_demo_audio.py"),
                        "--out", str(wav)], check=True, capture_output=True)
        analysis = engine.analyze_audio(wav)
    look = engine.Look(width=width, height=height, layout="mirror", bars=96,
                       max_height_ratio=0.9, opacity=255).with_theme("Violet")
    track = engine.shape_track(analysis, look)
    return engine.make_frame(track, analysis.slot_rate, FRAME_T, look)


FRAME_T = 3.0


def social_preview() -> Image.Image:
    w, h = 1280, 640
    bg = np.empty((h, w, 3), float)
    bg[:] = BG
    frame = engine_frame(1120, 300)
    px = np.asarray(frame.convert("RGBA"), float)
    rgb, a = px[..., :3], px[..., 3:] / 255
    x0, y0 = 80, 250
    region = bg[y0:y0 + 300, x0:x0 + 1120]
    bg[y0:y0 + 300, x0:x0 + 1120] = rgb + (1 - a) * region  # premultiplied "over"
    img = Image.fromarray(np.rint(np.clip(bg, 0, 255)).astype(np.uint8), "RGB")
    mark = render_mark(96)
    img.paste(mark, (80, 80), mark)
    draw = ImageDraw.Draw(img)
    draw.text((196, 128), "Rubato Waveform Generator", font=instrument_sans(54), fill=TEXT, anchor="lm")
    draw.text((80, 214), "Audio waveforms as transparent video for your editor",
              font=instrument_sans(28, 400), fill=MUTED, anchor="lm")
    return img


def main() -> None:
    brand = ROOT / "assets/brand"
    icons = {s: render_icon(s) for s in (16, 32, 64, 128, 256, 512, 1024)}
    brand.joinpath("icon-1024.png").write_bytes(png_bytes(icons[1024]))
    write_icns(brand / "icon.icns", icons)
    write_ico(brand / "icon.ico", {s: render_mark(s, 0.02) for s in (16, 24, 32, 48, 64, 128, 256)})
    favicon = png_bytes(render_mark(64, 0.02))
    (ROOT / "rubato_waveform/ui/brand/favicon.png").write_bytes(favicon)
    (ROOT / "docs/assets/favicon.png").write_bytes(favicon)
    for target in (brand / "mark.svg", ROOT / "rubato_waveform/ui/brand/mark.svg", ROOT / "docs/assets/favicon.svg"):
        target.write_text(mark_svg(), encoding="utf-8")
    preview = png_bytes(social_preview())
    (brand / "social-preview.png").write_bytes(preview)
    (ROOT / "docs/assets/social-preview.png").write_bytes(preview)


if __name__ == "__main__":
    main()
