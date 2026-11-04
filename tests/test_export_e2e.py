# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""End to end: a real render is transparent ProRes 4444 and replaces the output safely."""

import json
import subprocess
from pathlib import Path

import pytest
from conftest import requires_ffmpeg

from rubato_waveform import ffmpeg_tools
from rubato_waveform.engine import Look, analyze_audio, render_movie

pytestmark = requires_ffmpeg

LOOK = Look(width=320, height=64, fps=20, bars=45)


@pytest.fixture(scope="module")
def analysis(demo_wav):
    return analyze_audio(demo_wav)


def _ffprobe(path, *args) -> str:
    exe = ffmpeg_tools.find_ffprobe()
    if exe is None:
        pytest.skip("ffprobe not found")
    return subprocess.run([str(exe), "-v", "error", *args, str(path)],
                          check=True, capture_output=True, text=True).stdout


def _alpha_bytes(path) -> bytes:
    ffmpeg = ffmpeg_tools.find_ffmpeg()
    return subprocess.run([str(ffmpeg), "-v", "error", "-i", str(path), "-vf", "alphaextract",
                           "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                          check=True, capture_output=True).stdout


def test_render_is_transparent_prores_4444(analysis, tmp_path) -> None:
    out = tmp_path / "wave.mov"
    render_movie(analysis, out, LOOK)

    info = json.loads(_ffprobe(out, "-select_streams", "v:0", "-count_frames", "-show_entries",
                               "stream=codec_name,profile,pix_fmt,nb_read_frames", "-of", "json"))
    stream = info["streams"][0]
    assert stream["codec_name"] == "prores"
    assert "4444" in stream["profile"]
    assert stream["pix_fmt"].startswith("yuva444p")
    assert int(stream["nb_read_frames"]) == analysis.frames(LOOK.fps)

    alpha = _alpha_bytes(out)
    assert len(alpha) == LOOK.width * LOOK.height * analysis.frames(LOOK.fps)
    assert 0 in alpha and any(alpha), "alpha plane must contain both transparent and visible pixels"


def test_success_replaces_existing_output_and_leaves_no_temp_file(analysis, tmp_path) -> None:
    out = tmp_path / "wave.mov"
    out.write_bytes(b"old render")
    render_movie(analysis, out, LOOK)
    assert out.read_bytes() != b"old render"
    assert _ffprobe(out, "-show_entries", "stream=codec_name", "-of", "csv=p=0").strip() == "prores"
    assert list(tmp_path.iterdir()) == [out]


def test_leading_dash_output_name_is_not_an_option(analysis, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    render_movie(analysis, Path("-dash.mov"), LOOK)
    assert (tmp_path / "-dash.mov").is_file()


def test_gradient_flow_is_premultiplied_safe(analysis, tmp_path) -> None:
    """Straight alpha: no channel may exceed alpha beyond 10-bit ProRes rounding (the old engine hit 254)."""
    out = tmp_path / "flow.mov"
    look = Look(width=320, height=64, fps=20, bars=45, layout="flow").with_theme("Violet", gradient=True)
    render_movie(analysis, out, look)
    ffmpeg = ffmpeg_tools.find_ffmpeg()
    frame = analysis.frames(look.fps) // 2
    raw = subprocess.run([str(ffmpeg), "-v", "error", "-i", str(out), "-vf", f"select=eq(n\\,{frame})",
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                         check=True, capture_output=True).stdout
    assert len(raw) == look.width * look.height * 4
    visible = 0
    for i in range(0, len(raw), 4):
        r, g, b, a = raw[i:i + 4]
        if a > 0:
            visible += 1
            assert max(r, g, b) <= a + 2
    assert visible > 0
