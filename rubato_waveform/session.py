# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Application session: the state machine between the HTTP server and the engine.

No HTTP and no GUI toolkit here. Native dialogs come in through the small ``Dialogs``
interface. The session owns the loaded audio, its analysis, the export and the playback
fallback; analysis, export and transcoding each run on a worker thread. Every shared
field is guarded by one lock that is never held during slow work or while a dialog is open.

Analysis and export are tracked separately: a failed or cancelled export never touches the
loaded audio or its analysis. ``state()`` is what the UI polls; it carries file names only,
never directories, and error strings are scrubbed of paths.
"""

from __future__ import annotations

import copy
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Protocol

from PIL import Image

from . import ffmpeg_tools, settings
from .catalog import look_from_dict
from .engine import (
    Cancelled,
    Spectrum,
    _kill,
    _remove_file,
    _StderrDrain,
    analyze_audio,
    downscale_premultiplied,
    make_frame,
    render_movie,
    shape_track,
)
from .ffmpeg_tools import FFmpegNotFoundError

log = logging.getLogger(__name__)

PREVIEW_MIN = 16
PREVIEW_MAX = 4096
JOIN_TIMEOUT = 10.0            # seconds shutdown waits for all workers together
MAX_PATH = 4096
MAX_MESSAGE = 300
INPUT_ERROR_SECONDS = 8.0       # how long a refused drop stays on screen

STEP_AUDIO, STEP_ANALYSIS, STEP_STYLE, STEP_EXPORT = 0, 1, 2, 3

_MIME = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
    ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/ogg", ".aif": "audio/aiff", ".aiff": "audio/aiff", ".webm": "audio/webm",
    ".wma": "audio/x-ms-wma", ".mov": "video/quicktime", ".m4v": "video/mp4",
    ".mka": "audio/x-matroska", ".mkv": "video/x-matroska", ".caf": "audio/x-caf",
}
# Only these are accepted as input (video containers too: people extract audio from them). Anything
# else, such as a playlist, is refused before FFmpeg sees it; FFmpeg's format whitelist backs that up.
ALLOWED_EXTENSIONS = frozenset(_MIME)

# A path may contain spaces: one is taken as part of it when a word with a separator follows.
_PATH_RE = re.compile(r"(?<![\w.-])(?:[A-Za-z]:[\\/]|\\\\|/)(?:[^\s'\"<>|*?]| (?=[^\s/\\:]+[\\/]))+")


class SessionError(Exception):
    """A request the session refuses; the message is safe to show to the user."""


class NoAnalysisError(SessionError):
    """There is no finished analysis to preview or export."""


class Dialogs(Protocol):
    def pick_audio(self, start_dir: str | None) -> str | None: ...
    def pick_output(self, start_dir: str | None, suggested_name: str) -> str | None: ...
    def reveal(self, path: str) -> None: ...


def reveal_in_file_manager(path: str) -> None:
    """Shows a file in Finder, Explorer or (on Linux) opens its folder with xdg-open."""
    target = Path(path).absolute()
    if sys.platform == "darwin":
        cmd = ["open", "-R", str(target)]
    elif sys.platform == "win32":
        cmd = ["explorer", f"/select,{target}"]
    else:
        cmd = ["xdg-open", str(target.parent)]
    subprocess.run(cmd, check=False, timeout=15, stdin=subprocess.DEVNULL,
                   creationflags=ffmpeg_tools.subprocess_flags())


def _remove_tree(path: Path) -> None:
    """Deletes a directory, retrying briefly: Windows keeps files locked until their writer exits."""
    for attempt in range(20):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 19:
                return
            time.sleep(0.05)


def _clean_message(exc_or_text: object, known: tuple[Path | None, ...] = ()) -> str:
    """One line of error text with no directories in it.

    Known paths are replaced by their file name first (that also handles spaces in folder
    names; a known folder in front of a name is dropped altogether); any other absolute-looking path is cut down to its last component.
    """
    text = exc_or_text if isinstance(exc_or_text, str) else (str(exc_or_text) or type(exc_or_text).__name__)
    for path in sorted((p for p in known if p is not None), key=lambda p: len(str(p)), reverse=True):
        for variant in {str(path), path.as_posix()}:
            if variant and variant not in ("/", "."):
                text = re.sub(re.escape(variant) + r"[\\/]+", "", text)   # a folder in front of a name: drop it
                text = text.replace(variant, path.name)
    text = _PATH_RE.sub(lambda m: re.split(r"[\\/]", m.group(0).rstrip("\\/"))[-1], text)
    return " ".join(text.split())[:MAX_MESSAGE] or "Unknown error."


def _checked_dir(value: object) -> str | None:
    return value if isinstance(value, str) and os.path.isdir(value) else None


class Session:
    def __init__(self, dialogs: Dialogs, settings_dir: Path | str | None = None) -> None:
        self._dialogs = dialogs
        self._settings_dir = Path(settings_dir) if settings_dir is not None else None
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()       # orders settings writes; never taken inside _lock
        self._render_lock = threading.Lock()   # one preview render at a time
        self._settings = settings.load(self._settings_dir)
        self._ffmpeg = ffmpeg_tools.check()
        self._closed = False
        self._tmpdir: Path | None = None
        self._threads: list[threading.Thread] = []
        self._gen = 0

        self._audio_path: Path | None = None
        self._analysis = None
        self._spectrum: Spectrum | None = None
        self._analysis_state = "idle"          # idle | analyzing | ready | failed
        self._analysis_progress = 0.0
        self._analysis_error: str | None = None
        self._input_error: str | None = None   # a dropped file that was refused; cleared by the next selection or export
        self._input_error_at = 0.0             # time.monotonic() of that refusal; it expires after INPUT_ERROR_SECONDS
        self._analysis_cancel = threading.Event()

        self._export_state = "idle"            # idle | rendering | finalizing | done | failed | cancelled
        self._export_starting = False          # the save dialog is open
        self._export_current = 0
        self._export_total = 0
        self._export_error: str | None = None
        self._export_cancel = threading.Event()
        self._last_output: Path | None = None

        self._audio_version = 0
        self._play_mode = "native"             # native | transcoding | fallback | failed
        self._play_error: str | None = None
        self._fallback: Path | None = None
        self._transcode_cancel = threading.Event()

    # ------------------------------------------------------------------ audio

    def open_audio(self) -> bool:
        """Asks for a file and selects it; False if the dialog was cancelled."""
        with self._lock:
            start = _checked_dir(self._settings["last_input_dir"])
        chosen = self._dialogs.pick_audio(start)
        if not chosen:
            return False
        self.select_audio(chosen)
        return True

    def select_audio(self, path: str | os.PathLike) -> None:
        if not isinstance(path, (str, os.PathLike)) or "\0" in os.fspath(path) or not os.fspath(path):
            raise SessionError("Choose an existing audio file.")
        audio = Path(path).absolute()
        if not audio.is_file():
            raise SessionError("Choose an existing audio file.")
        if audio.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise SessionError("This file type is not supported. Use one of: "
                               + ", ".join(sorted(ALLOWED_EXTENSIONS)) + ".")
        with self._lock:
            self._require_open_locked()
            if self._export_busy_locked():
                raise SessionError("The audio cannot be changed while an export is running.")
            stale = self._clear_audio_locked()
            gen = self._gen
            self._audio_path = audio
            self._analysis_state = "analyzing"
            cancel = self._analysis_cancel
            self._spawn_locked(self._analyze, gen, audio, cancel)
            self._settings["last_input_dir"] = str(audio.parent)
        if stale is not None:
            _remove_file(stale)
        self._persist()

    def _analyze(self, gen: int, audio: Path, cancel: threading.Event) -> None:
        def progress(value: float) -> None:
            with self._lock:
                if gen == self._gen:
                    self._analysis_progress = value

        try:
            analysis = analyze_audio(audio, progress=progress, cancel=cancel)
        except Cancelled:
            return
        except Exception as exc:  # noqa: BLE001 - a failure is reported in the state, never a dead thread
            status = ffmpeg_tools.check() if isinstance(exc, FFmpegNotFoundError) else None
            message = _clean_message(exc, (audio,))
            with self._lock:
                if gen != self._gen:
                    return
                stale = self._clear_audio_locked()
                self._analysis_state = "failed"
                self._analysis_error = message
                if status is not None:
                    self._ffmpeg = status
            if stale is not None:
                _remove_file(stale)
            return
        spectrum = (Spectrum(analysis.spectrum, analysis.spectrum_rate, analysis.spectrum_bands)
                    if analysis.spectrum is not None else None)
        with self._lock:
            if gen != self._gen:
                return
            self._analysis = analysis
            self._spectrum = spectrum
            self._analysis_state = "ready"
            self._analysis_progress = 1.0

    def _clear_audio_locked(self) -> Path | None:
        """Forgets the loaded audio and everything derived from it; returns a fallback file to delete.

        Cancels the running analysis and transcode and invalidates their results. Export
        state is only reset to idle when no export is running (callers check that first).
        """
        self._analysis_cancel.set()
        self._transcode_cancel.set()
        self._gen += 1
        self._analysis_cancel = threading.Event()
        self._transcode_cancel = threading.Event()
        stale, self._fallback = self._fallback, None
        self._audio_path = None
        self._analysis = None
        self._spectrum = None
        self._analysis_state = "idle"
        self._analysis_progress = 0.0
        self._analysis_error = None
        self._input_error = None
        self._play_mode = "native"
        self._play_error = None
        self._audio_version += 1
        if not self._export_busy_locked():
            self._export_state = "idle"
            self._export_current = self._export_total = 0
            self._export_error = None
        return stale

    # --------------------------------------------------------------- playback

    def audio_file(self) -> tuple[Path, str, int] | None:
        """The file the browser should play: ``(path, mime, version)``. For the server only."""
        with self._lock:
            if self._audio_path is None:
                return None
            if self._fallback is not None:
                return self._fallback, "audio/wav", self._audio_version
            mime = _MIME.get(self._audio_path.suffix.lower(), "application/octet-stream")
            return self._audio_path, mime, self._audio_version

    def ensure_playable(self) -> None:
        """Transcodes the audio to PCM WAV so the browser can play it (idempotent).

        WAV rather than AAC: it has no encoder priming offset, so ``currentTime`` stays exact.
        """
        with self._lock:
            self._require_open_locked()
            if self._audio_path is None:
                raise SessionError("No audio is loaded.")
            if self._play_mode != "native":
                return
            self._play_mode = "transcoding"
            gen, source, cancel = self._gen, self._audio_path, self._transcode_cancel
            if self._tmpdir is None:
                self._tmpdir = Path(tempfile.mkdtemp(prefix="rubato-"))
            target = self._tmpdir / f"play-{gen}.wav"
            self._spawn_locked(self._transcode, gen, source, target, cancel)

    def _transcode(self, gen: int, source: Path, target: Path, cancel: threading.Event) -> None:
        error = None
        ffmpeg = ffmpeg_tools.find_ffmpeg()
        if ffmpeg is None:
            error = "FFmpeg was not found."
        else:
            cmd = [str(ffmpeg), "-nostdin", "-v", "error", *ffmpeg_tools.INPUT_WHITELISTS,
                   "-i", str(source), "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le",
                   "-f", "wav", "-y", str(target)]
            try:
                error = self._run_transcode(cmd, cancel)
            except Exception as exc:  # noqa: BLE001 - reported in the state
                error = _clean_message(exc, (source, target))
        stale = True
        with self._lock:
            if gen == self._gen and not self._closed:
                stale = False
                if error is None:
                    self._fallback = target
                    self._play_mode = "fallback"
                    self._audio_version += 1
                else:
                    self._play_mode = "failed"
                    self._play_error = _clean_message(error, (source, target))
        if stale or error is not None:
            _remove_file(target)

    @staticmethod
    def _run_transcode(cmd: list[str], cancel: threading.Event) -> str | None:
        """Runs FFmpeg until it exits or ``cancel`` is set; returns an error text or None."""
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, creationflags=ffmpeg_tools.subprocess_flags())
        assert proc.stderr is not None
        drain = _StderrDrain(proc.stderr)
        try:
            while proc.poll() is None:
                if cancel.wait(0.05):
                    return "Cancelled."
        finally:
            _kill(proc)
        text = drain.text()
        return None if proc.returncode == 0 else (text or "Could not convert the audio.")

    # ---------------------------------------------------------------- preview

    def render_preview(self, look_dict: object, t: object, max_w: object, max_h: object) -> tuple[int, int, bytes]:
        """One frame as straight-alpha RGBA, fitted into ``max_w`` x ``max_h`` and never upscaled."""
        look = look_from_dict(look_dict)
        for value in (max_w, max_h):
            if type(value) is not int or not PREVIEW_MIN <= value <= PREVIEW_MAX:
                raise ValueError(f"preview size must be an integer from {PREVIEW_MIN} to {PREVIEW_MAX}")
        if type(t) not in (int, float):
            raise ValueError("t: expected a finite number")
        try:
            t = float(t)                       # a huge integer overflows here, not in isfinite
        except OverflowError:
            raise ValueError("t: expected a finite number") from None
        if not math.isfinite(t):
            raise ValueError("t: expected a finite number")
        with self._lock:
            analysis, spectrum = self._analysis, self._spectrum
        if analysis is None:
            raise NoAnalysisError("There is no analysed audio to preview.")
        t = min(max(t, 0.0), analysis.duration)
        # Shape only a window around t: the preview is instant even for hour-long audio and
        # looks identical to the full render.
        margin = look.window_seconds + 0.5
        start = max(0, int((t - margin) * analysis.slot_rate))
        end = min(analysis.slots, int((t + margin) * analysis.slot_rate) + 1)
        with self._render_lock:
            track = shape_track(analysis, look, start, end)
            frame = make_frame(track, analysis.slot_rate, t, look, track_offset=start, spectrum=spectrum)
            scale = min(max_w / frame.width, max_h / frame.height, 1.0)
            if scale < 1.0:
                size = (max(1, round(frame.width * scale)), max(1, round(frame.height * scale)))
                frame = downscale_premultiplied(frame, size)
        # The engine's frames are premultiplied; the browser wants straight alpha.
        straight = Image.frombytes("RGBa", frame.size, frame.tobytes()).convert("RGBA")
        return frame.width, frame.height, straight.tobytes()

    # ----------------------------------------------------------------- export

    def start_export(self, look_dict: object) -> bool:
        """Asks where to save and starts rendering; False if the save dialog was cancelled.

        The output path comes only from the dialog, never from the caller.
        """
        look = look_from_dict(look_dict)
        with self._lock:
            self._require_open_locked()
            if self._export_busy_locked():
                raise SessionError("An export is already running.")
            if self._analysis_state == "analyzing":
                raise SessionError("Wait for the analysis to finish.")
            analysis, audio, gen = self._analysis, self._audio_path, self._gen
            if analysis is None or audio is None:
                raise NoAnalysisError("Choose an audio file first.")
            start_dir = _checked_dir(self._settings["last_output_dir"]) or str(audio.parent)
            self._export_starting = True
        try:
            chosen = self._dialogs.pick_output(start_dir, f"{audio.stem}_waveform.mov")
            if not chosen:
                return False
            output = self._checked_output(chosen, audio)
            cancel = threading.Event()
            with self._lock:
                self._require_open_locked()
                if gen != self._gen:
                    raise SessionError("The audio changed while the save dialog was open.")
                self._export_state = "rendering"
                self._export_current, self._export_total = 0, analysis.frames(look.fps)
                self._export_error = None
                self._input_error = None
                self._export_cancel = cancel
                self._settings["last_output_dir"] = str(output.parent)
                self._spawn_locked(self._export, analysis, output, look, cancel, audio)
        finally:
            with self._lock:
                self._export_starting = False
        self._persist()
        return True

    @staticmethod
    def _checked_output(chosen: object, audio: Path) -> Path:
        if isinstance(chosen, os.PathLike):
            chosen = os.fspath(chosen)
        # isinstance, not type(): the macOS save dialog returns an NSString, a str subclass
        if not isinstance(chosen, str) or not chosen or "\0" in chosen or len(chosen) > MAX_PATH:
            raise SessionError("The chosen file name is not valid.")
        output = Path(chosen).absolute()
        if output.suffix.lower() != ".mov":
            output = output.with_name(output.name + ".mov")
            if os.path.lexists(output):    # the save dialog never confirmed replacing this file
                raise SessionError(f"{output.name} already exists. Choose another name, "
                                   "or end the name with .mov to replace it.")
        try:
            same = output.exists() and os.path.samefile(output, audio)
        except OSError:
            same = False
        if same:
            raise SessionError("Choose a different file name than the audio file.")
        return output

    def _export(self, analysis, output: Path, look, cancel: threading.Event, audio: Path) -> None:
        def progress(current: int, total: int) -> None:
            with self._lock:
                self._export_current, self._export_total = current, total
                if current >= total:
                    if cancel.is_set():
                        raise Cancelled   # a cancel that arrived before "finalizing" is honoured, never dropped
                    self._export_state = "finalizing"   # all frames written; FFmpeg is still closing the file

        try:
            render_movie(analysis, output, look, progress=progress, cancel=cancel)
        except Cancelled:
            with self._lock:
                self._export_state = "cancelled"
                self._input_error = None
        except Exception as exc:  # noqa: BLE001 - reported in the state
            message = _clean_message(exc, (output, output.parent, audio))
            with self._lock:
                self._export_state = "failed"
                self._export_error = message
                self._input_error = None
        else:
            with self._lock:
                self._export_state = "done"
                self._input_error = None
                self._last_output = output

    def cancel_export(self) -> bool:
        """Cancels a rendering export; False if there is nothing to cancel or it is already finalizing."""
        with self._lock:
            if self._export_state != "rendering":
                return False
            self._export_cancel.set()
            return True

    def is_exporting(self) -> bool:
        """True while an export is starting, rendering or finalizing."""
        with self._lock:
            return self._export_busy_locked()

    def report_input_error(self, text: object) -> None:
        """Shows a refused input (a dropped file that cannot be used) through the state's ``error``."""
        message = _clean_message(text)
        with self._lock:
            self._input_error = message
            self._input_error_at = time.monotonic()

    def reveal_last(self) -> bool:
        """Shows the last successfully exported file; never takes a path from the caller."""
        with self._lock:
            target = self._last_output
        if target is None or not target.is_file():
            return False
        try:
            self._dialogs.reveal(str(target))
        except Exception as exc:  # noqa: BLE001 - a failed reveal must not break the UI
            log.warning("could not reveal the exported file: %s", _clean_message(exc, (target,)))
            return False
        return True

    # ----------------------------------------------------------------- FFmpeg

    def recheck_ffmpeg(self) -> dict:
        status = ffmpeg_tools.check()
        with self._lock:
            self._ffmpeg = status
            return self._ffmpeg_dict_locked()

    def _ffmpeg_dict_locked(self) -> dict:
        status = self._ffmpeg
        return {"ok": status.ok, "has_prores": status.has_prores, "problem": status.problem,
                "install_command": None if status.ok else ffmpeg_tools.install_command(),
                "install_alternative": None if status.ok else ffmpeg_tools.install_alternative()}

    # --------------------------------------------------------------- settings

    def get_settings(self) -> dict:
        """The look-related settings; the last-used directories are never exposed."""
        with self._lock:
            return self._settings_view_locked()

    def save_settings(self, data: object) -> dict:
        if not isinstance(data, dict):
            raise ValueError("settings: expected an object")  # noqa: TRY004
        with self._lock:
            merged = copy.deepcopy(self._settings)
            for key in ("look", "theme", "gradient", "auto_format"):
                if key in data:
                    merged[key] = data[key]
            clean = settings.validate_settings_dict(merged)
            for key in ("last_input_dir", "last_output_dir"):
                clean[key] = self._settings[key]
            self._settings = clean
            view = self._settings_view_locked()
        self._persist()
        return view

    def _settings_view_locked(self) -> dict:
        return copy.deepcopy({key: self._settings[key] for key in ("look", "theme", "gradient", "auto_format")})

    def _persist(self) -> None:
        with self._io_lock:
            with self._lock:
                snapshot = copy.deepcopy(self._settings)
            settings.save(snapshot, self._settings_dir)

    # ------------------------------------------------------------------ state

    def state(self) -> dict:
        """A JSON-serialisable snapshot for polling: file names only, no directories."""
        with self._lock:
            if self._input_error and time.monotonic() - self._input_error_at > INPUT_ERROR_SECONDS:
                self._input_error = None
            audio = None
            if self._audio_path is not None:
                ready = self._analysis is not None
                audio = {
                    "name": self._audio_path.name,
                    "extension": self._audio_path.suffix.lower().lstrip("."),
                    "duration": self._analysis.duration if ready else None,
                    "has_spectrum": ready and self._analysis.spectrum is not None,
                }
            if self._analysis_state == "analyzing":
                step = STEP_ANALYSIS
            elif self._analysis_state != "ready":
                step = STEP_AUDIO
            elif self._export_state == "idle":
                step = STEP_STYLE
            else:
                step = STEP_EXPORT
            return {
                "audio": audio,
                "step": step,
                "analysis": {"state": self._analysis_state, "progress": self._analysis_progress,
                             "error": self._analysis_error},
                "export": {"state": self._export_state, "current": self._export_current,
                           "total": self._export_total, "error": self._export_error,
                           "output_name": self._last_output.name if self._last_output else None,
                           "can_reveal": self._last_output is not None},
                "audio_version": self._audio_version,
                "playback": {"mode": self._play_mode, "error": self._play_error},
                "error": self._analysis_error or self._export_error or self._play_error or self._input_error,
                "ffmpeg": self._ffmpeg_dict_locked(),
            }

    # --------------------------------------------------------------- shutdown

    def shutdown(self) -> None:
        """Cancels and joins the workers (bounded), then removes the session's temp files. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._gen += 1
            for event in (self._analysis_cancel, self._export_cancel, self._transcode_cancel):
                event.set()
            threads = list(self._threads)
            tmpdir, self._tmpdir = self._tmpdir, None
            self._fallback = None
        deadline = time.monotonic() + JOIN_TIMEOUT
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if tmpdir is not None:
            _remove_tree(tmpdir)

    # ---------------------------------------------------------------- helpers

    def _require_open_locked(self) -> None:
        if self._closed:
            raise SessionError("The session has been shut down.")

    def _export_busy_locked(self) -> bool:
        return self._export_starting or self._export_state in ("rendering", "finalizing")

    def _spawn_locked(self, target, *args) -> None:
        self._threads = [t for t in self._threads if t.is_alive()]
        thread = threading.Thread(target=target, args=args, daemon=True)
        self._threads.append(thread)
        thread.start()
