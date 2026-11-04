# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Cancellation, failure and Ctrl-C leave no temp file, no live process and no damaged output."""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import make_synthetic_analysis, requires_ffmpeg

from rubato_waveform import engine
from rubato_waveform.engine import Cancelled, Look, analyze_audio, render_movie

LOOK = Look(width=320, height=64, fps=30, bars=45)
OLD = b"previous render"
TIMEOUT = 20.0


def wait_for(condition, timeout: float = TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@pytest.fixture
def spawned(monkeypatch) -> list[subprocess.Popen]:
    """Every process the engine starts, so tests can assert none is left running."""
    procs: list[subprocess.Popen] = []
    real = subprocess.Popen

    class Tracking(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            procs.append(self)

    monkeypatch.setattr(subprocess, "Popen", Tracking)
    return procs


def assert_all_exited(procs) -> None:
    assert procs, "expected the engine to start a process"
    assert all(wait_for(lambda p=p: p.poll() is not None) for p in procs)


@requires_ffmpeg
def test_cancel_at_frame_cleans_up_and_keeps_existing_output(tmp_path, spawned) -> None:
    out = tmp_path / "wave.mov"
    out.write_bytes(OLD)
    cancel = threading.Event()
    seen = []

    def progress(current, total):
        seen.append(current)
        if current >= 31:
            cancel.set()

    with pytest.raises(Cancelled):
        render_movie(make_synthetic_analysis(), out, LOOK, progress=progress, cancel=cancel)
    assert seen[-1] == 31, "rendering must stop at the next frame check"
    assert list(tmp_path.iterdir()) == [out]
    assert out.read_bytes() == OLD
    assert_all_exited(spawned)


@requires_ffmpeg
def test_cancel_before_first_frame(tmp_path, spawned) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        render_movie(make_synthetic_analysis(), tmp_path / "wave.mov", LOOK, cancel=cancel)
    assert list(tmp_path.iterdir()) == []
    assert_all_exited(spawned)


@requires_ffmpeg
def test_callback_exception_kills_ffmpeg(tmp_path, spawned) -> None:
    def progress(current, total):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        render_movie(make_synthetic_analysis(), tmp_path / "wave.mov", LOOK, progress=progress)
    assert list(tmp_path.iterdir()) == []
    assert_all_exited(spawned)


@pytest.mark.skipif(sys.platform == "win32", reason="fake executable is a POSIX shell script")
def test_ffmpeg_failure_keeps_existing_output_and_reports_stderr(tmp_path, monkeypatch) -> None:
    fake = tmp_path / "fake-ffmpeg"
    fake.write_text("#!/bin/sh\necho 'fake encoder exploded' >&2\nexit 3\n")
    fake.chmod(0o755)
    monkeypatch.setattr(engine, "find_ffmpeg", lambda: fake)
    out = tmp_path / "wave.mov"
    out.write_bytes(OLD)

    with pytest.raises(RuntimeError, match="fake encoder exploded"):
        render_movie(make_synthetic_analysis(), out, LOOK)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fake-ffmpeg", "wave.mov"]
    assert out.read_bytes() == OLD


@requires_ffmpeg
def test_analyze_cancel_raises_and_leaves_no_process(demo_wav, spawned) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        analyze_audio(demo_wav, cancel=cancel)
    assert_all_exited(spawned)


@requires_ffmpeg
def test_analyze_cancel_from_progress(demo_wav, spawned) -> None:
    cancel = threading.Event()
    with pytest.raises(Cancelled):
        analyze_audio(demo_wav, progress=lambda fraction: cancel.set(), cancel=cancel)
    assert_all_exited(spawned)


@requires_ffmpeg
@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT delivery is POSIX-only")
def test_cli_ctrl_c_exits_130_and_cleans_up(tmp_path) -> None:
    # Long enough (tens of seconds to render) that the CLI cannot finish before SIGINT lands.
    long_wav = tmp_path / "long.wav"
    script = Path(__file__).parent.parent / "scripts" / "make_demo_audio.py"
    subprocess.run([sys.executable, "-B", str(script), "--out", str(long_wav), "--seconds", "60"],
                   check=True, capture_output=True)
    out = tmp_path / "out" / "wave.mov"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent)}
    proc = subprocess.Popen(
        [sys.executable, "-B", "-m", "rubato_waveform", str(long_wav), str(out),
         "--width", "1080", "--height", "180", "--fps", "60"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
    )
    try:
        assert wait_for(lambda: out.parent.is_dir() and any(".part-" in p.name for p in out.parent.iterdir())), \
            "rendering never started"
        proc.send_signal(signal.SIGINT)
        _, stderr = proc.communicate(timeout=TIMEOUT)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 130
    assert b"Traceback" not in stderr
    assert list(out.parent.iterdir()) == []
