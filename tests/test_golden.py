# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Golden-record tests: render a fixed matrix of frames and compare with the manifest.

capture.py runs in a subprocess per mode (with and without numpy), because the
engine decides at import time whether numpy is available.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import GOLDEN_DIR, load_manifest, pinned_env_mismatch

ENGINE_PATH = Path(__file__).resolve().parents[1] / "rubato_waveform" / "engine.py"
SHOWN_KEYS = 10

MODES = {"numpy": [], "no_numpy": ["--no-numpy"]}


def capture(tmp_path: Path, mode: str) -> dict:
    out = tmp_path / f"{mode}.json"
    cmd = [sys.executable, str(GOLDEN_DIR / "capture.py"), "--engine", str(ENGINE_PATH),
           "--out", str(out), *MODES[mode]]
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert done.returncode == 0, f"capture.py failed ({mode}):\n{done.stderr[-2000:]}"
    return json.loads(out.read_text())


def diff_records(expected: dict, actual: dict) -> list[str]:
    """Keys whose value differs, is missing or is unexpected, sorted."""
    keys = set(expected) | set(actual)
    return sorted(k for k in keys if expected.get(k) != actual.get(k))


def assert_records_equal(expected: dict, actual: dict, label: str) -> None:
    bad = diff_records(expected, actual)
    if bad:
        shown = "\n  ".join(bad[:SHOWN_KEYS])
        more = f"\n  ... and {len(bad) - SHOWN_KEYS} more" if len(bad) > SHOWN_KEYS else ""
        pytest.fail(f"{label}: {len(bad)} of {len(expected)} records differ; first keys:\n  {shown}{more}")


@pytest.fixture(scope="module", params=sorted(MODES))
def captured(request, tmp_path_factory) -> tuple[str, dict]:
    mode = request.param
    return mode, capture(tmp_path_factory.mktemp(f"golden_{mode}"), mode)


def test_constant_records_match_manifest(captured) -> None:
    """Theme, layout, style and metric key hashes depend on neither floats nor Pillow."""
    mode, result = captured
    expected = {k: v for k, v in load_manifest()[mode]["records"].items() if k.startswith("const/")}
    actual = {k: v for k, v in result["records"].items() if k.startswith("const/")}
    assert expected, "the manifest has no const/ records"
    assert_records_equal(expected, actual, f"{mode} const records")


def test_frame_records_match_manifest(captured) -> None:
    """Every rendered frame and shaped track is bit-identical to the manifest.

    Low bits depend on libm and Pillow (the engine's sin-based pseudo-random amplifies
    them), so this is compared only on the platform the manifest was captured on.
    """
    mode, result = captured
    manifest = load_manifest()[mode]
    reason = pinned_env_mismatch(manifest["meta"], check_numpy=mode == "numpy")
    if reason:
        pytest.skip(reason)
    assert_records_equal(manifest["records"], result["records"], f"{mode} records")
