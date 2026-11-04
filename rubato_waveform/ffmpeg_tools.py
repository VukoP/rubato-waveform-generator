# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Locating FFmpeg and ffprobe, and checking that they can encode ProRes.

A GUI app started from Finder does not inherit the shell PATH, so a PATH search
alone fails for Homebrew users. The search therefore also looks next to the
executable and in the usual per-OS install locations. A failed search is never
cached, so a "Re-check" button works right after the user installs FFmpeg.

The PATH search only visits absolute entries: ``shutil.which`` on Windows looks in the
current directory first, and an empty or relative entry means the same thing, so an
``ffmpeg.exe`` dropped into a Downloads folder would otherwise run. The install locations
here are ones a regular local user cannot pre-create.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ENV_VAR = "RUBATO_FFMPEG"
PRORES_ENCODER = "prores_ks"

# Placed before every ``-i`` of FFmpeg and ffprobe: no network or playlist protocols, and only
# plain audio/video containers. Without the format list, a file named ``.mp3`` could still be
# an HLS playlist or an ``ffconcat`` script that makes FFmpeg read other local files. The names
# are libavformat demuxer names; a multi-name demuxer such as ``mov,mp4,m4a,3gp,3g2,mj2`` is
# allowed by any one of its names.
INPUT_WHITELISTS = (
    "-protocol_whitelist", "file,pipe",
    "-format_whitelist", "wav,mp3,mov,flac,ogg,aac,aiff,matroska,asf,caf",
)


class FFmpegNotFoundError(RuntimeError):
    """FFmpeg could not be found on this machine."""


def subprocess_flags() -> int:
    """``creationflags`` for every subprocess: no flashing console windows on Windows."""
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW
    return 0


def _exe_name(name: str, platform: str) -> str:
    return name + ".exe" if platform == "win32" else name


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _beside_executable() -> list[Path]:
    bases = [Path(sys.executable).parent]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        bases.append(Path(bundle))
    return [base / sub if sub else base for base in bases for sub in ("", "ffmpeg", "ffmpeg/bin")]


def _candidate_dirs(platform: str = sys.platform) -> list[Path]:
    """Usual install locations for the given platform, most likely first."""
    if platform == "darwin":
        return [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/opt/local/bin")]
    if platform == "win32":
        dirs = []
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "WinGet" / "Links")
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            dirs.append(Path(program_files) / "ffmpeg" / "bin")
        profile = os.environ.get("USERPROFILE")
        if profile:
            dirs.append(Path(profile) / "scoop" / "shims")
        return dirs
    return [Path("/usr/bin"), Path("/usr/local/bin"), Path("/snap/bin")]


def _on_path(name: str) -> Path | None:
    """First executable ``name`` in an absolute PATH entry; empty and relative entries are skipped."""
    exe = _exe_name(name, sys.platform)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip('"')
        if entry and Path(entry).is_absolute():
            path = Path(entry) / exe
            if _is_executable(path):
                return path
    return None


def _search(name: str) -> Path | None:
    exe = _exe_name(name, sys.platform)
    for directory in _beside_executable():
        path = directory / exe
        if _is_executable(path):
            return path
    found = _on_path(name)
    if found is not None:
        return found
    for directory in _candidate_dirs():
        path = directory / exe
        if _is_executable(path):
            return path
    return None


def find_ffmpeg() -> Path | None:
    """Path to ffmpeg, or None. Order: RUBATO_FFMPEG, next to the app, PATH, usual install dirs."""
    override = os.environ.get(ENV_VAR)
    if override:
        path = Path(override).expanduser()
        if _is_executable(path):
            return path
    return _search("ffmpeg")


def find_ffprobe() -> Path | None:
    """Path to ffprobe, or None. Looks next to the found ffmpeg first."""
    ffmpeg = find_ffmpeg()
    if ffmpeg is not None:
        sibling = ffmpeg.with_name(_exe_name("ffprobe", sys.platform))
        if _is_executable(sibling):
            return sibling
    return _search("ffprobe")


@dataclass(frozen=True)
class FFmpegStatus:
    ffmpeg: Path | None
    ffprobe: Path | None
    has_prores: bool
    problem: str | None = None

    @property
    def ok(self) -> bool:
        return self.problem is None


def _has_prores(ffmpeg: Path) -> bool:
    try:
        out = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=False, timeout=30,
            stdin=subprocess.DEVNULL, creationflags=subprocess_flags(),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(len(parts := line.split()) > 1 and parts[1] == PRORES_ENCODER for line in out.splitlines())


def check() -> FFmpegStatus:
    """Looks for FFmpeg and ffprobe afresh and reports what is missing, if anything."""
    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe()
    if ffmpeg is None:
        return FFmpegStatus(None, ffprobe, False, "FFmpeg was not found.")
    has_prores = _has_prores(ffmpeg)
    problem = None
    if not has_prores:
        problem = f"This FFmpeg build has no {PRORES_ENCODER} (ProRes) encoder."
    elif ffprobe is None:
        problem = "ffprobe was not found next to FFmpeg."
    return FFmpegStatus(ffmpeg, ffprobe, has_prores, problem)


def install_command(platform: str = sys.platform) -> str:
    """The command that installs FFmpeg on the given platform (a sys.platform value); valid as typed."""
    if platform == "darwin":
        return "brew install ffmpeg"
    if platform == "win32":
        return "winget install Gyan.FFmpeg"
    return "sudo apt install ffmpeg"


def install_alternative(platform: str = sys.platform) -> str | None:
    """Another way to install it, as display text only (not a command); None if there is none."""
    if platform in ("darwin", "win32"):
        return None
    return "Fedora: enable RPM Fusion, then sudo dnf install ffmpeg"


def install_hint(platform: str = sys.platform) -> str:
    """How to install FFmpeg, in one line for the command line."""
    alternative = install_alternative(platform)
    return install_command(platform) + (f"  ({alternative})" if alternative else "")
