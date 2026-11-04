"""Generate a deterministic synthetic demo WAV (speech-like syllables, then music-like).

Stdlib only. No recording of any real person is involved: the "speech" is made of
amplitude-modulated harmonic stacks with gliding pitch, separated by real pauses,
and the "music" is a bass pulse, soft chords and hi-hat-like noise bursts.
The same seed and options always produce byte-identical output.
"""

from __future__ import annotations

import argparse
import math
import random
import struct
import wave
from pathlib import Path

TAU = 2.0 * math.pi


def _smooth_env(pos: float, attack: float, release: float) -> float:
    """Raised-cosine attack and release; `pos` is the 0..1 position in the note."""
    if pos < attack:
        return 0.5 - 0.5 * math.cos(math.pi * pos / attack)
    if pos > 1.0 - release:
        return 0.5 - 0.5 * math.cos(math.pi * (1.0 - pos) / release)
    return 1.0


def _add_syllable(buf: list[float], rate: int, start: int, dur: float,
                  f0: float, f1: float, formant: float, level: float) -> None:
    """Harmonic stack with a gliding pitch and a formant-like spectral bump."""
    n = int(dur * rate)
    harmonics = [(h, 1.0 / h * (0.4 + math.exp(-((h * f0 - formant) / 700.0) ** 2) * 2.2))
                 for h in range(1, 17) if h * max(f0, f1) < rate * 0.45]
    phase = 0.0
    for i in range(n):
        pos = i / n
        freq = f0 + (f1 - f0) * pos
        phase += TAU * freq / rate
        # Fast syllabic tremor on top of the attack/release shape.
        env = _smooth_env(pos, 0.12, 0.45) * (0.8 + 0.2 * math.sin(TAU * 22.0 * pos * dur))
        acc = 0.0
        for h, amp in harmonics:
            acc += amp * math.sin(h * phase)
        idx = start + i
        if idx < len(buf):
            buf[idx] += level * env * acc * 0.22


def _render_speech(rate: int, seconds: float, rng: random.Random) -> list[float]:
    buf = [0.0] * int(seconds * rate)
    t = 0.25
    syllables_in_word = 0
    while t < seconds - 0.2:
        dur = rng.uniform(0.11, 0.30)
        f0 = rng.uniform(95.0, 210.0)
        f1 = f0 * rng.uniform(0.82, 1.22)
        formant = rng.choice((500.0, 800.0, 1200.0, 1800.0, 2400.0))
        level = rng.uniform(0.45, 1.0)
        _add_syllable(buf, rate, int(t * rate), dur, f0, f1, formant, level)
        t += dur
        syllables_in_word += 1
        if syllables_in_word >= rng.randint(1, 4):
            syllables_in_word = 0
            # Word gap, with an occasional longer pause between phrases.
            t += rng.uniform(0.10, 0.28) if rng.random() < 0.8 else rng.uniform(0.55, 0.95)
        else:
            t += rng.uniform(0.01, 0.04)
    return buf


def _render_music(rate: int, seconds: float, rng: random.Random, stereo: bool):
    """Return (left, right) lists; right is None when mono."""
    n = int(seconds * rate)
    left = [0.0] * n
    right = [0.0] * n if stereo else None
    beat = 60.0 / 120.0
    chords = ((110.0, 138.6, 164.8), (98.0, 123.5, 146.8),
              (87.3, 110.0, 130.8), (98.0, 123.5, 146.8))
    bass_roots = (55.0, 49.0, 43.65, 49.0)
    hat_state = 0.0
    for i in range(n):
        t = i / rate
        beat_no = int(t / beat)
        bar = (beat_no // 4) % 4
        in_beat = (t % beat) / beat
        # Bass pulse on every beat, plus a short kick-like thump.
        bass_env = math.exp(-in_beat * 4.5)
        bass = math.sin(TAU * bass_roots[bar] * t) * bass_env * 0.45
        thump = math.sin(TAU * (50.0 + 90.0 * math.exp(-in_beat * 30.0)) * t) * math.exp(-in_beat * 14.0) * 0.25
        # Chords: slightly detuned triads with a slow swell each bar.
        swell = 0.5 - 0.5 * math.cos(TAU * ((t / (beat * 4.0)) % 1.0))
        chord_l = chord_r = 0.0
        for f in chords[bar]:
            chord_l += math.sin(TAU * f * t) + 0.4 * math.sin(TAU * 2.0 * f * t)
            chord_r += math.sin(TAU * f * 1.004 * t) + 0.4 * math.sin(TAU * 2.0 * f * 1.004 * t)
        chord_l *= 0.07 * (0.35 + 0.65 * swell)
        chord_r *= 0.07 * (0.35 + 0.65 * swell)
        # Hi-hat: differentiated noise burst on every eighth note, louder off-beat.
        in_eighth = (t % (beat / 2.0)) / (beat / 2.0)
        accent = 1.0 if int(t / (beat / 2.0)) % 2 else 0.55
        raw = rng.uniform(-1.0, 1.0)
        hat = (raw - hat_state) * math.exp(-in_eighth * 16.0) * 0.16 * accent
        hat_state = raw
        left[i] = bass + thump + chord_l + hat
        if right is not None:
            right[i] = bass + thump + chord_r + hat * 0.6
    return left, right


def _fade(buf: list[float], rate: int, ms: float) -> None:
    m = min(int(ms * rate / 1000.0), len(buf) // 2)
    for i in range(m):
        g = i / m
        buf[i] *= g
        buf[-1 - i] *= g


def generate(seconds: float, rate: int, channels: int, seed: int) -> bytes:
    rng = random.Random(seed)
    total = int(seconds * rate)
    speech_n = int(total * 0.55)
    speech = _render_speech(rate, speech_n / rate, rng)
    music_l, music_r = _render_music(rate, (total - speech_n) / rate, rng, channels == 2)
    left = speech + music_l
    right = None
    if channels == 2:
        # Speech is centred with a tiny level offset; music keeps its own stereo image.
        right = [s * 0.96 for s in speech] + music_r
    for ch in (left, right):
        if ch is not None:
            _fade(ch, rate, 15.0)
    peak = max(max(abs(v) for v in ch) for ch in (left, right) if ch is not None)
    gain = 0.89 * 32767.0 / peak if peak else 0.0
    if right is None:
        frames = [left[i] for i in range(len(left))]
    else:
        frames = [v for pair in zip(left, right) for v in pair]
    ints = [max(-32768, min(32767, round(v * gain))) for v in frames]
    return struct.pack(f"<{len(ints)}h", *ints)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="output WAV path")
    parser.add_argument("--seconds", type=float, default=20.0, help="length in seconds (default 20)")
    parser.add_argument("--rate", type=int, choices=(44100, 48000), default=44100)
    parser.add_argument("--channels", type=int, choices=(1, 2), default=2)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    if args.seconds < 2.0:
        parser.error("--seconds must be at least 2")
    data = generate(args.seconds, args.rate, args.channels, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(args.out), "wb") as wav:
        wav.setnchannels(args.channels)
        wav.setsampwidth(2)
        wav.setframerate(args.rate)
        wav.writeframes(data)
    print(f"wrote {args.out} ({args.seconds:g} s, {args.rate} Hz, {args.channels} ch)")


if __name__ == "__main__":
    main()
