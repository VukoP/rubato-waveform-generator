# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Settings persistence: round trip, corruption handling, atomic writes."""

import json
import os
import sys

import pytest

from rubato_waveform import settings


def test_missing_file_gives_defaults(tmp_path) -> None:
    assert settings.load(tmp_path) == settings.defaults()


def test_round_trip(tmp_path) -> None:
    data = settings.defaults()
    data["look"].update(layout="neon", bars=99, color="#112233", color2=None, gain=1.5)
    data.update(theme="Blue", gradient=False, last_input_dir="/some/in", last_output_dir="/some/out")
    assert settings.save(data, tmp_path) is True
    assert settings.load(tmp_path) == data
    assert json.loads((tmp_path / "settings.json").read_text())["schema_version"] == 1


def test_corrupt_json_is_moved_to_bak(tmp_path) -> None:
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    assert settings.load(tmp_path) == settings.defaults()
    assert (tmp_path / "settings.json.bak").read_text(encoding="utf-8") == "{not json"
    assert not (tmp_path / "settings.json").exists()


def test_deeply_nested_json_is_corrupt_not_fatal(tmp_path) -> None:
    (tmp_path / "settings.json").write_text("[" * 200000, encoding="utf-8")
    assert settings.load(tmp_path) == settings.defaults()
    assert (tmp_path / "settings.json.bak").exists()
    assert not (tmp_path / "settings.json").exists()


def test_non_object_root_is_corrupt(tmp_path) -> None:
    (tmp_path / "settings.json").write_text("[1, 2]", encoding="utf-8")
    assert settings.load(tmp_path) == settings.defaults()
    assert (tmp_path / "settings.json.bak").exists()


def test_nan_literal_is_not_trusted(tmp_path) -> None:
    (tmp_path / "settings.json").write_text('{"look": {"gain": NaN, "bars": 90}}', encoding="utf-8")
    loaded = settings.load(tmp_path)
    assert loaded["look"]["gain"] == 1.0 and loaded["look"]["bars"] == 90


def test_partial_validity_keeps_good_values(tmp_path) -> None:
    raw = {"schema_version": 1, "look": {"bars": 120, "gain": "loud", "layout": "nope",
                                         "style": "dots", "width": 3840, "height": 360},
           "theme": "Nonexistent", "gradient": False, "last_input_dir": 5}
    (tmp_path / "settings.json").write_text(json.dumps(raw), encoding="utf-8")
    loaded = settings.load(tmp_path)
    base = settings.defaults()
    assert loaded["look"]["bars"] == 120 and loaded["look"]["style"] == "dots"
    assert (loaded["look"]["width"], loaded["look"]["height"]) == (3840, 360)
    assert loaded["look"]["gain"] == base["look"]["gain"]
    assert loaded["look"]["layout"] == base["look"]["layout"]
    assert loaded["theme"] == base["theme"] and loaded["gradient"] is False
    assert loaded["last_input_dir"] is None


def test_bad_format_pair_falls_back(tmp_path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"look": {"width": 1234, "height": 999}}),
                                            encoding="utf-8")
    look = settings.load(tmp_path)["look"]
    assert (look["width"], look["height"]) == (1080, 180)


def test_unknown_keys_dropped(tmp_path) -> None:
    raw = {"look": {"bars": 70, "px_scale": 4, "evil": 1}, "extra": {"a": 1}}
    (tmp_path / "settings.json").write_text(json.dumps(raw), encoding="utf-8")
    loaded = settings.load(tmp_path)
    assert set(loaded) == set(settings.defaults())
    assert set(loaded["look"]) == set(settings.defaults()["look"])
    assert loaded["look"]["bars"] == 70


def test_save_leaves_no_temp_files(tmp_path) -> None:
    settings.save(settings.defaults(), tmp_path)
    settings.save(settings.defaults(), tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_save_creates_missing_directory(tmp_path) -> None:
    target = tmp_path / "a" / "b"
    assert settings.save(settings.defaults(), target) is True
    assert (target / "settings.json").is_file()


def test_unwritable_target_does_not_raise(tmp_path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    # A directory path that is really a file cannot be created or written, on every OS.
    assert settings.save(settings.defaults(), blocker / "sub") is False
    assert settings.load(blocker / "sub") == settings.defaults()


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="read-only directories are not enforced on Windows or for root")
def test_read_only_dir_does_not_raise(tmp_path) -> None:
    tmp_path.chmod(0o500)
    try:
        assert settings.save(settings.defaults(), tmp_path) is False
        assert settings.load(tmp_path) == settings.defaults()
    finally:
        tmp_path.chmod(0o700)


def test_config_dir_per_os(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert settings.config_dir() == tmp_path / "Library" / "Application Support" / "Rubato Waveform Generator"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert settings.config_dir() == tmp_path / ".config" / "rubato-waveform-generator"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert settings.config_dir() == tmp_path / "xdg" / "rubato-waveform-generator"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert settings.config_dir() == tmp_path / "roaming" / "Rubato Waveform Generator"


def test_auto_format_is_kept_only_when_it_is_an_offered_format(tmp_path) -> None:
    for value, expected in (({"w": 3840, "h": 360}, {"w": 3840, "h": 360}), ({"w": 1, "h": 2}, None),
                            ({"w": "1080", "h": 180}, None), ([1080, 180], None), (None, None)):
        (tmp_path / "settings.json").write_text(json.dumps({"auto_format": value}), encoding="utf-8")
        assert settings.load(tmp_path)["auto_format"] == expected
