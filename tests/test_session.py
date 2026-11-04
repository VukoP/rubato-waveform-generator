# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""The application session: state machine, stale results, export isolation, cleanup, privacy."""

import json
import stat
import statistics
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import pytest
from conftest import make_synthetic_analysis, requires_ffmpeg, wait_until

from rubato_waveform import engine, ffmpeg_tools, settings
from rubato_waveform import session as session_mod
from rubato_waveform.catalog import default_look_dict
from rubato_waveform.engine import THEMES, Cancelled
from rubato_waveform.session import NoAnalysisError, Session, SessionError

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake FFmpeg is a POSIX shell script")


class FakeDialogs:
    def __init__(self) -> None:
        self.audio: str | None = None
        self.output: str | None = None
        self.audio_starts: list = []
        self.output_calls: list = []
        self.revealed: list = []
        self.on_pick_output = None

    def pick_audio(self, start_dir):
        self.audio_starts.append(start_dir)
        return self.audio

    def pick_output(self, start_dir, suggested_name):
        self.output_calls.append((start_dir, suggested_name))
        if self.on_pick_output:
            self.on_pick_output()
        return self.output

    def reveal(self, path):
        self.revealed.append(path)


def long_analysis(seconds: int):
    base = make_synthetic_analysis()
    analysis = engine.Analysis(base.peak * (seconds // 3), base.rms * (seconds // 3))
    engine._calibrate(analysis)
    return analysis


def use_analysis(monkeypatch, analysis) -> None:
    monkeypatch.setattr(session_mod, "analyze_audio", lambda path, progress=None, cancel=None: analysis)


@pytest.fixture
def dialogs() -> FakeDialogs:
    return FakeDialogs()


@pytest.fixture
def make_session(tmp_path, dialogs):
    made: list[Session] = []

    def factory(settings_dir=None) -> Session:
        session = Session(dialogs, settings_dir or tmp_path / "cfg")
        made.append(session)
        return session

    yield factory
    for session in made:
        session.shutdown()


@pytest.fixture
def audio(tmp_path) -> Path:
    path = tmp_path / "work" / "talk.wav"
    path.parent.mkdir()
    path.write_bytes(b"not really audio")
    return path


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


def assert_all_exited(procs) -> None:
    assert procs, "expected a process to have been started"
    assert all(wait_until(lambda p=p: p.poll() is not None) for p in procs)


def analysis_state(session: Session) -> str:
    return session.state()["analysis"]["state"]


def export_state(session: Session) -> str:
    return session.state()["export"]["state"]


def ready_session(make_session, monkeypatch, audio, analysis=None) -> Session:
    use_analysis(monkeypatch, analysis or make_synthetic_analysis())
    session = make_session()
    session.select_audio(audio)
    assert wait_until(lambda: analysis_state(session) == "ready")
    return session


def blocking_render(started: threading.Event, release: threading.Event):
    def render(analysis, output, look, progress=None, cancel=None):
        started.set()
        release.wait(20)
        output.write_bytes(b"movie")

    return render


# ---------------------------------------------------------------- select and preview


def test_select_analyses_and_previews(make_session, monkeypatch, audio) -> None:
    session = ready_session(make_session, monkeypatch, audio)
    state = session.state()
    assert state["audio"] == {"name": "talk.wav", "extension": "wav", "duration": 3.0,
                              "has_spectrum": state["audio"]["has_spectrum"]}
    assert state["step"] == 2
    w, h, data = session.render_preview(default_look_dict(), 1.5, 4000, 4000)
    assert (w, h) == (1080, 180)
    assert len(data) == w * h * 4
    assert any(data[3::4])


def test_open_audio_uses_dialog_and_remembers_directory(make_session, monkeypatch, dialogs, audio, tmp_path) -> None:
    use_analysis(monkeypatch, make_synthetic_analysis())
    session = make_session()
    assert session.open_audio() is False
    assert session.state()["audio"] is None
    dialogs.audio = str(audio)
    assert session.open_audio() is True
    assert wait_until(lambda: analysis_state(session) == "ready")
    session.open_audio()
    assert dialogs.audio_starts[-1] == str(audio.parent)
    assert settings.load(tmp_path / "cfg")["last_input_dir"] == str(audio.parent)


def test_select_rejects_missing_and_non_files(make_session, tmp_path) -> None:
    session = make_session()
    for bad in (tmp_path / "nope.wav", tmp_path, "", "a\0b"):
        with pytest.raises(SessionError):
            session.select_audio(bad)
    assert session.state()["audio"] is None


def test_preview_straight_alpha_aspect_and_no_upscale(make_session, monkeypatch, audio) -> None:
    session = ready_session(make_session, monkeypatch, audio)
    look = {**default_look_dict(), "color": "#FF0000", "color2": None, "opacity": 128}
    for max_w, max_h, expected in ((4000, 4000, (1080, 180)), (540, 540, (540, 90)), (4000, 90, (540, 90))):
        w, h, data = session.render_preview(look, 1.5, max_w, max_h)
        assert (w, h) == expected
        pixels = [data[i:i + 4] for i in range(0, len(data), 4)]
        solid = [px for px in pixels if px[3] >= 64]
        assert solid, "expected visible bars"
        # Straight alpha: a half transparent pixel keeps the full colour instead of being darkened.
        assert all(px[0] >= 235 and px[1] <= 20 and px[2] <= 20 for px in solid)
    square = {**look, "width": 1080, "height": 1080, "layout": "radial"}
    assert session.render_preview(square, 1.5, 200, 400)[:2] == (200, 200)


def test_preview_clamps_time_and_validates_input(make_session, monkeypatch, audio) -> None:
    session = ready_session(make_session, monkeypatch, audio)
    look = default_look_dict()
    assert session.render_preview(look, 1e9, 300, 300) == session.render_preview(look, 3.0, 300, 300)
    assert session.render_preview(look, -5, 300, 300) == session.render_preview(look, 0.0, 300, 300)
    for max_w, max_h in ((15, 100), (100, 4097), (True, 100), ("100", 100), (100.0, 100), (None, 100)):
        with pytest.raises(ValueError):
            session.render_preview(look, 1.0, max_w, max_h)
    for bad_t in (float("nan"), float("inf"), "1", None, True):
        with pytest.raises(ValueError):
            session.render_preview(look, bad_t, 100, 100)
    with pytest.raises(ValueError):
        session.render_preview({**look, "layout": "nope"}, 1.0, 100, 100)


def test_preview_without_analysis_raises_specific_error(make_session) -> None:
    with pytest.raises(NoAnalysisError):
        make_session().render_preview(default_look_dict(), 0.0, 100, 100)


def test_preview_cost_does_not_grow_with_audio_length(make_session, monkeypatch, audio) -> None:
    look = default_look_dict()

    def median_cost(seconds: int) -> float:
        session = ready_session(make_session, monkeypatch, audio, long_analysis(seconds))
        session.render_preview(look, seconds / 2, 400, 400)
        costs = []
        for _ in range(7):
            start = time.perf_counter()
            session.render_preview(look, seconds / 2, 400, 400)
            costs.append(time.perf_counter() - start)
        return statistics.median(costs)

    short, long = median_cost(60), median_cost(3600)
    assert long / short < 3


# ------------------------------------------------------------ stale analysis results


def test_stale_analysis_result_is_discarded(make_session, monkeypatch, tmp_path) -> None:
    first, second = tmp_path / "first.wav", tmp_path / "second.wav"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    gate, first_returned = threading.Event(), threading.Event()
    started = threading.Event()

    def fake(path, progress=None, cancel=None):
        if path.name == "first.wav":
            started.set()
            gate.wait(20)  # ignores cancel on purpose: the session must discard the result itself
            first_returned.set()
            return long_analysis(30)
        return long_analysis(6)

    monkeypatch.setattr(session_mod, "analyze_audio", fake)
    session = make_session()
    session.select_audio(first)
    assert started.wait(20)
    session.select_audio(second)
    assert wait_until(lambda: analysis_state(session) == "ready")
    gate.set()
    assert first_returned.wait(20)
    assert wait_until(lambda: not any(t.is_alive() for t in session._threads))
    state = session.state()
    assert state["audio"]["name"] == "second.wav"
    assert state["audio"]["duration"] == 6.0


# ------------------------------------------------------------------------- export


def test_export_failure_keeps_analysis_and_preview(make_session, monkeypatch, dialogs, audio, tmp_path) -> None:
    session = ready_session(make_session, monkeypatch, audio)

    def failing(analysis, output, look, progress=None, cancel=None):
        raise RuntimeError(f"cannot write {output}")

    monkeypatch.setattr(session_mod, "render_movie", failing)
    dialogs.output = str(tmp_path / "out" / "clip.mov")
    assert session.start_export(default_look_dict()) is True
    assert wait_until(lambda: export_state(session) == "failed")
    state = session.state()
    assert state["analysis"]["state"] == "ready"
    assert state["audio"]["name"] == "talk.wav"
    assert state["export"]["error"] == "cannot write clip.mov"
    assert session.render_preview(default_look_dict(), 1.0, 300, 300)[0] > 0
    # a failed export does not block the next one
    monkeypatch.setattr(session_mod, "render_movie", lambda analysis, output, look, progress=None, cancel=None: None)
    assert session.start_export(default_look_dict()) is True
    assert wait_until(lambda: export_state(session) == "done")
    assert session.state()["export"]["error"] is None


def test_dialog_cancel_changes_nothing_and_mov_is_forced(make_session, monkeypatch, dialogs, audio, tmp_path) -> None:
    session = ready_session(make_session, monkeypatch, audio)
    outputs = []

    def fake(analysis, output, look, progress=None, cancel=None):
        outputs.append(output)
        output.write_bytes(b"movie")

    monkeypatch.setattr(session_mod, "render_movie", fake)
    dialogs.output = None
    before = session.state()
    assert session.start_export(default_look_dict()) is False
    assert session.state() == before
    assert not outputs
    assert dialogs.output_calls[0] == (str(audio.parent), "talk_waveform.mov")
    dialogs.output = str(tmp_path / "clip")
    assert session.start_export(default_look_dict()) is True
    assert wait_until(lambda: export_state(session) == "done")
    assert outputs == [tmp_path / "clip.mov"]
    dialogs.output = str(tmp_path / "other.MOV")
    session.start_export(default_look_dict())
    assert wait_until(lambda: outputs[-1:] == [tmp_path / "other.MOV"] and export_state(session) == "done")
    assert dialogs.output_calls[-1][0] == str(tmp_path)  # the last output folder is remembered


def test_a_str_subclass_from_the_save_dialog_is_a_valid_output(make_session, monkeypatch, dialogs, audio,
                                                               tmp_path) -> None:
    class NativeString(str):
        pass

    session = ready_session(make_session, monkeypatch, audio)
    outputs = []
    monkeypatch.setattr(session_mod, "render_movie",
                        lambda analysis, output, look, progress=None, cancel=None: outputs.append(output))
    dialogs.output = NativeString(tmp_path / "clip.mov")
    assert session.start_export(default_look_dict()) is True
    assert wait_until(lambda: export_state(session) == "done")
    assert outputs == [tmp_path / "clip.mov"]


def test_the_real_macos_save_dialog_string_is_a_valid_output(tmp_path) -> None:
    foundation = pytest.importorskip("Foundation", reason="NSString exists only on macOS")
    chosen = foundation.NSString.stringWithString_(str(tmp_path / "clip.mov"))
    assert type(chosen) is not str      # what makes this case different from a plain path
    assert Session._checked_output(chosen, tmp_path / "talk.wav") == tmp_path / "clip.mov"


def test_output_may_not_be_the_audio_file(make_session, monkeypatch, dialogs, tmp_path) -> None:
    source = tmp_path / "talk.mov"
    source.write_bytes(b"source")
    session = ready_session(make_session, monkeypatch, source)
    dialogs.output = str(source)
    with pytest.raises(SessionError):
        session.start_export(default_look_dict())
    assert export_state(session) == "idle"
    assert source.read_bytes() == b"source"


def test_adding_mov_never_replaces_an_unconfirmed_file(make_session, monkeypatch, dialogs, audio, tmp_path) -> None:
    session = ready_session(make_session, monkeypatch, audio)
    written = []

    def fake(analysis, output, look, progress=None, cancel=None):
        written.append(output)
        output.write_bytes(b"new")

    monkeypatch.setattr(session_mod, "render_movie", fake)
    existing = tmp_path / "clip.mov"
    existing.write_bytes(b"precious")
    dialogs.output = str(tmp_path / "clip")                  # the dialog never saw clip.mov
    with pytest.raises(SessionError, match=r"clip\.mov already exists"):
        session.start_export(default_look_dict())
    assert existing.read_bytes() == b"precious" and not written and export_state(session) == "idle"
    dialogs.output = str(existing)                           # typed with .mov: the dialog confirmed it
    assert session.start_export(default_look_dict()) is True
    assert wait_until(lambda: export_state(session) == "done")
    assert existing.read_bytes() == b"new"


def test_cancel_during_finalizing_changes_nothing(make_session, monkeypatch, dialogs, audio, tmp_path) -> None:
    session = ready_session(make_session, monkeypatch, audio)
    reached, release = threading.Event(), threading.Event()
    seen = []

    def render(analysis, output, look, progress=None, cancel=None):
        progress(10, 10)                                     # all frames written: finalizing
        reached.set()
        release.wait(20)
        seen.append(cancel.is_set())
        output.write_bytes(b"movie")

    monkeypatch.setattr(session_mod, "render_movie", render)
    dialogs.output = str(tmp_path / "clip.mov")
    session.start_export(default_look_dict())
    assert reached.wait(20)
    assert export_state(session) == "finalizing"
    assert session.cancel_export() is False
    assert export_state(session) == "finalizing"
    release.set()
    assert wait_until(lambda: export_state(session) == "done")
    assert seen == [False]
    assert session.cancel_export() is False                  # finished: nothing to cancel


def test_cancel_while_rendering_is_accepted_and_wins_over_the_last_frame(make_session, monkeypatch, dialogs, audio, tmp_path) -> None:
    session = ready_session(make_session, monkeypatch, audio)
    started, release = threading.Event(), threading.Event()

    def render(analysis, output, look, progress=None, cancel=None):
        progress(1, 10)
        started.set()
        release.wait(20)
        progress(10, 10)                                     # the cancel arrived before finalizing: honoured
        output.write_bytes(b"movie")

    monkeypatch.setattr(session_mod, "render_movie", render)
    dialogs.output = str(tmp_path / "clip.mov")
    session.start_export(default_look_dict())
    assert started.wait(20)
    assert session.cancel_export() is True
    release.set()
    assert wait_until(lambda: export_state(session) == "cancelled")
    assert not (tmp_path / "clip.mov").exists()


@pytest.mark.parametrize(("text", "known", "expected"), [
    ("/home/jane doe/Videos/clip.part-ab12cd34.mov: Permission denied", ("/home/jane doe/Videos",),
     "clip.part-ab12cd34.mov: Permission denied"),
    ("Error opening file:/home/bob/secret dir/a.wav", ("/home/bob/secret dir/a.wav",), "Error opening file:a.wav"),
    ("Error opening file:/home/bob/secret dir/a.wav: No such file", (), "Error opening file:a.wav: No such file"),
    (r"denied: D:\\Work files\\bob smith\\Videos\\clip.part-1.mov: no", (), "denied: clip.part-1.mov: no"),
    ("copy /home/a/x.wav /home/b/y.wav now", (), "copy x.wav y.wav now"),
    ("Error opening file:/home/bob/a.wav: No such file", (), "Error opening file:a.wav: No such file"),
    ("path=/home/bob/a.wav failed", (), "path=a.wav failed"),
    (r"D:\Media Files\clip.part-1.mov: Permission denied", (r"D:\Media Files",), "clip.part-1.mov: Permission denied"),
    ("D:/Media Files/clip.part-1.mov: Permission denied", ("D:/Media Files",), "clip.part-1.mov: Permission denied"),
    ("cannot create directory /home/jane doe/Videos", ("/home/jane doe/Videos",), "cannot create directory Videos"),
])
def test_clean_message_leaves_no_directory_fragments(text, known, expected) -> None:
    from pathlib import Path
    assert session_mod._clean_message(text, tuple(Path(k) for k in known)) == expected


def test_export_error_text_names_the_temp_file_without_its_folder(make_session, monkeypatch, dialogs, audio, tmp_path) -> None:
    session = ready_session(make_session, monkeypatch, audio)
    folder = tmp_path / "jane doe" / "My Videos"
    folder.mkdir(parents=True)

    def failing(analysis, output, look, progress=None, cancel=None):
        raise RuntimeError(f"{folder}/clip.part-ab12cd34.mov: Permission denied")

    monkeypatch.setattr(session_mod, "render_movie", failing)
    dialogs.output = str(folder / "clip.mov")
    session.start_export(default_look_dict())
    assert wait_until(lambda: export_state(session) == "failed")
    error = session.state()["export"]["error"]
    assert error == "clip.part-ab12cd34.mov: Permission denied"
    assert "jane" not in error and "Videos" not in error


def test_export_rejections(make_session, monkeypatch, dialogs, audio, tmp_path) -> None:
    session = make_session()
    dialogs.output = str(tmp_path / "x.mov")
    with pytest.raises(NoAnalysisError):
        session.start_export(default_look_dict())
    gate = threading.Event()
    started = threading.Event()

    def slow_analysis(path, progress=None, cancel=None):
        started.set()
        gate.wait(20)
        return make_synthetic_analysis()

    monkeypatch.setattr(session_mod, "analyze_audio", slow_analysis)
    session.select_audio(audio)
    assert started.wait(20)
    with pytest.raises(SessionError):
        session.start_export(default_look_dict())
    gate.set()
    assert wait_until(lambda: analysis_state(session) == "ready")
    with pytest.raises(ValueError):
        session.start_export({**default_look_dict(), "bars": -1})
    assert dialogs.output_calls == []


def test_a_refused_input_clears_on_selection_export_and_with_time(make_session, monkeypatch, dialogs, audio, tmp_path) -> None:
    session = ready_session(make_session, monkeypatch, audio)
    session.report_input_error(SessionError("This file type is not supported."))
    assert session.state()["error"] == "This file type is not supported."
    session.report_input_error("Choose an existing audio file.")
    monkeypatch.setattr(session_mod, "INPUT_ERROR_SECONDS", 0.0)      # it expires, whatever the state is
    time.sleep(0.01)
    assert session.state()["error"] is None
    monkeypatch.setattr(session_mod, "INPUT_ERROR_SECONDS", 8.0)

    session.report_input_error("Choose an existing audio file.")
    other = tmp_path / "other.wav"
    other.write_bytes(b"x")
    session.select_audio(other)                                       # a successful selection clears it
    assert session.state()["error"] is None

    use_analysis(monkeypatch, make_synthetic_analysis())
    assert wait_until(lambda: analysis_state(session) == "ready")
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(session_mod, "render_movie", blocking_render(started, release))
    dialogs.output = str(tmp_path / "x.mov")
    session.report_input_error("Choose an existing audio file.")
    assert session.start_export(default_look_dict()) is True          # an export start clears it
    assert started.wait(20)
    assert session.state()["error"] is None
    session.report_input_error("The audio cannot be changed while an export is running.")
    release.set()
    assert wait_until(lambda: export_state(session) == "done")
    assert session.state()["error"] is None                           # and the result is never hidden by it


def test_settings_keep_a_whitelisted_auto_format(make_session, tmp_path) -> None:
    cfg = tmp_path / "cfg"
    session = make_session(cfg)
    look = {**default_look_dict(), "layout": "radial", "width": 1080, "height": 1080}
    view = session.save_settings({"look": look, "auto_format": {"w": 1920, "h": 240}})
    assert view["auto_format"] == {"w": 1920, "h": 240}
    assert make_session(cfg).get_settings()["auto_format"] == {"w": 1920, "h": 240}      # survives a restart
    for bad in ({"w": 7, "h": 7}, {"w": 1920.0, "h": 240}, [1920, 240], "1920x240", {"w": True}):
        assert session.save_settings({"auto_format": bad})["auto_format"] is None


def test_concurrent_export_and_select_during_export_are_rejected(make_session, monkeypatch, dialogs, audio, tmp_path) -> None:
    session = ready_session(make_session, monkeypatch, audio)
    other = tmp_path / "other.wav"
    other.write_bytes(b"x")
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(session_mod, "render_movie", blocking_render(started, release))
    dialogs.output = str(tmp_path / "x.mov")

    # while the save dialog is open nothing else may start
    attempts = []

    def during_dialog():
        for action in (lambda: session.start_export(default_look_dict()), lambda: session.select_audio(other)):
            try:
                action()
            except SessionError:
                attempts.append("rejected")

    dialogs.on_pick_output = during_dialog
    assert session.start_export(default_look_dict()) is True
    dialogs.on_pick_output = None
    assert attempts == ["rejected", "rejected"]
    assert started.wait(20)
    with pytest.raises(SessionError):
        session.start_export(default_look_dict())
    with pytest.raises(SessionError):
        session.select_audio(other)
    assert session.state()["audio"]["name"] == "talk.wav"
    release.set()
    assert wait_until(lambda: export_state(session) == "done")
    session.select_audio(other)  # allowed again once the export is over
    assert wait_until(lambda: session.state()["audio"]["name"] == "other.wav")


def test_finalizing_is_reported_after_the_last_frame(make_session, monkeypatch, dialogs, audio, tmp_path) -> None:
    session = ready_session(make_session, monkeypatch, audio)
    reported, release = threading.Event(), threading.Event()

    def fake(analysis, output, look, progress=None, cancel=None):
        progress(1, 90)
        progress(90, 90)
        reported.set()
        release.wait(20)
        output.write_bytes(b"movie")

    monkeypatch.setattr(session_mod, "render_movie", fake)
    dialogs.output = str(tmp_path / "x.mov")
    session.start_export(default_look_dict())
    assert reported.wait(20)
    export = session.state()["export"]
    assert (export["state"], export["current"], export["total"]) == ("finalizing", 90, 90)
    release.set()
    assert wait_until(lambda: export_state(session) == "done")


def test_reveal_last_only_after_success(make_session, monkeypatch, dialogs, audio, tmp_path) -> None:
    session = ready_session(make_session, monkeypatch, audio)
    assert session.reveal_last() is False
    assert dialogs.revealed == []
    assert session.state()["export"]["can_reveal"] is False

    def fake(analysis, output, look, progress=None, cancel=None):
        output.write_bytes(b"movie")

    monkeypatch.setattr(session_mod, "render_movie", fake)
    dialogs.output = str(tmp_path / "done.mov")
    session.start_export(default_look_dict())
    assert wait_until(lambda: export_state(session) == "done")
    assert session.state()["export"]["can_reveal"] is True
    assert session.reveal_last() is True
    assert dialogs.revealed == [str(tmp_path / "done.mov")]


@requires_ffmpeg
def test_export_cancel_leaves_no_trace_and_session_working(make_session, monkeypatch, dialogs, audio, tmp_path, spawned) -> None:
    session = ready_session(make_session, monkeypatch, audio, long_analysis(120))
    out = tmp_path / "out" / "clip.mov"
    out.parent.mkdir()
    out.write_bytes(b"previous render")
    dialogs.output = str(out)
    assert session.start_export(default_look_dict()) is True
    assert wait_until(lambda: session.state()["export"]["current"] > 0)
    assert session.state()["export"]["state"] == "rendering"
    session.cancel_export()
    assert wait_until(lambda: export_state(session) == "cancelled")
    assert out.read_bytes() == b"previous render"
    assert list(out.parent.glob("*.part-*")) == []
    assert_all_exited(spawned)
    state = session.state()
    assert state["analysis"]["state"] == "ready" and state["audio"]["name"] == "talk.wav"
    assert state["error"] is None
    assert session.render_preview(default_look_dict(), 5.0, 300, 300)[0] > 0
    assert session.reveal_last() is False


@requires_ffmpeg
def test_real_analysis_and_export_end_to_end(make_session, dialogs, demo_wav, tmp_path, spawned) -> None:
    session = make_session()
    session.select_audio(demo_wav)
    assert wait_until(lambda: analysis_state(session) == "ready")
    assert abs(session.state()["audio"]["duration"] - 12.0) < 0.5
    dialogs.output = str(tmp_path / "result")
    look = {**default_look_dict(), "layout": "scroll"}
    assert session.start_export(look) is True
    assert wait_until(lambda: export_state(session) == "done", 120)
    assert (tmp_path / "result.mov").stat().st_size > 0
    assert session.reveal_last() is True
    assert dialogs.revealed == [str(tmp_path / "result.mov")]
    assert_all_exited(spawned)


# ------------------------------------------------------------------------ privacy


def test_state_json_never_contains_directories(make_session, monkeypatch, dialogs, tmp_path) -> None:
    folder = tmp_path / "My Music"
    folder.mkdir()
    audio = folder / "talk.wav"
    audio.write_bytes(b"x")
    dirs = {str(tmp_path), str(tmp_path.resolve()), str(folder), str(folder.resolve())}

    def assert_clean(session: Session) -> str:
        dump = json.dumps(session.state())
        assert not any(d in dump for d in dirs), dump
        assert "My Music" not in dump
        return dump

    session = ready_session(make_session, monkeypatch, audio)
    assert_clean(session)

    def failing(analysis, output, look, progress=None, cancel=None):
        raise RuntimeError(f"cannot write {output}: {audio} is busy; see {folder.parent}/log dir/trace.txt")

    monkeypatch.setattr(session_mod, "render_movie", failing)
    dialogs.output = str(tmp_path / "exports" / "clip.mov")
    session.start_export(default_look_dict())
    assert wait_until(lambda: export_state(session) == "failed")
    dump = assert_clean(session)
    assert "clip.mov" in dump and "trace.txt" in dump

    def broken(path, progress=None, cancel=None):
        raise RuntimeError(f"{path}: Invalid data found when processing input /srv/elsewhere/deep/x.bin")

    monkeypatch.setattr(session_mod, "analyze_audio", broken)
    session.select_audio(audio)
    assert wait_until(lambda: analysis_state(session) == "failed")
    dump = assert_clean(session)
    assert "talk.wav: Invalid data" in dump and "/srv" not in dump
    assert session.state()["audio"] is None


def test_failed_analysis_clears_only_that_file(make_session, monkeypatch, audio) -> None:
    def broken(path, progress=None, cancel=None):
        raise RuntimeError("Invalid data")

    monkeypatch.setattr(session_mod, "analyze_audio", broken)
    session = make_session()
    session.select_audio(audio)
    assert wait_until(lambda: analysis_state(session) == "failed")
    state = session.state()
    assert state["audio"] is None and state["step"] == 0 and state["error"] == "Invalid data"
    with pytest.raises(NoAnalysisError):
        session.render_preview(default_look_dict(), 0, 100, 100)
    use_analysis(monkeypatch, make_synthetic_analysis())
    session.select_audio(audio)
    assert wait_until(lambda: analysis_state(session) == "ready")
    assert session.state()["error"] is None


# --------------------------------------------------------------------- playback


@requires_ffmpeg
def test_ensure_playable_makes_a_wav_and_new_selection_deletes_it(make_session, monkeypatch, tmp_path) -> None:
    aiff = tmp_path / "tone.aiff"
    subprocess.run([str(ffmpeg_tools.find_ffmpeg()), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-y", str(aiff)], check=True)
    other = tmp_path / "other.mp3"
    other.write_bytes(b"x")
    session = ready_session(make_session, monkeypatch, aiff)
    path, mime, version = session.audio_file()
    assert (path, mime) == (aiff, "audio/aiff")
    assert session.state()["playback"]["mode"] == "native"
    session.ensure_playable()
    session.ensure_playable()  # idempotent while running
    assert wait_until(lambda: session.state()["playback"]["mode"] == "fallback")
    wav, wav_mime, wav_version = session.audio_file()
    assert wav.suffix == ".wav" and wav_mime == "audio/wav" and wav_version > version
    assert session.state()["audio_version"] == wav_version
    with wave.open(str(wav)) as handle:
        assert (handle.getnchannels(), handle.getframerate()) == (2, 44100)
    session.ensure_playable()
    assert session.audio_file()[2] == wav_version
    session.select_audio(other)
    assert not wav.exists()
    assert session.audio_file()[0] == other and session.audio_file()[1] == "audio/mpeg"
    assert session.state()["playback"]["mode"] == "native"


@posix_only
def test_transcode_failure_is_reported_and_removes_its_file(make_session, monkeypatch, audio) -> None:
    fake = audio.parent / "ffmpeg"
    fake.write_text("#!/bin/sh\necho 'bad input' >&2\nexit 1\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(ffmpeg_tools, "find_ffmpeg", lambda: fake)
    session = ready_session(make_session, monkeypatch, audio)
    session.ensure_playable()
    assert wait_until(lambda: session.state()["playback"]["mode"] == "failed")
    assert session.state()["playback"]["error"] == "bad input"
    assert session.audio_file()[0] == audio
    assert not list(session._tmpdir.glob("*.wav"))


def test_ensure_playable_needs_audio(make_session) -> None:
    with pytest.raises(SessionError):
        make_session().ensure_playable()


# --------------------------------------------------------------------- shutdown


def test_shutdown_safe_when_nothing_ran(make_session) -> None:
    session = make_session()
    session.shutdown()
    session.shutdown()
    with pytest.raises(SessionError):
        session.select_audio(Path(__file__))


def test_shutdown_during_analysis(make_session, monkeypatch, audio) -> None:
    started = threading.Event()

    def waiting(path, progress=None, cancel=None):
        started.set()
        while not cancel.wait(0.02):
            pass
        raise Cancelled

    monkeypatch.setattr(session_mod, "analyze_audio", waiting)
    session = make_session()
    session.select_audio(audio)
    assert started.wait(20)
    begin = time.monotonic()
    session.shutdown()
    assert time.monotonic() - begin < session_mod.JOIN_TIMEOUT
    assert not any(t.is_alive() for t in session._threads)
    session.shutdown()


@requires_ffmpeg
def test_shutdown_during_export(make_session, monkeypatch, dialogs, audio, tmp_path, spawned) -> None:
    session = ready_session(make_session, monkeypatch, audio, long_analysis(120))
    out = tmp_path / "out"
    out.mkdir()
    dialogs.output = str(out / "clip.mov")
    session.start_export(default_look_dict())
    assert wait_until(lambda: session.state()["export"]["current"] > 0)
    begin = time.monotonic()
    session.shutdown()
    assert time.monotonic() - begin < session_mod.JOIN_TIMEOUT
    assert list(out.iterdir()) == []
    assert_all_exited(spawned)
    session.shutdown()


@posix_only
def test_shutdown_during_transcode(make_session, monkeypatch, audio, spawned) -> None:
    fake = audio.parent / "ffmpeg"
    fake.write_text("#!/bin/sh\nexec sleep 60\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(ffmpeg_tools, "find_ffmpeg", lambda: fake)
    session = ready_session(make_session, monkeypatch, audio)
    session.ensure_playable()
    assert wait_until(lambda: bool(spawned))
    tmpdir = session._tmpdir
    assert tmpdir.is_dir()
    begin = time.monotonic()
    session.shutdown()
    assert time.monotonic() - begin < session_mod.JOIN_TIMEOUT
    assert not tmpdir.exists()
    assert_all_exited(spawned)
    session.shutdown()


# ------------------------------------------------------------------------ FFmpeg


@posix_only
def test_missing_ffmpeg_gives_a_clean_error_and_recheck_recovers(make_session, monkeypatch, audio, tmp_path) -> None:
    monkeypatch.delenv(ffmpeg_tools.ENV_VAR, raising=False)
    monkeypatch.setattr(ffmpeg_tools, "_on_path", lambda name: None)
    monkeypatch.setattr(ffmpeg_tools, "_beside_executable", list)
    monkeypatch.setattr(ffmpeg_tools, "_candidate_dirs", lambda platform=sys.platform: [])
    session = make_session()
    assert session.state()["ffmpeg"]["ok"] is False
    session.select_audio(audio)
    assert wait_until(lambda: analysis_state(session) == "failed")
    assert wait_until(lambda: not any(t.is_alive() for t in session._threads))
    state = session.state()
    assert state["analysis"]["error"] == "FFmpeg was not found."
    assert state["audio"] is None
    assert state["ffmpeg"]["install_command"]

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ffmpeg", "echo ' V....D prores_ks ProRes'\n"), ("ffprobe", "exit 0\n")):
        script = bin_dir / name
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv(ffmpeg_tools.ENV_VAR, str(bin_dir / "ffmpeg"))
    assert session.recheck_ffmpeg()["ok"] is True
    assert session.state()["ffmpeg"] == {"ok": True, "has_prores": True, "problem": None, "install_command": None,
                                          "install_alternative": None}


# ---------------------------------------------------------------------- settings


def test_settings_round_trip_ignores_directories(make_session, tmp_path) -> None:
    cfg = tmp_path / "cfg"
    session = make_session(cfg)
    assert set(session.get_settings()) == {"look", "theme", "gradient", "auto_format"}
    look = {**default_look_dict(), "gain": 1.5, "layout": "scroll"}
    theme = THEMES[1][0]
    view = session.save_settings({"look": look, "theme": theme, "gradient": False,
                                  "last_input_dir": "/etc", "last_output_dir": "/root"})
    assert view["look"]["gain"] == 1.5 and view["theme"] == theme and view["gradient"] is False
    on_disk = settings.load(cfg)
    assert on_disk["last_input_dir"] is None and on_disk["last_output_dir"] is None
    again = make_session(cfg)
    assert again.get_settings() == view
    with pytest.raises(ValueError):
        again.save_settings([])
    assert "dir" not in json.dumps(again.get_settings())
