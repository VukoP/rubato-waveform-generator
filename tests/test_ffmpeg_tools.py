# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""FFmpeg discovery, install hints, strict colour parsing and the stderr deadlock regression."""

import os
import re
import stat
import sys
import threading
from pathlib import Path

import pytest
from conftest import make_synthetic_analysis

from rubato_waveform import engine, ffmpeg_tools
from rubato_waveform.engine import Look, analyze_audio, parse_hex, render_movie

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake FFmpeg is a POSIX shell script")
JOIN_TIMEOUT = 30.0


def make_fake(directory: Path, name: str = "ffmpeg", body: str = "exit 0\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + (".exe" if sys.platform == "win32" else ""))
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """No env override, nothing on PATH, nothing beside the executable, no candidates."""
    monkeypatch.delenv(ffmpeg_tools.ENV_VAR, raising=False)
    monkeypatch.setattr(ffmpeg_tools, "_on_path", lambda name: None)
    monkeypatch.setattr(ffmpeg_tools, "_beside_executable", list)
    monkeypatch.setattr(ffmpeg_tools, "_candidate_dirs", lambda platform=sys.platform: [])
    return tmp_path


def test_nothing_found_is_not_cached(isolated, monkeypatch) -> None:
    assert ffmpeg_tools.find_ffmpeg() is None
    fake = make_fake(isolated / "late")
    monkeypatch.setenv(ffmpeg_tools.ENV_VAR, str(fake))
    assert ffmpeg_tools.find_ffmpeg() == fake


def test_env_var_wins_over_path(isolated, monkeypatch) -> None:
    mine = make_fake(isolated / "mine")
    other = make_fake(isolated / "other")
    monkeypatch.setenv(ffmpeg_tools.ENV_VAR, str(mine))
    monkeypatch.setattr(ffmpeg_tools, "_on_path", lambda name: other)
    assert ffmpeg_tools.find_ffmpeg() == mine


def test_env_var_pointing_nowhere_falls_through(isolated, monkeypatch) -> None:
    other = make_fake(isolated / "other")
    monkeypatch.setenv(ffmpeg_tools.ENV_VAR, str(isolated / "missing"))
    monkeypatch.setattr(ffmpeg_tools, "_on_path", lambda name: other)
    assert ffmpeg_tools.find_ffmpeg() == other


def test_found_through_path(isolated, monkeypatch) -> None:
    fake = make_fake(isolated / "bin")
    monkeypatch.setattr(ffmpeg_tools, "_on_path", lambda name: fake)
    assert ffmpeg_tools.find_ffmpeg() == fake


def test_found_beside_the_executable(isolated, monkeypatch) -> None:
    fake = make_fake(isolated / "app" / "ffmpeg" / "bin")
    monkeypatch.setattr(ffmpeg_tools, "_beside_executable", lambda: [isolated / "app", isolated / "app" / "ffmpeg" / "bin"])
    assert ffmpeg_tools.find_ffmpeg() == fake


def test_beside_executable_covers_sys_executable_and_bundle(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "tool"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    dirs = ffmpeg_tools._beside_executable()
    for base in (tmp_path / "app", tmp_path / "bundle"):
        assert {base, base / "ffmpeg", base / "ffmpeg" / "bin"} <= set(dirs)


def test_found_through_candidate_list(isolated, monkeypatch) -> None:
    fake = make_fake(isolated / "candidate")
    monkeypatch.setattr(ffmpeg_tools, "_candidate_dirs", lambda platform=sys.platform: [isolated / "nope", isolated / "candidate"])
    assert ffmpeg_tools.find_ffmpeg() == fake


def test_ffprobe_prefers_the_one_next_to_ffmpeg(isolated, monkeypatch) -> None:
    ffmpeg = make_fake(isolated / "a")
    probe_next = make_fake(isolated / "a", "ffprobe")
    other = make_fake(isolated / "b", "ffprobe")
    monkeypatch.setenv(ffmpeg_tools.ENV_VAR, str(ffmpeg))
    monkeypatch.setattr(ffmpeg_tools, "_on_path", lambda name: other)
    assert ffmpeg_tools.find_ffprobe() == probe_next


def test_ffprobe_falls_back_to_the_search(isolated, monkeypatch) -> None:
    ffmpeg = make_fake(isolated / "a")
    other = make_fake(isolated / "b", "ffprobe")
    monkeypatch.setenv(ffmpeg_tools.ENV_VAR, str(ffmpeg))
    monkeypatch.setattr(ffmpeg_tools, "_on_path", lambda name: other if name == "ffprobe" else None)
    assert ffmpeg_tools.find_ffprobe() == other


def test_candidate_dirs_per_platform(monkeypatch) -> None:
    assert Path("/opt/homebrew/bin") in ffmpeg_tools._candidate_dirs("darwin")
    assert Path("/snap/bin") in ffmpeg_tools._candidate_dirs("linux")
    monkeypatch.setenv("LOCALAPPDATA", "L")
    monkeypatch.setenv("USERPROFILE", "U")
    windows = ffmpeg_tools._candidate_dirs("win32")
    assert Path("L") / "Microsoft" / "WinGet" / "Links" in windows
    assert Path("U") / "scoop" / "shims" in windows


def test_windows_candidates_exclude_user_creatable_locations(monkeypatch) -> None:
    monkeypatch.setenv("ProgramData", "PD")
    monkeypatch.setenv("ProgramFiles", "PF")
    windows = ffmpeg_tools._candidate_dirs("win32")
    assert Path("PF") / "ffmpeg" / "bin" in windows
    assert Path("C:/ffmpeg/bin") not in windows
    assert Path("PD") / "chocolatey" / "bin" not in windows


@posix_only
def test_path_search_ignores_relative_and_empty_entries(monkeypatch, tmp_path) -> None:
    make_fake(tmp_path / "cwd")
    make_fake(tmp_path / "cwd" / "sub")
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "sub", "../cwd"]))
    assert ffmpeg_tools._on_path("ffmpeg") is None


@posix_only
def test_path_search_uses_absolute_entries(monkeypatch, tmp_path) -> None:
    fake = make_fake(tmp_path / "bin")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["", "relative", str(tmp_path / "empty"), str(tmp_path / "bin")]))
    assert ffmpeg_tools._on_path("ffmpeg") == fake


def test_path_search_adds_exe_on_windows(monkeypatch, tmp_path) -> None:
    (tmp_path / "ffmpeg.exe").write_text("")
    monkeypatch.setattr(ffmpeg_tools.sys, "platform", "win32")
    monkeypatch.setattr(ffmpeg_tools, "_is_executable", lambda path: path.is_file())
    monkeypatch.setenv("PATH", str(tmp_path))
    assert ffmpeg_tools._on_path("ffmpeg") == tmp_path / "ffmpeg.exe"


@posix_only
def test_check_reports_prores_and_missing_pieces(isolated, monkeypatch) -> None:
    assert not ffmpeg_tools.check().ok
    assert ffmpeg_tools.check().ffmpeg is None
    with_prores = make_fake(isolated / "p", body="echo ' V....D prores_ks           Apple ProRes (iCodec Pro)'\n")
    make_fake(isolated / "p", "ffprobe")
    monkeypatch.setenv(ffmpeg_tools.ENV_VAR, str(with_prores))
    status = ffmpeg_tools.check()
    assert status.ok and status.has_prores and status.ffprobe is not None
    without = make_fake(isolated / "q", body="echo ' V....D libx264  H.264'\n")
    monkeypatch.setenv(ffmpeg_tools.ENV_VAR, str(without))
    status = ffmpeg_tools.check()
    assert not status.ok and not status.has_prores and "prores_ks" in status.problem


@pytest.mark.parametrize(("platform", "expected"), [
    ("darwin", "brew install ffmpeg"),
    ("win32", "winget install Gyan.FFmpeg"),
    ("linux", "sudo apt install ffmpeg"),
])
def test_install_hint(platform, expected) -> None:
    assert expected in ffmpeg_tools.install_hint(platform)


def test_install_hint_linux_mentions_dnf() -> None:
    assert "dnf install ffmpeg" in ffmpeg_tools.install_hint("linux")


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_install_command_is_a_single_valid_command(platform) -> None:
    command = ffmpeg_tools.install_command(platform)
    assert "(" not in command and "  " not in command and "dnf" not in command
    assert ffmpeg_tools.install_hint(platform).startswith(command)


def test_install_alternative_is_display_text_for_linux_only() -> None:
    assert ffmpeg_tools.install_alternative("linux") == "Fedora: enable RPM Fusion, then sudo dnf install ffmpeg"
    assert ffmpeg_tools.install_alternative("darwin") is None
    assert ffmpeg_tools.install_alternative("win32") is None


def test_subprocess_flags(monkeypatch) -> None:
    monkeypatch.setattr(ffmpeg_tools.sys, "platform", "linux")
    assert ffmpeg_tools.subprocess_flags() == 0
    monkeypatch.setattr(ffmpeg_tools.sys, "platform", "win32")
    monkeypatch.setattr(ffmpeg_tools.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert ffmpeg_tools.subprocess_flags() == 0x08000000


def test_engine_reexports_the_error_class() -> None:
    assert engine.FFmpegNotFoundError is ffmpeg_tools.FFmpegNotFoundError
    assert issubclass(engine.FFmpegNotFoundError, RuntimeError)


# --- parse_hex -------------------------------------------------------------

@pytest.mark.parametrize(("text", "rgb"), [
    ("#8B4DFF", (139, 77, 255)),
    ("8b4dff", (139, 77, 255)),
    ("#000000", (0, 0, 0)),
    ("#FfFfFf", (255, 255, 255)),
])
def test_parse_hex_accepts(text, rgb) -> None:
    assert parse_hex(text) == rgb


@pytest.mark.parametrize("text", [
    "+1+1+1", "#+1+1+1", "12345", "#1234567", "gggggg", " #8B4DFF", "#8B4DFF ", "8B4D FF",
    "8B_4D_F", "#8B4DF\n", "##8B4DFF", "", "#", "-1-1-1", "0x8B4DFF",
])
def test_parse_hex_rejects(text) -> None:
    with pytest.raises(ValueError):
        parse_hex(text)


# --- stderr deadlock regression -------------------------------------------

STDERR_FLOOD = "head -c 131072 /dev/zero | tr '\\0' 'e' >&2\n"


def run_guarded(func):
    """Runs func in a daemon thread; fails instead of hanging the suite if it never returns."""
    box: dict = {}

    def target() -> None:
        try:
            box["result"] = func()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(JOIN_TIMEOUT)
    assert not thread.is_alive(), "deadlock: FFmpeg stderr was not drained"
    if "error" in box:
        raise box["error"]
    return box["result"]


@posix_only
def test_analyze_audio_survives_a_stderr_flood(isolated, monkeypatch) -> None:
    fake = make_fake(isolated / "fake", body=STDERR_FLOOD + "head -c 48000 /dev/zero\n")
    monkeypatch.setenv(ffmpeg_tools.ENV_VAR, str(fake))
    monkeypatch.setattr(engine, "find_ffprobe", lambda: None)
    audio = isolated / "in.wav"
    audio.write_bytes(b"x")
    analysis = run_guarded(lambda: analyze_audio(audio))
    assert analysis.slots == 100


@posix_only
def test_analyze_audio_error_message_is_the_stderr_tail(isolated, monkeypatch) -> None:
    fake = make_fake(isolated / "fake", body=STDERR_FLOOD + "echo 'the real error' >&2\nexit 3\n")
    monkeypatch.setenv(ffmpeg_tools.ENV_VAR, str(fake))
    monkeypatch.setattr(engine, "find_ffprobe", lambda: None)
    audio = isolated / "in.wav"
    audio.write_bytes(b"x")
    with pytest.raises(RuntimeError) as info:
        run_guarded(lambda: analyze_audio(audio))
    message = str(info.value)
    assert message.endswith("the real error")
    assert len(message) <= engine.STDERR_TAIL_BYTES


@posix_only
def test_render_movie_survives_a_stderr_flood(isolated, monkeypatch) -> None:
    fake = make_fake(isolated / "fake", body=STDERR_FLOOD + "cat > /dev/null\nfor last; do :; done\n: > \"$last\"\n")
    monkeypatch.setenv(ffmpeg_tools.ENV_VAR, str(fake))
    look = Look(width=64, height=32, fps=10, bars=16)
    run_guarded(lambda: render_movie(make_synthetic_analysis(), isolated / "out.mov", look))


@posix_only
def test_render_movie_failure_reports_the_tail(isolated, monkeypatch) -> None:
    fake = make_fake(isolated / "fake", body=STDERR_FLOOD + "cat > /dev/null\necho 'encoder broke' >&2\nexit 1\n")
    monkeypatch.setenv(ffmpeg_tools.ENV_VAR, str(fake))
    look = Look(width=64, height=32, fps=10, bars=16)
    with pytest.raises(RuntimeError, match=re.escape("encoder broke")):
        run_guarded(lambda: render_movie(make_synthetic_analysis(), isolated / "out.mov", look))


def test_engine_raises_a_clean_error_without_ffmpeg(isolated) -> None:
    audio = isolated / "in.wav"
    audio.write_bytes(b"x")
    with pytest.raises(engine.FFmpegNotFoundError):
        analyze_audio(audio)
    with pytest.raises(engine.FFmpegNotFoundError):
        render_movie(make_synthetic_analysis(), isolated / "out.mov", Look(width=64, height=32))


@posix_only
def test_render_writes_to_a_random_temp_name_and_never_overwrites(isolated, monkeypatch) -> None:
    record = isolated / "argv.txt"
    body = f'printf "%s\\n" "$@" > "{record}"\nfor last; do :; done\ncat > /dev/null\n: > "$last"\n'
    monkeypatch.setenv(ffmpeg_tools.ENV_VAR, str(make_fake(isolated / "fake", body=body)))
    out = isolated / "videos" / "clip.mov"
    render_movie(make_synthetic_analysis(), out, Look(width=320, height=64, fps=30, bars=45))
    argv = record.read_text().splitlines()
    temp = Path(argv[-1])
    assert re.fullmatch(r"clip\.part-[0-9a-f]{32}\.mov", temp.name) and temp.parent == out.parent
    assert "-n" in argv and "-y" not in argv
    assert out.is_file() and not temp.exists()
