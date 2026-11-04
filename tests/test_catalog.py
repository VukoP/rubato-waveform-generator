# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""The catalog is the UI's single source of truth: every value is pinned so a typo fails."""

import json
import math

import pytest

from rubato_waveform import catalog as cat
from rubato_waveform.engine import LAYOUTS, LOOK_LIMITS, REFLECT_BASELINE, THEMES, Look


@pytest.fixture(scope="module")
def data() -> dict:
    return cat.catalog()


def test_layouts_in_ui_order(data) -> None:
    assert [(x["key"], x["label"]) for x in data["layouts"]] == [
        ("scroll", "Scroll"), ("mirror", "Center-out"), ("pulse", "Pulse"), ("flow", "Flow"),
        ("ripple", "Ripple"), ("neon", "Neon"), ("mirror-reflect", "Reflection"),
        ("peakhold", "Peak hold"), ("radial", "Orbit"), ("voiceline", "Voice line"),
    ]
    assert {x["key"] for x in data["layouts"]} == set(LAYOUTS)


def test_small_enums(data) -> None:
    assert [(x["key"], x["label"]) for x in data["styles"]] == [
        ("bars", "Bars"), ("line", "Line"), ("area", "Area"), ("dots", "Dots")]
    assert [(x["key"], x["label"]) for x in data["metrics"]] == [("peak", "Peak"), ("rms", "Loudness")]
    assert [(x["stationary"], x["label"]) for x in data["motion"]] == [(False, "Moving"), (True, "Static")]
    assert [(x["gradient"], x["label"]) for x in data["fill"]] == [(False, "Solid"), (True, "Gradient")]
    assert [(x["key"], x["label"]) for x in data["sections"]] == [
        ("signal", "Signal"), ("form", "Form"), ("color", "Color")]


def test_presets_exact(data) -> None:
    got = [(p["label"], tuple(p["values"][f] for f in
                              ("gain", "bars", "max_height_ratio", "window_seconds", "contrast", "smoothing")))
           for p in data["presets"]]
    assert got == [
        ("Calm", (0.9, 49, 0.58, 1.6, 0.85, 0.55)),
        ("Balanced", (1.0, 61, 0.72, 1.05, 1.0, 0.25)),
        ("Energetic", (1.25, 85, 0.86, 0.7, 1.15, 0.08)),
    ]


def test_slider_ranges_and_formats(data) -> None:
    got = {s["key"]: (s["field"], s["label"], s["section"], s["min"], s["max"], s["step"])
           for s in data["sliders"]}
    assert list(got) == ["sensitivity", "contrast", "smoothing", "window", "density", "height",
                         "opacity", "reactivity", "drift", "thickness"]
    assert got == {
        "sensitivity": ("gain", "Sensitivity", "signal", 0.3, 2.5, None),
        "contrast": ("contrast", "Contrast", "signal", 0.4, 2.0, None),
        "smoothing": ("smoothing", "Smoothing", "signal", 0.0, 1.0, None),
        "window": ("window_seconds", "Time window", "form", 0.25, 3.0, None),
        "density": ("bars", "Density", "form", 16, 160, 1),
        "height": ("max_height_ratio", "Height", "form", 0.2, 0.98, None),
        "opacity": ("opacity", "Opacity", "form", 40, 255, 1),
        "reactivity": ("reactivity", "Reactivity", "form", 0.0, 1.0, None),
        "drift": ("drift", "Drift", "form", 0.0, 2.0, None),
        "thickness": ("line_width", "Thickness", "form", 1.0, 6.0, None),
    }
    by_key = {s["key"]: s for s in data["sliders"]}
    shown = {
        "sensitivity": (1.0, "1.00×"), "contrast": (1.0, "1.00"), "smoothing": (0.25, "25%"),
        "window": (1.05, "1.05 s"), "density": (61, "61"), "height": (0.72, "72%"),
        "opacity": (235, "92%"), "drift": (1.0, "1.00×"), "thickness": (2.0, "2.0 px"),
        "reactivity": (0.7, "70%"),
    }
    for key, (value, text) in shown.items():
        assert cat.format_value(by_key[key], value) == text


def test_sliders_fit_look_limits_and_cover_ui_fields(data) -> None:
    for s in data["sliders"]:
        lo, hi = LOOK_LIMITS[s["field"]]
        assert lo <= s["min"] < s["max"] <= hi, s["key"]
    slider_fields = {s["field"] for s in data["sliders"]}
    preset_fields = {f for p in data["presets"] for f in p["values"]}
    assert preset_fields <= slider_fields
    # Every Look field the UI can set is either a slider, an enum/toggle, a color or the format.
    covered = slider_fields | {"layout", "style", "metric", "stationary", "color", "color2",
                               "width", "height", "fps"}
    assert covered == set(cat.LOOK_FIELDS)
    assert set(data["defaults"]["look"]) == set(cat.LOOK_FIELDS)
    for p in data["presets"]:
        for field, value in p["values"].items():
            lo, hi = LOOK_LIMITS[field]
            assert lo <= value <= hi


def test_themes_and_formats(data) -> None:
    assert len(data["themes"]) == 18
    assert [(t["name"], t["color"], t["color2"]) for t in data["themes"]] == list(THEMES)
    assert data["themes"][0]["name"] == "Violet"
    assert data["formats"] == [
        {"w": 1080, "h": 180, "label": "1080 × 180"},
        {"w": 1920, "h": 240, "label": "1920 × 240"},
        {"w": 3840, "h": 360, "label": "3840 × 360"},
        {"w": 1080, "h": 1080, "label": "1080 × 1080"},
    ]


def test_rules(data) -> None:
    r = data["rules"]
    assert r["playhead_layouts"] == ["scroll", "flow", "neon", "mirror", "mirror-reflect"]
    assert r["stationary_optional"] == ["scroll", "mirror", "flow", "radial"]
    assert r["style_disabled_layouts"] == ["neon"]
    assert r["voice_only_layouts"] == ["voiceline"]
    assert r["square_layouts"] == ["radial"]
    assert r["square_format"] == {"w": 1080, "h": 1080}
    assert r["reflection_baseline"] == REFLECT_BASELINE
    assert r["no_baseline_layouts"] == ["peakhold", "radial"]


def test_catalog_is_strict_json(data) -> None:
    assert json.loads(json.dumps(data, allow_nan=False)) == data


EXPECTED_DEFAULT = Look(
    width=1080, height=180, fps=30, bars=61, style="bars", layout="mirror", window_seconds=1.05,
    metric="peak", gain=1.0, contrast=1.0, smoothing=0.25, color=(139, 77, 255),
    color2=(213, 94, 207), opacity=235, max_height_ratio=0.72, stationary=False,
    reactivity=0.7, drift=1.0, line_width=2.0)


def test_default_look_round_trips(data) -> None:
    assert cat.look_from_dict(data["defaults"]["look"]) == EXPECTED_DEFAULT
    assert cat.look_from_dict({}) == EXPECTED_DEFAULT
    assert data["defaults"]["theme"] == "Violet" and data["defaults"]["gradient"] is True


def test_valid_overrides_and_solid_color() -> None:
    look = cat.look_from_dict({"layout": "radial", "width": 1080, "height": 1080, "bars": 100,
                               "color": "ff0000", "color2": None, "gain": 2, "stationary": True})
    assert (look.layout, look.height, look.bars, look.color, look.color2) == (
        "radial", 1080, 100, (255, 0, 0), None)
    assert look.gain == 2.0 and isinstance(look.gain, float) and look.stationary is True


@pytest.mark.parametrize("payload", [
    {"gain": float("nan")}, {"gain": float("inf")}, {"gain": float("-inf")},
    {"gain": "1.0"}, {"bars": "61"}, {"gain": True}, {"bars": True}, {"stationary": 1},
    {"bars": 61.0}, {"bars": 10**30}, {"gain": 10**400}, {"bars": 241}, {"bars": 7},
    {"gain": 2.6}, {"gain": -1}, {"smoothing": -0.1}, {"opacity": 0}, {"line_width": -2.0},
    {"layout": "nope"}, {"layout": 3}, {"style": "BARS"}, {"metric": None},
    {"color": "#12345"}, {"color": "red"}, {"color": 5}, {"color2": "#GG0000"},
    {"width": 1234, "height": 180}, {"width": 1080, "height": 1081},
    {"fps": 60}, {"fps": 30.0}, {"fps": 0},
    {"surprise": 1}, {"px_scale": 2}, {"baseline_dots": False},
    {1: 2},
])
def test_rejections(payload) -> None:
    with pytest.raises(ValueError):
        cat.look_from_dict(payload)


@pytest.mark.parametrize("payload", [None, [], "look", 5, [{"gain": 1}]])
def test_non_dict_rejected(payload) -> None:
    with pytest.raises(ValueError):
        cat.look_from_dict(payload)


def test_huge_input_is_not_echoed() -> None:
    huge = "A" * 100_000
    for payload in ({"layout": huge}, {"color": huge}, {huge: 1}, {"gain": huge}):
        with pytest.raises(ValueError) as err:
            cat.look_from_dict(payload)
        assert len(str(err.value)) < 80 and "AAAA" not in str(err.value)


def test_infinite_range_check_is_finite() -> None:
    assert math.isfinite(cat.look_from_dict({"gain": 2.5}).gain)
