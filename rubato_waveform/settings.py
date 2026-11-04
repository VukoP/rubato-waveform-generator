# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Persistent settings: look values and last-used directories (never audio contents)."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from .catalog import (
    DEFAULT_GRADIENT,
    DEFAULT_THEME,
    FORMATS,
    LOOK_FIELDS,
    default_look_dict,
    validate_look_field,
)
from .engine import THEMES

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
APP_NAME = "Rubato Waveform Generator"
FILE_NAME = "settings.json"
_MAX_BYTES = 1_000_000
_MAX_PATH = 4096
_DIR_KEYS = ("last_input_dir", "last_output_dir")


def config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "rubato-waveform-generator"


def defaults() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "look": default_look_dict(),
        "theme": DEFAULT_THEME,
        "gradient": DEFAULT_GRADIENT,
        "auto_format": None,
        "last_input_dir": None,
        "last_output_dir": None,
    }


def validate_settings_dict(data: object) -> dict:
    """Lenient counterpart of ``look_from_dict``: same per-field rules, but every invalid
    value falls back to its default and unknown keys are dropped. Never raises."""
    result = defaults()
    if not isinstance(data, dict):
        return result
    look = data.get("look")
    if isinstance(look, dict):
        for name in LOOK_FIELDS:
            if name in look:
                try:
                    result["look"][name] = validate_look_field(name, look[name])
                except ValueError:
                    pass
        if (result["look"]["width"], result["look"]["height"]) not in FORMATS:
            result["look"]["width"], result["look"]["height"] = 1080, 180
    if data.get("theme") in {t[0] for t in THEMES}:
        result["theme"] = data["theme"]
    if type(data.get("gradient")) is bool:
        result["gradient"] = data["gradient"]
    auto = data.get("auto_format")      # the format a square layout replaced: only one the app offers
    if isinstance(auto, dict) and type(auto.get("w")) is int and type(auto.get("h")) is int \
            and (auto["w"], auto["h"]) in FORMATS:
        result["auto_format"] = {"w": auto["w"], "h": auto["h"]}
    for key in _DIR_KEYS:
        value = data.get(key)
        if type(value) is str and 0 < len(value) <= _MAX_PATH and "\0" not in value:
            result[key] = value
    return result


def load(directory: Path | None = None) -> dict:
    path = (directory or config_dir()) / FILE_NAME
    try:
        if path.stat().st_size > _MAX_BYTES:
            raise ValueError("settings file too large")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("settings root is not an object")  # noqa: TRY004
    except FileNotFoundError:
        return defaults()
    except (ValueError, UnicodeDecodeError, RecursionError):   # RecursionError: deeply nested JSON
        log.warning("settings file is corrupt; keeping a copy as %s.bak", FILE_NAME)
        try:
            os.replace(path, path.with_name(FILE_NAME + ".bak"))
        except OSError as exc:
            log.warning("could not back up the settings file: %s", exc)
        return defaults()
    except OSError as exc:
        log.warning("could not read settings: %s", exc)
        return defaults()
    return validate_settings_dict(data)


def save(data: dict, directory: Path | None = None) -> bool:
    """Validates and writes atomically; returns False (after logging) if it cannot write."""
    target = directory or config_dir()
    clean = validate_settings_dict(data)
    tmp_name = None
    try:
        target.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=FILE_NAME + ".", suffix=".tmp", dir=target)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(clean, handle, indent=2, allow_nan=False)
        os.replace(tmp_name, target / FILE_NAME)
        return True
    except OSError as exc:
        log.warning("could not save settings: %s", exc)
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return False
