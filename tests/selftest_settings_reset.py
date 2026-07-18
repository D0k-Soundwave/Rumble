"""Self-test for selective settings reset (rumble/core/settings_reset.py).

The load-bearing check is exhaustiveness: every DEFAULT_SETTINGS key must be
either in exactly one reset group or explicitly protected. That way adding a
setting forces a decision instead of letting it silently fall out of reset --
or silently get swept into one that shouldn't touch it.

Also checks that a reset restores only the groups asked for, reports what it
actually changed, never writes protected keys, and hands out copies of
container defaults rather than the shared DEFAULT_SETTINGS objects.

Run:  python tests/selftest_settings_reset.py"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core.profile_manager import DEFAULT_SETTINGS
from rumble.core.settings_reset import (
    PROTECTED_KEYS, RESET_GROUPS, group_for_key, reset_settings,
)

FAILURES: list[str] = []


def step(label: str, fn) -> None:
    try:
        fn()
        print(f"  PASS  {label}")
    except Exception:
        FAILURES.append(label)
        print(f"  FAIL  {label}\n{traceback.format_exc()}")


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def every_key_classified() -> None:
    grouped = [k for g in RESET_GROUPS for k in g.keys]
    covered = set(grouped) | set(PROTECTED_KEYS)
    missing = sorted(set(DEFAULT_SETTINGS) - covered)
    expect(not missing,
           "every DEFAULT_SETTINGS key must be in a reset group or PROTECTED_KEYS; "
           f"unclassified: {missing}. Add it to rumble/core/settings_reset.py.")


def no_unknown_keys() -> None:
    known = set(DEFAULT_SETTINGS)
    stray = sorted((set(k for g in RESET_GROUPS for k in g.keys) | set(PROTECTED_KEYS)) - known)
    expect(not stray,
           f"reset groups/PROTECTED_KEYS name keys that are not settings: {stray}")


def no_key_in_two_groups() -> None:
    seen: dict[str, str] = {}
    for group in RESET_GROUPS:
        for key in group.keys:
            expect(key not in seen,
                   f"'{key}' is in both '{seen.get(key)}' and '{group.key}' -- "
                   "a key must belong to exactly one group")
            seen[key] = group.key


def protected_never_grouped() -> None:
    for key in PROTECTED_KEYS:
        g = group_for_key(key)
        expect(g is None,
               f"protected key '{key}' must not be resettable, but group "
               f"'{g.key if g else ''}' lists it")


def group_keys_unique() -> None:
    keys = [g.key for g in RESET_GROUPS]
    expect(len(keys) == len(set(keys)), f"duplicate group ids: {keys}")
    for g in RESET_GROUPS:
        expect(g.label and g.description and g.keys,
               f"group '{g.key}' needs a label, a description and at least one key")


def resets_only_requested_group() -> None:
    settings = dict(DEFAULT_SETTINGS)
    settings["theme"] = "Panther"          # appearance
    settings["default_bands"] = 31         # eq_defaults
    changed = reset_settings(settings, ["appearance"])
    expect(changed == ["theme"], f"only theme should have changed, got {changed}")
    expect(settings["theme"] == DEFAULT_SETTINGS["theme"], "theme should be back to default")
    expect(settings["default_bands"] == 31,
           "an unrequested group must be left completely alone")


def reports_only_real_changes() -> None:
    settings = dict(DEFAULT_SETTINGS)
    expect(reset_settings(settings, ["appearance"]) == [],
           "resetting already-default values must report no changes")
    settings["ui_double_size"] = True
    expect(reset_settings(settings, ["appearance"]) == ["ui_double_size"],
           "only the key that actually differed should be reported")


def resets_multiple_groups() -> None:
    settings = dict(DEFAULT_SETTINGS)
    settings["theme"] = "Prime"
    settings["auto_switch_rules"] = [{"profile_id": "x"}]
    settings["auto_switch_enabled"] = True
    changed = reset_settings(settings, ["appearance", "automation"])
    expect(changed == ["auto_switch_enabled", "auto_switch_rules", "theme"],
           f"unexpected change set: {changed}")
    expect(settings["auto_switch_rules"] == [], "rules should be cleared")


def unknown_group_ignored() -> None:
    settings = dict(DEFAULT_SETTINGS)
    settings["theme"] = "Sage"
    expect(reset_settings(settings, ["nope"]) == [], "an unknown group id must be a no-op")
    expect(settings["theme"] == "Sage", "an unknown group must not touch anything")
    expect(reset_settings(settings, []) == [], "resetting no groups must be a no-op")


def protected_keys_survive_every_group() -> None:
    settings = dict(DEFAULT_SETTINGS)
    sentinels = {
        "apo_managed": False,
        "last_active_profile": "my_profile",
        "bypassed": True,
        "legacy_hearing_migrated": True,
    }
    expect(set(sentinels) == set(PROTECTED_KEYS),
           "this check must cover every protected key; update it alongside "
           f"PROTECTED_KEYS (missing: {sorted(set(PROTECTED_KEYS) - set(sentinels))})")
    settings.update(sentinels)
    reset_settings(settings, [g.key for g in RESET_GROUPS])
    for key, value in sentinels.items():
        expect(settings[key] == value,
               f"protected key '{key}' was changed by a full reset "
               f"({value!r} -> {settings[key]!r}) -- {PROTECTED_KEYS[key]}")


def containers_are_copied() -> None:
    settings = dict(DEFAULT_SETTINGS)
    settings["auto_switch_rules"] = [{"profile_id": "x"}]
    settings["device_profile_assignments"] = {"guid": "p"}
    reset_settings(settings, ["automation", "devices"])
    expect(settings["auto_switch_rules"] is not DEFAULT_SETTINGS["auto_switch_rules"],
           "reset handed out the shared DEFAULT_SETTINGS list object")
    expect(settings["device_profile_assignments"] is not DEFAULT_SETTINGS["device_profile_assignments"],
           "reset handed out the shared DEFAULT_SETTINGS dict object")
    # Mutating the reset value must not corrupt the defaults for the next reset.
    settings["auto_switch_rules"].append({"profile_id": "y"})
    settings["device_profile_assignments"]["g2"] = "p2"
    expect(DEFAULT_SETTINGS["auto_switch_rules"] == [],
           "DEFAULT_SETTINGS['auto_switch_rules'] was mutated through a reset value")
    expect(DEFAULT_SETTINGS["device_profile_assignments"] == {},
           "DEFAULT_SETTINGS['device_profile_assignments'] was mutated through a reset value")


def full_reset_matches_defaults() -> None:
    settings = dict(DEFAULT_SETTINGS)
    for key in settings:
        if key in PROTECTED_KEYS:
            continue
        value = settings[key]
        # Perturb every resettable key away from its default.
        if isinstance(value, bool):
            settings[key] = not value
        elif isinstance(value, (int, float)):
            settings[key] = value + 7
        elif isinstance(value, str):
            settings[key] = value + "_changed"
        elif isinstance(value, list):
            settings[key] = ["changed"]
        elif isinstance(value, dict):
            settings[key] = {"changed": "changed"}
        else:  # None-valued keys
            settings[key] = "changed"
    reset_settings(settings, [g.key for g in RESET_GROUPS])
    for key in DEFAULT_SETTINGS:
        if key in PROTECTED_KEYS:
            continue
        expect(settings[key] == DEFAULT_SETTINGS[key],
               f"'{key}' did not come back to its default after a full reset "
               f"({settings[key]!r} != {DEFAULT_SETTINGS[key]!r})")


def main() -> int:
    step("every setting is classified (grouped or protected)", every_key_classified)
    step("no group/protected entry names a non-setting", no_unknown_keys)
    step("no key appears in two groups", no_key_in_two_groups)
    step("protected keys are never in a group", protected_never_grouped)
    step("group ids are unique and fully described", group_keys_unique)
    step("resets only the requested group", resets_only_requested_group)
    step("reports only real changes", reports_only_real_changes)
    step("resets several groups at once", resets_multiple_groups)
    step("unknown/empty group selection is a no-op", unknown_group_ignored)
    step("protected keys survive a full reset", protected_keys_survive_every_group)
    step("container defaults are copied, not shared", containers_are_copied)
    step("a full reset restores every resettable key", full_reset_matches_defaults)

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
