"""Selftest: B2 headphone crossfeed (rumble/core/crossfeed.py +
ProfileManager._render_crossfeed_block()/_render_layered_body()).

Run:  python tests/selftest_crossfeed.py

Covers: the generated Copy:/Channel:/Filter:/Delay:/Copy: block survives a
parse/render/re-parse round trip; the three presets' published parameters;
strength 0 renders no block; a disabled layer leaves the render
byte-identical; a non-stereo channel layout silently skips the layer; and
preview_config() carries the block through the same choke point ABX
playback and canonical activation use.
"""
from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import apo_config, crossfeed
from rumble.core.apo_config import ChannelSection, ProfileConfig
from rumble.core.crossover import BUTTERWORTH_Q
from rumble.core.profile_manager import PREVIEW_FILENAME, ProfileManager

_TOL = 1e-9


def _manager(tmp) -> ProfileManager:
    data_dir = Path(tmp) / "data"
    config_dir = Path(tmp) / "apo"
    return ProfileManager(data_dir, config_path=config_dir)


def _stereo_config() -> ProfileConfig:
    return ProfileConfig(preamp=0.0, channel_sections=[ChannelSection(channel="all")])


# ── render_crossfeed_block() ────────────────────────────────────────────

def test_zero_strength_is_flat_empty_block():
    assert crossfeed.render_crossfeed_block("chumoy", 0.0) == ""


def test_negative_and_over_100_strength_are_clamped():
    assert crossfeed.render_crossfeed_block("meier", -5.0) == ""
    at_100 = crossfeed.render_crossfeed_block("meier", 100.0)
    at_over = crossfeed.render_crossfeed_block("meier", 250.0)
    assert at_100 == at_over


def test_full_strength_reproduces_preset_gain_exactly():
    for name, preset in crossfeed.PRESETS.items():
        block = crossfeed.render_crossfeed_block(name, 100.0)
        parsed = apo_config.parse_config(block)
        section = parsed.channel_sections[0]
        assert abs(section.preamp_db - preset.gain_db) < 0.05, name


def test_strength_scales_toward_unity_not_toward_more_attenuation():
    # Half strength should sit strictly between the full-strength gain and
    # 0 dB (unity, no crossfeed) -- not simply halve the dB number.
    full = apo_config.parse_config(
        crossfeed.render_crossfeed_block("chumoy", 100.0)).channel_sections[0].preamp_db
    half = apo_config.parse_config(
        crossfeed.render_crossfeed_block("chumoy", 50.0)).channel_sections[0].preamp_db
    assert full < half < 0.0
    naive_half_db = full / 2.0
    assert not math.isclose(half, naive_half_db, abs_tol=0.05)


# ── preset parameter table (cited values) ───────────────────────────────

def test_preset_table_matches_cited_values():
    assert crossfeed.CHU_MOY.gain_db == -6.0
    assert crossfeed.CHU_MOY.freq_hz == 700.0
    assert crossfeed.CHU_MOY.delay_us == 0.0

    assert crossfeed.MEIER.gain_db == -9.5
    assert crossfeed.MEIER.freq_hz == 650.0
    assert crossfeed.MEIER.delay_us == 0.0

    # -4.5 dB is bs2b's default (Bauer 30-degree/3 m virtual-speaker) feed
    # level, the same preset catalogue the cmoy/jmeier figures come from.
    assert crossfeed.LINKWITZ.gain_db == -4.5
    assert crossfeed.LINKWITZ.freq_hz == 700.0
    assert crossfeed.LINKWITZ.delay_us == 300.0

    # bauer is the same -4.5 dB/700 Hz figure as LINKWITZ's gain, but
    # undelayed -- bs2b's default preset exposed on its own.
    assert crossfeed.BAUER.gain_db == -4.5
    assert crossfeed.BAUER.freq_hz == 700.0
    assert crossfeed.BAUER.delay_us == 0.0

    # direct is a plain unfiltered blend: no low-pass corner at all.
    assert crossfeed.DIRECT.gain_db == -6.0
    assert crossfeed.DIRECT.freq_hz is None
    assert crossfeed.DIRECT.delay_us == 0.0

    assert crossfeed.PRESETS == {
        "chumoy": crossfeed.CHU_MOY,
        "meier": crossfeed.MEIER,
        "linkwitz": crossfeed.LINKWITZ,
        "bauer": crossfeed.BAUER,
        "direct": crossfeed.DIRECT,
    }
    assert crossfeed.DEFAULT_PRESET in crossfeed.PRESETS


def test_direct_preset_emits_no_filter_line():
    # freq_hz=None means a plain full-band gain blend -- no Filter: line at
    # all, unlike every other preset which always emits an LP filter.
    block = crossfeed.render_crossfeed_block("direct", 100.0)
    assert "Filter" not in block
    parsed = apo_config.parse_config(block)
    section = parsed.channel_sections[0]
    assert section.filters == []
    assert abs(section.preamp_db - crossfeed.DIRECT.gain_db) < 0.05


def test_only_linkwitz_emits_a_delay_line():
    for name in ("chumoy", "meier"):
        block = crossfeed.render_crossfeed_block(name, 100.0)
        assert "Delay:" not in block
    linkwitz_block = crossfeed.render_crossfeed_block("linkwitz", 100.0)
    assert "Delay: 0.3 ms" in linkwitz_block


# ── render_block() dispatch, Custom mode ───────────────────────────────

def test_render_block_matches_render_crossfeed_block_for_named_presets():
    for name in crossfeed.PRESETS:
        assert crossfeed.render_block(name, 100.0, {}) == \
            crossfeed.render_crossfeed_block(name, 100.0)
        assert crossfeed.render_block(name, 0.0, {}) == \
            crossfeed.render_crossfeed_block(name, 0.0)


def test_custom_mode_uses_settings_gain_freq_delay():
    settings = {
        "crossfeed_custom_gain_db": -8.0,
        "crossfeed_custom_freq_hz": 500.0,
        "crossfeed_custom_delay_us": 200.0,
    }
    block = crossfeed.render_block("custom", 100.0, settings)
    parsed = apo_config.parse_config(block)
    section = parsed.channel_sections[0]
    assert abs(section.preamp_db - (-8.0)) < 0.05
    assert abs(section.filters[0].fc - 500.0) < _TOL
    assert abs(section.delay_ms - 0.2) < 0.005


def test_custom_mode_ignores_the_strength_argument():
    # Custom always renders its own exact figures -- the *strength* argument
    # passed through the dispatch is irrelevant to it (only crossfeed_enabled
    # gates it upstream in ProfileManager).
    settings = {"crossfeed_custom_gain_db": -5.0}
    assert crossfeed.render_block("custom", 0.0, settings) == \
        crossfeed.render_block("custom", 100.0, settings)


def test_custom_mode_defaults_without_settings():
    block = crossfeed.render_block("custom", 100.0, {})
    parsed = apo_config.parse_config(block)
    assert abs(parsed.channel_sections[0].preamp_db - (-6.0)) < 0.05


# ── round trip through apo_config ───────────────────────────────────────

def test_block_round_trips_through_parse_and_render():
    block = crossfeed.render_crossfeed_block("meier", 100.0)
    parsed = apo_config.parse_config(block)
    assert parsed.copy_lines == ["XL=R", "XR=L", "L=L+XL R=R+XR"]
    assert len(parsed.channel_sections) == 1
    section = parsed.channel_sections[0]
    assert section.channel == "XL XR"
    assert abs(section.preamp_db - crossfeed.MEIER.gain_db) < _TOL
    assert len(section.filters) == 1
    assert section.filters[0].type == "LP"
    assert abs(section.filters[0].fc - crossfeed.MEIER.freq_hz) < _TOL
    assert abs(section.filters[0].q - BUTTERWORTH_Q) < 0.005

    # Re-rendering the parsed structure and re-parsing that output is a
    # fixed point (no further data loss), even though render_config_body()
    # always orders all copy_lines before all channel_sections -- so the
    # re-rendered text's own Copy: line order differs from the original
    # block's (KB §17.4), which is exactly why crossfeed appends raw text
    # rather than going through ProfileConfig for real.
    rerendered = apo_config.render_config_body(parsed)
    reparsed = apo_config.parse_config(rerendered)
    assert reparsed.copy_lines == parsed.copy_lines
    assert reparsed.channel_sections[0].channel == section.channel
    assert reparsed.channel_sections[0].preamp_db == section.preamp_db
    assert reparsed.channel_sections[0].filters[0].fc == section.filters[0].fc


# ── can_crossfeed() ──────────────────────────────────────────────────────

def test_can_crossfeed_accepts_every_layout_carrying_l_and_r():
    # Crossfeed's only real precondition is "the stream has an L and an R" --
    # the block it emits touches nothing else. Every one of these qualifies.
    assert crossfeed.can_crossfeed([ChannelSection(channel="all")])
    assert crossfeed.can_crossfeed([ChannelSection(channel="L"), ChannelSection(channel="R")])
    # The COMBINED stereo section -- what builtin_profiles_data._body() and
    # RUMBLE's own AI prompts emit.
    assert crossfeed.can_crossfeed([ChannelSection(channel="L R")])
    assert crossfeed.can_crossfeed([ChannelSection(channel="R L")])
    assert crossfeed.can_crossfeed([ChannelSection(channel="l r")])
    # NO Channel: line at all -- APO applies such a profile to every channel,
    # so L/R are there. This is the shape of essentially every real headphone
    # profile (AutoEQ/convolution exports), and it used to be REJECTED, which
    # is why crossfeed stayed greyed out even after the "L R" fix.
    assert crossfeed.can_crossfeed([])
    # Multichannel layouts that contain L and R: crossfeed leaves C/LFE/
    # surrounds untouched, so there is no reason to withhold it.
    assert crossfeed.can_crossfeed([ChannelSection(channel="L R C LFE RL RR")])
    assert crossfeed.can_crossfeed([
        ChannelSection(channel="L"), ChannelSection(channel="C"),
        ChannelSection(channel="R"), ChannelSection(channel="LFE")])


def test_can_crossfeed_rejects_only_layouts_with_no_stereo_pair():
    # The genuine no-op cases: nothing to crossfeed because there is no L/R.
    assert not crossfeed.can_crossfeed([ChannelSection(channel="SUB")])
    assert not crossfeed.can_crossfeed([ChannelSection(channel="C")])
    assert not crossfeed.can_crossfeed([ChannelSection(channel="LFE")])
    assert not crossfeed.can_crossfeed(
        [ChannelSection(channel="L"), ChannelSection(channel="C")])  # L but no R


def test_every_builtin_playback_profile_can_take_crossfeed():
    """End-to-end guard: the gate is only meaningful if it passes the
    profiles users actually have active. Every shipped playback built-in must
    qualify -- if a future change to _body()'s channel shape (or to the gate)
    breaks that, crossfeed silently dies again."""
    from rumble.core.builtin_profiles_data import BUILTIN_PROFILES
    checked = 0
    for pid, entry in BUILTIN_PROFILES.items():
        if pid.startswith("rumble_voice_"):
            continue  # capture/mic built-ins: crossfeed is not for them
        config = apo_config.parse_config(entry["body"])
        assert crossfeed.can_crossfeed(config.channel_sections), (
            f"built-in {pid!r} (sections "
            f"{[s.channel for s in config.channel_sections]}) cannot take crossfeed")
        checked += 1
    assert checked == 7


def test_channelless_autoeq_convolution_profile_can_take_crossfeed():
    """The exact shape of a real imported headphone profile: Device + a
    Convolution IR + sample-rate conditionals, and NO Channel: line anywhere.
    This is what a user actually has active, and it must not be blocked."""
    body = (
        "Device: all\n"
        "Convolution: irs/headphone_48000.wav\n"
        "If: sampleRate == 44100\n"
        "  Convolution: irs/headphone_44100.wav\n"
        "EndIf:\n"
    )
    config = apo_config.parse_config(body)
    assert config.channel_sections == [], "precondition: no Channel: sections"
    assert crossfeed.can_crossfeed(config.channel_sections)


# ── ProfileManager wiring ────────────────────────────────────────────────

def test_no_spec_render_is_byte_identical():
    # Per-profile crossfeed: a profile with no CrossfeedSpec renders exactly
    # as it would with no crossfeed layer at all.
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        config = _stereo_config()
        without = apo_config.render_config_body(mgr._apply_render_layers(config))
        body = mgr._render_layered_body(config, None)
        assert body == without


def test_spec_stereo_config_carries_the_block():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        spec = crossfeed.CrossfeedSpec(preset="chumoy", strength=100)
        body = mgr._render_layered_body(_stereo_config(), spec)
        assert "Channel: XL XR" in body
        assert "Copy: XL=R" in body
        assert "Copy: L=L+XL R=R+XR" in body


def test_spec_zero_strength_carries_no_block():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        spec = crossfeed.CrossfeedSpec(preset="chumoy", strength=0)
        config = _stereo_config()
        without = apo_config.render_config_body(mgr._apply_render_layers(config))
        assert mgr._render_layered_body(config, spec) == without


def test_layout_without_a_stereo_pair_silently_skips():
    # The genuine no-op: no L/R anywhere, so there is nothing to crossfeed.
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        spec = crossfeed.CrossfeedSpec(preset="chumoy")
        config = ProfileConfig(preamp=0.0, channel_sections=[
            ChannelSection(channel="C"), ChannelSection(channel="LFE"),
        ])
        without = apo_config.render_config_body(mgr._apply_render_layers(config))
        assert mgr._render_layered_body(config, spec) == without


def test_multichannel_layout_with_l_and_r_carries_the_block():
    # A layout that HAS both ears gets crossfeed -- the block only ever
    # touches L/R and leaves C/LFE/surrounds alone, so there is no reason to
    # withhold it (deliberate widening, 2026-07-13).
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        spec = crossfeed.CrossfeedSpec(preset="chumoy")
        config = ProfileConfig(preamp=0.0, channel_sections=[
            ChannelSection(channel="L"), ChannelSection(channel="C"),
            ChannelSection(channel="R"), ChannelSection(channel="LFE"),
        ])
        body = mgr._render_layered_body(config, spec)
        assert "Channel: XL XR" in body
        assert "Copy: L=L+XL R=R+XR" in body


def test_channelless_profile_carries_the_block():
    # The real-world headphone case: no Channel: line at all (AutoEQ /
    # convolution export). This is what a user actually has active, and it
    # was silently skipped before 2026-07-13.
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        spec = crossfeed.CrossfeedSpec(preset="chumoy")
        config = ProfileConfig(preamp=0.0, channel_sections=[],
                               convolution="irs/headphone_48000.wav")
        body = mgr._render_layered_body(config, spec)
        assert "Channel: XL XR" in body
        assert "Copy: XL=R" in body
        assert "Copy: L=L+XL R=R+XR" in body
        # The profile's own convolution must survive alongside it.
        assert "Convolution: irs/headphone_48000.wav" in body


def test_custom_spec_ignores_leftover_zero_strength():
    # strength is irrelevant to "custom" -- it must still render even at 0.
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        spec = crossfeed.CrossfeedSpec(preset="custom", strength=0, custom_gain_db=-8.0)
        body = mgr._render_layered_body(_stereo_config(), spec)
        assert "Channel: XL XR" in body


def test_preview_config_carries_the_block():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        spec = crossfeed.CrossfeedSpec(preset="linkwitz")
        assert mgr.preview_config(_stereo_config(), spec) is True
        text = (mgr.rumble_dir / PREVIEW_FILENAME).read_text(encoding="utf-8")
        assert "Channel: XL XR" in text
        assert "Delay: 0.3 ms" in text


def test_per_profile_crossfeed_round_trips_and_activates():
    # End-to-end: a profile's own crossfeed persists to its file, reloads
    # from disk, and its block reaches the active render -- and switches off
    # when the active profile has none.
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        cfg = _stereo_config()
        plain = mgr.create_profile("Plain", config=cfg)
        spatial = mgr.create_profile("Spatial", config=cfg)
        spatial.crossfeed = crossfeed.CrossfeedSpec(preset="meier", strength=90)
        mgr._write_profile_file(spatial, cfg)

        reloaded = _manager(tmp)
        rs = reloaded.get_profile(spatial.id)
        assert rs.crossfeed == crossfeed.CrossfeedSpec(preset="meier", strength=90)

        reloaded.activate_profile(spatial.id)
        active = (reloaded.rumble_dir / "rumble_listener_active.txt").read_text(encoding="utf-8")
        assert "Channel: XL XR" in active and "Copy: L=L+XL R=R+XR" in active

        # Switching to a profile with no crossfeed drops the block.
        reloaded.activate_profile(plain.id)
        pointer = (reloaded.config_path / "rumble_active.txt").read_text(encoding="utf-8")
        assert "XL" not in pointer  # plain single-Include, no composed block


def _active_render(mgr) -> str:
    # Follow the active pointer to whatever actually carries the composed
    # body (the listener scratch when a render layer is active, else the
    # plain Include pointer itself), the same way the UI selftest does.
    pointer = (mgr.config_path / "rumble_active.txt").read_text(encoding="utf-8")
    if "rumble_listener_active.txt" in pointer:
        return (mgr.rumble_dir / "rumble_listener_active.txt").read_text(encoding="utf-8")
    return pointer


def test_crossfeed_mode_off_profile_global():
    # The machine-level crossfeed_mode decides which value the render layer
    # uses. Two stereo profiles -- one carrying its OWN spec (meier, no delay
    # line), one carrying none -- against a distinct GLOBAL value (linkwitz,
    # which emits a "Delay:" line) let us tell own-vs-global apart in the
    # rendered active pointer.
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _manager(tmp)
        mgr.ensure_first_run()
        cfg = _stereo_config()
        own = mgr.create_profile("Own", config=cfg)
        own.crossfeed = crossfeed.CrossfeedSpec(preset="meier", strength=90)
        mgr._write_profile_file(own, cfg)
        unset = mgr.create_profile("Unset", config=cfg)
        # Global value: linkwitz at full strength -- emits a Delay: line that
        # meier does not, so it is unmistakable in the render.
        mgr.settings["crossfeed_preset"] = "linkwitz"
        mgr.settings["crossfeed_strength"] = 100

        def render_of(profile_id: str) -> str:
            mgr.activate_profile(profile_id)
            return _active_render(mgr)

        # Off: no crossfeed for anyone.
        mgr.settings["crossfeed_mode"] = "off"
        assert "Channel: XL XR" not in render_of(own.id)
        assert "Channel: XL XR" not in render_of(unset.id)

        # Profile: own profile uses its own value (meier, no delay); the
        # un-set profile falls back to the global value (linkwitz, delay).
        mgr.settings["crossfeed_mode"] = "profile"
        own_render = render_of(own.id)
        assert "Channel: XL XR" in own_render and "Delay:" not in own_render
        unset_render = render_of(unset.id)
        assert "Channel: XL XR" in unset_render and "Delay: 0.3 ms" in unset_render

        # Global: the global value overrides EVERY profile, so even the one
        # with its own spec renders the global linkwitz (delay present).
        mgr.settings["crossfeed_mode"] = "global"
        own_global = render_of(own.id)
        assert "Channel: XL XR" in own_global and "Delay: 0.3 ms" in own_global
        assert "Delay: 0.3 ms" in render_of(unset.id)


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
