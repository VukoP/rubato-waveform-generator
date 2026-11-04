# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Single source of labels, ranges, presets and rules for the UI, plus input validation.

No GUI toolkit and no rendering here: plain data (``catalog()`` is JSON-serialisable) and the strict
validators that every untrusted Look (HTTP API, settings file) goes through.
"""

from __future__ import annotations

import math

from . import __version__
from .engine import (
    LAYOUTS,
    LOOK_LIMITS,
    METRICS,
    REFLECT_BASELINE,
    STATIONARY_OPTIONAL_LAYOUTS,
    STYLES,
    THEMES,
    Look,
    parse_hex,
)

LAYOUT_LABELS = (
    ("scroll", "Scroll"),
    ("mirror", "Center-out"),
    ("pulse", "Pulse"),
    ("flow", "Flow"),
    ("ripple", "Ripple"),
    ("neon", "Neon"),
    ("mirror-reflect", "Reflection"),
    ("peakhold", "Peak hold"),
    ("radial", "Orbit"),
    ("voiceline", "Voice line"),
)
STYLE_LABELS = (("bars", "Bars"), ("line", "Line"), ("area", "Area"), ("dots", "Dots"))
METRIC_LABELS = (("peak", "Peak"), ("rms", "Loudness"))
MOTION_LABELS = ((False, "Moving"), (True, "Static"))
FILL_LABELS = ((False, "Solid"), (True, "Gradient"))

# Preset values: gain, bars, max_height_ratio, window_seconds, contrast, smoothing.
_PRESET_FIELDS = ("gain", "bars", "max_height_ratio", "window_seconds", "contrast", "smoothing")
PRESETS = (
    ("calm", "Calm", (0.9, 49, 0.58, 1.6, 0.85, 0.55)),
    ("balanced", "Balanced", (1.0, 61, 0.72, 1.05, 1.0, 0.25)),
    ("energetic", "Energetic", (1.25, 85, 0.86, 0.7, 1.15, 0.08)),
)

SECTIONS = (("signal", "Signal"), ("form", "Form"), ("color", "Color"))

# Display value = raw / div * mul, shown with `decimals` places and `suffix`.
# (id, Look field, label, section, min, max, step or None, mul, div, decimals, suffix, voice only)
_SLIDERS = (
    ("sensitivity", "gain", "Sensitivity", "signal", 0.3, 2.5, None, 1, 1, 2, "×", False),
    ("contrast", "contrast", "Contrast", "signal", 0.4, 2.0, None, 1, 1, 2, "", False),
    ("smoothing", "smoothing", "Smoothing", "signal", 0.0, 1.0, None, 100, 1, 0, "%", False),
    ("window", "window_seconds", "Time window", "form", 0.25, 3.0, None, 1, 1, 2, " s", False),
    ("density", "bars", "Density", "form", 16, 160, 1, 1, 1, 0, "", False),
    ("height", "max_height_ratio", "Height", "form", 0.2, 0.98, None, 100, 1, 0, "%", False),
    ("opacity", "opacity", "Opacity", "form", 40, 255, 1, 100, 255, 0, "%", False),
    ("reactivity", "reactivity", "Reactivity", "form", 0.0, 1.0, None, 100, 1, 0, "%", True),
    ("drift", "drift", "Drift", "form", 0.0, 2.0, None, 1, 1, 2, "×", True),
    ("thickness", "line_width", "Thickness", "form", 1.0, 6.0, None, 1, 1, 1, " px", True),
)

FORMATS = ((1080, 180), (1920, 240), (3840, 360), (1080, 1080))
SQUARE_FORMAT = (1080, 1080)
ALLOWED_FPS = (30,)

PLAYHEAD_LAYOUTS = ("scroll", "flow", "neon", "mirror", "mirror-reflect")
STYLE_DISABLED_LAYOUTS = ("neon",)
VOICE_ONLY_LAYOUTS = ("voiceline",)
SQUARE_LAYOUTS = ("radial",)
NO_BASELINE_LAYOUTS = ("peakhold", "radial")

DEFAULT_THEME = THEMES[0][0]
DEFAULT_GRADIENT = True


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def default_look_dict() -> dict:
    """The app's starting look in the wire format accepted by ``look_from_dict``."""
    return {
        "width": 1080, "height": 180, "fps": 30,
        "layout": "mirror", "style": "bars", "metric": "peak", "stationary": False,
        "gain": 1.0, "contrast": 1.0, "smoothing": 0.25,
        "window_seconds": 1.05, "bars": 61, "max_height_ratio": 0.72, "opacity": 235,
        "reactivity": 0.7, "drift": 1.0, "line_width": 2.0,
        "color": THEMES[0][1], "color2": THEMES[0][2] if DEFAULT_GRADIENT else None,
    }


def format_value(slider: dict, value: float) -> str:
    """Applies a slider's display descriptor (the UI mirrors this)."""
    fmt = slider["format"]
    return f"{value / fmt['div'] * fmt['mul']:.{fmt['decimals']}f}{fmt['suffix']}"


def catalog() -> dict:
    return {
        "version": __version__,
        "layouts": [{"key": k, "label": label} for k, label in LAYOUT_LABELS],
        "styles": [{"key": k, "label": label} for k, label in STYLE_LABELS],
        "metrics": [{"key": k, "label": label} for k, label in METRIC_LABELS],
        "motion": [{"stationary": k, "label": label} for k, label in MOTION_LABELS],
        "fill": [{"gradient": k, "label": label} for k, label in FILL_LABELS],
        "presets": [
            {"key": key, "label": label, "values": dict(zip(_PRESET_FIELDS, values, strict=True))}
            for key, label, values in PRESETS
        ],
        "sections": [{"key": k, "label": label} for k, label in SECTIONS],
        "sliders": [
            {"key": key, "field": field, "label": label, "section": section, "min": lo, "max": hi,
             "step": step, "format": {"mul": mul, "div": div, "decimals": dec, "suffix": suffix},
             "voice_only": voice}
            for key, field, label, section, lo, hi, step, mul, div, dec, suffix, voice in _SLIDERS
        ],
        "themes": [{"name": n, "color": a, "color2": b} for n, a, b in THEMES],
        "formats": [{"w": w, "h": h, "label": f"{w} × {h}"} for w, h in FORMATS],
        "rules": {
            "playhead_layouts": list(PLAYHEAD_LAYOUTS),
            "stationary_optional": list(STATIONARY_OPTIONAL_LAYOUTS),
            "style_disabled_layouts": list(STYLE_DISABLED_LAYOUTS),
            "voice_only_layouts": list(VOICE_ONLY_LAYOUTS),
            "square_layouts": list(SQUARE_LAYOUTS),
            "square_format": {"w": SQUARE_FORMAT[0], "h": SQUARE_FORMAT[1]},
            "reflection_baseline": REFLECT_BASELINE,
            "no_baseline_layouts": list(NO_BASELINE_LAYOUTS),
        },
        "defaults": {"look": default_look_dict(), "theme": DEFAULT_THEME, "gradient": DEFAULT_GRADIENT},
    }


# ---------------------------------------------------------------------------
# Validation of untrusted input
# ---------------------------------------------------------------------------

_INT_FIELDS = ("width", "height", "fps", "bars", "opacity")
_FLOAT_FIELDS = ("window_seconds", "gain", "contrast", "smoothing", "max_height_ratio",
                 "reactivity", "drift", "line_width")
_ENUMS = {"layout": LAYOUTS, "style": STYLES, "metric": METRICS}
LOOK_FIELDS = (*_INT_FIELDS, *_FLOAT_FIELDS, *_ENUMS, "stationary", "color", "color2")
_MAX_STR = 64


def validate_look_field(name: str, value: object) -> object:
    """Returns the checked (and normalised) value of one field; raises ValueError."""
    if name in _INT_FIELDS:
        if type(value) is not int:
            raise ValueError(f"{name}: expected an integer")
    elif name in _FLOAT_FIELDS:
        if type(value) not in (int, float):
            raise ValueError(f"{name}: expected a number")
    elif name in _ENUMS:
        if type(value) is not str or value not in _ENUMS[name]:
            raise ValueError(f"{name}: not an allowed value")
        return value
    elif name == "stationary":
        if type(value) is not bool:
            raise ValueError(f"{name}: expected true or false")
        return value
    elif name in ("color", "color2"):
        if name == "color2" and value is None:
            return None
        if type(value) is not str or len(value) > _MAX_STR:
            raise ValueError(f"{name}: expected a #RRGGBB color")
        try:
            return _hex(parse_hex(value))
        except ValueError:
            raise ValueError(f"{name}: expected a #RRGGBB color") from None
    else:
        raise ValueError("unknown field")
    if type(value) is float and not math.isfinite(value):
        raise ValueError(f"{name}: must be a finite number")
    lo, hi = LOOK_LIMITS[name]
    if not lo <= value <= hi:
        raise ValueError(f"{name}: out of range")
    if name == "fps" and value not in ALLOWED_FPS:
        raise ValueError("fps: not an allowed value")
    return float(value) if name in _FLOAT_FIELDS else value


def _check_format(width: int, height: int) -> None:
    if (width, height) not in FORMATS:
        raise ValueError("format: not an allowed size")


def look_from_dict(data: object) -> Look:
    """Builds a Look from untrusted JSON; missing keys take the defaults.

    Unknown keys are rejected (a typo must not silently render the wrong thing).
    """
    if not isinstance(data, dict):
        raise ValueError("look: expected an object")  # noqa: TRY004
    merged = default_look_dict()
    for key, value in data.items():
        if type(key) is not str or key not in LOOK_FIELDS:
            raise ValueError("unknown field")
        merged[key] = validate_look_field(key, value)
    _check_format(merged["width"], merged["height"])
    color = parse_hex(merged["color"])
    color2 = None if merged["color2"] is None else parse_hex(merged["color2"])
    values = {k: v for k, v in merged.items() if k not in ("color", "color2")}
    return Look(**values, color=color, color2=color2)
