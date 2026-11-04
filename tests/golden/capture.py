"""Capture golden frame hashes from a waveform rendering engine.

Usage: capture.py --engine PATH --out JSON [--no-numpy]
       capture.py --merge NUMPY_JSON NO_NUMPY_JSON --out MANIFEST

--merge combines one run of each mode into a manifest with the top-level keys
"numpy" and "no_numpy".

The engine is loaded from a file path (as part of its package), fed a deterministic synthetic analysis
(no FFmpeg involved) and rendered across a matrix of layouts, styles, sizes and
parameters. Each frame is hashed with SHA-256 over size, mode and raw RGBA bytes.
The output is {"meta": {...}, "records": {key: hash}}.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import importlib.util
import json
import math
import platform
import random
import struct
import sys
import time
from pathlib import Path

SEED = 20240607
ANALYSIS_RATE = 24000
SLOT = 120                    # samples per 5 ms slot at 24 kHz
SAMPLE_SECONDS = 6.0
SMALL = (320, 80)
EXPORT_SIZES = ((1080, 180), (1920, 240), (3840, 360), (1080, 1080))
TIMES = (0.6, 1.5, 2.05)
SPECTRUM_TIMES = (1.5, 4.2)
THEME_INDEX = 0               # themes are referenced by index, never by name


def load_engine(path: Path, no_numpy: bool):
    if no_numpy:
        sys.modules["numpy"] = None  # any `import numpy` now raises ImportError
    # The engine uses relative imports, so load its package from the file's directory.
    package = importlib.util.spec_from_file_location(
        "golden_pkg", path.parent / "__init__.py", submodule_search_locations=[str(path.parent)])
    sys.modules["golden_pkg"] = importlib.util.module_from_spec(package)
    package.loader.exec_module(sys.modules["golden_pkg"])
    module = importlib.import_module(f"golden_pkg.{path.stem}")
    if no_numpy and getattr(module, "np", None) is not None:
        raise SystemExit("engine still has numpy loaded; cannot force the no-numpy path")
    if not no_numpy and getattr(module, "np", None) is None:
        raise SystemExit("numpy is not available; use --no-numpy to capture without it")
    return module


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def synth_samples() -> list[float]:
    """Mono samples at the analysis rate: bursty 'speech', a pause, then a beat."""
    rng = random.Random(SEED)
    total = int(SAMPLE_SECONDS * ANALYSIS_RATE)
    out = [0.0] * total
    for i in range(total):
        t = i / ANALYSIS_RATE
        noise = rng.uniform(-1.0, 1.0)
        if t < 0.4:                                   # quiet room tone
            v = 0.004 * noise
        elif t < 3.0:                                 # syllables: gliding tones, gaps
            gate = max(0.0, math.sin(2 * math.pi * 3.1 * t) + 0.15 * math.sin(2 * math.pi * 0.7 * t))
            f = 140.0 + 60.0 * math.sin(2 * math.pi * 0.9 * t)
            tone = (math.sin(2 * math.pi * f * t) + 0.5 * math.sin(4 * math.pi * f * t)
                    + 0.25 * math.sin(6 * math.pi * f * t + 1.0))
            v = 0.45 * gate * (0.6 + 0.4 * math.sin(2 * math.pi * 0.4 * t)) * tone + 0.004 * noise
        elif t < 3.4:                                 # pause
            v = 0.003 * noise
        else:                                         # beat: bass, chord, hat
            beat = (t - 3.4) % 0.5 / 0.5
            bass = 0.5 * math.sin(2 * math.pi * 55.0 * t) * math.exp(-4.0 * beat)
            chord = 0.12 * (math.sin(2 * math.pi * 220.0 * t) + math.sin(2 * math.pi * 277.2 * t)
                            + math.sin(2 * math.pi * 330.0 * t))
            hat = 0.2 * noise * math.exp(-18.0 * ((t - 3.4) % 0.25 / 0.25))
            v = bass + chord + hat
        out[i] = v
    return out


def build_analysis(eng, samples: list[float]):
    peaks, rmss = array.array("f"), array.array("f")
    for start in range(0, len(samples) - SLOT + 1, SLOT):
        chunk = samples[start:start + SLOT]
        peaks.append(max(abs(v) for v in chunk))
        rmss.append(math.sqrt(sum(v * v for v in chunk) / SLOT))
    analysis = eng.Analysis(peaks, rmss)
    eng._calibrate(analysis)
    return analysis


def frame_hash(img) -> str:
    h = hashlib.sha256()
    h.update(f"{img.width}x{img.height}|{img.mode}|".encode())
    h.update(img.tobytes())
    return h.hexdigest()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--no-numpy", action="store_true")
    parser.add_argument("--merge", nargs=2, type=Path, metavar=("NUMPY_JSON", "NO_NUMPY_JSON"))
    args = parser.parse_args()

    if args.merge:
        merged = {
            "numpy": json.loads(args.merge[0].read_text()),
            "no_numpy": json.loads(args.merge[1].read_text()),
        }
        args.out.write_text(json.dumps(merged, indent=1, sort_keys=True) + "\n")
        return
    if args.engine is None:
        parser.error("--engine is required unless --merge is used")

    started = time.monotonic()
    eng = load_engine(args.engine, args.no_numpy)
    import PIL

    samples = synth_samples()
    analysis = build_analysis(eng, samples)
    spectrum = None
    if getattr(eng, "np", None) is not None:
        data, rate, bands = eng._compute_spectrum_from_samples(samples)
        if data is not None:
            spectrum = eng.Spectrum(data, rate, bands)
    slot_rate = analysis.slot_rate

    primary, secondary = (hex_rgb(c) for c in eng.THEMES[THEME_INDEX][1:3])
    records: dict[str, str] = {}

    def look_for(gradient: bool, **kw):
        base = {"color": primary, "color2": secondary if gradient else None}
        base.update(kw)
        return eng.Look(**base)

    def render(key: str, look, t: float, spec=None, window: bool = False) -> None:
        if window:   # same windowing as the interactive preview
            margin = look.window_seconds + 0.5
            start = max(0, int((t - margin) * slot_rate))
            end = min(analysis.slots, int((t + margin) * slot_rate) + 1)
            track = eng.shape_track(analysis, look, start, end)
            img = eng.make_frame(track, slot_rate, t, look, track_offset=start, spectrum=spec)
        else:
            track = eng.shape_track(analysis, look)
            img = eng.make_frame(track, slot_rate, t, look, spectrum=spec)
        assert key not in records, key
        records[key] = frame_hash(img)

    def tag(gradient: bool) -> str:
        return "gradient" if gradient else "solid"

    w, h = SMALL
    small = {"width": w, "height": h, "bars": 31}

    # 1. layouts x styles x colour mode x time
    for layout in eng.LAYOUTS:
        for style in eng.STYLES:
            for gradient in (False, True):
                for t in TIMES:
                    render(f"grid/{layout}/{style}/{tag(gradient)}/t{t}",
                           look_for(gradient, layout=layout, style=style, **small), t)

    # 2. stationary mode of the optional layouts
    for layout in eng.STATIONARY_OPTIONAL_LAYOUTS:
        for style in eng.STYLES:
            for gradient in (False, True):
                for t in TIMES:
                    render(f"stationary/{layout}/{style}/{tag(gradient)}/t{t}",
                           look_for(gradient, layout=layout, style=style, stationary=True, **small), t)

    # 3. spectrum-driven frames (numpy only)
    if spectrum is not None:
        spec_layouts = ("peakhold", "mirror-reflect", "neon")
        for layout in spec_layouts:
            for style in eng.STYLES:
                for gradient in (False, True):
                    for t in SPECTRUM_TIMES:
                        render(f"spectrum/{layout}/{style}/{tag(gradient)}/t{t}",
                               look_for(gradient, layout=layout, style=style, **small), t, spectrum)
        for gradient in (False, True):
            for t in SPECTRUM_TIMES:
                render(f"spectrum/voiceline/bars/{tag(gradient)}/t{t}",
                       look_for(gradient, layout="voiceline", **small), t, spectrum)
        for layout in eng.STATIONARY_OPTIONAL_LAYOUTS:
            for style in eng.STYLES:
                for gradient in (False, True):
                    for t in SPECTRUM_TIMES:
                        render(f"spectrum-stationary/{layout}/{style}/{tag(gradient)}/t{t}",
                               look_for(gradient, layout=layout, style=style, stationary=True, **small),
                               t, spectrum)

    # 4. export sizes (supersampling branches)
    for layout in eng.LAYOUTS:
        for (sw, sh) in EXPORT_SIZES:
            for t in (1.5, 2.05):
                render(f"size/{layout}/{sw}x{sh}/gradient/t{t}",
                       look_for(True, layout=layout, width=sw, height=sh, bars=61), t)

    # 5. parameter sweep at slider extremes
    sweeps = [
        ("metric", ("peak", "rms"), ("scroll", "ripple", "voiceline")),
        ("gain", (0.3, 2.5), ("scroll", "ripple", "neon")),
        ("contrast", (0.4, 2.0), ("scroll", "mirror-reflect")),
        ("smoothing", (0.0, 1.0), ("scroll", "peakhold", "voiceline")),
        ("opacity", (40, 255), ("scroll", "neon", "voiceline")),
        ("max_height_ratio", (0.2, 0.98), ("mirror", "radial", "voiceline")),
        ("window_seconds", (0.25, 3.0), ("scroll", "peakhold", "radial")),
        ("bars", (16, 160), ("scroll", "radial", "neon")),
        ("reactivity", (0.0, 1.0), ("voiceline",)),
        ("drift", (0.0, 2.0), ("voiceline",)),
        ("line_width", (1.0, 6.0), ("voiceline",)),
    ]
    if hasattr(eng.Look(), "baseline_dots"):
        sweeps.append(("baseline_dots", (False,), ("scroll", "mirror", "pulse")))
    for param, values, layouts in sweeps:
        for layout in layouts:
            for value in values:
                for gradient in (False, True):
                    for t in (0.2, 1.5):
                        kw = {**small}
                        kw[param] = value
                        render(f"sweep/{param}={value}/{layout}/{tag(gradient)}/t{t}",
                               look_for(gradient, layout=layout, **kw), t)
                        if spectrum is not None and layout == "voiceline":
                            render(f"sweep-spectrum/{param}={value}/{layout}/{tag(gradient)}/t{t}",
                                   look_for(gradient, layout=layout, **kw), t, spectrum)

    # 6. preview path: windowed shape_track + track_offset
    for layout in eng.LAYOUTS:
        for gradient in (False, True):
            for t in (0.3, 1.5, 5.8):
                look = look_for(gradient, layout=layout, **small)
                render(f"preview/{layout}/{tag(gradient)}/t{t}", look, t, window=True)
                if spectrum is not None:
                    render(f"preview-spectrum/{layout}/{tag(gradient)}/t{t}", look, t, spectrum, window=True)

    # 7. constants
    themes = "|".join(f"{p}{s}" for _name, p, s in eng.THEMES)
    records["const/theme-hex"] = sha(themes.encode())
    records["const/theme-count"] = sha(str(len(eng.THEMES)).encode())
    for name in ("LAYOUTS", "STYLES", "METRICS", "STATIONARY_OPTIONAL_LAYOUTS"):
        records[f"const/{name}"] = sha("|".join(getattr(eng, name)).encode())

    # 8. raw shape_track output
    track_looks = {
        "default": {},
        "rms": {"metric": "rms"},
        "gain-contrast": {"gain": 2.5, "contrast": 0.4},
        "raw": {"smoothing": 0.0},
        "smooth": {"smoothing": 1.0, "contrast": 2.0},
        "voiceline": {"layout": "voiceline"},
        "voiceline-smooth": {"layout": "voiceline", "smoothing": 1.0, "metric": "rms"},
    }
    for name, kw in track_looks.items():
        look = eng.Look(**kw)
        full = eng.shape_track(analysis, look)
        part = eng.shape_track(analysis, look, 300, 700)
        for label, values in (("full", full), ("window", part)):
            records[f"track/{name}/{label}"] = sha(struct.pack(f"<{len(values)}d", *values))

    meta = {
        "pillow": PIL.__version__,
        "numpy": getattr(getattr(eng, "np", None), "__version__", None),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "no_numpy": args.no_numpy,
        "spectrum_records": spectrum is not None,
        "count": len(records),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"meta": meta, "records": records}, indent=1, sort_keys=True) + "\n")
    print(f"{len(records)} records in {time.monotonic() - started:.1f} s -> {args.out}")


if __name__ == "__main__":
    main()
