# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""
Audio-reactive waveform renderer: draws the real shape of the audio, not a
generic animation.

The engine analyses audio at 5 ms resolution (200 slots per second) and keeps,
for every slot, the PEAK (as in a DAW) and the RMS (loudness). Each bar on
screen corresponds to a different slice of time, so the strip is a true plot of
the sound: silence is flat, syllables show up as separate peaks, and loud
passages are visibly taller than quiet ones.

When numpy is available, a coarse frequency spectrum (48 log-spaced bands) is
computed as well and drives the spectrum-based layouts.

Output: a transparent ProRes 4444 MOV, ready for video editors such as
DaVinci Resolve.

Requirements: Pillow, and FFmpeg with the prores_ks encoder (numpy is optional).
Rendering writes to a temporary file next to the output and only replaces the
output once FFmpeg has finished successfully.
"""

from __future__ import annotations

import array
import bisect
import collections
import itertools
import math
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from .ffmpeg_tools import INPUT_WHITELISTS, FFmpegNotFoundError, find_ffmpeg, find_ffprobe, subprocess_flags

try:
    import numpy as np
except ImportError:
    np = None


ANALYSIS_RATE = 24000          # Hz - enough for the envelope of voice and music
SLOT_RATE = 200                # slots per second (5 ms)
SLOT = ANALYSIS_RATE // SLOT_RATE

# Spectral analysis (FFT) - optional, requires numpy. Without numpy,
# Analysis.spectrum stays None and stationary layouts fall back to lagged
# amplitude sampling.
SPECTRUM_RATE = 50             # spectrum frames per second
SPECTRUM_BANDS = 48            # frequency bands (log-spaced)
SPECTRUM_FFT = 1024            # FFT window size (Hann)
SPECTRUM_FMIN = 45.0           # Hz - lower edge of the bands
SPECTRUM_FMAX = 10000.0        # Hz - upper edge of the bands
SPECTRUM_HOP = ANALYSIS_RATE // SPECTRUM_RATE

METRICS = ("peak", "rms")
LAYOUTS = ("scroll", "mirror", "pulse", "flow", "ripple", "mirror-reflect",
           "peakhold", "neon", "radial", "voiceline")
STYLES = ("bars", "line", "area", "dots")

# Layouts with stationary sampling: the bars stay in place and pulse to the
# current audio (no sideways motion), instead of travelling sideways like scroll.
STATIONARY_LAYOUTS = ("peakhold", "mirror-reflect", "neon")
STATIONARY_LAG = 0.25          # fraction of window_seconds - how far back a bar reaches
# "Moving" layouts (time travels sideways) that can opt in to a stationary mode
# (Look.stationary=True): same drawing/gradient/colors, but the bars stay in
# place and pulse to the current audio - no sideways travel.
STATIONARY_OPTIONAL_LAYOUTS = ("scroll", "mirror", "flow", "radial")

# Constants of the "ripple" layout - a pulse of waves spreading from the strip's center.
RIPPLE_OMEGA = 2.4
RIPPLE_WAVE = 0.42
RIPPLE_DECAY = 0.05
RIPPLE_FLOOR = 0.08
RIPPLE_REF_BARS = 44

# Constants of the "peakhold" layout - a deterministic, stateless peak hold.
PEAK_FALL = 0.3            # cap fall per second, in level units 0..1
PEAK_LOOKBACK = 1.2        # seconds of history taken into account
PEAK_CAP_ALPHA = 0.85
PEAK_CAP_COLOR = (236, 234, 251)

# Constants of the "radial" layout (Orbit) - fractions of min(width, height).
RADIAL_SPOKES_MIN = 36
RADIAL_ROT_DEG_PER_S = 14.0
RADIAL_BASE_R = 25 / 180
RADIAL_LEN_MIN = 6 / 180
RADIAL_LEN_SPAN = 27 / 180
RADIAL_RING_D = 38 / 180

# Constants of the "mirror-reflect" layout (Reflection) - baseline and the mirrored reflection below it.
REFLECT_BASELINE = 0.62
REFLECT_SCALE = 0.55
REFLECT_ALPHA = 0.28

# Constants of the "neon" layout - the glowing line and its halo.
NEON_LINE_W = 1.6 / 180
NEON_GLOW_ALPHA = 0.35
NEON_GLOW_BLUR = 0.03

# Constants of the "voiceline" layout (Voice line) - a thin, calm, oscilloscope-like line.
# Three drifting sine waves: (periods across the width, phase speed in periods/s, base weight).
VOICE_WAVES = ((3.0, -0.45, 0.55), (7.3, 0.8, 0.30), (13.1, -1.7, 0.15))
VOICE_POINTS_MIN = 160         # line points (wide frames: one point every ~4 px)
VOICE_AMP = 0.42 / 0.72        # x height x max_height_ratio -> 0.42 H at the default Height
VOICE_AMP_FLOOR = 0.04         # in silence the line is almost flat, but still drifts
VOICE_TAPER = 1.6              # sin(pi*u)^1.6 - the ends are pinned to the center
VOICE_EDGE_FADE = 0.06         # fraction of the width over which the line ends fade out (alpha)
VOICE_ATTACK = (0.04, 0.12)    # s: base + Smoothing x range (0.25 -> 70 ms)
VOICE_RELEASE = (0.20, 0.60)   # s: (0.25 -> 350 ms) - as in the reference formula
VOICE_BAND_SPLITS = (80.0, 300.0, 2000.0)  # Hz: low / mid / high of the voice (up to SPECTRUM_FMAX)
VOICE_BAND_WINDOW = (0.12, 0.48)  # s: smoothing of the band energies (Smoothing 0.25 -> 0.24 s)
VOICE_TIMBRE_EPS = 0.15        # sine weight ~ base x ((energy + eps) / (mean + eps))^gamma -
VOICE_TIMBRE_GAMMA = 1.5       # what counts is the band balance (timbre), not loudness; silence -> base weights
VOICE_SUPERSAMPLE = 3          # the line mask is drawn 3x and reduced with a box filter
VOICE_SUPERSAMPLE_MAX_PIXELS = 1_000_000  # above this it drops to 2x, to stay within the frame budget

# Supersampling for the six newer layouts (flow/ripple/mirror-reflect/peakhold/neon/
# radial): the frame is rendered internally at SUPERSAMPLE x the resolution, then
# reduced with LANCZOS - smoother bar and line edges. scroll/mirror/pulse render
# unchanged, so their output stays byte-identical.
# The threshold is computed from the area (not the width alone), because the cost of
# LANCZOS grows with the pixel count - for square frames (radial at 1080x1080) a width
# limit of <=1920 alone does not protect the 33 ms/frame budget, as profiling showed.
SUPERSAMPLE = 2
SUPERSAMPLE_MAX_PIXELS = 900_000  # above this we render at 1x, to stay within the frame budget

# Themes: (name, primary color, secondary color for the gradient)
THEMES: tuple[tuple[str, str, str], ...] = (
    ("Violet", "#8B4DFF", "#D55ECF"),
    ("Lavender", "#A78BFA", "#F0ABFC"),
    ("Magenta", "#E040A0", "#FF7AB6"),
    ("Pink", "#FF5C8A", "#FFB3C6"),
    ("Red", "#FF3B3B", "#FF8A5B"),
    ("Crimson", "#DC143C", "#FF4D6D"),
    ("Ruby", "#C8102E", "#FF3B5C"),
    ("Burgundy", "#8E1B2F", "#D64545"),
    ("Coral", "#FF6B6B", "#FFA07A"),
    ("Orange", "#FF7A18", "#FFB84D"),
    ("Gold", "#F5C242", "#FFE29A"),
    ("Lime", "#B4F04A", "#E3FF9C"),
    ("Green", "#2ECC71", "#A8FF78"),
    ("Mint", "#2DD4BF", "#99F6E4"),
    ("Cyan", "#22D3EE", "#8BE9FD"),
    ("Blue", "#3B82F6", "#93C5FD"),
    ("White", "#FFFFFF", "#B8B8CC"),
    ("Graphite", "#3A3A46", "#8A8AA0"),
)


_HEX_COLOR = re.compile(r"#?[0-9a-fA-F]{6}")


def parse_hex(value: str) -> tuple[int, int, int]:
    """Parses #RRGGBB (the # is optional); raises ValueError for anything else."""
    if not _HEX_COLOR.fullmatch(value):
        raise ValueError("Give the color as #RRGGBB, e.g. #8B4DFF")
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def theme_colors(name: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    wanted = name.strip().casefold()
    for theme_name, primary, secondary in THEMES:
        if theme_name.casefold() == wanted:
            return parse_hex(primary), parse_hex(secondary)
    raise KeyError(name)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

@dataclass
class Analysis:
    """Peak and RMS for every 5 ms slot of audio."""

    peak: array.array
    rms: array.array
    slot_rate: int = SLOT_RATE
    peak_floor: float = 0.0
    peak_ref: float = 1.0
    rms_floor: float = 0.0
    rms_ref: float = 1.0
    spectrum: array.array | None = None    # uint8, flattened [frame][band], 0..255; None without numpy
    spectrum_rate: int = SPECTRUM_RATE
    spectrum_bands: int = SPECTRUM_BANDS

    @property
    def slots(self) -> int:
        return len(self.peak)

    @property
    def duration(self) -> float:
        return self.slots / self.slot_rate

    def frames(self, fps: int) -> int:
        return max(1, math.ceil(self.duration * fps))


def percentile(values, q: float) -> float:
    if not len(values):
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return ordered[idx]


PROBE_TIMEOUT = 30.0           # seconds ffprobe may take before the duration is given up on
STDERR_TAIL_BYTES = 8192       # how much of FFmpeg's stderr is kept for error messages


class _StderrDrain:
    """Reads a process's stderr on a daemon thread so a full pipe can never block FFmpeg.

    Only the last STDERR_TAIL_BYTES are kept, which is where the error message is.
    """

    def __init__(self, stream) -> None:
        self._tail: collections.deque[bytes] = collections.deque()
        self._size = 0
        self._thread = threading.Thread(target=self._run, args=(stream,), daemon=True)
        self._thread.start()

    def _run(self, stream) -> None:
        try:
            while chunk := stream.read(4096):
                self._tail.append(chunk)
                self._size += len(chunk)
                while self._size - len(self._tail[0]) >= STDERR_TAIL_BYTES:
                    self._size -= len(self._tail.popleft())
        except (OSError, ValueError):
            pass

    def text(self) -> str:
        """Waits for the stream to end and returns the kept tail."""
        self._thread.join()
        return b"".join(self._tail)[-STDERR_TAIL_BYTES:].decode(errors="replace").strip()


class Cancelled(Exception):
    """Raised when the caller's cancel event is set during analysis or rendering."""


def _kill(proc: subprocess.Popen) -> None:
    """Stops a child process and reaps it, closing our ends of its pipes."""
    if proc.poll() is None:
        proc.kill()
    for stream in (proc.stdin, proc.stdout):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    proc.wait()


def _remove_file(path: Path) -> None:
    """Deletes a file, retrying briefly: Windows keeps it locked until the writer has exited."""
    for attempt in range(20):
        try:
            path.unlink(missing_ok=True)
            return
        except OSError:
            if attempt == 19:
                return
            time.sleep(0.05)


def probe_duration(audio_path: Path, cancel: threading.Event | None = None) -> float | None:
    """Duration in seconds, or None if ffprobe is missing, fails or takes longer than PROBE_TIMEOUT.

    Raises Cancelled when ``cancel`` is set; ffprobe is killed on cancel and on timeout.
    """
    ffprobe = find_ffprobe()
    if ffprobe is None:
        return None
    cmd = [str(ffprobe), "-v", "error", *INPUT_WHITELISTS,
           "-show_entries", "format=duration", "-of", "csv=p=0", str(Path(audio_path).absolute())]
    # The answer goes to a temp file, not a pipe: a pipe could be held open by a child of a wrapper
    # script, and then neither reading nor closing it would return after the kill.
    with tempfile.TemporaryFile() as answer:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=answer, stderr=subprocess.DEVNULL,
                                    creationflags=subprocess_flags())
        except OSError:
            return None
        waiter = cancel if cancel is not None else threading.Event()
        deadline = time.monotonic() + PROBE_TIMEOUT
        try:
            while proc.poll() is None:
                if waiter.wait(0.05):
                    raise Cancelled
                if time.monotonic() >= deadline:
                    return None
        finally:
            _kill(proc)
        answer.seek(0)
        out = answer.read(4096).decode(errors="replace").strip()
    try:
        return float(out) if out else None
    except ValueError:
        return None


def _zero_non_finite(samples: array.array) -> array.array:
    """A copy of ``samples`` with NaN and infinite values replaced by 0."""
    return array.array("f", [x if math.isfinite(x) else 0.0 for x in samples])


def analyze_audio(audio_path: Path, progress=None, cancel: threading.Event | None = None) -> Analysis:
    """
    Decodes the audio through ffmpeg (mono, 24 kHz) as a stream and computes the
    peak and RMS for every 5 ms slot. The whole file is never held in memory, so
    an hour-long podcast is fine too.

    Raises Cancelled when ``cancel`` is set (checked for every chunk read).
    """
    expected = probe_duration(audio_path, cancel)
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise FFmpegNotFoundError("FFmpeg was not found.")
    cmd = [str(ffmpeg), "-v", "error", *INPUT_WHITELISTS,
           "-i", str(Path(audio_path).absolute()), "-vn", "-ac", "1",
           "-ar", str(ANALYSIS_RATE), "-f", "f32le", "-"]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=subprocess_flags())
    except FileNotFoundError as exc:
        raise FFmpegNotFoundError("FFmpeg was not found.") from exc
    assert proc.stderr is not None
    drain = _StderrDrain(proc.stderr)

    done = False
    try:
        peaks, rmss = array.array("f"), array.array("f")
        slot_bytes = SLOT * 4
        block = slot_bytes * SLOT_RATE * 2  # 2 seconds of audio per read
        carry = b""
        sumprod = math.sumprod

        # Spectrum (optional, numpy only): band power computed in batches per block read,
        # with a ~SPECTRUM_FFT-1 sample carry between blocks (the STFT does not know block boundaries).
        spectrum_frames = [] if np is not None else None
        spec_carry = np.empty(0, dtype=np.float32) if np is not None else None
        band_ranges = _spectrum_band_ranges() if np is not None else None
        hann = _spectrum_hann() if np is not None else None

        assert proc.stdout is not None
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled
            chunk = proc.stdout.read(block)
            if not chunk:
                break
            data = carry + chunk
            usable = len(data) // slot_bytes * slot_bytes
            carry = data[usable:]
            samples = array.array("f")
            samples.frombytes(data[:usable])
            if sys.byteorder != "little":
                samples.byteswap()
            if np is not None:
                chunk_np = np.frombuffer(samples, dtype=np.float32)
                chunk_np[~np.isfinite(chunk_np)] = 0.0     # in place: NaN and Inf count as silence
            for i in range(0, len(samples), SLOT):
                seg = samples[i:i + SLOT]
                energy = sumprod(seg, seg)
                if not math.isfinite(energy):
                    seg = _zero_non_finite(seg)
                    energy = sumprod(seg, seg)
                peaks.append(max(max(seg), -min(seg)))
                rmss.append(math.sqrt(energy / SLOT))
            if np is not None:
                band_powers, spec_carry = _stft_band_powers(chunk_np, spec_carry, band_ranges, hann)
                if band_powers.shape[0]:
                    spectrum_frames.append(band_powers)
            if progress and expected:
                progress(min(0.99, len(peaks) / SLOT_RATE / expected))
        if len(carry) >= 4:
            samples = array.array("f")
            samples.frombytes(carry[:len(carry) // 4 * 4])
            if sys.byteorder != "little":
                samples.byteswap()
            energy = sumprod(samples, samples)
            if not math.isfinite(energy):
                samples = _zero_non_finite(samples)
                energy = sumprod(samples, samples)
            peaks.append(max(max(samples), -min(samples)))
            rmss.append(math.sqrt(energy / len(samples)))
        stderr = drain.text()
        proc.wait()
        if proc.returncode != 0 or not peaks:
            raise RuntimeError(stderr or "Could not read the audio.")
        done = True
    finally:
        if not done:
            _kill(proc)

    analysis = Analysis(peaks, rmss)
    _calibrate(analysis)
    if spectrum_frames:
        band_powers_full = np.concatenate(spectrum_frames, axis=0)
        analysis.spectrum, analysis.spectrum_rate, analysis.spectrum_bands = _quantize_spectrum(band_powers_full)
    if progress:
        progress(1.0)
    return analysis


def _calibrate(analysis: Analysis) -> None:
    """
    Reference point = nearly the loudest slots (98.5th percentile), so the
    normalisation does not depend on a single click or pop. Floor = quiet
    background noise: everything below it is drawn truly flat.
    """
    for name in ("peak", "rms"):
        values = getattr(analysis, name)
        ref = max(1e-4, percentile(values, 0.985))
        floor = min(percentile(values, 0.08) * 1.6, ref * 0.18)
        setattr(analysis, f"{name}_ref", ref)
        setattr(analysis, f"{name}_floor", floor)


# ---------------------------------------------------------------------------
# Spectral analysis (FFT) - optional, numpy only
# ---------------------------------------------------------------------------

@dataclass
class Spectrum:
    """Frequency spectrum: flattened [frame][band] data (uint8, 0..255).
    Indexing is ABSOLUTE in time (no window/offset) - the structure is small
    and is passed to make_frame as a whole."""

    data: array.array
    rate: int
    bands: int


_spectrum_band_ranges_cache: list[tuple[int, int]] | None = None
_spectrum_hann_cache = None


def _spectrum_band_ranges() -> list[tuple[int, int]]:
    """Log-spaced edges of the 48 bands, 45 Hz-10 kHz, mapped to rfft bins
    (SPECTRUM_FFT @ ANALYSIS_RATE). Bands without bins of their own get the
    nearest neighbouring bin (by forcing hi = lo + 1), so no band is empty."""
    global _spectrum_band_ranges_cache
    if _spectrum_band_ranges_cache is not None:
        return _spectrum_band_ranges_cache
    n_bins = SPECTRUM_FFT // 2 + 1
    freqs = [k * ANALYSIS_RATE / SPECTRUM_FFT for k in range(n_bins)]
    edges = [SPECTRUM_FMIN * (SPECTRUM_FMAX / SPECTRUM_FMIN) ** (i / SPECTRUM_BANDS)
             for i in range(SPECTRUM_BANDS + 1)]
    ranges = []
    for i in range(SPECTRUM_BANDS):
        lo = bisect.bisect_left(freqs, edges[i])
        hi = bisect.bisect_left(freqs, edges[i + 1])
        if hi <= lo:
            hi = lo + 1
        lo = min(lo, n_bins - 1)
        hi = min(max(hi, lo + 1), n_bins)
        ranges.append((lo, hi))
    _spectrum_band_ranges_cache = ranges
    return ranges


def _spectrum_hann():
    global _spectrum_hann_cache
    if _spectrum_hann_cache is None:
        _spectrum_hann_cache = np.hanning(SPECTRUM_FFT).astype(np.float32)
    return _spectrum_hann_cache


def _stft_band_powers(chunk, carry, band_ranges, hann):
    """Computes band power for every full window (SPECTRUM_FFT, hop=SPECTRUM_HOP)
    that fits in carry+chunk. Returns (band_power[frame][band] float32, new carry
    - the tail of samples after the last full window, to be prepended on the next call)."""
    combined = np.concatenate((carry, chunk)) if len(carry) else chunk
    n = len(combined)
    if n < SPECTRUM_FFT:
        return np.empty((0, len(band_ranges)), dtype=np.float32), combined
    num_windows = (n - SPECTRUM_FFT) // SPECTRUM_HOP + 1
    frames = np.lib.stride_tricks.sliding_window_view(combined, SPECTRUM_FFT)[::SPECTRUM_HOP][:num_windows]
    power = np.abs(np.fft.rfft(frames * hann, axis=1)) ** 2
    bands = np.empty((num_windows, len(band_ranges)), dtype=np.float32)
    for i, (lo, hi) in enumerate(band_ranges):
        bands[:, i] = power[:, lo:hi].mean(axis=1)
    new_carry = combined[num_windows * SPECTRUM_HOP:]
    return bands.astype(np.float32), new_carry


def _quantize_spectrum(band_powers) -> tuple[array.array | None, int, int]:
    """Per-band calibration - like _calibrate, but in the log domain (dB), because FFT
    power spans many orders of magnitude: ref = 98.5th percentile, floor = adjusted
    8th percentile, cap relative to ref (the same factors as _calibrate, carried over
    to dB as additive offsets: x1.6 -> +10*log10(1.6), x0.18 -> +10*log10(0.18)).
    Bands with no dynamics over time (digital silence, a steady tone) have a raw
    ref-floor_pct spread close to zero; a forced floor margin would then artificially
    inflate the span and yield level~1.0 (an inverted result). Such "dead" bands
    (raw_span < 2 dB) are clipped to level 0."""
    if band_powers.shape[0] == 0:
        return None, SPECTRUM_RATE, SPECTRUM_BANDS
    bands = band_powers.shape[1]
    db = (10.0 * np.log10(band_powers + np.float32(1e-12))).astype(np.float32)
    ref = np.percentile(db, 98.5, axis=0)
    floor_pct = np.percentile(db, 8.0, axis=0)
    dead = (ref - floor_pct) < 2.0
    floor = np.minimum(floor_pct + 10.0 * math.log10(1.6), ref + 10.0 * math.log10(0.18))
    span = np.maximum(ref - floor, 1e-6)
    level = np.clip((db - floor) / span, 0.0, 1.0)
    level[:, dead] = 0.0
    quant = np.round(level * 255).astype(np.uint8)
    spectrum_arr = array.array("B")
    spectrum_arr.frombytes(quant.tobytes())
    return spectrum_arr, SPECTRUM_RATE, bands


def _compute_spectrum_from_samples(samples) -> tuple[array.array | None, int, int]:
    """Computes the spectrum from a ready array of mono samples @ ANALYSIS_RATE (no ffmpeg) -
    the same computation path as analyze_audio (_stft_band_powers + _quantize_spectrum), only
    called once on the whole array instead of per block. Used by tests."""
    if np is None:
        return None, SPECTRUM_RATE, SPECTRUM_BANDS
    arr = np.asarray(samples, dtype=np.float32)
    band_powers, _carry = _stft_band_powers(
        arr, np.empty(0, dtype=np.float32), _spectrum_band_ranges(), _spectrum_hann())
    return _quantize_spectrum(band_powers)


# ---------------------------------------------------------------------------
# Signal shaping (Sensitivity, Contrast, Smoothing)
# ---------------------------------------------------------------------------

# Allowed (min, max) per Look field, inclusive. The CLI validates and documents its
# options from this table, and so can any UI.
LOOK_LIMITS: dict[str, tuple[float, float]] = {
    "width": (16, 8192),
    "height": (16, 8192),
    "fps": (1, 120),
    "bars": (8, 240),
    "window_seconds": (0.2, 6.0),
    "gain": (0.3, 2.5),
    "contrast": (0.4, 2.0),
    "smoothing": (0.0, 1.0),
    "opacity": (1, 255),
    "max_height_ratio": (0.15, 0.98),
    "reactivity": (0.0, 1.0),
    "drift": (0.0, 2.0),
    "line_width": (1.0, 6.0),
}


@dataclass
class Look:
    """Everything that decides the appearance - shared by preview, export and the CLI."""

    width: int = 1080
    height: int = 180
    fps: int = 30
    bars: int = 61
    style: str = "bars"          # bars | line | area | dots
    layout: str = "scroll"       # scroll | mirror | pulse | flow | ripple | mirror-reflect |
                                  # peakhold | neon | radial | voiceline
    window_seconds: float = 1.05
    metric: str = "peak"         # peak | rms
    gain: float = 1.0            # Sensitivity
    contrast: float = 1.0        # gamma: <1 emphasises quieter parts, >1 emphasises peaks
    smoothing: float = 0.25      # 0 = raw signal, 1 = very smooth
    color: tuple[int, int, int] = (139, 77, 255)
    color2: tuple[int, int, int] | None = None   # None = solid color
    opacity: int = 235
    max_height_ratio: float = 0.72
    baseline_dots: bool = True
    stationary: bool = False      # scroll/mirror/flow/radial: bars stay in place instead of travelling sideways
    px_scale: int = 1             # multiplier for absolute pixel constants when supersampling
    reactivity: float = 0.7       # voiceline: 0 = reference formula (loudness only), 1 = shape fully from the audio
    drift: float = 1.0            # voiceline: multiplier of the sines' drift speed (0 = the shape stands still)
    line_width: float = 2.0       # voiceline: line thickness in px at 1080x180 (scales with the frame)

    def with_theme(self, name: str, gradient: bool = True) -> Look:
        primary, secondary = theme_colors(name)
        return replace(self, color=primary, color2=secondary if gradient else None)


def shape_track(analysis: Analysis, look: Look, start: int = 0, end: int | None = None) -> list[float]:
    """
    Turns the raw slot values into 0..1 levels for drawing:
    silence gate -> normalisation -> sensitivity -> contrast -> attack/release envelope.
    Returns a list for slots [start, end); earlier slots only serve to "warm up"
    the smoothing, so a preview window looks like the full render.
    """
    source = analysis.peak if look.metric == "peak" else analysis.rms
    floor = analysis.peak_floor if look.metric == "peak" else analysis.rms_floor
    ref = analysis.peak_ref if look.metric == "peak" else analysis.rms_ref
    total = len(source)
    end = total if end is None else max(0, min(total, end))
    start = max(0, min(start, end))
    lead = min(start, 2 * analysis.slot_rate)  # 2 s of run-up for the smoothing

    scale = look.gain / max(1e-6, ref - floor)
    gamma = look.contrast
    dt = 1.0 / analysis.slot_rate
    s = max(0.0, min(1.0, look.smoothing))
    if look.layout == "voiceline":
        # The voice line is meant to be calm: the envelope is never raw (Smoothing 0.25 -> 70/350 ms).
        attack = 1.0 - math.exp(-dt / (VOICE_ATTACK[0] + VOICE_ATTACK[1] * s))
        release = 1.0 - math.exp(-dt / (VOICE_RELEASE[0] + VOICE_RELEASE[1] * s))
    else:
        attack = 1.0 - math.exp(-dt / (0.003 + 0.05 * s))
        release = 1.0 - math.exp(-dt / (0.010 + 0.40 * s * s))

    out: list[float] = []
    prev = 0.0
    append = out.append
    for idx in range(start - lead, end):
        v = (source[idx] - floor) * scale
        if v <= 0.0:
            v = 0.0
        elif v >= 1.0:
            v = 1.0
        elif gamma != 1.0:
            v = v ** gamma
        prev += (v - prev) * (attack if v > prev else release)
        if idx >= start:
            append(prev)
    return out


# ---------------------------------------------------------------------------
# Frame drawing
# ---------------------------------------------------------------------------

_gradient_cache: dict = {}
_lut_cache: dict[int, list[int]] = {}


def _color_layer(look: Look, left: int, usable: int, axis: str = "x") -> Image.Image:
    key = (look.width, look.height, look.color, look.color2, look.layout, left, usable, axis)
    cached = _gradient_cache.get(key)
    if cached is not None:
        return cached
    if look.color2 is None:
        layer = Image.new("RGB", (look.width, look.height), look.color)
    elif axis == "y":
        layer = Image.new("RGB", (look.width, look.height), look.color)
        draw = ImageDraw.Draw(layer)
        c1, c2 = look.color, look.color2
        span = max(1, look.height - 1)
        for y in range(look.height):
            p = y / span
            col = tuple(round(c2[i] + (c1[i] - c2[i]) * p) for i in range(3))
            draw.line((0, y, look.width, y), fill=col)
    else:
        layer = Image.new("RGB", (look.width, look.height), look.color)
        draw = ImageDraw.Draw(layer)
        c1, c2 = look.color, look.color2
        span = max(1, usable)
        for x in range(look.width):
            if look.layout != "scroll":
                p = abs((x - left) - span / 2) / (span / 2)
            else:
                p = (x - left) / span
            p = max(0.0, min(1.0, p))
            col = tuple(round(c1[i] + (c2[i] - c1[i]) * p) for i in range(3))
            draw.line((x, 0, x, look.height), fill=col)
    if len(_gradient_cache) > 24:
        _gradient_cache.clear()
    _gradient_cache[key] = layer
    return layer


def _stationary_rand(n: float) -> float:
    """Deterministic pseudo-random in [0,1): frac(sin(n*127.1+13.7)*43758.5453),
    so the same n always gives the same value."""
    v = math.sin(n * 127.1 + 13.7) * 43758.5453
    return v - math.floor(v)


def _spectrum_band_value(data: array.array, bands: int, frames: int, band_idx: int, fr: float) -> float:
    """Value (0..1) of band band_idx at continuous time fr (in spectrum frames),
    linearly interpolated between neighbouring frames; clamped to the available range."""
    fr = max(0.0, min(frames - 1, fr))
    f0 = int(fr)
    f1 = min(frames - 1, f0 + 1)
    frac = fr - f0
    v0 = data[f0 * bands + band_idx]
    v1 = data[f1 * bands + band_idx]
    return (v0 + (v1 - v0) * frac) / 255.0


def _spectrum_bar_levels(spectrum: Spectrum, bars: int, t: float, look: Look) -> list[float]:
    """
    Level of each bar = a frequency band (bass on the left -> treble on the right), not
    a sample in time. pos = i/(bars-1) -> band f = pos*(bands-1), linearly interpolated
    between neighbouring bands. Temporal smoothing is deterministic (a weighted average
    of the last k frames, weights falling off linearly), so scrubbing and export
    produce identical frames.
    """
    data, rate, bands = spectrum.data, spectrum.rate, spectrum.bands
    frames = len(data) // bands
    if frames == 0:
        return [0.0] * bars
    k = 1 + round(max(0.0, min(1.0, look.smoothing)) * 8)
    weights = [k - j for j in range(k)]
    wsum = sum(weights)

    levels: list[float] = []
    for i in range(bars):
        pos = i / (bars - 1) if bars > 1 else 0.0
        f = pos * (bands - 1)
        b0 = int(f)
        b1 = min(bands - 1, b0 + 1)
        bfrac = f - b0
        acc = 0.0
        for j, w in enumerate(weights):
            fr = t * rate - j
            v0 = _spectrum_band_value(data, bands, frames, b0, fr)
            v1 = _spectrum_band_value(data, bands, frames, b1, fr)
            acc += (v0 + (v1 - v0) * bfrac) * w
        v = acc / wsum
        level = max(0.0, min(1.0, look.gain * v))
        if look.contrast != 1.0:
            level = level ** look.contrast
        levels.append(level)
    return levels


def is_stationary(look: Look) -> bool:
    """Whether the bars should stay in place ("stationary" sampling) for this Look."""
    return look.layout in STATIONARY_LAYOUTS or (
        look.stationary and look.layout in STATIONARY_OPTIONAL_LAYOUTS)


def bar_levels(track, slot_rate: int, t: float, look: Look, track_offset: int = 0,
               spectrum: Spectrum | None = None) -> list[float]:
    """
    Level of each bar = the maximum of the shaped signal within ITS time slice.
    scroll: left side = earlier, right side = later, center = now.
    mirror: center = now, the sound "spreads" sideways into the past.
    pulse:  bars stay in place and pulse with the current level; the center is
            strongest, the edges weaker and slightly delayed (the time window sets the delay).
    flow:   sampling like scroll (a separate name for the UI; the color is drawn symmetrically).
    ripple: bars stay in place; the current audio level modulates a sine wave
            spreading sideways from the center of the strip.
    mirror-reflect, peakhold, neon: "stationary" sampling - without a spectrum the bars
            stay in place (no sideways motion) and pulse to the current audio. Each
            bar i has a deterministic pseudo-random delay lag_i
            (the _stationary_rand function) and its own gain
            variation, so the bars are decorrelated but do not "travel" sideways.
            With a spectrum (numpy available): the bars show frequency bands
            instead of lag - see _spectrum_bar_levels.
    radial: sampling like scroll (the else branch); drawn by _make_radial_frame
            (the rotation of the spokes is its own animation, the sampling is unchanged).
    Look.stationary=True for scroll/mirror/flow/radial: the same "stationary"
            sampling as above (amplitude-based, no spectrum) - the bars do not travel sideways,
            while the drawing, gradient and playhead stay as in the moving version.
    """
    bars = max(1, look.bars)
    if look.layout in STATIONARY_LAYOUTS and spectrum is not None and spectrum.data:
        return _spectrum_bar_levels(spectrum, bars, t, look)
    total = len(track)
    center = (bars - 1) / 2.0
    stationary = is_stationary(look)
    if stationary:
        span = 1.0 / look.fps
    elif look.layout == "mirror":
        half = look.window_seconds / 2.0
        span = half / max(1.0, center)
    elif look.layout == "pulse":
        lag = look.window_seconds * 0.15
        span = 1.0 / look.fps
    elif look.layout == "ripple":
        if track:
            idx = max(0, min(total - 1, int(t * slot_rate) - track_offset))
            ripple_a = track[idx]
        else:
            ripple_a = 0.0
    else:
        span = look.window_seconds / bars

    levels: list[float] = []
    for i in range(bars):
        if look.layout == "ripple":
            d = abs(i - center) * (RIPPLE_REF_BARS / bars)
            w = max(0.0, math.sin(RIPPLE_OMEGA * t - RIPPLE_WAVE * d))
            level = min(1.0, RIPPLE_FLOOR + w * math.exp(-RIPPLE_DECAY * d) * ripple_a)
            levels.append(level)
            continue
        if stationary:
            lag_i = _stationary_rand(i) * STATIONARY_LAG * look.window_seconds
            t_end = t - lag_i
            t_start = t_end - span
        elif look.layout == "mirror":
            d = abs(i - center) / center if center else 0.0
            t_end = t - d * half
            t_start = t_end - span
        elif look.layout == "pulse":
            d = abs(i - center) / center if center else 0.0
            t_end = t - d * lag
            t_start = t_end - span
        else:
            t_start = t + (i / bars - 0.5) * look.window_seconds
            t_end = t_start + span
        a = math.floor(t_start * slot_rate) - track_offset
        b = math.ceil(t_end * slot_rate) - track_offset
        b = max(a + 1, b)
        a = max(0, a)
        b = min(total, b)
        level = max(track[a:b]) if b > a else 0.0
        if look.layout == "pulse":
            d = abs(i - center) / center if center else 0.0
            level *= 0.12 + 0.88 * (1.0 - d * d) ** 0.9   # gentle "bell" from the center
        elif stationary:
            level *= 0.78 + 0.22 * _stationary_rand(i * 3 + 7)  # per-bar gain variation
            level = max(0.0, min(1.0, level))
        levels.append(level)
    return levels


@dataclass
class _Geometry:
    """Bar dimensions and scale - shared by every style and layout."""

    width: int
    height: int
    baseline: int
    left: int
    right: int
    usable: int
    spacing: float
    bar_w: int
    min_bar: int
    max_bar: int
    radius: int


def _geometry_common(look: Look) -> tuple[int, int, int, float, int, int, int]:
    """Left/right/usable/spacing/bar_w/min_bar/radius - common to every geometry (independent of the baseline).
    Absolute pixel constants (clamps/minimums) are multiplied by look.px_scale so that, with
    supersampling (rendering at 2x and a LANCZOS downscale), a bar has the same thickness
    after reduction as in a 1x render, instead of being cut off by an absolute maximum
    measured in 2x pixels."""
    s = look.px_scale
    width, height = look.width, look.height
    left = int(width * 0.040)
    right = width - left
    usable = right - left
    bars = max(1, look.bars)
    spacing = usable / max(1, bars - 1)
    bar_w = max(2 * s, min(10 * s, round(spacing * 0.5)))
    min_bar = max(2 * s, round(height * 0.012))
    radius = max(1 * s, bar_w // 2)
    return left, right, usable, spacing, bar_w, min_bar, radius


def _geometry(look: Look) -> _Geometry:
    width, height = look.width, look.height
    baseline = height // 2
    left, right, usable, spacing, bar_w, min_bar, radius = _geometry_common(look)
    max_bar = max(min_bar + 2, int(height * look.max_height_ratio))
    return _Geometry(width, height, baseline, left, right, usable, spacing,
                     bar_w, min_bar, max_bar, radius)


def _geometry_reflect(look: Look) -> _Geometry:
    """Geometry of the mirror-reflect layout: baseline at 62% of the height, bars grow upward."""
    width, height = look.width, look.height
    baseline = int(height * REFLECT_BASELINE)
    left, right, usable, spacing, bar_w, min_bar, radius = _geometry_common(look)
    margin = height * 0.04
    available = max(min_bar + 2, baseline - margin)
    max_bar = max(min_bar + 2, int(available * look.max_height_ratio))
    return _Geometry(width, height, baseline, left, right, usable, spacing,
                     bar_w, min_bar, max_bar, radius)


def _geometry_bottom(look: Look) -> _Geometry:
    """Geometry of the peakhold layout: baseline near the bottom edge (margin ~4% of the height), bars grow upward."""
    width, height = look.width, look.height
    margin = max(1 * look.px_scale, round(height * 0.04))
    baseline = height - margin
    left, right, usable, spacing, bar_w, min_bar, radius = _geometry_common(look)
    max_bar = max(min_bar + 2, int(baseline * look.max_height_ratio))
    return _Geometry(width, height, baseline, left, right, usable, spacing,
                     bar_w, min_bar, max_bar, radius)


def _draw_centered_bars(draw: ImageDraw.ImageDraw, levels: list[float], geom: _Geometry, look: Look) -> None:
    """Draws bars/dots/line centered on the baseline, in grayscale."""
    tops: list[tuple[float, float]] = []
    for i, level in enumerate(levels):
        h = geom.min_bar + level * (geom.max_bar - geom.min_bar)
        x = geom.left + i * geom.spacing
        strength = int(255 * (0.70 + 0.30 * level))
        y_top = geom.baseline - h / 2
        y_bottom = geom.baseline + h / 2
        if look.style == "bars":
            draw.rounded_rectangle((x - geom.bar_w / 2, y_top, x + geom.bar_w / 2, y_bottom),
                                   radius=geom.radius, fill=strength)
        elif look.style == "dots":
            r = max(1.5 * look.px_scale, geom.bar_w * 0.55)
            draw.ellipse((x - r, y_top - r, x + r, y_top + r), fill=strength)
            draw.ellipse((x - r, y_bottom - r, x + r, y_bottom + r), fill=strength)
        else:
            tops.append((x, y_top))

    if look.style in ("line", "area") and len(tops) > 1:
        line_w = max(2 * look.px_scale, min(7 * look.px_scale, geom.bar_w))
        bottoms = [(x, geom.baseline + (geom.baseline - y)) for x, y in tops]
        if look.style == "area":
            draw.polygon(tops + bottoms[::-1], fill=150)
        draw.line(tops, fill=255, width=line_w, joint="curve")
        draw.line(bottoms, fill=255, width=line_w, joint="curve")


def _draw_upward_bars(draw: ImageDraw.ImageDraw, levels: list[float], geom: _Geometry, look: Look) -> None:
    """Draws bars/dots/line growing only upward from the baseline, in grayscale."""
    tops: list[tuple[float, float]] = []
    for i, level in enumerate(levels):
        h = geom.min_bar + level * (geom.max_bar - geom.min_bar)
        x = geom.left + i * geom.spacing
        strength = int(255 * (0.70 + 0.30 * level))
        y_top = geom.baseline - h
        if look.style == "bars":
            draw.rounded_rectangle((x - geom.bar_w / 2, y_top, x + geom.bar_w / 2, geom.baseline),
                                   radius=geom.radius, fill=strength)
        elif look.style == "dots":
            r = max(1.5 * look.px_scale, geom.bar_w * 0.55)
            draw.ellipse((x - r, y_top - r, x + r, y_top + r), fill=strength)
            draw.ellipse((x - r, geom.baseline - r, x + r, geom.baseline + r), fill=strength)
        else:
            tops.append((x, y_top))

    if look.style in ("line", "area") and len(tops) > 1:
        line_w = max(2 * look.px_scale, min(7 * look.px_scale, geom.bar_w))
        bottoms = [(x, geom.baseline) for x, _ in tops]
        if look.style == "area":
            draw.polygon(tops + bottoms[::-1], fill=150)
        draw.line(tops, fill=255, width=line_w, joint="curve")
        draw.line(bottoms, fill=255, width=line_w, joint="curve")


def _colorize(mask: Image.Image, look: Look, left: int, usable: int, axis: str = "x") -> Image.Image:
    """Applies the color (solid or gradient) to a grayscale mask."""
    lut = _lut_cache.get(look.opacity)
    if lut is None:
        lut = [v * look.opacity // 255 for v in range(256)]
        _lut_cache[look.opacity] = lut
    alpha = mask.point(lut)
    rgb = ImageChops.multiply(_color_layer(look, left, usable, axis), Image.merge("RGB", (alpha, alpha, alpha)))
    rgb.putalpha(alpha)
    return rgb


def _make_centered_frame(track, slot_rate: int, t: float, look: Look, track_offset: int,
                          spectrum: Spectrum | None = None) -> Image.Image:
    """
    Draws one transparent RGBA frame (premultiplied - video editors such as
    DaVinci Resolve handle this best). The shape is drawn into a grayscale mask
    first, and the color (solid or gradient) is applied through the mask.
    """
    geom = _geometry(look)
    mask = Image.new("L", (geom.width, geom.height), 0)
    draw = ImageDraw.Draw(mask)

    if look.baseline_dots:
        dot_step = max(9 * look.px_scale, round(geom.width / 118))
        dot_r = max(1.0 * look.px_scale, geom.height / 125)
        for x in range(geom.left, geom.right + 1, dot_step):
            draw.ellipse((x - dot_r, geom.baseline - dot_r, x + dot_r, geom.baseline + dot_r), fill=70)

    levels = bar_levels(track, slot_rate, t, look, track_offset, spectrum)
    _draw_centered_bars(draw, levels, geom, look)

    return _colorize(mask, look, geom.left, geom.usable)


def _make_reflect_frame(track, slot_rate: int, t: float, look: Look, track_offset: int,
                         spectrum: Spectrum | None = None) -> Image.Image:
    """
    Mirror-reflect layout (Reflection): the bars grow upward from the baseline (62% of
    the height), and below the baseline a mirrored reflection appears = 55% of the
    height of the part above the baseline, with an opacity ramp from 0.28 (at the
    baseline) to 0.0 (the bottom edge of the reflection).
    """
    geom = _geometry_reflect(look)
    bars_mask = Image.new("L", (geom.width, geom.height), 0)
    draw = ImageDraw.Draw(bars_mask)

    levels = bar_levels(track, slot_rate, t, look, track_offset, spectrum)
    _draw_upward_bars(draw, levels, geom, look)

    top_region = bars_mask.crop((0, 0, geom.width, geom.baseline))
    reflect_h = max(1, round(geom.baseline * REFLECT_SCALE))
    scaled = top_region.resize((geom.width, reflect_h))
    flipped = ImageOps.flip(scaled)

    ramp = Image.new("L", (geom.width, reflect_h))
    ramp_draw = ImageDraw.Draw(ramp)
    for y in range(reflect_h):
        p = y / max(1, reflect_h - 1)
        ramp_draw.line((0, y, geom.width, y), fill=round(255 * REFLECT_ALPHA * (1.0 - p)))
    reflection = ImageChops.multiply(flipped, ramp)

    reflect_layer = Image.new("L", (geom.width, geom.height), 0)
    reflect_layer.paste(reflection, (0, geom.baseline))

    mask = ImageChops.lighter(bars_mask, reflect_layer)

    if look.baseline_dots:
        dot_step = max(9 * look.px_scale, round(geom.width / 118))
        dot_r = max(1.0 * look.px_scale, geom.height / 125)
        dots_draw = ImageDraw.Draw(mask)
        for x in range(geom.left, geom.right + 1, dot_step):
            dots_draw.ellipse((x - dot_r, geom.baseline - dot_r, x + dot_r, geom.baseline + dot_r), fill=70)

    return _colorize(mask, look, geom.left, geom.usable, axis="y")


def _make_peakhold_frame(track, slot_rate: int, t: float, look: Look, track_offset: int,
                          spectrum: Spectrum | None = None) -> Image.Image:
    """
    Peakhold layout: the bars grow upward from the bottom edge, and a peak-hold cap
    floats above each one. The cap is COMPLETELY stateless - for a given t it always
    comes out as the same bytes, because it is computed only from the audio samples of
    the last PEAK_LOOKBACK seconds (no memory between frames).
    """
    geom = _geometry_bottom(look)
    mask = Image.new("L", (geom.width, geom.height), 0)
    draw = ImageDraw.Draw(mask)

    levels = bar_levels(track, slot_rate, t, look, track_offset, spectrum)
    _draw_upward_bars(draw, levels, geom, look)
    frame = _colorize(mask, look, geom.left, geom.usable, axis="y")

    caps = list(levels)
    lookback = min(10, int(PEAK_LOOKBACK * look.fps))
    for k in range(1, lookback + 1):
        t_past = t - k / look.fps
        if t_past < 0:
            continue
        past_levels = bar_levels(track, slot_rate, t_past, look, track_offset, spectrum)
        decay = PEAK_FALL * (k / look.fps)
        for i, h in enumerate(past_levels):
            caps[i] = max(caps[i], h - decay)

    thickness = max(2 * look.px_scale, round(geom.height * 2 / 180))
    r, g, b = PEAK_CAP_COLOR
    a = int(255 * PEAK_CAP_ALPHA * look.opacity / 255)
    fill = (r * a // 255, g * a // 255, b * a // 255, a)
    cap_draw = ImageDraw.Draw(frame)
    for i, cap in enumerate(caps):
        cap = max(0.0, min(1.0, cap))
        h = geom.min_bar + cap * (geom.max_bar - geom.min_bar)
        x = geom.left + i * geom.spacing
        y_top = geom.baseline - h
        cap_draw.rectangle((x - geom.bar_w / 2, y_top - thickness, x + geom.bar_w / 2, y_top), fill=fill)

    return frame


def _smooth_levels_spatial(levels: list[float]) -> list[float]:
    """The kernel (prev + 2*h + next)/4 over the level array; the edges clone their neighbour."""
    n = len(levels)
    out = []
    for i in range(n):
        prev = levels[i - 1] if i > 0 else levels[i]
        nxt = levels[i + 1] if i < n - 1 else levels[i]
        out.append((prev + 2 * levels[i] + nxt) / 4.0)
    return out


def _interpolate_polyline(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Inserts midpoints between adjacent points (2x the points) - softens the
    angularity of the neon polyline when the bar count is low."""
    if len(points) < 2:
        return points
    out = [points[0]]
    for (x0, y0), (x1, y1) in itertools.pairwise(points):
        out.append(((x0 + x1) / 2.0, (y0 + y1) / 2.0))
        out.append((x1, y1))
    return out


def _make_neon_frame(track, slot_rate: int, t: float, look: Look, track_offset: int,
                      spectrum: Spectrum | None = None) -> Image.Image:
    """
    Neon layout: a thin glowing line oscillating symmetrically around the baseline
    (50%) and its mirrored reflection below the baseline - look.style is ignored here.
    """
    geom = _geometry(look)
    levels = _smooth_levels_spatial(bar_levels(track, slot_rate, t, look, track_offset, spectrum))

    xs = [geom.left + i * geom.spacing for i in range(len(levels))]
    amp = geom.max_bar * 0.5
    top_pts = _interpolate_polyline([(x, geom.baseline - lv * amp) for x, lv in zip(xs, levels)])
    bottom_pts = _interpolate_polyline([(x, geom.baseline + lv * amp) for x, lv in zip(xs, levels)])

    thickness = max(1 * look.px_scale, round(geom.height * NEON_LINE_W))

    sharp = Image.new("L", (geom.width, geom.height), 0)
    if len(top_pts) > 1:
        sharp_draw = ImageDraw.Draw(sharp)
        sharp_draw.line(top_pts, fill=255, width=thickness, joint="curve")
        sharp_draw.line(bottom_pts, fill=255, width=thickness, joint="curve")

    glow = Image.new("L", (geom.width, geom.height), 0)
    if len(top_pts) > 1:
        glow_draw = ImageDraw.Draw(glow)
        glow_w = max(1 * look.px_scale, thickness * 3)
        glow_fill = int(255 * NEON_GLOW_ALPHA)
        glow_draw.line(top_pts, fill=glow_fill, width=glow_w, joint="curve")
        glow_draw.line(bottom_pts, fill=glow_fill, width=glow_w, joint="curve")
    glow = glow.filter(ImageFilter.GaussianBlur(radius=max(2 * look.px_scale, geom.height * NEON_GLOW_BLUR)))

    mask = ImageChops.lighter(sharp, glow)
    return _colorize(mask, look, geom.left, geom.usable, axis="x")


def _make_radial_frame(track, slot_rate: int, t: float, look: Look, track_offset: int,
                        spectrum: Spectrum | None = None) -> Image.Image:
    """Radial layout (Orbit): spokes rotating around the center of the frame, length = audio level."""
    n = max(RADIAL_SPOKES_MIN, look.bars)
    levels_look = replace(look, bars=n) if n != look.bars else look
    levels = bar_levels(track, slot_rate, t, levels_look, track_offset, spectrum)

    width, height = look.width, look.height
    m = min(width, height)
    cx, cy = width / 2.0, height / 2.0
    bar_w = max(2 * look.px_scale, round(m * 3 / 180))

    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)

    r0 = RADIAL_BASE_R * m
    for i, level in enumerate(levels):
        deg = i * 360.0 / n + t * RADIAL_ROT_DEG_PER_S
        rad = math.radians(deg)
        r1 = r0 + (RADIAL_LEN_MIN + level * RADIAL_LEN_SPAN) * m
        x0, y0 = cx + r0 * math.cos(rad), cy + r0 * math.sin(rad)
        x1, y1 = cx + r1 * math.cos(rad), cy + r1 * math.sin(rad)
        strength = int(255 * (0.70 + 0.30 * level))
        draw.line((x0, y0, x1, y1), fill=strength, width=bar_w)

    ring_d = RADIAL_RING_D * m
    draw.ellipse((cx - ring_d / 2, cy - ring_d / 2, cx + ring_d / 2, cy + ring_d / 2),
                outline=45, width=max(1, look.px_scale))

    return _colorize(mask, look, 0, width, axis="y")


_voice_groups_cache: dict[int, tuple[tuple[int, int], ...]] = {}
_voice_fade_cache: dict[tuple[int, int], Image.Image] = {}


def _voiceline_band_groups(bands: int) -> tuple[tuple[int, int], ...]:
    """Spectrum band ranges [lo, hi) for the low / mid / high of the voice (VOICE_BAND_SPLITS -> FMAX)."""
    cached = _voice_groups_cache.get(bands)
    if cached is None:
        span = math.log(SPECTRUM_FMAX / SPECTRUM_FMIN)
        edges = [round(bands * math.log(f / SPECTRUM_FMIN) / span) for f in VOICE_BAND_SPLITS] + [bands]
        cached = tuple((lo, max(lo + 1, hi)) for lo, hi in itertools.pairwise(edges))
        _voice_groups_cache[bands] = cached
    return cached


def voiceline_weights(spectrum: Spectrum | None, t: float, look: Look) -> tuple[float, ...]:
    """
    Weights of the three voice-line sines. Without a spectrum (no numpy) or with
    Reactivity 0 - the fixed weights of the reference formula. With a spectrum, each
    sine gets "its own" band: low -> long waves, mid -> medium, high (sibilants: s, sh,
    ch) -> fine ones. What counts is the band balance (which part of the voice
    dominates), not their loudness, and the energy is smoothed deterministically
    (a weighted average over the VOICE_BAND_WINDOW window). The weights still sum to 1 -
    the line's height is still decided by loudness, the spectrum only changes its
    character (a vowel: one gentle wave, "sh": a fine ripple).
    """
    base = tuple(w for _, _, w in VOICE_WAVES)
    m = max(0.0, min(1.0, look.reactivity))
    if spectrum is None or m == 0.0:
        return base
    data, rate, bands = spectrum.data, spectrum.rate, spectrum.bands
    frames = len(data) // bands
    if frames == 0:
        return base
    s = max(0.0, min(1.0, look.smoothing))
    k = max(1, round((VOICE_BAND_WINDOW[0] + VOICE_BAND_WINDOW[1] * s) * rate))
    energies = []
    for lo, hi in _voiceline_band_groups(bands):
        acc = wsum = 0.0
        for j in range(k):
            fr = max(0.0, min(frames - 1, t * rate - j))
            f0 = int(fr)
            f1 = min(frames - 1, f0 + 1)
            v0 = sum(data[f0 * bands + lo:f0 * bands + hi])
            v1 = sum(data[f1 * bands + lo:f1 * bands + hi])
            acc += (v0 + (v1 - v0) * (fr - f0)) * (k - j)
            wsum += k - j
        energies.append(acc / (wsum * (hi - lo) * 255.0))
    mean = sum(energies) / len(energies) + VOICE_TIMBRE_EPS
    raw = [b * ((e + VOICE_TIMBRE_EPS) / mean) ** VOICE_TIMBRE_GAMMA for b, e in zip(base, energies)]
    total = sum(raw)
    return tuple((1.0 - m) * b + m * r / total for b, r in zip(base, raw))


def _voiceline_envelope(track, slot_rate: int, t: float, look: Look, track_offset: int,
                        us: list[float]) -> list[float]:
    """
    Loudness level (0..1) along the line for positions u. Center = now; with Reactivity > 0
    the sides show the level from d x (Time window / 2) earlier, d = distance from the
    center - the voice spreads sideways from the center and fades out there. Works
    without numpy too (envelope only).
    """
    n = len(track)
    if not n:
        return [0.0] * len(us)

    def level(sec: float) -> float:
        pos = max(0.0, min(n - 1, sec * slot_rate - track_offset))
        i0 = int(pos)
        i1 = min(n - 1, i0 + 1)
        return track[i0] + (track[i1] - track[i0]) * (pos - i0)

    now = level(t)
    m = max(0.0, min(1.0, look.reactivity))
    if m == 0.0:
        return [now] * len(us)
    half = look.window_seconds / 2.0
    return [(1.0 - m) * now + m * level(t - abs(2.0 * u - 1.0) * half) for u in us]


def _voiceline_width(look: Look) -> float:
    """Line thickness in frame px: look.line_width at 1080x180, scaled with the smaller dimension."""
    return max(1.0, look.line_width * min(look.width / 1080, look.height / 180))


def voiceline_points(track, slot_rate: int, t: float, look: Look, track_offset: int = 0,
                     spectrum: Spectrum | None = None) -> list[tuple[float, float]]:
    """
    Points (x, y) of the voice line in frame pixels: y = sum of weight*sin(2*pi*(u*periods + t*drift*speed))
    x amplitude x taper. Amplitude = (0.04 + 0.96 x level) x 0.42 H (at the default
    Height), taper = sin(pi*u)^1.6. With Reactivity 0 and no spectrum - the reference formula.
    """
    left, _right, usable = _geometry_common(look)[:3]
    n = max(VOICE_POINTS_MIN, round(usable / 4))
    us = [i / (n - 1) for i in range(n)]
    env = _voiceline_envelope(track, slot_rate, t, look, track_offset, us)
    weights = voiceline_weights(spectrum, t, look)
    waves = [(cycles, speed * t * look.drift, w)
             for (cycles, speed, _), w in zip(VOICE_WAVES, weights)]
    amp_max = max(0.0, min(look.height * look.max_height_ratio * VOICE_AMP,
                           look.height / 2.0 - _voiceline_width(look)))
    baseline = look.height / 2.0
    two_pi = 2.0 * math.pi
    pts = []
    for u, e in zip(us, env):
        y = 0.0
        for cycles, phase, w in waves:
            y += math.sin(two_pi * (u * cycles + phase)) * w
        amp = (VOICE_AMP_FLOOR + (1.0 - VOICE_AMP_FLOOR) * e) * amp_max
        pts.append((left + u * usable, baseline + y * amp * math.sin(math.pi * u) ** VOICE_TAPER))
    return pts


def _voiceline_fade(look: Look) -> Image.Image:
    """Horizontal alpha ramp (smoothstep): the line ends fade out gently instead of being cut off."""
    key = (look.width, look.height)
    cached = _voice_fade_cache.get(key)
    if cached is not None:
        return cached
    left, right, usable = _geometry_common(look)[:3]
    fade = max(1.0, usable * VOICE_EDGE_FADE)
    values = []
    for x in range(look.width):
        p = max(0.0, min(1.0, (x - left) / fade, (right - x) / fade))
        values.append(round(255 * p * p * (3.0 - 2.0 * p)))
    row = Image.new("L", (look.width, 1))
    row.putdata(values)
    ramp = row.resize((look.width, look.height), Image.NEAREST)
    if len(_voice_fade_cache) > 8:
        _voice_fade_cache.clear()
    _voice_fade_cache[key] = ramp
    return ramp


def _make_voiceline_frame(track, slot_rate: int, t: float, look: Look, track_offset: int,
                          spectrum: Spectrum | None = None) -> Image.Image:
    """
    Voiceline layout (Voice line): one thin, smooth line like an oscilloscope trace.
    The mask is drawn at VOICE_SUPERSAMPLE x (2x for large frames) and reduced with a
    box filter - antialiasing as in the reference formula; the color is applied at the
    target resolution. look.style and look.bars are ignored here.
    """
    ss = VOICE_SUPERSAMPLE if look.width * look.height <= VOICE_SUPERSAMPLE_MAX_PIXELS else 2
    pts = voiceline_points(track, slot_rate, t, look, track_offset, spectrum)
    hi = Image.new("L", (look.width * ss, look.height * ss), 0)
    ImageDraw.Draw(hi).line([((x + 0.5) * ss - 0.5, (y + 0.5) * ss - 0.5) for x, y in pts],
                            fill=255, width=max(1, round(_voiceline_width(look) * ss)), joint="curve")
    mask = ImageChops.multiply(hi.reduce(ss), _voiceline_fade(look))
    left, _right, usable = _geometry_common(look)[:3]
    return _colorize(mask, look, left, usable)


_SUPERSAMPLED_LAYOUTS =("flow", "ripple", "mirror-reflect", "peakhold", "neon", "radial")


def _render_frame(track, slot_rate: int, t: float, look: Look, track_offset: int,
                   spectrum: Spectrum | None = None) -> Image.Image:
    """Renders one frame at look.width x look.height (no supersampling)."""
    if look.layout in ("flow", "ripple"):
        return _make_centered_frame(track, slot_rate, t, look, track_offset, spectrum)
    if look.layout == "mirror-reflect":
        return _make_reflect_frame(track, slot_rate, t, look, track_offset, spectrum)
    if look.layout == "peakhold":
        return _make_peakhold_frame(track, slot_rate, t, look, track_offset, spectrum)
    if look.layout == "neon":
        return _make_neon_frame(track, slot_rate, t, look, track_offset, spectrum)
    if look.layout == "radial":
        return _make_radial_frame(track, slot_rate, t, look, track_offset, spectrum)
    raise ValueError(f"Unknown layout: {look.layout}")


def downscale_premultiplied(frame: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Downscale a premultiplied RGBA frame band by band.

    Image.resize assumes straight alpha for RGBA and would apply alpha a second
    time, leaving edge pixels brighter than their alpha allows. LANCZOS ringing can
    also push a colour band above alpha, so the colour is clamped to alpha afterwards.
    """
    bands = [band.resize(size, Image.LANCZOS) for band in frame.split()]
    alpha = bands[3]
    return Image.merge("RGBA", [ImageChops.darker(band, alpha) for band in bands[:3]] + [alpha])


def make_frame(track, slot_rate: int, t: float, look: Look, track_offset: int = 0,
               spectrum: Spectrum | None = None) -> Image.Image:
    """
    Dispatcher by layout. scroll/mirror/pulse render unchanged (their output stays
    byte-identical). The other six layouts are rendered internally at SUPERSAMPLE x the
    resolution and reduced with LANCZOS for better antialiasing - unless the target
    area (width x height) exceeds SUPERSAMPLE_MAX_PIXELS (then a 1x render, to stay
    within the frame budget). spectrum (optional, numpy only): for peakhold/mirror-reflect/
    neon it replaces the lagged stationary sampling with frequency bands, and for
    voiceline it gives the line its shape from the timbre of the voice. voiceline has its
    own supersampling.
    """
    if look.layout in ("scroll", "mirror", "pulse"):
        return _make_centered_frame(track, slot_rate, t, look, track_offset, spectrum)
    if look.layout == "voiceline":
        return _make_voiceline_frame(track, slot_rate, t, look, track_offset, spectrum)
    if look.layout in _SUPERSAMPLED_LAYOUTS and look.width * look.height <= SUPERSAMPLE_MAX_PIXELS:
        hi_look = replace(look, width=look.width * SUPERSAMPLE, height=look.height * SUPERSAMPLE,
                          px_scale=look.px_scale * SUPERSAMPLE)
        hi_frame = _render_frame(track, slot_rate, t, hi_look, track_offset, spectrum)
        return downscale_premultiplied(hi_frame, (look.width, look.height))
    return _render_frame(track, slot_rate, t, look, track_offset, spectrum)


# ---------------------------------------------------------------------------
# Movie rendering
# ---------------------------------------------------------------------------

def render_movie(analysis: Analysis, output: Path, look: Look, progress=None,
                 cancel: threading.Event | None = None) -> None:
    """Frames go as a raw RGBA stream straight into ffmpeg - no thousands of PNGs.

    FFmpeg writes to a temporary file next to ``output``, which replaces ``output``
    only after FFmpeg exits successfully; on cancel or failure an existing ``output``
    is left untouched. Raises Cancelled when ``cancel`` is set (checked every frame).
    """
    output = Path(output).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    total = analysis.frames(look.fps)
    track = shape_track(analysis, look)
    spectrum = (Spectrum(analysis.spectrum, analysis.spectrum_rate, analysis.spectrum_bands)
                if analysis.spectrum is not None else None)
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise FFmpegNotFoundError("FFmpeg was not found.")
    temp = output.with_name(f"{output.stem}.part-{secrets.token_hex(16)}.mov")
    cmd = [
        str(ffmpeg), "-n", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{look.width}x{look.height}",
        "-r", str(look.fps), "-i", "-",
        "-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le",
        "-vendor", "apl0", "-movflags", "+faststart", "-f", "mov", str(temp),
    ]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=subprocess_flags())
    except FileNotFoundError as exc:
        raise FFmpegNotFoundError("FFmpeg was not found.") from exc
    assert proc.stdin is not None and proc.stderr is not None
    drain = _StderrDrain(proc.stderr)
    done = False
    try:
        broken = False
        try:
            for frame_idx in range(total):
                if cancel is not None and cancel.is_set():
                    raise Cancelled
                frame = make_frame(track, analysis.slot_rate, frame_idx / look.fps, look, spectrum=spectrum)
                proc.stdin.write(frame.tobytes())
                if progress and (frame_idx % 30 == 0 or frame_idx == total - 1):
                    progress(frame_idx + 1, total)
        except OSError:
            # BrokenPipeError on POSIX, EINVAL on Windows: FFmpeg died; its stderr says why.
            broken = True
        try:
            proc.stdin.close()
        except OSError:
            broken = True
        stderr = drain.text()
        proc.wait()
        if proc.returncode != 0 or broken:
            raise RuntimeError(stderr or f"ffmpeg exited with code {proc.returncode}")
        os.replace(temp, output)
        done = True
    finally:
        if not done:
            _kill(proc)
            _remove_file(temp)
