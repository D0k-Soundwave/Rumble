"""Self-test for the auto-preamp / headroom maths (core only, no Qt).

Run:  python tests/selftest_preamp_math.py
Covers margin selection (0.5 dB default, 1.0 dB escalation), 0.1 dB floor
quantisation, GraphicEQ contribution, the per-section trim offset and the
pre-sampled impulse-response contribution. Exits non-zero on failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from rumble.core import eq_math, preamp_calc
from rumble.core.apo_config import Filter

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def pk(fc: float, gain: float, q: float = 1.0, enabled: bool = True) -> Filter:
    return Filter(enabled=enabled, type="PK", fc=fc, gain=gain, q=q)


def peak_of(filters: list[Filter], geq: str = "") -> float:
    freqs = eq_math.frequency_array()
    resp = eq_math.combined_response(filters, freqs, eq_math.DEFAULT_SAMPLE_RATE)
    if geq:
        resp = resp + eq_math.graphic_eq_response(geq, freqs)
    return float(np.max(resp))


def main() -> int:
    print("[1] basics")
    check(preamp_calc.calculate_auto_preamp([]) == 0.0, "no filters -> 0.0")
    check(preamp_calc.calculate_auto_preamp([pk(1000, -6.0)]) == 0.0, "pure cuts -> 0.0")
    check(preamp_calc.calculate_headroom([], -3.0) == 3.0, "empty headroom = -preamp")
    check(preamp_calc.calculate_auto_preamp([pk(1000, 3.0, enabled=False)]) == 0.0,
          "disabled bands ignored")

    print("[2] margins and quantisation")
    modest = [pk(1000, 3.0)]
    auto = preamp_calc.calculate_auto_preamp(modest)
    required = -(peak_of(modest) + 0.5)
    check(auto <= required + 1e-9 and auto > required - 0.1 - 1e-9,
          "modest boost: 0.5 dB margin, floored to 0.1 dB grid")
    check(abs(auto * 10 - round(auto * 10)) < 1e-9, "value quantised to 0.1 dB")

    hot = [pk(1000, 8.0)]
    auto_hot = preamp_calc.calculate_auto_preamp(hot)
    required_hot = -(peak_of(hot) + 1.0)
    check(auto_hot <= required_hot + 1e-9 and auto_hot > required_hot - 0.1 - 1e-9,
          "high boost escalates to 1.0 dB margin")

    steep = [pk(100, 4.5, 2.5), pk(1000, 4.5, 2.5), pk(10000, 4.5, 2.5)]
    required_steep = -(peak_of(steep) + 1.0)
    check(preamp_calc.calculate_auto_preamp(steep) <= required_steep + 1e-9,
          "three narrow-Q sizeable boosts escalate the margin")

    # Q is not a valid predictor of worst-case peak risk (measured empirically,
    # see docs/RUMBLE_Headroom_Maths.md Step 4 addendum) -- three WIDE-Q boosts
    # of the same gain must escalate the margin exactly like narrow-Q ones.
    wide = [pk(100, 4.5, 0.7), pk(1000, 4.5, 0.7), pk(10000, 4.5, 0.7)]
    required_wide = -(peak_of(wide) + 1.0)
    check(preamp_calc.calculate_auto_preamp(wide) <= required_wide + 1e-9,
          "three wide-Q sizeable boosts also escalate the margin")

    # Fewer than 3 sizeable boosts, and no single boost over ~6 dB, must stay
    # at the default 0.5 dB margin regardless of Q.
    two_only = [pk(100, 4.5, 0.7), pk(1000, 4.5, 0.7)]
    required_two = -(peak_of(two_only) + 0.5)
    check(preamp_calc.calculate_auto_preamp(two_only) <= required_two + 1e-9
          and preamp_calc.calculate_auto_preamp(two_only) > required_two - 0.1 - 1e-9,
          "only two sizeable boosts stays at the default margin")

    print("[3] graphic eq contribution")
    gauto = preamp_calc.calculate_auto_preamp([], geq_string="20 6; 20000 6")
    check(abs(gauto + 6.5) < 1e-9, "flat +6 dB GEQ -> -6.5 dB preamp")
    check(abs(preamp_calc.calculate_headroom([], -10.0, geq_string="20 6; 20000 6")
              - 4.0) < 1e-9, "headroom includes the GEQ peak")

    print("[4] per-section trim offset")
    base = preamp_calc.calculate_auto_preamp(modest)
    trimmed = preamp_calc.calculate_auto_preamp(modest, section_offset_db=2.0)
    check(abs((base - trimmed) - 2.0) < 1e-9,
          "positive trim deepens auto-preamp by exactly the offset")
    check(abs(preamp_calc.calculate_headroom(modest, -10.0, section_offset_db=2.0)
              - (preamp_calc.calculate_headroom(modest, -10.0) - 2.0)) < 1e-9,
          "trim shifts headroom by exactly the offset")
    check(preamp_calc.calculate_auto_preamp(modest, section_offset_db=-30.0) == 0.0,
          "deep negative trim -> no preamp needed")
    check(abs(preamp_calc.calculate_headroom([], -3.0, section_offset_db=1.0)
              - 2.0) < 1e-9, "trim-only chain still yields headroom")

    print("[5] impulse-response contribution")
    freqs = eq_math.frequency_array()
    flat2 = np.full(freqs.shape, 2.0)
    with_ir = preamp_calc.calculate_auto_preamp(modest, ir_response=flat2)
    check(abs((base - with_ir) - 2.0) < 1e-9,
          "flat +2 dB IR deepens auto-preamp by exactly 2 dB")
    check(abs(preamp_calc.calculate_auto_preamp([], ir_response=flat2) + 2.5) < 1e-9,
          "IR alone still produces a preamp")
    check(abs(preamp_calc.calculate_headroom([], 0.0, ir_response=flat2) + 2.0) < 1e-9,
          "headroom accounts for the IR peak")
    cut_ir = np.full(freqs.shape, -30.0)
    check(preamp_calc.calculate_auto_preamp(modest, ir_response=cut_ir) == 0.0,
          "attenuating IR removes the need for preamp")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
