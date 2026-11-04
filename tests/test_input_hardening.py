# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Hostile input reaching FFmpeg: extension allow-list, format whitelist, ffprobe hang, NaN samples."""

import math
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import requires_ffmpeg, requires_numpy, wait_until
from server_support import FakeDialogs
from test_ffmpeg_tools import make_fake

from rubato_waveform import engine, ffmpeg_tools
from rubato_waveform.catalog import default_look_dict
from rubato_waveform.engine import analyze_audio, probe_duration
from rubato_waveform.session import ALLOWED_EXTENSIONS, Session, SessionError

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake FFprobe is a POSIX shell script")

SINE = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
VIDEO = ["-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10"]

# extension -> (extra input, encoder arguments). The Ogg files hold FLAC: not every FFmpeg has a
# Vorbis encoder, and the demuxer (ogg) is the same.
FORMATS = {
    ".wav": ([], []),
    ".mp3": ([], ["-c:a", "libmp3lame"]),
    ".m4a": ([], ["-c:a", "aac"]),
    ".aac": ([], ["-c:a", "aac", "-f", "adts"]),
    ".flac": ([], []),
    ".aiff": ([], []),
    ".aif": ([], ["-f", "aiff"]),
    ".ogg": ([], ["-c:a", "flac", "-f", "ogg"]),
    ".oga": ([], ["-c:a", "flac", "-f", "ogg"]),
    ".opus": ([], ["-c:a", "libopus"]),
    ".wma": ([], ["-c:a", "wmav2"]),
    ".mka": ([], []),
    ".caf": ([], ["-f", "caf"]),
    ".mp4": ([], ["-c:a", "aac"]),
    ".mov": ([], ["-c:a", "aac"]),
    ".m4v": (VIDEO, ["-c:a", "aac", "-c:v", "mpeg4", "-f", "mp4"]),
    ".mkv": (VIDEO, ["-c:a", "aac", "-c:v", "mpeg4"]),
    ".webm": (VIDEO, ["-c:a", "libopus", "-c:v", "libvpx"]),
}


def encode(path: Path, extra_input: list[str], codec_args: list[str]) -> None:
    ffmpeg = ffmpeg_tools.find_ffmpeg()
    cmd = [str(ffmpeg), "-hide_banner", "-v", "error", "-y", *SINE, *extra_input, *codec_args, str(path)]
    done = subprocess.run(cmd, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        pytest.skip(f"this FFmpeg cannot encode {path.suffix}: {done.stderr.strip()[:120]}")


def test_every_allowed_extension_has_a_fixture_and_a_mime() -> None:
    assert set(FORMATS) == ALLOWED_EXTENSIONS


@requires_ffmpeg
@pytest.mark.parametrize("extension", sorted(FORMATS))
def test_each_allowed_format_still_decodes(extension, tmp_path) -> None:
    path = tmp_path / f"clip{extension}"
    encode(path, *FORMATS[extension])
    analysis = analyze_audio(path)
    assert 0.8 < analysis.duration < 1.3
    assert max(analysis.peak) > 0.1
    assert probe_duration(path) == pytest.approx(1.0, abs=0.3)


# ------------------------------------------------------------------ refusals


@pytest.fixture
def secret_wav(tmp_path) -> Path:
    """A loud file that a playlist or script would pull in; if it is decoded, the attack worked."""
    path = tmp_path / "secret" / "loud.wav"
    path.parent.mkdir()
    if ffmpeg_tools.find_ffmpeg() is not None:
        encode(path, [], [])
    return path


def write_attack(tmp_path: Path, secret: Path, kind: str) -> Path:
    base = tmp_path / "attack"
    base.mkdir()
    if kind == "hls":
        path = base / "list.m3u8"
        path.write_text(f"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXTINF:1,\n{secret}\n#EXT-X-ENDLIST\n")
    elif kind == "hls-named-wav":
        path = base / "list.wav"
        path.write_text(f"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXTINF:1,\n{secret}\n#EXT-X-ENDLIST\n")
    elif kind == "ffconcat-named-mp3":
        (base / "loud.wav").write_bytes(secret.read_bytes())
        path = base / "script.mp3"
        path.write_text("ffconcat version 1.0\nfile loud.wav\n")
    else:                                              # an image sequence pattern: s1.png, s2.png as s%d.png
        for number in (1, 2):
            subprocess.run([str(ffmpeg_tools.find_ffmpeg()), "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
                            "-i", "testsrc=duration=0.1:size=16x16:rate=10", "-frames:v", "1",
                            str(base / f"s{number}.png")], check=True, stdin=subprocess.DEVNULL)
        path = base / "s%d.png"
    return path


@requires_ffmpeg
@pytest.mark.parametrize("kind", ["hls", "hls-named-wav", "ffconcat-named-mp3", "image-sequence"])
def test_playlists_scripts_and_sequences_are_refused(kind, tmp_path, secret_wav) -> None:
    path = write_attack(tmp_path, secret_wav, kind)
    with pytest.raises(RuntimeError):
        analyze_audio(path)
    assert probe_duration(path) is None


@requires_ffmpeg
def test_the_concat_script_works_without_the_format_whitelist(tmp_path, secret_wav) -> None:
    """Control: proves the refusal above comes from the whitelist, not from a broken fixture."""
    path = write_attack(tmp_path, secret_wav, "ffconcat-named-mp3")
    cmd = [str(ffmpeg_tools.find_ffmpeg()), "-v", "error", "-protocol_whitelist", "file,pipe", "-i", str(path),
           "-f", "null", "-"]
    if subprocess.run(cmd, capture_output=True, check=False, stdin=subprocess.DEVNULL).returncode != 0:
        pytest.skip("this FFmpeg does not follow ffconcat scripts anyway")
    with pytest.raises(RuntimeError, match="whitelist"):
        analyze_audio(path)


@pytest.mark.parametrize("name", ["list.m3u8", "list.m3u", "s%d.png", "script.ffconcat", "noextension", "x.txt", "x.mp3.m3u8"])
def test_session_refuses_other_extensions(name, tmp_path) -> None:
    path = tmp_path / name
    path.write_text("#EXTM3U\n")
    session = Session(FakeDialogs(), tmp_path / "cfg")
    try:
        with pytest.raises(SessionError, match="not supported"):
            session.select_audio(path)
        assert session.state()["audio"] is None
    finally:
        session.shutdown()


def test_the_allow_list_is_case_insensitive_and_every_entry_has_a_mime(tmp_path) -> None:
    from rubato_waveform.session import _MIME
    assert ALLOWED_EXTENSIONS == set(_MIME)
    assert all(_MIME[extension] != "application/octet-stream" for extension in ALLOWED_EXTENSIONS)
    path = tmp_path / "CLIP.WAV"
    path.write_bytes(b"x")
    session = Session(FakeDialogs(), tmp_path / "cfg")
    try:
        session.select_audio(path)
    finally:
        session.shutdown()


# --------------------------------------------------------------- ffprobe hang


@pytest.fixture(params=["exec sleep 120", "sleep 20"], ids=["plain", "wrapper-with-child"])
def hanging_ffprobe(request, tmp_path, monkeypatch) -> Path:
    """RUBATO_FFMPEG points at a fake ffmpeg whose sibling ffprobe never answers.

    The wrapper variant leaves a child behind when killed (it keeps any inherited pipe open).
    """
    ffmpeg = make_fake(tmp_path / "fake")
    make_fake(tmp_path / "fake", "ffprobe", body=request.param + "\n")
    monkeypatch.setenv(ffmpeg_tools.ENV_VAR, str(ffmpeg))
    return tmp_path / "fake"


@pytest.fixture
def spawned(monkeypatch) -> list[subprocess.Popen]:
    procs: list[subprocess.Popen] = []
    real = subprocess.Popen

    class Tracking(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            procs.append(self)

    monkeypatch.setattr(subprocess, "Popen", Tracking)
    return procs


def probes(procs) -> list[subprocess.Popen]:
    return [p for p in procs if Path(p.args[0]).stem == "ffprobe"]


@posix_only
def test_shutdown_does_not_wait_for_a_stalled_ffprobe(hanging_ffprobe, tmp_path, spawned) -> None:
    audio = tmp_path / "talk.wav"
    audio.write_bytes(b"x")
    session = Session(FakeDialogs(), tmp_path / "cfg")
    session.select_audio(audio)
    assert wait_until(lambda: probes(spawned))
    start = time.monotonic()
    session.shutdown()
    assert time.monotonic() - start < 3.0
    assert all(wait_until(lambda p=p: p.poll() is not None) for p in probes(spawned))


@posix_only
def test_probe_gives_up_after_the_timeout_and_kills_ffprobe(hanging_ffprobe, tmp_path, spawned, monkeypatch) -> None:
    monkeypatch.setattr(engine, "PROBE_TIMEOUT", 0.3)
    audio = tmp_path / "talk.wav"
    audio.write_bytes(b"x")
    start = time.monotonic()
    assert probe_duration(audio) is None
    assert time.monotonic() - start < 5.0
    assert probes(spawned) and all(p.poll() is not None for p in probes(spawned))


@posix_only
def test_probe_raises_cancelled_and_kills_ffprobe(hanging_ffprobe, tmp_path, spawned) -> None:
    import threading
    cancel = threading.Event()
    audio = tmp_path / "talk.wav"
    audio.write_bytes(b"x")
    threading.Timer(0.2, cancel.set).start()
    with pytest.raises(engine.Cancelled):
        probe_duration(audio, cancel)
    assert probes(spawned) and all(p.poll() is not None for p in probes(spawned))


# ------------------------------------------------------------------ NaN samples


def write_float_wav(path: Path, samples: list[float], rate: int = 48000) -> None:
    data = struct.pack(f"<{len(samples)}f", *samples)
    fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    path.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<I", 16) + fmt
                     + b"data" + struct.pack("<I", len(data)) + data)


@requires_ffmpeg
@pytest.mark.parametrize("use_numpy", [True, False], ids=["numpy", "no-numpy"])
def test_non_finite_samples_count_as_silence(use_numpy, tmp_path, monkeypatch) -> None:
    if use_numpy:
        if engine.np is None:
            pytest.skip("numpy is not available")
    else:
        monkeypatch.setattr(engine, "np", None)
    rate = 48000
    samples = [0.5 * math.sin(2 * math.pi * 440 * i / rate) for i in range(3 * rate)]
    samples[rate] = math.nan                           # 1.0 s
    samples[rate + 100], samples[rate + 200] = math.inf, -math.inf
    path = tmp_path / "nan.wav"
    write_float_wav(path, samples, rate)

    session = Session(FakeDialogs(), tmp_path / "cfg")
    try:
        session.select_audio(path)
        assert wait_until(lambda: session.state()["analysis"]["state"] in ("ready", "failed"))
        assert session.state()["analysis"]["state"] == "ready", session.state()["analysis"]["error"]
        analysis = session._analysis
        assert all(math.isfinite(v) for v in analysis.peak) and all(math.isfinite(v) for v in analysis.rms)
        assert max(analysis.peak) < 1.5                # an Inf sample did not become a giant peak
        for t in (0.9, 1.0, 1.0005, 1.5, 2.9):         # before, at and after the bad samples
            width, height, rgba = session.render_preview(default_look_dict(), t, 300, 100)
            assert len(rgba) == width * height * 4
    finally:
        session.shutdown()


@requires_ffmpeg
@requires_numpy
def test_non_finite_samples_do_not_poison_the_spectrum(tmp_path) -> None:
    rate = 48000
    samples = [0.5 * math.sin(2 * math.pi * 440 * i / rate) for i in range(2 * rate)]
    samples[rate] = math.nan
    path = tmp_path / "nan.wav"
    write_float_wav(path, samples, rate)
    analysis = analyze_audio(path)
    assert analysis.spectrum is not None and len(analysis.spectrum) > 0
