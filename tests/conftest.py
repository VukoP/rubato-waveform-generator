# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and skip helpers for the engine and golden tests."""

import array
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import PIL
import pytest

from rubato_waveform.engine import ANALYSIS_RATE, SLOT, Analysis, _calibrate
from rubato_waveform.engine import np as engine_np

GOLDEN_DIR = Path(__file__).parent / "golden"
MANIFEST_PATH = GOLDEN_DIR / "manifest.json"

requires_numpy = pytest.mark.skipif(engine_np is None, reason="numpy is not available")
requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not found on PATH")


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


def pinned_env_mismatch(meta: dict, check_numpy: bool = True) -> str | None:
    """Why this machine cannot reproduce exact hashes recorded in the manifest, or None.

    Exact pixel hashes depend on libm and Pillow, so they are only compared on the
    platform, architecture and library versions the manifest was captured with. They are
    never compared in CI, where the OS libm and Python build can differ from the author's.
    """
    if os.environ.get("CI"):
        return "exact hashes are checked on the maintainer's machine, not in CI"
    if sys.platform != "darwin":
        return f"exact hashes are captured on darwin, this is {sys.platform}"
    if platform.machine() != "arm64":
        return f"exact hashes are captured on arm64, this is {platform.machine()}"
    if PIL.__version__ != meta["pillow"]:
        return f"Pillow {PIL.__version__} differs from the manifest Pillow {meta['pillow']}"
    have_numpy = engine_np.__version__ if engine_np is not None else None
    if check_numpy and have_numpy != meta["numpy"]:
        return f"numpy {have_numpy} differs from the manifest numpy {meta['numpy']}"
    return None


def pinned_env_skip_reason() -> str | None:
    return pinned_env_mismatch(load_manifest()["numpy"]["meta"])


requires_pinned_env = pytest.mark.skipif(
    pinned_env_skip_reason() is not None,
    reason=f"exact-hash test: {pinned_env_skip_reason()}",
)


def wait_until(predicate, timeout: float = 20.0) -> bool:
    """Polls ``predicate`` until it is true or ``timeout`` seconds pass; returns its last value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def make_synthetic_analysis() -> Analysis:
    """3 s: 1 s of near-silence, 1 s loud, 1 s quiet. No FFmpeg needed."""
    peaks, rmss = array.array("f"), array.array("f")
    for slot in range(3 * ANALYSIS_RATE // SLOT):
        second = slot * SLOT / ANALYSIS_RATE
        amp = 0.002 if second < 1 else (0.8 if second < 2 else 0.2)
        peaks.append(amp)
        rmss.append(amp / math.sqrt(2))
    analysis = Analysis(peaks, rmss)
    _calibrate(analysis)
    return analysis


@pytest.fixture
def synthetic_analysis() -> Analysis:
    return make_synthetic_analysis()


@pytest.fixture(scope="session")
def demo_wav(tmp_path_factory) -> Path:
    """A generated 12 s demo WAV (scripts/make_demo_audio.py): deterministic, no real recording."""
    script = Path(__file__).parent.parent / "scripts" / "make_demo_audio.py"
    out = tmp_path_factory.mktemp("demo") / "demo.wav"
    subprocess.run([sys.executable, "-B", str(script), "--out", str(out), "--seconds", "12"],
                   check=True, capture_output=True)
    return out
