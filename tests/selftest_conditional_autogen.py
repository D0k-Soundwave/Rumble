"""Self-test: rumble.core.auto_switch.generate_native_conditional (Rumble 4.4).

Translates device-triggered auto-switch rules into a native Equalizer APO
If/ElseIf/Else/EndIf block, as an export alternative to the existing
Include-pointer RuleEngine mechanism (docs/PLANNED_WORK.md #1). App/time
rules have no native runtime-variable equivalent (APO's expression language
has no "currently playing process" or wall-clock variable) and are reported
as untranslatable rather than dropped.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core import apo_config
from rumble.core.audio_devices import ApoDevice
from rumble.core.auto_switch import Rule, generate_native_conditional


def _profile(fc: float = 1000.0, gain: float = -3.0) -> apo_config.ProfileConfig:
    cfg = apo_config.ProfileConfig(device="all", preamp=-3.0)
    section = apo_config.ChannelSection(channel="all")
    section.filters.append(apo_config.Filter(enabled=True, type="PK", fc=fc, gain=gain, q=1.0))
    cfg.channel_sections.append(section)
    return cfg


def test_device_rule_translates_to_if_block():
    devices = [ApoDevice(guid="{a}", device_name="Speakers", connection_name="")]
    rules = [Rule("device", "Speakers", "prof-a")]
    profiles = {"prof-a": _profile()}
    result = generate_native_conditional(rules, devices, profiles.get)
    assert result.translated == rules
    assert result.untranslatable == []
    assert result.text.startswith('If: deviceName == "Speakers"')
    assert result.text.strip().endswith("EndIf:")


def test_device_rule_with_connection_name_uses_and():
    devices = [ApoDevice(guid="{b}", device_name="Speakers", connection_name="FIIO K13 R2R")]
    rules = [Rule("device", "Speakers FIIO K13 R2R", "prof-a")]
    profiles = {"prof-a": _profile()}
    result = generate_native_conditional(rules, devices, profiles.get)
    assert 'deviceName == "Speakers"' in result.text
    assert 'connectionName == "FIIO K13 R2R"' in result.text
    assert " and " in result.text


def test_app_and_time_rules_are_untranslatable():
    rules = [
        Rule("app", "spotify.exe", "prof-a"),
        Rule("time", "22:00-07:00", "prof-a"),
    ]
    profiles = {"prof-a": _profile()}
    result = generate_native_conditional(rules, [], profiles.get)
    assert result.text == ""
    assert result.untranslatable == rules
    assert result.translated == []


def test_unknown_device_pattern_is_untranslatable():
    # No matching ApoDevice for this pattern (e.g. a stale rule for a
    # since-unplugged endpoint) -- can't safely translate without the real
    # device_name/connection_name split, so report it back rather than guess.
    rules = [Rule("device", "Unplugged Thing", "prof-a")]
    profiles = {"prof-a": _profile()}
    result = generate_native_conditional(rules, [], profiles.get)
    assert result.translated == []
    assert result.untranslatable == rules


def test_multiple_device_rules_produce_if_elseif_else():
    devices = [
        ApoDevice(guid="{a}", device_name="Speakers", connection_name=""),
        ApoDevice(guid="{b}", device_name="Headphones", connection_name=""),
    ]
    rules = [
        Rule("device", "Speakers", "prof-speakers"),
        Rule("device", "Headphones", "prof-headphones"),
    ]
    profiles = {"prof-speakers": _profile(fc=100.0), "prof-headphones": _profile(fc=200.0)}
    result = generate_native_conditional(rules, devices, profiles.get,
                                          baseline_profile_id="prof-speakers")
    top_lines = result.text.splitlines()
    assert sum(1 for ln in top_lines if ln.startswith("If: ")) == 1
    assert sum(1 for ln in top_lines if ln.startswith("ElseIf: ")) == 1
    assert "Else:" in top_lines
    assert result.text.strip().endswith("EndIf:")


def test_no_baseline_omits_else_branch():
    devices = [ApoDevice(guid="{a}", device_name="Speakers", connection_name="")]
    rules = [Rule("device", "Speakers", "prof-a")]
    profiles = {"prof-a": _profile()}
    result = generate_native_conditional(rules, devices, profiles.get)
    assert "Else:" not in result.text


def test_disabled_rule_is_skipped_entirely():
    devices = [ApoDevice(guid="{a}", device_name="Speakers", connection_name="")]
    rules = [Rule("device", "Speakers", "prof-a", enabled=False)]
    profiles = {"prof-a": _profile()}
    result = generate_native_conditional(rules, devices, profiles.get)
    assert result.translated == []
    assert result.untranslatable == []
    assert result.text == ""


def test_unquotable_device_name_is_untranslatable():
    devices = [ApoDevice(guid="{a}", device_name='Weird "Quoted" Name', connection_name="")]
    rules = [Rule("device", 'Weird "Quoted" Name', "prof-a")]
    profiles = {"prof-a": _profile()}
    result = generate_native_conditional(rules, devices, profiles.get)
    assert result.translated == []
    assert result.untranslatable == rules


def test_generated_block_round_trips_through_apo_config():
    devices = [ApoDevice(guid="{a}", device_name="Speakers", connection_name="")]
    rules = [Rule("device", "Speakers", "prof-a")]
    profiles = {"prof-a": _profile()}
    result = generate_native_conditional(rules, devices, profiles.get)

    full_text = f"Device: Test\n\n{result.text}\n"
    parsed = apo_config.parse_config(full_text)
    assert parsed.conditional_blocks == [result.text]

    rendered = apo_config.render_config_body(parsed)
    reparsed = apo_config.parse_config(rendered)
    assert reparsed.conditional_blocks == [result.text]


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
