"""Selective "reset settings to defaults" — which settings a reset restores,
grouped so the user can pick what to clear rather than being handed one
all-or-nothing button.

Every key in DEFAULT_SETTINGS is classified here as either a member of exactly
one RESET_GROUP or as PROTECTED. That is enforced by
tests/selftest_settings_reset.py, so a newly added setting cannot quietly
become unresettable (or, worse, get swept into a reset that shouldn't touch
it) without someone deciding which it is.

PROTECTED keys are deliberately NOT restorable here. They are not preferences
-- they are ownership, session and one-shot-migration state, and "restoring"
them to their defaults would perform an action rather than clear a choice:
re-taking APO's config.txt from a user who handed it back, deactivating the
running profile, or re-running the legacy hearing migration over a Listener
Slot the user has since edited. See PROTECTED_KEYS for the per-key reason.
"""
from __future__ import annotations

from dataclasses import dataclass

from rumble.core.profile_manager import DEFAULT_SETTINGS


@dataclass(frozen=True)
class ResetGroup:
    """One user-selectable chunk of a settings reset."""
    key: str            # stable id, used by the UI and the caller
    label: str          # checkbox label
    description: str    # one line under the label: what goes back to default
    keys: tuple[str, ...]  # the DEFAULT_SETTINGS keys this group restores


RESET_GROUPS: tuple[ResetGroup, ...] = (
    ResetGroup(
        key="appearance",
        label="Appearance",
        description="Theme, interface size, graph height and the analyzer overlay.",
        keys=(
            "theme",
            "ui_double_size",
            "graph_y_range",
            "graph_double_height",
            "spectrum_enabled",
            "spectrum_mode",
            "spectrum_peak_hold",
            "spectrum_display",
            "spectrum_compare_front",
            "meter_mode",
            "meter_k_reference",
        ),
    ),
    ResetGroup(
        key="eq_defaults",
        label="EQ defaults",
        description="New-profile band count, auto pre-amp, sample rate and the "
                    "speaker-measurement import options.",
        keys=(
            "default_bands",
            "auto_preamp",
            "sample_rate",
            "sample_rate_mode",
            "spinorama_default_tilt",
            "spinorama_smoothing_oct",
            "spinorama_add_crossover",
            "spinorama_directivity_limiting",
            "adaptive_smoothing",
        ),
    ),
    ResetGroup(
        key="sound_layers",
        label="Sound layers",
        description="Crossfeed, ISO 226 loudness, profile loudness matching and "
                    "the listener-correction toggle. Your Ear Calibration "
                    "results are kept.",
        keys=(
            "crossfeed_enabled",
            "crossfeed_preset",
            "crossfeed_strength",
            "crossfeed_custom_gain_db",
            "crossfeed_custom_freq_hz",
            "crossfeed_custom_delay_us",
            "crossfeed_soundstage_blend",
            "crossfeed_soundstage_width",
            "iso226_loudness_enabled",
            "iso226_reference_phon",
            "profile_loudness_matching",
            "listener_correction_enabled",
            "default_listener_slot",
            "hearing_frequency_set",
        ),
    ),
    ResetGroup(
        key="startup_warnings",
        label="Startup & warnings",
        description="Start with Windows, start minimised, the safety warning, "
                    "the unsaved-changes prompt and which profiles are listed.",
        keys=(
            "start_with_windows",
            "start_minimised",
            "show_safety_warning",
            "warn_unsaved_changes",
            "show_builtin_profiles",
            "show_microphone_profiles",
        ),
    ),
    ResetGroup(
        key="apo",
        label="APO integration",
        description="Clears a custom config.txt location and un-dismisses the "
                    "import and coverage prompts, so they can offer again.",
        keys=(
            "config_path_override",
            "ignored_apo_sources",
            "apo_coverage_dismissed_guids",
        ),
    ),
    ResetGroup(
        key="automation",
        label="Auto-switch rules",
        description="Turns automatic switching off and DELETES every rule you "
                    "have written.",
        keys=(
            "auto_switch_enabled",
            "auto_switch_rules",
        ),
    ),
    ResetGroup(
        key="devices",
        label="Per-device assignments",
        description="Every endpoint goes back to following the active profile. "
                    "Your profiles are not touched.",
        keys=(
            "device_profile_assignments",
        ),
    ),
    ResetGroup(
        key="calibration",
        label="Volume calibration",
        description="Discards the reference volume measured for the ISO 226 "
                    "loudness layer, which stays flat until re-calibrated.",
        keys=(
            "iso226_reference_volume_db",
            "hearing_screening_last_seed",
        ),
    ),
)

# Keys a reset must never touch, and why. See this module's docstring.
PROTECTED_KEYS: dict[str, str] = {
    "apo_managed":
        "Whether RUMBLE owns config.txt. Resetting it to the default would "
        "silently re-take control from a user who handed it back to Equalizer "
        "APO -- an action, not a preference. Use APO Integration's own "
        "hand-back/resume control.",
    "last_active_profile":
        "Which profile is active. Resetting it would deactivate the running "
        "profile and change what the user is hearing.",
    "bypassed":
        "Live session state (the bypass button), not a stored preference.",
    "legacy_hearing_migrated":
        "A one-shot migration gate. Clearing it would re-copy legacy hearing "
        "history back into Listener Slot 1 over whatever the user has since "
        "made of that slot.",
}


def group_for_key(key: str) -> ResetGroup | None:
    """The group that resets `key`, or None if it is protected/unclassified."""
    for group in RESET_GROUPS:
        if key in group.keys:
            return group
    return None


def reset_settings(settings: dict, group_keys) -> list[str]:
    """Restore the defaults for the named groups, in place.

    `settings` is mutated; `group_keys` is an iterable of ResetGroup.key
    values (unknown ones are ignored, so a stale caller cannot raise). Returns
    the sorted list of setting keys whose value actually changed -- callers use
    it to decide what needs re-applying live, so a no-op reset stays a no-op.

    Protected keys are never written, even if a caller names a group. That
    cannot currently happen (no group lists one, and the test suite enforces
    it) but the guard is cheap and keeps the invariant local to the function
    that does the writing.
    """
    wanted = set(group_keys)
    changed: list[str] = []
    for group in RESET_GROUPS:
        if group.key not in wanted:
            continue
        for key in group.keys:
            if key in PROTECTED_KEYS:
                continue
            default = DEFAULT_SETTINGS[key]
            if settings.get(key) == default:
                continue
            # Containers are copied: DEFAULT_SETTINGS holds one shared list/
            # dict per key, and handing that object out would let a later edit
            # of the live settings mutate the defaults themselves.
            settings[key] = (type(default)(default)
                             if isinstance(default, (list, dict)) else default)
            changed.append(key)
    return sorted(changed)
