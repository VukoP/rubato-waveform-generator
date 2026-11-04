# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Command line interface: ``rubato-waveform`` / ``python -m rubato_waveform``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .engine import (
    LAYOUTS,
    LOOK_LIMITS,
    METRICS,
    STYLES,
    THEMES,
    Look,
    analyze_audio,
    parse_hex,
    render_movie,
)
from .ffmpeg_tools import FFmpegNotFoundError, install_hint


def _range_text(field: str) -> str:
    lo, hi = LOOK_LIMITS[field]
    return f"{lo:g}-{hi:g}"


def _ranged(field: str, kind: type):
    """argparse type that accepts a number inside LOOK_LIMITS[field]."""
    lo, hi = LOOK_LIMITS[field]

    def convert(text: str):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid {kind.__name__} value: {text!r}") from None
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"must be in the range {lo:g}-{hi:g}")
        return value

    return convert


def _color(text: str) -> tuple[int, int, int]:
    try:
        return parse_hex(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rubato-waveform",
        description="Creates an audio-reactive waveform (transparent MOV) for video editors such as DaVinci Resolve.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("audio", type=Path, help="WAV, MP3, M4A, etc.")
    parser.add_argument("output", type=Path, help="output transparent MOV file")
    parser.add_argument("--width", type=_ranged("width", int), default=1080,
                        help=f"width in pixels ({_range_text('width')})")
    parser.add_argument("--height", type=_ranged("height", int), default=180,
                        help=f"height in pixels ({_range_text('height')})")
    parser.add_argument("--fps", type=_ranged("fps", int), default=30,
                        help=f"frames per second ({_range_text('fps')})")
    parser.add_argument("--bars", type=_ranged("bars", int), default=61,
                        help=f"number of bars ({_range_text('bars')}); 45-90 recommended")
    parser.add_argument("--style", choices=STYLES, default="bars")
    parser.add_argument("--layout", choices=LAYOUTS, default="scroll",
                        help="scroll: time flows to the right; mirror: now in the center, echo to the sides; "
                             "pulse: stationary bars pulsing from the center; flow: like scroll, symmetric gradient; "
                             "ripple: stationary bars pulsing as a wave from the center; "
                             "mirror-reflect: bars above a 62%% line with a mirrored reflection (55%% of the "
                             "height of the part above the baseline) underneath (with numpy: frequency bands); "
                             "peakhold: bars from the bottom with a stateless peak-hold cap (with numpy: frequency bands); "
                             "neon: a glowing line oscillating around the center (with numpy: frequency bands); "
                             "radial: spokes rotating around the center of the frame; "
                             "voiceline: a thin, calm oscilloscope-like line - amplitude from loudness "
                             "(with numpy the wave shape follows the timbre of the voice)")
    parser.add_argument("--stationary", action="store_true",
                        help="for scroll/mirror/flow/radial: the bars stay in place and pulse "
                             "to the current audio instead of travelling sideways (stationary version)")
    parser.add_argument("--metric", choices=METRICS, default="peak",
                        help="peak: true peaks (as in a DAW); rms: loudness")
    parser.add_argument("--window-seconds", type=_ranged("window_seconds", float), default=1.05,
                        help=f"how many seconds of audio fit across the width of the strip "
                             f"({_range_text('window_seconds')})")
    parser.add_argument("--sensitivity", type=_ranged("gain", float), default=1.0,
                        help=f"sensitivity ({_range_text('gain')})")
    parser.add_argument("--contrast", type=_ranged("contrast", float), default=1.0,
                        help=f"{_range_text('contrast')}; <1 emphasises quiet parts")
    parser.add_argument("--smoothing", type=_ranged("smoothing", float), default=0.25,
                        help=f"{_range_text('smoothing')}: 0 raw ... 1 very smooth")
    parser.add_argument("--theme", default=None,
                        help="theme name: " + ", ".join(name for name, _, _ in THEMES))
    parser.add_argument("--color", type=_color, default=None,
                        help="primary color #RRGGBB; overrides the theme's first color")
    parser.add_argument("--color2", type=_color, default=None,
                        help="second gradient color #RRGGBB; overrides the theme's second color")
    parser.add_argument("--no-gradient", action="store_true",
                        help="always a solid color: the second color is dropped, even when --theme or "
                             "--color2 is given (--color still sets the solid color)")
    parser.add_argument("--opacity", type=_ranged("opacity", int), default=235,
                        help=f"opacity ({_range_text('opacity')})")
    parser.add_argument("--max-height", type=_ranged("max_height_ratio", float), default=0.72,
                        help=f"tallest bar as a fraction of the height ({_range_text('max_height_ratio')})")
    parser.add_argument("--no-dots", action="store_true", help="no dots on the baseline")
    parser.add_argument("--reactivity", type=_ranged("reactivity", float), default=0.7,
                        help=f"voiceline: 0 = loudness only (reference formula) ... 1 = shape fully from the "
                             f"audio ({_range_text('reactivity')})")
    parser.add_argument("--drift", type=_ranged("drift", float), default=1.0,
                        help=f"voiceline: wave drift speed ({_range_text('drift')})")
    parser.add_argument("--line-width", type=_ranged("line_width", float), default=2.0,
                        help=f"voiceline: line thickness in px at 1080x180 ({_range_text('line_width')})")
    return parser


def build_look(args: argparse.Namespace) -> Look:
    """Look from parsed arguments. Raises KeyError for an unknown theme."""
    look = Look(
        width=args.width, height=args.height, fps=args.fps, bars=args.bars,
        style=args.style, layout=args.layout, window_seconds=args.window_seconds,
        metric=args.metric, gain=args.sensitivity, contrast=args.contrast,
        smoothing=args.smoothing, opacity=args.opacity,
        max_height_ratio=args.max_height, baseline_dots=not args.no_dots,
        stationary=args.stationary, reactivity=args.reactivity, drift=args.drift,
        line_width=args.line_width,
    )
    if args.theme:
        look = look.with_theme(args.theme, gradient=not args.no_gradient)
    if args.color:
        look.color = args.color
    if args.color2:
        look.color2 = args.color2
    if args.no_gradient:
        look.color2 = None
    return look


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.audio.is_file():
        parser.error(f"No such file: {args.audio}")
    if args.output.suffix.lower() != ".mov":
        parser.error("The output file should have the .mov extension")
    try:
        look = build_look(args)
    except KeyError:
        parser.error(f"Unknown theme: {args.theme}")

    try:
        print("1/3 Analysing audio (peaks and RMS every 5 ms)...")
        analysis = analyze_audio(args.audio)
        print(f"    {analysis.duration:.1f} s, {analysis.slots} slots")
        if analysis.spectrum is not None:
            print(f"    frequency spectrum computed: {analysis.spectrum_bands} bands "
                  f"@ {analysis.spectrum_rate} frames/s")
        else:
            print("    frequency spectrum unavailable (no numpy) - stationary layouts use amplitude sampling")

        total = analysis.frames(look.fps)
        detail = ("voice line" if look.layout == "voiceline"
                  else f"{look.bars} bars, {look.style}/{look.layout}")
        print(f"2/3 Rendering {total} frames ({detail})...")
        render_movie(
            analysis, args.output, look,
            progress=lambda current, total: print(f"   {current}/{total}")
            if current % 180 == 1 or current == total else None,
        )
    except FFmpegNotFoundError:
        print(f"error: FFmpeg was not found. Install it with: {install_hint()}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # The engine's finally blocks have already stopped FFmpeg and removed the temp file.
        print("Interrupted.", file=sys.stderr)
        return 130
    print("3/3 Encoded a transparent ProRes 4444 MOV.")
    print(f"\nDone: {args.output}")
    print("Import the MOV into your video editor as a layer above your footage.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
