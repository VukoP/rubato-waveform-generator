# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Engine checks that need neither FFmpeg nor a rendered movie (except where marked)."""

import array
import hashlib
import itertools
import math
import wave
from dataclasses import replace

import pytest
from conftest import (
    requires_ffmpeg,
    requires_numpy,
    requires_pinned_env,
)
from PIL import ImageChops

from rubato_waveform.engine import (
    ANALYSIS_RATE,
    LAYOUTS,
    SLOT_RATE,
    SPECTRUM_BANDS,
    STYLES,
    THEMES,
    VOICE_WAVES,
    Look,
    Spectrum,
    _color_layer,
    _compute_spectrum_from_samples,
    _geometry_bottom,
    _geometry_common,
    _voiceline_envelope,
    analyze_audio,
    bar_levels,
    make_frame,
    shape_track,
    theme_colors,
    voiceline_points,
    voiceline_weights,
)

# Hashes generated once from the reference code: a changed value means the rendered
# image changed (a regression), not merely a refactor.
GOLDEN_LAYOUTS = ("scroll", "mirror", "pulse", "flow", "ripple", "mirror-reflect",
                  "peakhold", "neon", "radial")
# These values are for Pillow 13 (see requires_pinned_env).
GOLDEN_FRAMES = {
    ("bars", "scroll"): "adc6d7576ff9",
    ("bars", "mirror"): "cd2ab6974329",
    ("bars", "pulse"): "890d24967d1a",
    ("line", "scroll"): "f4aa84a115b0",
    ("line", "mirror"): "b238bd33600a",
    ("line", "pulse"): "ca842e80141c",
    ("area", "scroll"): "2a41032facc7",
    ("area", "mirror"): "2411a0b1aae2",
    ("area", "pulse"): "2c5f2c8b3de3",
    ("dots", "scroll"): "2a1f56ffa87c",
    ("dots", "mirror"): "3a79aea7a7f9",
    ("dots", "pulse"): "3cca8a5ef3b0",
    ("bars", "flow"): "77dbc43f9988",
    ("bars", "ripple"): "ada4bbb996b5",
    ("bars", "mirror-reflect"): "0f896fed4da7",
    ("bars", "peakhold"): "e8540f36d1d9",
    ("bars", "neon"): "3254b706709a",
    ("bars", "radial"): "7f00f0a4a3b5",
    ("line", "flow"): "0a12c54dbccf",
    ("line", "ripple"): "d14043a96424",
    ("line", "mirror-reflect"): "43cb0beb5e89",
    ("line", "peakhold"): "f67fcf7da87d",
    ("line", "neon"): "3254b706709a",
    ("line", "radial"): "7f00f0a4a3b5",
    ("area", "flow"): "416ecf590704",
    ("area", "ripple"): "dcf3b6cae057",
    ("area", "mirror-reflect"): "b9bf7e6a5e03",
    ("area", "peakhold"): "c3af8e5d1d3b",
    ("area", "neon"): "3254b706709a",
    ("area", "radial"): "7f00f0a4a3b5",
    ("dots", "flow"): "76ba34aa77d8",
    ("dots", "ripple"): "97a51c47d9de",
    ("dots", "mirror-reflect"): "39cd016790a7",
    ("dots", "peakhold"): "2218138b2266",
    ("dots", "neon"): "3254b706709a",
    ("dots", "radial"): "7f00f0a4a3b5",
}
# Voice line (no spectrum, 320x80, t=2.2) - style does not apply to it, so one hash.
GOLDEN_VOICELINE = "290c34cd8004"


def short_hash(frame) -> str:
    return hashlib.sha256(frame.tobytes()).hexdigest()[:12]


def two_tone_spectrum() -> Spectrum:
    """3 s signal: 100 Hz in the first half, 5 kHz in the second."""
    sr = ANALYSIS_RATE
    n = int(3.0 * sr)
    samples = array.array("f", [0.0]) * n
    for i in range(n):
        freq = 100.0 if i < n // 2 else 5000.0
        samples[i] = 0.8 * math.sin(2 * math.pi * freq * i / sr)
    data, rate, bands = _compute_spectrum_from_samples(samples)
    assert data is not None and bands == SPECTRUM_BANDS
    return Spectrum(data, rate, bands)


def test_engine(synthetic_analysis) -> None:
    analysis = synthetic_analysis
    assert abs(analysis.duration - 3.0) < 1e-6

    look = Look(smoothing=0.0)
    track = shape_track(analysis, look)
    assert len(track) == analysis.slots
    quiet, loud, soft = track[100], track[300], track[500]
    assert quiet == 0.0, "silence must be flat"
    assert loud > 0.9 and 0.05 < soft < loud, (loud, soft)

    louder = shape_track(analysis, Look(gain=1.6, smoothing=0.0))
    assert louder[500] > soft, "sensitivity lifts the quieter parts"
    compressed = shape_track(analysis, Look(contrast=0.5, smoothing=0.0))
    assert compressed[500] > soft, "contrast < 1 emphasises quiet parts"

    window = shape_track(analysis, look, start=280, end=320)
    assert len(window) == 40 and abs(window[20] - track[300]) < 1e-6, "window = full render"

    for style in STYLES:
        for layout in LAYOUTS:
            frame = make_frame(track, analysis.slot_rate, 1.5,
                               Look(style=style, layout=layout, width=320, height=80, bars=31))
            assert frame.mode == "RGBA" and frame.getbbox(), (style, layout)

    gradient = Look(width=320, height=80).with_theme("Red")
    assert gradient.color == theme_colors("Red")[0] and gradient.color2
    frame = make_frame(track, analysis.slot_rate, 1.5, gradient)
    assert frame.getbbox()
    assert len({name for name, _, _ in THEMES}) == len(THEMES)


def test_golden_table_complete() -> None:
    assert set(GOLDEN_FRAMES) == {(s, ly) for s in STYLES for ly in GOLDEN_LAYOUTS}


@requires_pinned_env
@pytest.mark.parametrize(("style", "layout"), sorted(GOLDEN_FRAMES))
def test_golden_frames(synthetic_analysis, style, layout) -> None:
    """Frames must look bit-for-bit the same as before the refactor."""
    analysis = synthetic_analysis
    track = shape_track(analysis, Look(smoothing=0.0))
    frame = make_frame(track, analysis.slot_rate, 1.5,
                       Look(style=style, layout=layout, width=320, height=80, bars=31))
    assert short_hash(frame) == GOLDEN_FRAMES[(style, layout)], (style, layout)


def test_gradient_axis_y() -> None:
    look = Look(width=320, height=80).with_theme("Red")
    layer = _color_layer(look, left=0, usable=320, axis="y")
    assert layer.getpixel((5, 0)) != layer.getpixel((5, 79))


def ink_ratio(frame, x0: float, x1: float) -> float:
    """Red/blue ratio of the ink colour in a horizontal slice (0..1) of a premultiplied frame.

    A ratio is used because it does not depend on the alpha scaling or on
    antialiasing, only on which gradient stop the pixels came from.
    """
    width, height = frame.size
    region = frame.crop((int(width * x0), 0, int(width * x1), height))
    red, blue = (sum(region.getchannel(c).tobytes()) for c in "RB")
    return red / blue


@pytest.mark.parametrize("size", [(320, 80), (3840, 360)])
def test_scroll_and_flow_gradients_differ(size) -> None:
    """scroll has a left-to-right gradient, flow is symmetric around the centre.

    At 3840x360 both layouts render at 1x, so supersampling cannot be what
    distinguishes them there.
    """
    width, height = size
    track = [0.9] * (3 * SLOT_RATE)
    ratios = {}
    for layout in ("scroll", "flow"):
        look = Look(layout=layout, width=width, height=height, bars=31).with_theme("Violet")
        frame = make_frame(track, SLOT_RATE, 1.5, look)
        ratios[layout] = tuple(ink_ratio(frame, a, b) for a, b in ((0, 0.15), (0.45, 0.55), (0.85, 1)))

    left, centre, right = ratios["scroll"]
    assert left < centre - 0.1 and centre < right - 0.1, ratios["scroll"]

    left, centre, right = ratios["flow"]
    assert abs(left - right) < 0.05, ratios["flow"]
    assert centre < left - 0.2, ratios["flow"]


@pytest.mark.parametrize("size", [(320, 80), (3840, 360)])
def test_layouts_mutually_distinct(synthetic_analysis, size) -> None:
    """With a gradient Look every layout produces its own frame (no layout is an alias)."""
    analysis = synthetic_analysis
    frames = {}
    for layout in LAYOUTS:
        look = Look(layout=layout, width=size[0], height=size[1], bars=31).with_theme("Violet")
        track = shape_track(analysis, look)
        frames[layout] = make_frame(track, analysis.slot_rate, 1.5, look).tobytes()
    for first, second in itertools.combinations(LAYOUTS, 2):
        assert frames[first] != frames[second], (first, second)


@pytest.mark.parametrize("size", [(1080, 180), (320, 80)])
@pytest.mark.parametrize("style", ["bars", "line"])
@pytest.mark.parametrize("layout", LAYOUTS)
def test_frames_are_valid_premultiplied(synthetic_analysis, layout, style, size) -> None:
    """No pixel may be brighter than its alpha (the frames are premultiplied)."""
    width, height = (1080, 1080) if layout == "radial" and size[0] == 1080 else size
    look = Look(style=style, layout=layout, width=width, height=height,
                bars=31).with_theme("Red")
    track = shape_track(synthetic_analysis, look)
    frame = make_frame(track, synthetic_analysis.slot_rate, 1.5, look)
    red, green, blue, alpha = frame.split()
    brightest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    excess = ImageChops.subtract(brightest, alpha)
    assert excess.getextrema()[1] == 0, (layout, style, size)


def test_ripple_levels() -> None:
    """The centre ripple bar must visibly follow the sine wave when the audio is
    loud (t=0 -> sin(0)=0; t=0.65 -> sin near its peak)."""
    look = Look(layout="ripple", bars=31)
    track = [0.9] * (2 * SLOT_RATE)
    levels = bar_levels(track, SLOT_RATE, 0.0, look)
    center = (look.bars - 1) // 2
    level0 = levels[center]
    level65 = bar_levels(track, SLOT_RATE, 0.65, look)[center]
    assert abs(level65 - level0) > 0.1, (level0, level65)


def test_mirror_reflect(synthetic_analysis) -> None:
    """The reflection must exist below the 62% line and fade (alpha decreases downward)."""
    analysis = synthetic_analysis
    track = shape_track(analysis, Look(smoothing=0.0))

    reflect_look = Look(layout="mirror-reflect", style="bars", width=320, height=80, bars=31)
    frame = make_frame(track, analysis.slot_rate, 1.5, reflect_look)
    alpha = frame.getchannel("A")

    baseline = int(reflect_look.height * 0.62)
    below_bbox = alpha.crop((0, baseline, reflect_look.width, reflect_look.height)).getbbox()
    assert below_bbox, "no reflection pixels below the baseline"

    reflect_h = max(1, round(baseline * 0.55))
    _, near_baseline = alpha.crop((0, baseline, reflect_look.width, baseline + 1)).getextrema()
    _, bottom_edge = alpha.crop(
        (0, baseline + reflect_h - 1, reflect_look.width, baseline + reflect_h)).getextrema()
    assert near_baseline > bottom_edge, (near_baseline, bottom_edge)


def test_peakhold_stateless(synthetic_analysis) -> None:
    """The peak-hold cap has no memory between frames: same t -> same bytes."""
    analysis = synthetic_analysis
    look = Look(layout="peakhold", width=320, height=80, bars=31, smoothing=0.0)
    track = shape_track(analysis, look)
    frame_a = make_frame(track, analysis.slot_rate, 2.05, look)
    frame_b = make_frame(track, analysis.slot_rate, 2.05, look)
    assert frame_a.tobytes() == frame_b.tobytes(), "peakhold must be stateless"


def test_peakhold_cap_above_bar(synthetic_analysis) -> None:
    """Shortly after a loud -> quiet transition the cap hangs above the centre bar's top."""
    analysis = synthetic_analysis
    look = Look(layout="peakhold", width=320, height=80, bars=31, smoothing=0.0)
    track = shape_track(analysis, look)
    t = 2.05  # 0.05 s after the drop from loud (1-2 s) to quiet (2-3 s)

    geom = _geometry_bottom(look)
    levels = bar_levels(track, analysis.slot_rate, t, look)
    center = (look.bars - 1) // 2
    h_current = geom.min_bar + levels[center] * (geom.max_bar - geom.min_bar)
    y_top_bar = geom.baseline - h_current

    frame = make_frame(track, analysis.slot_rate, t, look)
    alpha = frame.getchannel("A")
    x = int(geom.left + center * geom.spacing)
    above = alpha.crop((x, 0, x + 1, int(y_top_bar))).getbbox()
    assert above, "no peak-hold cap above the current bar"


def test_neon_glow(synthetic_analysis) -> None:
    """The neon line is non-empty and has blurred glow pixels around the sharp line."""
    analysis = synthetic_analysis
    look = Look(layout="neon", width=320, height=80, bars=31, smoothing=0.0)
    track = shape_track(analysis, look)
    frame = make_frame(track, analysis.slot_rate, 1.5, look)
    assert frame.mode == "RGBA" and frame.getbbox()
    alpha_bytes = frame.getchannel("A").tobytes()
    assert any(0 < v < 60 for v in alpha_bytes), "no trace of the glow blur"


def test_radial(synthetic_analysis) -> None:
    """Spokes: symmetric about the frame centre and rotating over time."""
    analysis = synthetic_analysis
    look = Look(layout="radial", width=200, height=200, bars=31, smoothing=0.0)
    track = shape_track(analysis, look)
    frame0 = make_frame(track, analysis.slot_rate, 0.0, look)
    frame3 = make_frame(track, analysis.slot_rate, 3.0, look)
    assert frame0.tobytes() != frame3.tobytes(), "the rotation should change the frame over time"

    bbox = frame0.getbbox()
    assert bbox, "the radial frame must not be empty"
    left, top, right, bottom = bbox
    cx, cy = look.width / 2, look.height / 2
    assert abs((left + right) / 2 - cx) <= 6, "bbox should be roughly centred (x)"
    assert abs((top + bottom) / 2 - cy) <= 6, "bbox should be roughly centred (y)"


def test_stationary_option(synthetic_analysis) -> None:
    """Look.stationary: moving layouts get stationary sampling; the rest are unchanged."""
    analysis = synthetic_analysis
    for layout in ("scroll", "mirror", "flow", "radial"):
        moving = Look(layout=layout, bars=31)
        still = Look(layout=layout, bars=31, stationary=True)
        track = shape_track(analysis, moving)
        t = 1.5  # in the middle of the loud second
        assert (bar_levels(track, analysis.slot_rate, t, moving)
                != bar_levels(track, analysis.slot_rate, t, still)), layout
        # Stationary levels = the amplitude "stationary" branch (like peakhold without spectrum).
        ref = Look(layout="peakhold", bars=31)
        assert (bar_levels(track, analysis.slot_rate, t, still)
                == bar_levels(track, analysis.slot_rate, t, ref)), layout
        # Without spectrum a change of t does not shift the bars sideways: a bar's level
        # depends only on the current moment and its own lag, not on a position on the time axis.
        frame = make_frame(track, analysis.slot_rate, t, still)
        assert frame.size == (still.width, still.height)
    for layout in ("pulse", "ripple", "neon", "mirror-reflect", "peakhold"):
        look = Look(layout=layout, bars=31)
        track = shape_track(analysis, look)
        assert bar_levels(track, analysis.slot_rate, 1.0, look) == bar_levels(
            track, analysis.slot_rate, 1.0, Look(layout=layout, bars=31, stationary=True)), layout


@requires_numpy
def test_spectrum_bands() -> None:
    """Two-tone signal: stationary layouts with spectrum must show bass on the left and
    treble on the right, like a DAW spectrum analyser."""
    spectrum = two_tone_spectrum()

    look = Look(layout="peakhold", bars=61, smoothing=0.0)
    bars = look.bars
    lo_idx, hi_idx = bars // 4, bars * 3 // 4

    levels_low = bar_levels([], SLOT_RATE, 0.5, look, spectrum=spectrum)
    assert sum(levels_low[:lo_idx]) > sum(levels_low[hi_idx:]), "100 Hz: bass (left) > treble (right)"

    levels_high = bar_levels([], SLOT_RATE, 2.5, look, spectrum=spectrum)
    assert sum(levels_high[hi_idx:]) > sum(levels_high[:lo_idx]), "5 kHz: treble (right) > bass (left)"

    for layout in ("peakhold", "neon"):
        frame = make_frame([], SLOT_RATE, 0.5,
                           Look(layout=layout, width=320, height=80, bars=31, smoothing=0.0),
                           spectrum=spectrum)
        assert frame.mode == "RGBA" and frame.getbbox(), (layout, "spectrum: the frame must not be empty")


@requires_numpy
def test_spectrum_silence() -> None:
    """2 s of digital silence (all zeros) must not give a maxed-out spectrum: a band with
    no dynamics over time must be clipped to 0, not inverted to 255."""
    samples = array.array("f", [0.0]) * int(2.0 * ANALYSIS_RATE)
    data, _rate, bands = _compute_spectrum_from_samples(samples)
    assert data is not None and bands == SPECTRUM_BANDS
    assert all(v == 0 for v in data), "silence must give a flat spectrum at 0, not 255"


def _alpha_height(frame) -> int:
    """Vertical extent of the frame's opaque pixels (0 = empty)."""
    bbox = frame.getchannel("A").getbbox()
    return bbox[3] - bbox[1] if bbox else 0


def test_voiceline_deterministic(synthetic_analysis) -> None:
    """The voice line is stateless: same t -> same bytes, and a preview window = full render."""
    analysis = synthetic_analysis
    look = Look(layout="voiceline", width=320, height=80)
    track = shape_track(analysis, look)
    frame_a = make_frame(track, analysis.slot_rate, 2.2, look)
    frame_b = make_frame(track, analysis.slot_rate, 2.2, look)
    assert frame_a.mode == "RGBA" and frame_a.tobytes() == frame_b.tobytes(), "voiceline must be stateless"

    start, end = 300, 500   # a window like the app preview (with run-up from the file start)
    window = shape_track(analysis, look, start, end)
    frame_w = make_frame(window, analysis.slot_rate, 2.2, look, track_offset=start)
    assert frame_w.tobytes() == frame_a.tobytes(), "the preview window must give the same frame"


@requires_pinned_env
def test_voiceline_golden_hash(synthetic_analysis) -> None:
    analysis = synthetic_analysis
    look = Look(layout="voiceline", width=320, height=80)
    track = shape_track(analysis, look)
    frame = make_frame(track, analysis.slot_rate, 2.2, look)
    assert short_hash(frame) == GOLDEN_VOICELINE


def test_voiceline_silence_flat(synthetic_analysis) -> None:
    """In silence the line is almost flat but still drifts slightly (Drift 0 -> it stands still)."""
    analysis = synthetic_analysis
    look = Look(layout="voiceline", width=320, height=80)
    track = shape_track(analysis, look)
    quiet = make_frame(track, analysis.slot_rate, 0.6, look)
    assert quiet.getbbox(), "the line must stay visible in silence"
    assert _alpha_height(quiet) <= 0.1 * look.height, _alpha_height(quiet)
    later = make_frame(track, analysis.slot_rate, 0.9, look)
    assert quiet.tobytes() != later.tobytes(), "in silence the line must drift slightly"
    still = replace(look, drift=0.0)
    assert (make_frame(track, analysis.slot_rate, 0.6, still).tobytes()
            == make_frame(track, analysis.slot_rate, 0.9, still).tobytes()), "Drift 0: the shape stands still"


def test_voiceline_loud_amplitude(synthetic_analysis) -> None:
    """A loud part has a clearly larger amplitude than a quiet one, quiet > silence."""
    analysis = synthetic_analysis
    look = Look(layout="voiceline", width=320, height=80)
    track = shape_track(analysis, look)

    def swing(t: float) -> float:
        return max(abs(y - look.height / 2) for _, y in voiceline_points(track, analysis.slot_rate, t, look))

    quiet, loud, soft = swing(0.6), swing(1.5), swing(2.8)
    assert loud > 2.0 * soft > 2.0 * quiet, (quiet, loud, soft)
    assert loud <= look.height / 2, "the line must not leave the frame"
    assert (_alpha_height(make_frame(track, analysis.slot_rate, 1.5, look))
            > 2 * _alpha_height(make_frame(track, analysis.slot_rate, 0.6, look)))


def test_voiceline_echo(synthetic_analysis) -> None:
    """Reactivity > 0: right after a loud -> quiet transition the line's sides remember the
    loud part (the voice spreads outward from the centre); Reactivity 0 -> one level along the line."""
    analysis = synthetic_analysis
    look = Look(layout="voiceline", reactivity=1.0)
    track = shape_track(analysis, look)
    centre, edge = _voiceline_envelope(track, analysis.slot_rate, 2.5, look, 0, [0.5, 0.1])
    assert edge > centre + 0.2, (centre, edge)
    flat = _voiceline_envelope(track, analysis.slot_rate, 2.5, replace(look, reactivity=0.0), 0, [0.5, 0.1])
    assert flat[0] == flat[1]


def test_voiceline_reference_formula(synthetic_analysis) -> None:
    """Without spectrum (the no-numpy path) and at Reactivity 0 the line is exactly the
    reference formula: 3 sines 0.55/0.30/0.15, amplitude (0.04 + 0.96*level)*0.42 H,
    taper sin(pi*u)^1.6."""
    analysis = synthetic_analysis
    look = Look(layout="voiceline", width=320, height=80, reactivity=0.0)
    track = shape_track(analysis, look)
    assert voiceline_weights(None, 1.5, Look(layout="voiceline")) == tuple(w for _, _, w in VOICE_WAVES)
    t = 1.7
    env = track[int(t * analysis.slot_rate)]
    left, _right, usable = _geometry_common(look)[:3]
    pts = voiceline_points(track, analysis.slot_rate, t, look)
    for i, (x, y) in enumerate(pts):
        u = i / (len(pts) - 1)
        ref = (math.sin(2 * math.pi * (u * 3.0 - t * 0.45)) * 0.55
               + math.sin(2 * math.pi * (u * 7.3 + t * 0.8)) * 0.30
               + math.sin(2 * math.pi * (u * 13.1 - t * 1.7)) * 0.15)
        ref *= (0.04 + 0.96 * env) * look.height * 0.42 * math.sin(math.pi * u) ** 1.6
        assert abs(x - (left + u * usable)) < 1e-9 and abs(y - (look.height / 2 + ref)) < 1e-6, i


@requires_numpy
def test_voiceline_spectrum_shape() -> None:
    """With spectrum (numpy) the shape follows the timbre: 100 Hz -> the long wave dominates,
    5 kHz -> the fine one; the weights still sum to 1."""
    spectrum = two_tone_spectrum()
    base = tuple(w for _, _, w in VOICE_WAVES)

    look = Look(layout="voiceline", reactivity=1.0)
    low = voiceline_weights(spectrum, 0.5, look)
    high = voiceline_weights(spectrum, 2.5, look)
    assert low[0] > base[0] and low[2] < base[2], low
    assert high[2] > base[2] and high[2] > low[2], high
    assert abs(sum(low) - 1.0) < 1e-9 and abs(sum(high) - 1.0) < 1e-9
    assert voiceline_weights(spectrum, 0.5, replace(look, reactivity=0.0)) == base

    small = Look(layout="voiceline", width=320, height=80)
    track = [0.8] * (3 * SLOT_RATE)
    with_spectrum = make_frame(track, SLOT_RATE, 0.5, small, spectrum=spectrum)
    without = make_frame(track, SLOT_RATE, 0.5, small)
    assert with_spectrum.getbbox() and with_spectrum.tobytes() != without.tobytes()
    ref = replace(small, reactivity=0.0)
    assert (make_frame(track, SLOT_RATE, 0.5, ref, spectrum=spectrum).tobytes()
            == make_frame(track, SLOT_RATE, 0.5, ref).tobytes()), "Reactivity 0 does not depend on numpy"


@requires_ffmpeg
def test_analyze_audio_with_ffmpeg(tmp_path) -> None:
    """End to end through FFmpeg: a 1 s sine at amplitude 0.5 gives 1 s and a peak near 0.5."""
    path = tmp_path / "tone.wav"
    rate = 24000
    frames = b"".join(
        int(0.5 * 32767 * math.sin(2 * math.pi * 440 * i / rate)).to_bytes(2, "little", signed=True)
        for i in range(rate)
    )
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)
    analysis = analyze_audio(path)
    assert abs(analysis.duration - 1.0) < 0.02, analysis.duration
    assert abs(max(analysis.peak) - 0.5) < 0.02, max(analysis.peak)
