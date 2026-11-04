# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Command line behaviour: validation, help generated from LOOK_LIMITS, error exits."""

import pytest

from rubato_waveform import __version__, cli, engine
from rubato_waveform.engine import LOOK_LIMITS

BASE = ["in.wav", "out.mov"]


def parse(*extra: str):
    return cli.build_parser().parse_args([*BASE, *extra])


def test_version(capsys) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.strip() == f"rubato-waveform {__version__}"


@pytest.mark.parametrize("flags", [
    ["--fps", "0"], ["--fps", "121"], ["--width", "15"], ["--height", "8193"], ["--bars", "7"],
    ["--sensitivity", "0.29"], ["--contrast", "2.1"], ["--fps", "x"], ["--sensitivity", "nan"],
    ["--color", "+1+1+1"], ["--color2", "12345"], ["--opacity", "0"],
])
def test_invalid_values_exit_2_without_traceback(flags, capsys) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main([*BASE, *flags])
    assert info.value.code == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err and "error:" in err


def test_limits_are_inclusive() -> None:
    args = parse("--fps", "120", "--width", "16", "--sensitivity", "2.5", "--contrast", "0.4")
    assert (args.fps, args.width, args.sensitivity, args.contrast) == (120, 16, 2.5, 0.4)


def test_help_ranges_come_from_look_limits(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    for field in ("gain", "contrast", "fps", "width", "height", "bars"):
        assert cli._range_text(field) in text
    assert "sensitivity (0.3-2.5)" in text and "0.4-2; <1 emphasises" in text
    assert cli._range_text("gain") == f"{LOOK_LIMITS['gain'][0]:g}-{LOOK_LIMITS['gain'][1]:g}"


def test_changed_limit_changes_validation_and_help(monkeypatch, capsys) -> None:
    monkeypatch.setitem(LOOK_LIMITS, "fps", (1, 60))
    with pytest.raises(SystemExit) as info:
        cli.main([*BASE, "--fps", "61"])
    assert info.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "(1-60)" in capsys.readouterr().out


def test_defaults_are_unchanged() -> None:
    look = cli.build_look(parse())
    assert look == engine.Look()
    assert look.color2 is None


def test_no_gradient_without_theme_is_solid() -> None:
    look = cli.build_look(parse("--no-gradient"))
    assert look.color2 is None and look.color == engine.Look().color


def test_no_gradient_wins_over_color2_and_theme() -> None:
    assert cli.build_look(parse("--no-gradient", "--color2", "#112233")).color2 is None
    look = cli.build_look(parse("--theme", "Blue", "--no-gradient"))
    assert look.color2 is None and look.color == (0x3B, 0x82, 0xF6)


def test_color_flags_override_the_theme_individually() -> None:
    theme = cli.build_look(parse("--theme", "Blue"))
    assert theme.color2 is not None
    only1 = cli.build_look(parse("--theme", "Blue", "--color", "#010203"))
    assert only1.color == (1, 2, 3) and only1.color2 == theme.color2
    only2 = cli.build_look(parse("--theme", "Blue", "--color2", "#010203"))
    assert only2.color == theme.color and only2.color2 == (1, 2, 3)
    assert cli.build_look(parse("--color2", "#010203")).color2 == (1, 2, 3)


def test_unknown_theme_is_a_usage_error(tmp_path, capsys) -> None:
    audio = tmp_path / "in.wav"
    audio.write_bytes(b"x")
    with pytest.raises(SystemExit) as info:
        cli.main([str(audio), str(tmp_path / "o.mov"), "--theme", "Nope"])
    assert info.value.code == 2
    assert "Unknown theme" in capsys.readouterr().err


def test_missing_input_is_a_clean_error(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main([str(tmp_path / "missing.wav"), str(tmp_path / "o.mov")])
    assert info.value.code == 2
    err = capsys.readouterr().err
    assert "No such file" in err and "Traceback" not in err


def test_missing_ffmpeg_exits_1_with_hint(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("RUBATO_FFMPEG", raising=False)
    monkeypatch.setattr(engine, "find_ffmpeg", lambda: None)
    monkeypatch.setattr(engine, "find_ffprobe", lambda: None)
    monkeypatch.setattr(cli, "install_hint", lambda: "HINT-COMMAND")
    audio = tmp_path / "in.wav"
    audio.write_bytes(b"x")
    assert cli.main([str(audio), str(tmp_path / "o.mov")]) == 1
    err = capsys.readouterr().err
    assert "HINT-COMMAND" in err and "Traceback" not in err
