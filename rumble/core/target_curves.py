"""Named, cited headphone target-response curves (Rumble 3.4).

Until now every headphone import used whatever target AutoEQ baked into its
own published correction, and every speaker import used a plain linear tilt
(:func:`rumble.core.eq_target.tilt_target`). Neither lets RUMBLE reason about
*which* target curve is appropriate independent of whether AutoEQ happens to
publish data for a given device. This module holds real, numerically-cited
target curves so :func:`rumble.core.eq_target.make_correction` can target one
directly (via its ``target_curve`` parameter) instead of only a straight tilt.

Every curve here is real published data, resampled onto
:func:`rumble.core.eq_target.log_grid`'s standard 20 Hz-20 kHz/12-point-per-
octave grid (log-linear interpolation, the same convention
:mod:`rumble.core.autoeq_merge` uses everywhere else) — never invented or
eyeballed. Sources, fetched 2026-07-10 from AutoEQ's own bundled reference
targets (commit ``6c9a097626213b8cbb0973e5a4dd645f5f9e3fd4``, the last commit
to touch ``targets/`` as of this writing):

* Harman over-ear 2018 — ``targets/Harman over-ear 2018.csv``
  https://github.com/jaakkopasanen/AutoEq/blob/6c9a097/targets/Harman%20over-ear%202018.csv
  (Olive/Welti/Khonsaripour, "The Preference for a Well Balanced Bass and
  Treble Response in Headphones", Harman International, presented AES 2018.)
* Harman in-ear 2019 — ``targets/Harman in-ear 2019.csv``
  https://github.com/jaakkopasanen/AutoEq/blob/6c9a097/targets/Harman%20in-ear%202019.csv
  (same research program's follow-up in-ear/IEM preference target.)
* Diffuse-field (5128 rig) — ``targets/Diffuse field 5128.csv``
  https://github.com/jaakkopasanen/AutoEq/blob/6c9a097/targets/Diffuse%20field%205128.csv
  (the classical "flat as measured in a diffuse sound field" reference,
  B&K 5128 head-and-torso-simulator variant — AutoEQ also bundles GRAS KEMAR
  and ISO 11904-1 rig variants of this same curve; 5128 is used here as it is
  the newer, more commonly cited rig in current headphone measurement work.)

**Free-field is intentionally not implemented.** AutoEQ's own ``targets/``
directory (checked in full — every file listed, see the module test for the
exact set) publishes no free-field curve, and no numerically-complete,
freely-available free-field target dataset was found during this session's
research (ISO 11904-1's free-field part is paywalled; several papers describe
it qualitatively but do not publish a full point table). Per this project's
"never guess a constant" rule, inventing curve points from a qualitative
description would be worse than not offering the option — this is tracked as
a follow-up research item (RUMBLE_ACTION_GUIDE.md #3.4), not silently dropped.
"""
from __future__ import annotations

Point = tuple[float, float]

_HARMAN_OVER_EAR_2018: list[Point] = [
    (20, 3.86), (21.2, 4.01), (22.4, 4.04), (23.8, 4.03), (25.2, 4.02), (26.7, 4.01),
    (28.3, 3.98), (30, 3.96), (31.7, 3.93), (33.6, 3.89), (35.6, 3.83), (37.8, 3.77),
    (40, 3.7), (42.4, 3.65), (44.9, 3.54), (47.6, 3.45), (50.4, 3.3), (53.4, 3.17),
    (56.6, 3.03), (59.9, 2.9), (63.5, 2.72), (67.3, 2.55), (71.3, 2.38), (75.5, 2.17),
    (80, 1.99), (84.8, 1.78), (89.8, 1.55), (95.1, 1.29), (100.8, 1.01), (106.8, 0.73),
    (113.1, 0.43), (119.9, 0.12), (127, -0.17), (134.5, -0.46), (142.5, -0.7), (151, -0.93),
    (160, -1.16), (169.5, -1.38), (179.6, -1.6), (190.3, -1.8), (201.6, -1.96),
    (213.6, -2.06), (226.3, -2.09), (239.7, -2.06), (254, -1.99), (269.1, -1.89),
    (285.1, -1.77), (302, -1.65), (320, -1.51), (339, -1.39), (359.2, -1.28), (380.5, -1.19),
    (403.2, -1.12), (427.1, -1.06), (452.5, -0.99), (479.5, -0.91), (508, -0.82),
    (538.2, -0.71), (570.2, -0.61), (604.1, -0.53), (640, -0.43), (678.1, -0.35),
    (718.4, -0.28), (761.1, -0.22), (806.3, -0.17), (854.3, -0.15), (905.1, -0.11),
    (958.9, -0.06), (1015.9, 0.03), (1076.3, 0.17), (1140.4, 0.37), (1208.2, 0.64),
    (1280, 0.97), (1356.1, 1.36), (1436.8, 1.79), (1522.2, 2.23), (1612.7, 2.75),
    (1708.6, 3.35), (1810.2, 4.02), (1917.8, 4.73), (2031.9, 5.43), (2152.7, 6.05),
    (2280.7, 6.63), (2416.3, 7.15), (2560, 7.56), (2712.2, 7.91), (2873.5, 8.17),
    (3044.4, 8.37), (3225.4, 8.53), (3417.2, 8.58), (3620.4, 8.52), (3835.7, 8.35),
    (4063.7, 8.1), (4305.4, 7.74), (4561.4, 7.28), (4832.6, 6.77), (5120, 6.26),
    (5424.5, 5.8), (5747, 5.4), (6088.7, 5.0), (6450.8, 4.5), (6834.4, 4.03), (7240.8, 3.47),
    (7671.3, 2.85), (8127.5, 2.25), (8610.8, 1.57), (9122.8, 0.8), (9665.3, -0.1),
    (10240, -1.1), (10848.9, -2.14), (11494, -3.34), (12177.5, -4.52), (12901.6, -5.62),
    (13668.8, -6.62), (14481.5, -7.47), (15342.7, -8.36), (16255, -9.6), (17221.6, -11.53),
    (18245.6, -14.65), (19330.5, -19.52), (20480, -22.92),
]

_HARMAN_IN_EAR_2019: list[Point] = [
    (20, 7.61), (21.2, 7.66), (22.4, 7.69), (23.8, 7.71), (25.2, 7.72), (26.7, 7.72),
    (28.3, 7.72), (30, 7.72), (31.7, 7.7), (33.6, 7.67), (35.6, 7.61), (37.8, 7.52),
    (40, 7.42), (42.4, 7.28), (44.9, 7.1), (47.6, 6.89), (50.4, 6.63), (53.4, 6.38),
    (56.6, 6.1), (59.9, 5.85), (63.5, 5.49), (67.3, 5.2), (71.3, 4.86), (75.5, 4.5),
    (80, 4.19), (84.8, 3.85), (89.8, 3.51), (95.1, 3.15), (100.8, 2.76), (106.8, 2.43),
    (113.1, 2.06), (119.9, 1.68), (127, 1.34), (134.5, 0.99), (142.5, 0.67), (151, 0.39),
    (160, 0.11), (169.5, -0.18), (179.6, -0.47), (190.3, -0.75), (201.6, -1.0), (213.6, -1.2),
    (226.3, -1.38), (239.7, -1.51), (254, -1.6), (269.1, -1.66), (285.1, -1.7), (302, -1.71),
    (320, -1.71), (339, -1.7), (359.2, -1.67), (380.5, -1.63), (403.2, -1.59), (427.1, -1.55),
    (452.5, -1.51), (479.5, -1.45), (508, -1.38), (538.2, -1.32), (570.2, -1.24),
    (604.1, -1.18), (640, -1.07), (678.1, -0.98), (718.4, -0.86), (761.1, -0.75),
    (806.3, -0.62), (854.3, -0.49), (905.1, -0.34), (958.9, -0.15), (1015.9, 0.06),
    (1076.3, 0.29), (1140.4, 0.58), (1208.2, 0.93), (1280, 1.32), (1356.1, 1.76),
    (1436.8, 2.22), (1522.2, 2.68), (1612.7, 3.21), (1708.6, 3.81), (1810.2, 4.49),
    (1917.8, 5.24), (2031.9, 6.0), (2152.7, 6.69), (2280.7, 7.37), (2416.3, 7.99),
    (2560, 8.49), (2712.2, 8.9), (2873.5, 9.17), (3044.4, 9.34), (3225.4, 9.42),
    (3417.2, 9.38), (3620.4, 9.25), (3835.7, 9.05), (4063.7, 8.82), (4305.4, 8.55),
    (4561.4, 8.25), (4832.6, 7.92), (5120, 7.61), (5424.5, 7.3), (5747, 7.0), (6088.7, 6.66),
    (6450.8, 6.18), (6834.4, 5.66), (7240.8, 4.97), (7671.3, 4.17), (8127.5, 3.33),
    (8610.8, 2.33), (9122.8, 1.22), (9665.3, -0.02), (10240, -1.26), (10848.9, -2.44),
    (11494, -3.68), (12177.5, -4.87), (12901.6, -5.99), (13668.8, -7.08), (14481.5, -8.1),
    (15342.7, -9.21), (16255, -10.66), (17221.6, -12.62), (18245.6, -15.26),
    (19330.5, -18.58), (20480, -20.61),
]

_DIFFUSE_FIELD_5128: list[Point] = [
    (20, -4.25), (21.2, -4.25), (22.4, -4.24), (23.8, -4.24), (25.2, -4.23), (26.7, -4.22),
    (28.3, -4.21), (30, -4.2), (31.7, -4.19), (33.6, -4.18), (35.6, -4.17), (37.8, -4.16),
    (40, -4.15), (42.4, -4.14), (44.9, -4.13), (47.6, -4.11), (50.4, -4.09), (53.4, -4.08),
    (56.6, -4.07), (59.9, -4.06), (63.5, -4.04), (67.3, -4.02), (71.3, -4.0), (75.5, -3.99),
    (80, -3.98), (84.8, -3.96), (89.8, -3.94), (95.1, -3.91), (100.8, -3.89), (106.8, -3.87),
    (113.1, -3.84), (119.9, -3.82), (127, -3.78), (134.5, -3.75), (142.5, -3.73),
    (151, -3.69), (160, -3.65), (169.5, -3.61), (179.6, -3.55), (190.3, -3.52),
    (201.6, -3.46), (213.6, -3.4), (226.3, -3.33), (239.7, -3.26), (254, -3.2),
    (269.1, -3.12), (285.1, -3.03), (302, -2.95), (320, -2.84), (339, -2.75), (359.2, -2.65),
    (380.5, -2.54), (403.2, -2.42), (427.1, -2.29), (452.5, -2.15), (479.5, -1.97),
    (508, -1.8), (538.2, -1.59), (570.2, -1.36), (604.1, -1.13), (640, -0.83), (678.1, -0.59),
    (718.4, -0.33), (761.1, -0.14), (806.3, -0.02), (854.3, -0.0), (905.1, -0.08),
    (958.9, -0.1), (1015.9, 0.09), (1076.3, 0.48), (1140.4, 0.99), (1208.2, 1.4),
    (1280, 1.66), (1356.1, 1.83), (1436.8, 2.11), (1522.2, 2.65), (1612.7, 3.51),
    (1708.6, 4.25), (1810.2, 4.64), (1917.8, 5.19), (2031.9, 6.13), (2152.7, 7.14),
    (2280.7, 8.09), (2416.3, 9.24), (2560, 10.43), (2712.2, 11.35), (2873.5, 12.04),
    (3044.4, 12.3), (3225.4, 11.91), (3417.2, 11.52), (3620.4, 10.94), (3835.7, 10.49),
    (4063.7, 10.06), (4305.4, 9.82), (4561.4, 9.37), (4832.6, 9.33), (5120, 9.0),
    (5424.5, 8.86), (5747, 8.53), (6088.7, 7.95), (6450.8, 7.5), (6834.4, 7.55),
    (7240.8, 7.93), (7671.3, 8.29), (8127.5, 8.3), (8610.8, 7.16), (9122.8, 4.4),
    (9665.3, 3.55), (10240, 4.99), (10848.9, 4.74), (11494, 2.49), (12177.5, 1.43),
    (12901.6, 1.13), (13668.8, 0.98), (14481.5, -0.28), (15342.7, -1.0), (16255, -0.33),
    (17221.6, -1.53), (18245.6, -3.0), (19330.5, -3.74), (20480, -5.31),
]

#: name -> (curve, label, applicable device types). Device types use the same
#: strings as AutoEqEntry.device_type / a speaker's "Speaker" device type.
_CURVES: dict[str, tuple[list[Point], str, tuple[str, ...]]] = {
    "harman_2018_over_ear": (
        _HARMAN_OVER_EAR_2018, "Harman 2018 (over-ear)", ("Over-ear",)),
    "harman_2019_in_ear": (
        _HARMAN_IN_EAR_2019, "Harman 2019 (in-ear)", ("In-ear", "Earbud")),
    "diffuse_field": (
        _DIFFUSE_FIELD_5128, "Diffuse-field (5128 rig)",
        ("Over-ear", "In-ear", "Earbud", "Speaker")),
}

DEFAULT_BY_DEVICE_TYPE: dict[str, str] = {
    "Over-ear": "harman_2018_over_ear",
    "In-ear": "harman_2019_in_ear",
    "Earbud": "harman_2019_in_ear",
}


def names() -> list[str]:
    """Every named curve's key, in a stable display order."""
    return list(_CURVES.keys())


def get_curve(name: str) -> list[Point]:
    """The named curve's points (a copy — callers may freely mutate it)."""
    return list(_CURVES[name][0])


def label(name: str) -> str:
    """Human-readable label for a named curve."""
    return _CURVES[name][1]


def applies_to(name: str, device_type: str) -> bool:
    """Whether *name* is a sensible target for *device_type*
    ("Over-ear"/"In-ear"/"Earbud"/"Speaker")."""
    return device_type in _CURVES[name][2]


def options_for_device_type(device_type: str) -> list[tuple[str, str]]:
    """``(label, name)`` pairs applicable to *device_type*, in display order."""
    return [(lbl, key) for key, (_, lbl, types) in _CURVES.items()
            if device_type in types]


def default_for_device_type(device_type: str) -> str | None:
    """The named curve this device type should default to, or ``None`` if
    there's no research-backed default for it (e.g. a speaker, which keeps
    its existing linear-tilt default unless the user opts into a named
    curve)."""
    return DEFAULT_BY_DEVICE_TYPE.get(device_type)
