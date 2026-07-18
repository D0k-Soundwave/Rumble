"""Self-test: a curve-fitted profile survives a device sample-rate change.

PK bands are biquads and warp with the sample rate; a GraphicEQ line does not.
So the residual that completes a fitted profile only cancels its bands exactly
at the rate it was solved for. ProfileManager.refresh_fits_for_sample_rate()
re-solves that residual when the rate changes, restoring exactness — WITHOUT
touching hand-authored profiles, whose GraphicEQ layer is deliberate tuning and
not a residual at all.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from rumble.core import autoeq_merge, eq_math
from rumble.core.apo_config import ChannelSection, Filter, ProfileConfig
from rumble.core.profile_manager import ProfileManager

failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


WRITE_PRECISION_DB = 0.1
FREQS = np.geomspace(20.0, 20000.0, 127)


def _bell(f, fc, gain, q):
    return gain * np.exp(-((np.log2(f / fc) * q) ** 2))


def _curve():
    g = (_bell(FREQS, 45, 6.5, 1.1) + _bell(FREQS, 3000, -5.0, 2.2)
         + _bell(FREQS, 5500, 3.0, 3.0) + _bell(FREQS, 9000, -3.5, 2.5)
         + _bell(FREQS, 14000, 2.0, 1.8))
    return list(zip(FREQS.tolist(), np.round(g, 1).tolist()))


def _deviation(section, curve, play_rate):
    """max |what the profile produces at play_rate - the curve it means|."""
    freqs = eq_math.frequency_array()
    total = eq_math.combined_response(section.filters, freqs, play_rate)
    if section.graphic_eq:
        total = total + eq_math.graphic_eq_response(section.graphic_eq, freqs)
    want = eq_math.graphic_eq_response(autoeq_merge.format_graphiceq(curve), freqs)
    band = (freqs >= curve[0][0]) & (freqs <= curve[-1][0])
    return float(np.max(np.abs(want - total)[band]))


def _manager():
    tmp = Path(tempfile.mkdtemp(prefix="rumble_fitrate_"))
    # config_path MUST be set or the test writes into the real APO config dir.
    return ProfileManager(data_dir=tmp / "data", config_path=tmp / "config")


curve = _curve()

# ── the drift this exists to fix is real ──────────────────────────────────
built = autoeq_merge.build_curve_config(curve, sample_rate=96000)
sec = built.channel_sections[0]
at_96k = _deviation(sec, curve, 96000)
at_44k = _deviation(sec, curve, 44100)
check(at_96k <= WRITE_PRECISION_DB,
      f"a profile fitted at 96k is exact when played at 96k ({at_96k:.3f} dB)")
check(at_44k > 3 * WRITE_PRECISION_DB,
      f"...and genuinely drifts when played at 44.1k ({at_44k:.3f} dB) — biquads "
      "warp with sample rate, which is what this refresh exists to repair")

# ── re-solving the residual restores exactness ────────────────────────────
changed = autoeq_merge.retune_config_for_sample_rate(built, 96000, 44100)
sec = built.channel_sections[0]
after = _deviation(sec, curve, 44100)
check(changed, "retune reports that it changed the config")
check(after <= WRITE_PRECISION_DB,
      f"after re-solving for 44.1k the profile is exact again at 44.1k "
      f"({at_44k:.3f} dB -> {after:.3f} dB)")

bands_before = [(f.fc, f.gain, f.q) for f in
                autoeq_merge.build_curve_config(curve, sample_rate=96000)
                .channel_sections[0].filters]
bands_after = [(f.fc, f.gain, f.q) for f in sec.filters]
check(bands_before == bands_after,
      "the BANDS are untouched — only the residual layer moves, so the editable "
      "representation the user sees never changes under them")

# ── a fit good enough to need no residual is still repaired ───────────────
# (nothing to adjust would mean nothing to fix, so a residual must be created)
gentle = list(zip(FREQS.tolist(), np.round(_bell(FREQS, 1000, 3.0, 1.0), 1).tolist()))
cfg = autoeq_merge.build_curve_config(gentle, sample_rate=96000)
autoeq_merge.retune_config_for_sample_rate(cfg, 96000, 44100)
check(_deviation(cfg.channel_sections[0], gentle, 44100) <= WRITE_PRECISION_DB,
      "a profile whose bands needed no residual still gets one when the rate "
      "changes, absorbing the bands' drift")

# ── repeated rate flips must not accumulate drift ─────────────────────────
cfg = autoeq_merge.build_curve_config(curve, sample_rate=48000)
rate = 48000
for nxt in (44100, 96000, 48000, 44100, 96000, 44100, 48000, 96000):
    autoeq_merge.retune_config_for_sample_rate(cfg, rate, nxt)
    rate = nxt
drift = _deviation(cfg.channel_sections[0], curve, rate)
check(drift <= WRITE_PRECISION_DB,
      f"8 consecutive rate changes do not accumulate error ({drift:.3f} dB) — each "
      "re-solve reconstructs the intended curve from the profile itself")

# ── the manager only touches profiles it fitted ───────────────────────────
mgr = _manager()
fitted = mgr.create_profile(
    "Fitted Import", config=autoeq_merge.build_curve_config(curve, sample_rate=96000),
    fit_sample_rate=96000)

# A hand-authored profile that deliberately uses BOTH bands and a Graphic EQ
# layer (rumble/user/arc_raiders_cache_hunter.txt is a real one). Its GraphicEQ is
# tuning, not a residual: rewriting it would silently destroy the user's work.
hand_made_geq = "20 -1; 60 -3; 1000 0; 8000 0.5; 20000 0"
hand = mgr.create_profile("Hand Made", config=ProfileConfig(
    preamp=-6.0, channel_sections=[ChannelSection(
        channel="L R", graphic_eq=hand_made_geq,
        filters=[Filter(type="PK", fc=1000.0, gain=3.0, q=1.41)])]))

changed_ids = mgr.refresh_fits_for_sample_rate(44100)
check(changed_ids == [fitted.id],
      f"only the curve-fitted profile is re-solved ({changed_ids}) — the "
      "hand-authored one is left alone")
check(mgr.load_profile_config(hand.id).channel_sections[0].graphic_eq == hand_made_geq,
      "the hand-authored profile's Graphic EQ layer is byte-for-byte untouched")
check(mgr.get_profile(fitted.id).fit_sample_rate == 44100,
      "the fitted profile's recorded fit rate is updated to the new device rate")

reloaded = mgr.load_profile_config(fitted.id)
check(_deviation(reloaded.channel_sections[0], curve, 44100) <= WRITE_PRECISION_DB,
      "the profile ON DISK is exact at the new rate after the refresh")

# ── the marker round-trips through the file ───────────────────────────────
mgr2 = ProfileManager(data_dir=mgr.data_dir, config_path=mgr.config_path)
again = mgr2.get_profile(fitted.id)
check(again is not None and again.fit_sample_rate == 44100,
      "the Fit Sample Rate header survives a save/scan round-trip, so the next "
      "session still knows the Graphic EQ layer is a residual")
check(mgr2.get_profile(hand.id).fit_sample_rate is None,
      "a hand-authored profile carries no fit marker and never will")

# ── a second refresh at the same rate is a no-op ──────────────────────────
check(mgr2.refresh_fits_for_sample_rate(44100) == [],
      "refreshing at the rate a profile is already fitted for changes nothing")

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("All fit/sample-rate checks passed.")
