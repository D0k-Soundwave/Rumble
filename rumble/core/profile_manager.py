"""Profile CRUD, metadata persistence, activation/bypass.

Profiles are stored as .txt files in `<config_path>/rumble/defaults/` (builtin)
and `<config_path>/rumble/user/` (user-created). All profile metadata is
embedded in file headers as comments; files are parsed fresh on every scan.

`data/profiles.json` stores only app settings and the active profile ID.
Profile discovery is live: folders are scanned at startup, so added/removed/
updated files are immediately reflected in the loaded profile list.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from rumble.core import abx, apo_config, apo_path, audio_devices, device_format, ir_analysis, profile_file
from rumble.core import crossfeed, foreign_tool_detection, listener_correction, listener_slots
from rumble.core import loudness_layer, master_volume
from rumble.core import peace_import
from rumble.core import autoeq_merge, eq_target, rew_import
from rumble.core import tone_trim
from rumble.core import vst_registry
from rumble.core.builtin_profiles_data import BUILTIN_PROFILES
from rumble.core.apo_config import ProfileConfig
from rumble.core.profile_file import ProfileDocument
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME

RUMBLE_SUBDIR = "rumble"
IR_SUBDIR = "irs"
#: Deleted user profiles live here (under data_dir, not the APO rumble/ tree)
#: as <utc-timestamp>_<id>.txt + a sidecar .json until restored or purged.
#: See delete_profile() / list_trashed().
TRASH_SUBDIR = "trash"
#: A recycle bin keeps at most this many entries; trashing a 26th purges the
#: oldest (same cap-on-write pattern as listener_slots.MAX_SAVED_RUNS).
MAX_TRASHED = 25
BACKUP_FORMAT_VERSION = 2  # full data/ + rumble/ tree

# Zip-bomb guard for every member we decompress from an untrusted archive (a
# hand-crafted/tampered backup or profile bundle): each member is read as a
# stream up to a hard byte ceiling, never trusting the zip's own (spoofable)
# declared size. Two ceilings, since IR audio and small text/JSON have very
# different legitimate sizes:
#
# Text/JSON members (profile .txt, profiles.json, custom_themes.json, hearing
# test results) -- there's no single physical unit to derive this from, but
# no real file RUMBLE writes gets anywhere near a low-single-digit-MB profile
# text file even at MAX_BANDS (apo_config.MAX_BANDS = 32) with verbatim/eval/
# copy/VST-chunk lines included; this is deliberate headroom, not a tight
# bound.
_MAX_TEXT_MEMBER_BYTES_REQUIRED = 4 * 1024 * 1024  # 4 MiB
_MAX_TEXT_MEMBER_BYTES = 2 * _MAX_TEXT_MEMBER_BYTES_REQUIRED

# IR (.wav) members -- precisely derived from RUMBLE's own established
# ceilings: ir_analysis.read_wav never uses more than _MAX_IR_SECONDS of
# audio, at up to the highest sample rate RUMBLE offers (settings_dialog.py's
# 192 kHz option), the widest channel layout RUMBLE models (editor_panel.py's
# CHANNEL_PRESETS "7.1 Surround" = 8 channels), at the widest sample width
# read_wav supports (64-bit float, 8 bytes/sample) -- anything past that is
# padding no IR this app can use actually needs.
_MAX_IR_SECONDS_REQUIRED = ir_analysis._MAX_IR_SECONDS  # 10.0
_MAX_IR_SAMPLE_RATE = 192_000       # settings_dialog.py's sample-rate ceiling
_MAX_IR_CHANNELS = 8                # editor_panel.CHANNEL_PRESETS' widest layout (7.1)
_MAX_IR_BYTES_PER_SAMPLE = 8         # read_wav's widest supported width (64-bit float)
_MAX_IR_MEMBER_BYTES_REQUIRED = int(
    _MAX_IR_SECONDS_REQUIRED * _MAX_IR_SAMPLE_RATE
    * _MAX_IR_CHANNELS * _MAX_IR_BYTES_PER_SAMPLE
)  # ~117 MiB
_MAX_IR_MEMBER_BYTES = 2 * _MAX_IR_MEMBER_BYTES_REQUIRED
ACTIVE_POINTER_FILENAME = "rumble_active.txt"
PREVIEW_FILENAME = "rumble_preview.txt"
#: Scratch file for the composed (listener-correction and/or ISO 226
#: loudness-layer) canonical output -- same pattern as PREVIEW_FILENAME, but
#: for _write_active_pointer() rather than preview_config(). See
#: _write_active_pointer()'s _apply_render_layers() branch. Kept its
#: original "listener" name (Session 9.3) rather than renaming when the
#: loudness layer (Rumble 3.5) started sharing it -- it's an internal
#: scratch filename, not a user-facing concept.
LISTENER_ACTIVE_FILENAME = "rumble_listener_active.txt"
#: Per-device composed scratch files (D1a dispatch), one per assignment --
#: rumble_device_<8-char-guid-hash>.txt under rumble_dir, same "temporary,
#: safe to delete" category as LISTENER_ACTIVE_FILENAME/PREVIEW_FILENAME.
#: See _device_scratch_filename()/_render_dispatch()/_cleanup_stale_device_scratch().
DEVICE_SCRATCH_PREFIX = "rumble_device_"
CONFIG_FILENAME = "config.txt"
CONFIG_BACKUP_SUFFIX = ".rumble_backup"
# Later displacements (config.txt clobbered after RUMBLE owned it, or a foreign
# tool wrote alongside RUMBLE's block) are preserved here, timestamped, so the
# sacred first backup is never touched and declining the import never loses them.
CONFIG_DRIFT_SUFFIX = ".rumble_drift"

# Marker that identifies a config.txt RUMBLE owns. Its presence is the
# source of truth for "we own this file" — more robust than substring-matching
# the include, and the version lets future releases migrate the format.
MANAGED_MARKER = "# RUMBLE-Managed:"
MANAGED_CONFIG_VERSION = 1

# RUMBLE's own files in rumble/ root that the auto-discovery scan must never
# mistake for a user profile. LISTENER_ACTIVE_FILENAME added alongside
# DEVICE_SCRATCH_PREFIX in D1a -- both are render scratch, never user
# content, and _load_profile_from_file() has no header-based rejection that
# would otherwise skip them (see _scan_profile_folders()).
_ROOT_SCAN_EXCLUDE: frozenset[str] = frozenset({PREVIEW_FILENAME, LISTENER_ACTIVE_FILENAME})

# Equalizer APO's own bundled sample/demo configs — installer boilerplate, never
# imported as profiles (RUMBLE ships its own built-ins). Matched by file stem.
_APO_SAMPLE_STEMS: frozenset[str] = frozenset({
    "demo", "example", "iir_lowpass", "multichannel", "selective_delay",
})


def _strip_rumble_includes(text: str) -> str:
    """Drop RUMBLE's own `Include:` pointer lines, leaving the foreign config —
    handles a config RUMBLE had only *prepended* its include to."""
    out: list[str] = []
    for raw in text.splitlines():
        target = apo_config.include_target(raw)
        if target is not None:
            low = target.lower()
            if low in (ACTIVE_POINTER_FILENAME, PREVIEW_FILENAME) or low.startswith(f"{RUMBLE_SUBDIR}/"):
                continue
        out.append(raw)
    return "\n".join(out).strip("\n")


def _foreign_includes(text: str) -> list[str]:
    """`Include:` targets that point outside RUMBLE — e.g. another EQ tool's file
    (Peace's `peace.txt`). Used to warn about tools co-writing the APO config."""
    found: list[str] = []
    for raw in text.splitlines():
        target = apo_config.include_target(raw)
        if target is None:
            continue
        low = target.lower()
        if low in (ACTIVE_POINTER_FILENAME, PREVIEW_FILENAME) or low.startswith(f"{RUMBLE_SUBDIR}/"):
            continue
        found.append(target)
    return found


def _foreign_raw_directives(text: str) -> list[str]:
    """Foreign config.txt lines that are actual APO directives written
    directly (not via an `Include:` pointer) alongside or instead of
    RUMBLE's managed block — e.g. another tool, or the user, editing
    config.txt by hand. Excludes Include: lines, which `_foreign_includes()`
    already surfaces separately."""
    out: list[str] = []
    for raw in _foreign_config_body(text).splitlines():
        if apo_config.include_target(raw) is None:
            out.append(raw.strip())
    return out


def _foreign_config_body(text: str) -> str:
    """The active APO directives in config.txt that RUMBLE does NOT own — content
    a foreign tool or the user placed there, ignoring comments (incl. RUMBLE's own
    marker block) and RUMBLE's own ``Include:`` pointers.

    Non-empty ⇒ someone is co-writing config.txt alongside RUMBLE's block, so even
    a marker-present file needs re-asserting to keep RUMBLE the sole authority."""
    out: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        target = apo_config.include_target(raw)
        if target is not None:
            low = target.lower()
            if low in (ACTIVE_POINTER_FILENAME, PREVIEW_FILENAME) or low.startswith(f"{RUMBLE_SUBDIR}/"):
                continue
        out.append(raw)
    return "\n".join(out).strip("\n")


def _config_hash(config: ProfileConfig) -> str:
    """Stable short hash of a config's *effective* command body, so cosmetic
    whitespace/comment differences don't read as real changes."""
    body = apo_config.render_config_body(config)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _text_hash(text: str) -> str:
    return _config_hash(apo_config.parse_config(text))


def _graphic_eq_is_flat(geq: str) -> bool:
    """True when a `GraphicEQ` string is all (near-)zero gains — no shaping."""
    for point in geq.split(";"):
        parts = point.split()
        if len(parts) >= 2:
            try:
                if abs(float(parts[-1])) > 0.01:
                    return False
            except ValueError:
                continue
    return True


def _has_real_eq(config: ProfileConfig) -> bool:
    """True when a parsed config carries audible *shaping* worth importing.

    A lone Preamp (a level offset — "the cut") or an all-zero GraphicEQ do not
    count, matching the user's rule that APO's "test the tool" setup is ignored.
    """
    if config.convolution or apo_config.conditional_convolution_paths(config):
        return True
    for section in config.channel_sections:
        if section.filters:
            return True
        if section.graphic_eq and not _graphic_eq_is_flat(section.graphic_eq):
            return True
    return False


@dataclass
class ApoImportCandidate:
    """An existing Equalizer APO config offered for import as a RUMBLE profile."""
    source: str        # path relative to the config dir (or CONFIG_FILENAME for the swapped chain)
    name: str          # suggested profile name
    text: str          # effective (flattened) APO text to import
    source_hash: str   # hash of the source's effective content


@dataclass
class DriftSnapshot:
    """A preserved `config.txt.rumble_drift-<ts>` file — the content displaced
    when RUMBLE re-asserted ownership after config.txt was externally clobbered
    or co-written by a foreign tool. See ensure_first_run()."""
    path: Path          # the snapshot file on disk
    timestamp: datetime  # parsed from the filename suffix
    label: str           # human-readable timestamp for display


@dataclass
class TrashedProfile:
    """A deleted user profile preserved in `data_dir/trash/` (see
    delete_profile()) as a `<utc-timestamp>_<id>.txt` + sidecar `.json` pair
    until restored or purged. Never created for built-ins — delete_profile()
    refuses those outright (ProfileMeta.protected)."""
    id: str              # the profile's id at the time it was trashed
    name: str
    description: str
    tags: list[str]
    locked: bool          # meta.readonly at the time it was trashed
    original_file: str    # meta.file at the time it was trashed (info only)
    deleted_at: datetime  # parsed from the sidecar's "deleted" field (UTC)
    label: str            # human-readable timestamp for display
    txt_path: Path        # the preserved profile body
    json_path: Path       # the sidecar metadata file


@dataclass
class EffectiveConfigReport:
    """The fully-resolved, traceable Equalizer APO config chain currently
    applying — read-only, for the config-health/effective-chain viewer
    (docs/PLANNED_WORK.md #3)."""
    resolved_text: str             # config.txt with every Include: chain inlined, in application order
    sources: list[str]             # contributing files, in order of first appearance
    conditional_block_count: int   # If/ElseIf/Else/EndIf blocks found — raw/unevaluated, since APO
                                    # itself evaluates these at runtime, not RUMBLE

DEFAULT_SETTINGS: dict = {
    "config_path_override": None,
    "default_bands": 10,
    "auto_preamp": True,
    "start_with_windows": True,
    "start_minimised": True,
    "theme": DEFAULT_THEME_NAME,
    "ui_double_size": False,
    "graph_y_range": 30,
    # Grows the window so the extra height goes entirely to the frequency
    # graph (see window_fixed_size()) -- roughly doubling its vertical space.
    "graph_double_height": False,
    # Processing sample rate (Hz) used for response/preamp maths and for
    # displaying sample-denominated delays in ms. Global — the physical
    # playback rate is a device/system property, not a per-profile one.
    "sample_rate": 48000,
    # "auto" (default): effective_sample_rate() detects the current default
    # playback device's WASAPI mix format and uses that, falling back to the
    # manual "sample_rate" value above when detection fails (no device, COM
    # error, offscreen/CI). "manual": always use "sample_rate" verbatim.
    # GOTCHA: because "auto" is the default, every freshly-constructed
    # ProfileManager/MainWindow queries the REAL host device via COM and may
    # adopt whatever sample rate that machine reports (not this 48000 value).
    # Any test asserting an exact sample-rate-derived number (delay ms/samples,
    # filter coefficients, etc.) must pin sample_rate_mode="manual" + a fixed
    # sample_rate to stay deterministic across machines.
    "sample_rate_mode": "auto",
    "bypassed": False,
    "show_safety_warning": True,
    "last_active_profile": None,
    # When False the curated built-in profiles are not generated/shown (the user
    # prefers only their own). See ensure_builtin_profiles().
    "show_builtin_profiles": True,
    # When False capture-only (microphone) profiles are hidden from the library
    # and tray list. Display-only — the files stay on disk and any per-device
    # capture assignment still works. See ProfileMeta.capture_only.
    # Defaults OFF: RUMBLE is an output/playback EQ for the overwhelming
    # majority of users, and EQ'ing a capture endpoint requires APO to be
    # hooked on a mic (a niche setup). Opt in from Settings when you want it.
    "show_microphone_profiles": False,
    # When False RUMBLE has handed config.txt back to Equalizer APO and will
    # not re-take ownership on launch until the user resumes. See ensure_first_run.
    "apo_managed": True,
    # Source paths (relative to the APO config dir) the user declined to import,
    # so the boot reconcile doesn't re-offer them every launch.
    "ignored_apo_sources": [],
    # Default speaker target tilt (dB/octave) for Spinorama corrections: 0 = flat
    # "anechoic", a mild negative ≈ room-preference for users who won't measure.
    "spinorama_default_tilt": -0.9,
    "spinorama_smoothing_oct": 1 / 3,
    # When True, importing a single speaker measurement adds a subsonic
    # high-pass filter near its published low-frequency extension.
    "spinorama_add_crossover": False,
    # When True, a speaker's published directivity estimates damp its EQ
    # boost cap when off-axis control is poor. See eq_target.directivity_boost_cap().
    "spinorama_directivity_limiting": True,
    # When True, auto-generated speaker corrections use frequency-dependent
    # smoothing and taper boost above ~8 kHz toward zero (AutoEQ's own
    # published treble convention -- measurement-rig/room variance dominates
    # real device differences up there). Never applied to manual GEQ/PEQ
    # editing. See eq_target.octave_smooth_adaptive() / _hf_guardrail_gain().
    "adaptive_smoothing": True,
    # Phase 3 automatic profile switching (rumble/core/auto_switch.py): master
    # on/off and the ordered rule list (list of Rule.to_dict() dicts;
    # priority = list order, first match wins).
    "auto_switch_enabled": False,
    "auto_switch_rules": [],
    # Phase 6 live spectrum analyzer (rumble/core/spectrum.py +
    # rumble/ui/spectrum_overlay.py + rumble/ui/eq_graph.py): persisted overlay
    # state.
    # Off by default. It *should* be on-by-default (the live spectrum is meant
    # to be visible as you edit), but the WASAPI loopback capture that backs it
    # conflicts with Qt's STA main thread on at least one machine — a constant
    # RPC_E_CANTCALLOUT_ININPUTSYNCCALL (0x8001010d) fail/reconnect loop that
    # destabilises the app's COM and stops profiles applying. Until the capture
    # is made robust against that, the analyzer stays opt-in via the Live button
    # so a shaky capture can never take the rest of the app down with it.
    "spectrum_enabled": False,
    # "output" = the captured post-EQ signal; "source" = the estimated pre-EQ
    # input (capture minus the EQ's own response); "both" = overlay the two for
    # live before/after comparison. ("source" kept as the on-disk value for the
    # input series so profiles saved before the "both" option still load.)
    "spectrum_mode": "output",  # "output" | "source" (input) | "both"
    "spectrum_peak_hold": False,
    # Retired: the in-graph "bars" display was replaced by the dedicated
    # right-side VU/Peak meter panel (see "meter_mode" below). No longer read
    # anywhere -- kept only so an old settings.json's stray value is silently
    # ignored rather than dropped.
    "spectrum_display": "waveform",  # "waveform" (continuous trace) | "bars" (unused)
    # In "both" mode, which series draws on top; the other sits behind at reduced
    # opacity. "output" | "input".
    "spectrum_compare_front": "output",
    # "vu" | "peak" | "ref" -- the right-side stereo level-meter panel's
    # ballistics mode. Visibility/activity follows spectrum_enabled (the
    # Analyser toggle), not a separate on/off key.
    "meter_mode": "vu",
    # "k12" | "k14" | "k20" -- which Bob Katz K-System reference level "ref"
    # mode marks on the scale. Only meaningful when meter_mode == "ref".
    "meter_k_reference": "k14",
    # Phase 7 hearing test suite (rumble/core/hearing_test.py +
    # rumble/core/digit_screening.py + rumble/ui/hearing_dialog.py): Test A's
    # frequency-set toggle ("standard" | "coarse", see STANDARD_FREQS /
    # COARSE_FREQS) and a settings slot reserved for Test B's RNG seed.
    # hearing_screening_last_seed is int or None, NOT used for anything
    # security-sensitive -- the intent is a future retest-reuse-same-triplets
    # feature (None = "randomize every run"), but no production code reads or
    # writes this key yet (DigitScreeningTest's rng seam is currently test-only,
    # see hearing_dialog.py); it round-trips through settings/backup only.
    "hearing_frequency_set": "standard",
    "hearing_screening_last_seed": None,
    # Phase 8 Listener Slots (rumble/core/listener_slots.py +
    # listener_correction.py): which of the 4 fixed slots RUMBLE starts
    # with on every launch -- a mid-session switch via the header/tray
    # dropdown is deliberately NOT sticky across restarts (see
    # docs/superpowers/specs/2026-07-09-listener-correction-design.md §6).
    "default_listener_slot": 1,
    # When False the active Listener Slot's Ear Calibration correction is NOT
    # composed into renders -- results stay saved, the listener just hears
    # profiles uncorrected until re-enabled. Gives the slot-derived correction
    # (which is otherwise "on whenever the slot has data") the same explicit
    # off-switch the ISO 226 loudness layer has always had.
    "listener_correction_enabled": True,
    # Gates the existing _confirm_discard_changes() prompt in main_window.py
    # (switching/activating a different profile with unsaved editor changes).
    # Default ON preserves existing behavior; OFF is an explicit opt-out.
    "warn_unsaved_changes": True,
    # One-time migration gate: True once any pre-existing global Test A
    # history (data_dir/hearing/frequency/) has been copied into Listener
    # Slot 1 (see listener_slots.migrate_legacy_hearing_history()). Prevents
    # re-migrating on every launch once a user has since edited/deleted
    # Slot 1's own history.
    "legacy_hearing_migrated": False,
    # Rumble 3.5 (rumble/core/iso226.py + loudness_layer.py): a real ISO
    # 226:2003 equal-loudness-contour-derived compensation, volume-linked to
    # the live WASAPI master volume -- independent layer from Listener Slots
    # above (see ProfileManager._apply_render_layers()). Off by default: it
    # needs the reference volume calibrated below before it does anything
    # meaningful, and an uncalibrated correction would be silently wrong.
    "iso226_loudness_enabled": False,
    # The loudness level (phon) the user's content is assumed to sound
    # tonally flat at -- 80 phon is one of ISO 226:2003's own headline
    # illustrated contours (20/40/60/80/100 phon, see the standard's own
    # published comparison figure), used here as a documented "typical
    # critical-listening reference level" default, not an arbitrary guess.
    "iso226_reference_phon": 80.0,
    # WASAPI master volume (dB) captured at the moment the user last ran
    # ProfileManager.calibrate_loudness_reference() -- "this is my normal
    # listening volume, matching iso226_reference_phon above". None until
    # calibrated at least once, which keeps the layer flat (see
    # loudness_layer.derive_loudness_correction()).
    "iso226_reference_volume_db": None,
    # A1 (docs/superpowers/plans/2026-07-11-adaptive-frontend-roadmap.md):
    # endpoint GUIDs (audio_devices.ApoDevice.guid shape) the user dismissed
    # the "default device isn't APO-hooked" banner for, so the banner doesn't
    # re-nag every launch/device-change for the same endpoint -- same
    # cap-free append-only shape as ignored_apo_sources above.
    "apo_coverage_dismissed_guids": [],
    # A2 (docs/superpowers/plans/2026-07-11-adaptive-frontend-roadmap.md):
    # when True, an extra render layer (last in _apply_render_layers(), see
    # ProfileManager._apply_loudness_matching()) pulls a composed config's
    # estimated broadband gain (abx.estimated_gain_db()) down to the same
    # anchor a flat/EQ-off config sits at, reduction-only, so switching
    # between profiles of different loudness -- or toggling bypass -- never
    # makes a jump in level that could masquerade as a quality difference
    # (same bias abx.level_match exists to remove from blind listening
    # tests). Default OFF for 1.0.x: it quietly lowers hot profiles by
    # several dB, which surprises anyone who calibrated their volume around
    # a favourite profile already at that level (see roadmap A2 Decisions).
    "profile_loudness_matching": False,
    # B2 (docs/superpowers/plans/2026-07-11-adaptive-frontend-roadmap.md):
    # headphone crossfeed, generated as raw APO config text and appended
    # after the rest of the composed render (never through ProfileConfig's
    # structured fields -- see rumble/core/crossfeed.py's module docstring
    # and KB §17.4). Crossfeed is primarily a property of the profile now
    # (each profile carries its own `# Crossfeed:` selection, edited in the
    # editor); this machine-level SOURCE mode -- flipped from the main-UI
    # switch -- decides which value the render layer uses:
    #   "off"     -> no crossfeed for any profile,
    #   "profile" -> each profile's own value, falling back to the global
    #                value below for a profile that sets none (the default),
    #   "global"  -> the global value below for EVERY profile (override).
    "crossfeed_mode": "profile",
    # The GLOBAL crossfeed value (preset/strength/custom), used by the
    # "profile" fallback and the "global" override above. Edited in Settings.
    # Strength defaults to 0 (inert): out of the box the "profile" mode above
    # renders each profile's OWN crossfeed and nothing for un-set profiles --
    # crossfeed stays the opt-in, taste/positioning effect it always was,
    # while a per-profile selection still applies immediately. Raising this
    # value in Settings is what opts a machine into a global fallback/override.
    "crossfeed_preset": crossfeed.DEFAULT_PRESET,
    "crossfeed_strength": 0,
    # "custom" preset mode: user-set gain/freq/delay instead of a named
    # circuit (crossfeed.render_block()'s "custom" branch). Defaults mirror
    # the CHU_MOY/LINKWITZ figures so a first-time switch to Custom starts
    # from a sensible, already-cited point rather than silence.
    "crossfeed_custom_gain_db": -6.0,
    "crossfeed_custom_freq_hz": 700.0,
    "crossfeed_custom_delay_us": 300.0,
    # D1 (docs/superpowers/plans/2026-07-11-adaptive-frontend-roadmap.md):
    # endpoint GUID (audio_devices.ApoDevice.guid shape) -> profile id.
    # Unassigned endpoints follow the global active profile -- current
    # behavior is the default, nothing changes until a user assigns one
    # (see ProfileManager._render_dispatch()/_write_active_pointer()). D2
    # reuses this SAME dict for Capture (microphone) endpoints too -- flow
    # is resolved fresh at render time via _device_for_guid(), not stored
    # here, so one dict covers both Render and Capture assignments.
    "device_profile_assignments": {},
}


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "profile"


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


_DEVICE_LINE_RE = re.compile(r"^[ \t]*Device[ \t]*:[ \t]*(.+?)[ \t]*$",
                             re.IGNORECASE | re.MULTILINE)
_CHANNEL_LINE_RE = re.compile(r"^[ \t]*Channel[ \t]*:", re.IGNORECASE | re.MULTILINE)


def _remap_unmatched_devices(text: str, devices: list) -> tuple[str, bool]:
    """Rewrite `Device:` lines that select no endpoint on this machine to the
    first active device, so profiles arriving from another machine are audible
    immediately instead of silently applying to nothing.

    Only the matched lines change — the rest of the text is preserved
    byte-for-byte. No-op when endpoint enumeration is unavailable (empty
    *devices*): with no basis to judge, leave the profile untouched.
    """
    if not devices:
        return text, False
    changed = False
    first_device = audio_devices.first_active_device_pattern()

    def _replace(match: re.Match) -> str:
        nonlocal changed
        if audio_devices.pattern_matches_any(match.group(1), devices):
            return match.group(0)
        changed = True
        return f"Device: {first_device}"

    return _DEVICE_LINE_RE.sub(_replace, text), changed


def _strip_untrusted_vst(config: ProfileConfig, label: str) -> str | None:
    """Drop any `VSTPlugin:` entries from an *imported* profile, in place.

    A plugin path saved on someone else's machine practically never resolves
    to anything real on this one — unlike an EQ filter or even a convolution
    IR (which we copy into our own store because IRs travel meaningfully
    between machines), a VST path is tied to wherever that specific DLL
    happened to be installed on the machine that wrote the profile. Carrying
    it through unexamined would otherwise let a downloaded "EQ profile"
    silently load an arbitrary native DLL into the live audio pipeline the
    moment it's applied. This only runs on the import path — a VST plugin
    added directly in the Advanced panel (a profile you build yourself) is
    never touched.

    Returns a human-readable note when something was actually dropped, else
    None.
    """
    n = len(config.vst_plugins)
    if n == 0:
        return None
    config.vst_plugins = []
    return (f'{label}: {n} VST plugin reference{"s" if n != 1 else ""} not imported '
            f"(plugin paths rarely resolve across machines and are not carried over "
            f"automatically for safety — add one directly in the Advanced panel instead)")


def _default_to_stereo(text: str, config: ProfileConfig) -> None:
    """Default a channel-less import to a stereo section, in place.

    APO files that carry *no* `Channel:` line (e.g. AutoEQ exports) parse into a
    single section whose channel is the implicit "all" catch-all. The
    user-facing default for such imports is a stereo (L + R) layout, so we
    retarget that lone section at the stereo pair. It stays a single combined
    "L R" block — the canonical linked-stereo form — so it shows as one channel
    strip in the editor (unlinking later splits it into L/R).

    This only applies when the source genuinely specifies no channel: any
    explicit `Channel:` directive — including an explicit "Channel: all" or a
    per-channel layout — is a deliberate choice and is left untouched.
    """
    if _CHANNEL_LINE_RE.search(text):
        return
    sections = config.channel_sections
    if len(sections) != 1 or sections[0].channel.strip().lower() != "all":
        return
    sections[0].channel = "L R"


@dataclass
class ProfileMeta:
    id: str
    name: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    file: str = ""
    created: str = ""
    modified: str = ""
    active: bool = False
    readonly: bool = False
    builtin: bool = False   # system protected — never editable, lockable or deletable
    # First `Stage:` line of the APO body (lower-cased, e.g. "capture" or
    # "pre-mix post-mix"), or None when the profile states no stage. Lets the
    # library hide capture-only (microphone) profiles — see capture_only.
    stage: str | None = None
    # Provenance, set only on profiles imported from an existing APO config so
    # the boot reconcile can keep them current (see sync_apo_imports).
    apo_source: str | None = None
    apo_source_hash: str | None = None
    import_signature: str | None = None
    # Sample rate this profile's fitted bands + GraphicEQ residual were solved
    # at, set only on curve-derived profiles (autoeq_merge.build_curve_config).
    # Its presence marks the GraphicEQ layer as a RESIDUAL that the manager may
    # re-solve when the device rate changes (refresh_fits_for_sample_rate); its
    # absence marks the layer as the user's own tuning, never to be rewritten.
    fit_sample_rate: int | None = None
    # This profile's own headphone crossfeed selection (Rumble: per-profile
    # crossfeed). None ⇒ no crossfeed for this profile. The render layer applies
    # the active/previewed profile's spec, so crossfeed switches with the
    # profile -- unlike the other render layers, which stay machine-level.
    crossfeed: "crossfeed.CrossfeedSpec | None" = None

    @property
    def protected(self) -> bool:
        """True when writes must be refused (user-locked or built-in)."""
        return self.readonly or self.builtin

    @property
    def capture_only(self) -> bool:
        """A microphone/recording profile: scoped to APO's capture stage only,
        so it never touches playback. "All stages" (which also names capture)
        still applies to playback, so it is deliberately not counted here.

        This reads `stage` and nothing else, so a profile is only a microphone
        profile if its body actually SAYS `Stage: capture` -- a mic-shaped
        profile that omits the line is indistinguishable from a playback one
        and the library's "Microphone profiles" toggle cannot act on it. The
        built-in mic profiles get theirs from
        builtin_profiles_data._mic_body(); a user's own come from the editor's
        Advanced -> Stage dropdown ("Capture only (microphones)").
        """
        return self.stage == "capture"


DEFAULT_FLAT_FILTERS_BANDS = 10
MAX_FLAT_BANDS = 32

_CLASSIC_FLAT_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


def flat_band_frequencies(num_bands: int) -> list[float]:
    """Band centre frequencies for a flat starter profile.

    The classic 10 ISO octave centres for counts up to 10 (matching the
    historical default), log-spaced across 20 Hz–20 kHz for larger counts.
    """
    num_bands = max(1, min(int(num_bands), MAX_FLAT_BANDS))
    if num_bands <= len(_CLASSIC_FLAT_FREQS):
        return [float(f) for f in _CLASSIC_FLAT_FREQS[:num_bands]]
    lo, hi = 20.0, 20000.0
    return [round(lo * (hi / lo) ** (i / (num_bands - 1)), 1) for i in range(num_bands)]


def make_flat_config(num_bands: int = DEFAULT_FLAT_FILTERS_BANDS,
                     channels: list[str] | None = None,
                     device: str = "all") -> ProfileConfig:
    """A flat (no-op) profile: PK bands at 0 dB gain, one channel section per
    requested channel. Each section gets its own Filter instances so the
    sections can diverge later without aliasing."""
    freqs = flat_band_frequencies(num_bands)
    sections = [
        apo_config.ChannelSection(
            channel=channel,
            filters=[apo_config.Filter(enabled=True, type="PK", fc=f, gain=0.0, q=1.41)
                     for f in freqs],
        )
        for channel in (channels or ["all"])
    ]
    return ProfileConfig(device=device or "all", preamp=0.0, channel_sections=sections)


def _default_flat_config(num_bands: int = DEFAULT_FLAT_FILTERS_BANDS) -> ProfileConfig:
    return make_flat_config(num_bands)


@dataclass
class ImportResult:
    """Outcome of a batch import: profiles successfully created, plus a
    human-readable note for every source/profile that was skipped, plus any
    non-fatal notes about an imported profile (e.g. a stripped VST plugin
    reference — see _strip_untrusted_vst)."""
    imported: list[ProfileMeta] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.imported)


class ProfileManager:
    """Owns profile metadata and mediates all reads/writes to the APO config dir."""

    def __init__(self, data_dir: Path, config_path: Path | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.data_dir / "profiles.json"
        self._config_path_hint = config_path
        # Session-only escape hatch: when a user-set config_path_override turns
        # out to be inaccessible at startup, the app drops it for this run and
        # falls back to the default path (see rumble/__main__) rather than
        # stranding the user. The persisted override is left intact so it is
        # retried next launch (e.g. once an unplugged drive is back); this flag
        # is never saved, so an adoption save() can't clobber the setting.
        self._ignore_config_override = False
        self._active_profile_id: str | None = None
        # Plain-Python observers fired after a successful activate_profile(),
        # regardless of the initiator (main_window manual activation, tray,
        # or Phase 3's AutoSwitchController) — one shared path, one place to
        # subscribe. Never touches Qt; adapters marshal to the main thread.
        self.on_profile_activated: list = []
        # Phase 8 Listener Slots: which of the 4 fixed slots a session is
        # correcting for right now. Session-only -- always reset from
        # settings["default_listener_slot"] on construction (see _load()),
        # never persisted as "last active" (design spec §6).
        self.active_listener_slot: int = 1
        self.on_listener_changed: list = []
        self._listener_correction_cache: "listener_correction.EarCorrection | None" = None
        self._listener_correction_cache_slot: int | None = None
        # A2 loudness-matching layer: the numpy broadband-gain estimate is
        # cached against the rendered text of the config it was computed
        # from (cheap string compare) so the volume-poller's
        # refresh_render_layers() ticks (see loudness_volume_adapter.py)
        # don't re-run abx.estimated_gain_db() every second when the
        # composed config hasn't actually changed.
        self._loudness_matching_cache_key: str | None = None
        self._loudness_matching_cache_gain: float | None = None
        # B1 session tone-trim layer: runtime-only, same category as
        # active_listener_slot above and the editor's own Pre-Amp/Auto
        # session controls (design spec §6) -- always starts flat, never
        # persisted, dies with the process. See set_session_trim().
        self.session_trim: dict = {"bass_db": 0.0, "treble_db": 0.0, "tilt_db_per_oct": 0.0}

        self.profiles: list[ProfileMeta] = []
        self.settings: dict = dict(DEFAULT_SETTINGS)
        self._previewing = False
        # Incremented on every preview_config() write so the scratch file's
        # bytes always change even when the EQ content itself repeats (e.g.
        # ABX switching back to a side already heard). Equalizer APO's own
        # hot-reload has been observed to miss a change when a preview file
        # is rewritten with byte-identical content to a prior write in the
        # same Include slot -- a bare mtime bump isn't enough to force it,
        # but a changed comment line is.
        self._preview_seq = 0
        # Runtime-only cache of the last known default-device sample rate,
        # kept warm by MainWindow._refresh_device_format() (via
        # set_detected_sample_rate()) so "auto" mode's effective_sample_rate()
        # doesn't have to make a live COM round-trip on every call. NOT a
        # settings key — never persisted, never added to DEFAULT_SETTINGS.
        self._detected_sample_rate: int | None = None
        # Foreign config.txt content displaced by the ownership swap, stashed by
        # ensure_first_run for the boot reconcile to offer as an import.
        self._displaced_config_text = ""
        self._load()

    # ── persistence ──────────────────────────────────────────────────

    def _load(self) -> None:
        """Load settings from JSON and scan profile folders."""
        # Load settings from JSON (if it exists)
        if self.metadata_path.exists():
            try:
                data = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        else:
            data = {}

        loaded_settings = data.get("settings", {})
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update({k: v for k, v in loaded_settings.items() if k in DEFAULT_SETTINGS})

        # Load active profile ID from JSON
        self._active_profile_id = data.get("active_profile_id")

        # Scan profile folders and load all profiles
        self._scan_profile_folders()

        self.active_listener_slot = int(self.settings.get("default_listener_slot", 1))
        self._listener_correction_cache = None
        self._listener_correction_cache_slot = None
        if not self.settings.get("legacy_hearing_migrated"):
            # migrate_legacy_hearing_history() is a cheap no-op when there's no
            # legacy dir or Slot 1 already has data, so only persist the gate
            # (and only write settings to disk) once it actually migrated
            # something -- otherwise a plain fresh launch with no legacy Test A
            # history would write JSON merely from constructing the manager.
            if listener_slots.migrate_legacy_hearing_history(self.data_dir):
                self.settings["legacy_hearing_migrated"] = True
                self.save()

    def refresh_profiles(self) -> None:
        """Rescan profile folders to pick up new/modified/deleted profiles.
        Call this on startup or when you want to reload from disk."""
        self._scan_profile_folders()

    def _scan_profile_folders(self) -> None:
        """Discover profile .txt files: the organised defaults/ and user/
        folders first, then any loose files dropped straight into rumble/ root.

        Root-level files are auto-discovered as user profiles so a profile can
        be added by simply dropping it into the rumble folder. RUMBLE's own
        scratch/system files are skipped, and a folder copy of an id always
        wins over a root copy (ids stay unique)."""
        self.profiles = []
        seen_ids: set[str] = set()

        def absorb(file_path: Path, is_builtin: bool) -> None:
            meta = self._load_profile_from_file(file_path, is_builtin=is_builtin)
            if meta and meta.id not in seen_ids:
                self.profiles.append(meta)
                seen_ids.add(meta.id)

        # Organised folders take precedence.
        for folder_name in ("defaults", "user"):
            folder_path = self.rumble_dir / folder_name
            if not folder_path.is_dir():
                continue
            try:
                for profile_path in sorted(folder_path.glob("*.txt")):
                    absorb(profile_path, is_builtin=(folder_name == "defaults"))
            except OSError:
                pass

        # Loose files dropped directly into rumble/ root.
        if self.rumble_dir.is_dir():
            try:
                for profile_path in sorted(self.rumble_dir.glob("*.txt")):
                    if (profile_path.name in _ROOT_SCAN_EXCLUDE
                            or profile_path.name.startswith(DEVICE_SCRATCH_PREFIX)):
                        continue
                    absorb(profile_path, is_builtin=False)
            except OSError:
                pass

    def _load_profile_from_file(self, file_path: Path, is_builtin: bool = False) -> ProfileMeta | None:
        """Load a single profile from a .txt file."""
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError:
            return None

        # Foreign hardware → our active device. A `Device:` line that selects no
        # active endpoint here (a profile imported or dropped in from another
        # machine) is rewritten in place to the first active device, so it is
        # audible immediately rather than applying to nothing. No-op when no
        # endpoints are enumerable, or when the device already matches.
        remapped, changed = _remap_unmatched_devices(text, audio_devices.list_apo_devices())
        if changed:
            text = remapped
            try:
                file_path.write_text(remapped, encoding="utf-8")
            except OSError:
                pass

        doc = profile_file.parse_profile_text(text)
        if not doc.name:
            doc.name = file_path.stem.replace("_", " ").title()

        # Determine if this profile is currently active
        is_active = (self._active_profile_id == file_path.stem)

        meta = ProfileMeta(
            id=file_path.stem,
            name=doc.name,
            description=doc.description or "",
            tags=list(doc.tags or []),
            # POSIX '/' separators so the APO Include path is consistent with
            # create_profile() regardless of OS (Windows str() would use '\\').
            file=(file_path.relative_to(self.config_path) if file_path.is_absolute() else file_path).as_posix(),
            created=doc.created or _now_iso(),
            modified=doc.modified or _now_iso(),
            active=is_active,
            readonly=bool(doc.readonly),
            builtin=is_builtin,
            stage=doc.config.stage,
            apo_source=doc.apo_source,
            apo_source_hash=doc.apo_source_hash,
            import_signature=doc.import_signature,
            fit_sample_rate=doc.fit_sample_rate,
            crossfeed=doc.crossfeed,
        )

        # Legacy (old 3-line header) and foreign (header-less) files are
        # migrated in place to the canonical full header. The file is the sole
        # source of truth, so absent fields become stated-empty rather than
        # recovered from any index. Migration is one-shot: once rewritten the
        # file carries a full header and is skipped on later loads. The body is
        # preserved byte-for-byte by replace_header.
        if not doc.has_full_header():
            self._rewrite_file_metadata(meta)

        return meta


    def save(self) -> None:
        """Save only settings and active profile ID to JSON."""
        data = {
            "settings": self.settings,
            "active_profile_id": self._active_profile_id,
        }
        tmp_path = self.metadata_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp_path.replace(self.metadata_path)

    # ── path resolution ──────────────────────────────────────────────

    @property
    def config_path(self) -> Path:
        override = None if self._ignore_config_override else self.settings.get("config_path_override")
        if override:
            return Path(override)
        return self._config_path_hint or apo_path.get_config_path()

    @property
    def rumble_dir(self) -> Path:
        return self.config_path / RUMBLE_SUBDIR

    def _profile_file_path(self, meta: ProfileMeta) -> Path:
        return self.config_path / meta.file

    # ── canonical file IO helpers ────────────────────────────────────

    def _doc_for_meta(self, meta: ProfileMeta, config: ProfileConfig | None = None) -> ProfileDocument:
        """Build the canonical document for a profile from its index entry."""
        return ProfileDocument(
            name=meta.name,
            description=meta.description,
            tags=list(meta.tags),
            created=meta.created,
            modified=meta.modified,
            readonly=meta.readonly,
            builtin=meta.builtin,
            apo_source=meta.apo_source,
            apo_source_hash=meta.apo_source_hash,
            import_signature=meta.import_signature,
            fit_sample_rate=meta.fit_sample_rate,
            crossfeed=meta.crossfeed,
            config=config if config is not None else ProfileConfig(),
        )

    def _write_profile_file(self, meta: ProfileMeta, config: ProfileConfig) -> bool:
        """Write the full canonical file (header + body). No readonly guard —
        this is the internal path used for creation, healing and migration."""
        path = self._profile_file_path(meta)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(profile_file.render_profile_text(self._doc_for_meta(meta, config)),
                            encoding="utf-8")
            return True
        except OSError:
            return False

    def _rewrite_file_metadata(self, meta: ProfileMeta) -> None:
        """Refresh the metadata header of an existing canonical file while
        preserving its APO command body byte-for-byte."""
        path = self._profile_file_path(meta)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            path.write_text(profile_file.replace_header(text, self._doc_for_meta(meta)),
                            encoding="utf-8")
        except OSError:
            pass

    # ── queries ──────────────────────────────────────────────────────

    def list_profiles(self, tag: str | None = None) -> list[ProfileMeta]:
        if tag is None:
            return list(self.profiles)
        return [p for p in self.profiles if tag in p.tags]

    def get_profile(self, profile_id: str) -> ProfileMeta | None:
        return next((p for p in self.profiles if p.id == profile_id), None)

    def get_active_profile(self) -> ProfileMeta | None:
        return next((p for p in self.profiles if p.active), None)

    # ── id generation ────────────────────────────────────────────────

    def _generate_id(self, name: str) -> str:
        base = _slugify(name)
        candidate = base
        existing = {p.id for p in self.profiles}
        n = 2
        while candidate in existing:
            candidate = f"{base}_{n}"
            n += 1
        return candidate

    # ── CRUD ─────────────────────────────────────────────────────────

    def create_profile(self, name: str, description: str = "", tags: list[str] | None = None,
                       config: ProfileConfig | None = None, readonly: bool = False,
                       created: str | None = None, profile_id: str | None = None,
                       apo_source: str | None = None, apo_source_hash: str | None = None,
                       import_signature: str | None = None,
                       fit_sample_rate: int | None = None) -> ProfileMeta:
        """Create a new user profile. Provenance args are set only when importing
        from an existing APO config (see sync_apo_imports).

        `fit_sample_rate` is set only by the curve-derived paths (see
        autoeq_merge.build_curve_config) and marks this profile's GraphicEQ layer
        as a re-solvable residual rather than user-authored tuning.
        """
        if profile_id is None or self.get_profile(profile_id) is not None:
            profile_id = self._generate_id(name)
        if config is None:
            config = _default_flat_config(self.settings.get("default_bands", DEFAULT_FLAT_FILTERS_BANDS))

        now = _now_iso()
        meta = ProfileMeta(
            id=profile_id,
            name=name,
            description=description,
            tags=list(tags or []),
            file=f"{RUMBLE_SUBDIR}/user/{profile_id}.txt",
            created=created or now,
            modified=now,
            active=False,
            readonly=readonly,
            builtin=False,
            apo_source=apo_source,
            apo_source_hash=apo_source_hash,
            import_signature=import_signature,
            fit_sample_rate=fit_sample_rate,
        )
        self.profiles.append(meta)
        self._write_profile_file(meta, config)
        self.save()
        return meta

    def duplicate_profile(self, source_id: str, new_name: str) -> ProfileMeta | None:
        source = self.get_profile(source_id)
        if source is None:
            return None
        config = self.load_profile_config(source_id) or ProfileConfig()
        return self.create_profile(new_name, description=source.description,
                                   tags=list(source.tags), config=config)

    def rename_profile(self, profile_id: str, new_name: str) -> bool:
        meta = self.get_profile(profile_id)
        if meta is None or not new_name.strip() or meta.protected:
            return False
        meta.name = new_name.strip()
        meta.modified = _now_iso()
        self.save()
        self._rewrite_file_metadata(meta)
        return True

    def update_profile_meta(self, profile_id: str, description: str | None = None,
                            tags: list[str] | None = None) -> bool:
        meta = self.get_profile(profile_id)
        if meta is None or meta.protected:
            return False
        if description is not None:
            meta.description = description
        if tags is not None:
            meta.tags = list(tags)
        meta.modified = _now_iso()
        self.save()
        self._rewrite_file_metadata(meta)
        return True

    def set_profile_locked(self, profile_id: str, locked: bool) -> bool:
        """Lock or unlock a profile. Locked profiles cannot be saved, renamed,
        cleared or deleted until unlocked — this is the one write that is
        always allowed regardless of the current lock state. Built-in profiles
        are system protected and never participate in user locking."""
        meta = self.get_profile(profile_id)
        if meta is None or meta.builtin or meta.readonly == locked:
            return False
        meta.readonly = locked
        self.save()
        self._rewrite_file_metadata(meta)
        return True

    def delete_profile(self, profile_id: str) -> bool:
        """Delete a user profile. Not a real unlink -- the file is preserved in
        the recycle bin (see _trash_profile_file) so it is restorable; built-ins
        never reach here (meta.protected refuses them), so they bypass the
        trash entirely and are left for ensure_builtin_profiles() to manage."""
        meta = self.get_profile(profile_id)
        if meta is None or meta.protected:
            return False
        was_active = meta.active
        self._trash_profile_file(meta)
        try:
            self._profile_file_path(meta).unlink(missing_ok=True)
        except OSError:
            pass
        self.profiles.remove(meta)
        if was_active:
            self._active_profile_id = None
            self._write_active_pointer(None)
        self.save()
        # Rescan to ensure the deleted profile is no longer in the list
        self._scan_profile_folders()
        return True

    # ── recycle bin (trash) ───────────────────────────────────────────

    @property
    def _trash_dir(self) -> Path:
        return self.data_dir / TRASH_SUBDIR

    def _trash_profile_file(self, meta: ProfileMeta) -> None:
        """Preserve a profile about to be deleted as a timestamped
        `<utc-timestamp>_<id>.txt` + sidecar `.json` pair under
        `data_dir/trash/`, then enforce the retention cap. Best-effort: a
        source file that can't be read (already missing) leaves nothing to
        preserve, and delete_profile() proceeds with the delete regardless."""
        try:
            text = self._profile_file_path(meta).read_text(encoding="utf-8")
        except OSError:
            return
        trash_dir = self._trash_dir
        try:
            trash_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        # Microsecond precision so rapid successive deletes (batch delete,
        # tests) never collide on the same stem.
        stem = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}_{meta.id}"
        sidecar = {
            "id": meta.id,
            "name": meta.name,
            "description": meta.description,
            "tags": list(meta.tags),
            "locked": meta.readonly,
            "original_file": meta.file,
            "deleted": datetime.now(timezone.utc).isoformat(),
        }
        try:
            (trash_dir / f"{stem}.txt").write_text(text, encoding="utf-8")
            (trash_dir / f"{stem}.json").write_text(
                json.dumps(sidecar, indent=2), encoding="utf-8")
        except OSError:
            return
        self._enforce_trash_retention()

    def _enforce_trash_retention(self) -> None:
        """Keep only the newest MAX_TRASHED entries -- same cap-on-write
        pattern as listener_slots._enforce_retention(). The utc-timestamp
        prefix is fixed-width, so a plain lexicographic sort is chronological."""
        trash_dir = self._trash_dir
        if not trash_dir.is_dir():
            return
        stems = sorted({p.stem for p in trash_dir.glob("*.json")})
        excess = len(stems) - MAX_TRASHED
        for stem in stems[:max(excess, 0)]:
            (trash_dir / f"{stem}.txt").unlink(missing_ok=True)
            (trash_dir / f"{stem}.json").unlink(missing_ok=True)

    def list_trashed(self) -> list[TrashedProfile]:
        """List deleted profiles preserved in the recycle bin, newest first."""
        trash_dir = self._trash_dir
        if not trash_dir.is_dir():
            return []
        entries: list[TrashedProfile] = []
        for json_path in trash_dir.glob("*.json"):
            txt_path = json_path.with_suffix(".txt")
            if not txt_path.exists():
                continue
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            try:
                deleted_at = datetime.fromisoformat(data.get("deleted", ""))
            except ValueError:
                deleted_at = datetime.now(timezone.utc)
            entries.append(TrashedProfile(
                id=data.get("id", json_path.stem),
                name=data.get("name") or json_path.stem,
                description=data.get("description", ""),
                tags=list(data.get("tags", [])),
                locked=bool(data.get("locked", False)),
                original_file=data.get("original_file", ""),
                deleted_at=deleted_at,
                label=deleted_at.strftime("%b %d, %Y %I:%M %p"),
                txt_path=txt_path,
                json_path=json_path,
            ))
        entries.sort(key=lambda e: e.json_path.stem, reverse=True)
        return entries

    def restore_trashed(self, entry: TrashedProfile) -> ProfileMeta | None:
        """Restore a trashed profile as a new user profile (collision-safe id
        via create_profile's own _generate_id) -- the trashed copy is left in
        place so restoring never destroys the recycle-bin entry; purge it
        separately once no longer wanted."""
        try:
            text = entry.txt_path.read_text(encoding="utf-8")
        except OSError:
            return None
        remapped, _ = _remap_unmatched_devices(text, audio_devices.list_apo_devices())
        config = apo_config.parse_config(remapped, sample_rate=self.effective_sample_rate())
        return self.create_profile(entry.name, description=entry.description,
                                   tags=list(entry.tags), config=config)

    def purge_trashed(self, entry: TrashedProfile) -> bool:
        """Permanently remove a recycle-bin entry (both the body and sidecar)."""
        ok = True
        for path in (entry.txt_path, entry.json_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                ok = False
        return ok

    # ── profile config IO ────────────────────────────────────────────

    def load_profile_config(self, profile_id: str) -> ProfileConfig | None:
        meta = self.get_profile(profile_id)
        if meta is None:
            return None
        path = self._profile_file_path(meta)
        if not path.exists():
            import sys
            print(f"WARNING: Profile file not found: {path}", file=sys.stderr)
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        return apo_config.parse_config(
            text, sample_rate=self.effective_sample_rate())

    def set_profile_crossfeed(self, profile_id: str,
                              spec: "crossfeed.CrossfeedSpec | None",
                              *, write: bool = True) -> bool:
        """Set a profile's own crossfeed (per-profile). With write=True it
        persists the file and re-renders if the profile is active -- used by
        the tray quick-toggle. With write=False it only updates the in-memory
        meta, for callers (the editor save) that write the file themselves
        immediately after."""
        meta = self.get_profile(profile_id)
        if meta is None or meta.protected:
            return False
        meta.crossfeed = spec
        if not write:
            return True
        config = self.load_profile_config(profile_id)
        if config is None:
            return False
        meta.modified = _now_iso()
        if not self._write_profile_file(meta, config):
            return False
        self.save()
        if meta.active:
            self._write_active_pointer(meta)
        return True

    def save_profile_config(self, profile_id: str, config: ProfileConfig) -> bool:
        meta = self.get_profile(profile_id)
        if meta is None or meta.protected:
            return False
        meta.modified = _now_iso()
        if not self._write_profile_file(meta, config):
            return False
        self.save()
        if meta.active:
            self._write_active_pointer(meta)
        else:
            self.end_preview()
        return True

    # ── import / export ──────────────────────────────────────────────

    def import_profile(self, source_path: Path,
                       name: str | None = None) -> tuple[ProfileMeta | None, list[str]]:
        """Import a single profile from a plain `.txt` or a `.zip` bundle
        (profile + impulse response). When a zip holds several profiles only
        the first is imported — use `import_profiles` for batch/pack imports.
        Returns the new profile plus any non-fatal import notes (e.g. a
        stripped VST plugin reference — see `_strip_untrusted_vst`)."""
        source_path = Path(source_path)
        docs, notes = self._read_import_docs(source_path)
        if not docs:
            return None, notes
        doc, fallback = docs[0]
        meta = self._create_from_doc(doc, fallback, source_path.name, name_override=name)
        return meta, notes

    def import_peace_file(self, source_path: Path,
                          name: str | None = None) -> tuple[ProfileMeta | None, list[str]]:
        """Import a Peace (`.peace`) preset as a new user profile (PLANNED_WORK.md
        #5). Peace's save format is its own INI structure, not raw Equalizer APO
        text — see peace_import.py. Returns `(None, [])` when the file holds no
        real EQ; otherwise the new profile plus any per-band skip notes (Peace's
        Butterworth/Linkwitz-Riley/adjustable-Q-shelf filter designs have no
        literal Equalizer APO filter type and can't be faithfully imported)."""
        source_path = Path(source_path)
        try:
            text = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None, []
        result = peace_import.parse_peace(text)
        if not _has_real_eq(result.config):
            return None, result.skipped
        display_name = name or source_path.stem.replace("_", " ").title()
        meta = self.create_profile(
            display_name,
            description=result.description or f"Imported from Peace ({source_path.name})",
            tags=["Imported"], config=result.config)
        return meta, result.skipped

    def import_rew_filter_settings(self, source_path: Path,
                                   name: str | None = None
                                   ) -> tuple[ProfileMeta | None, list[str]]:
        """Import a REW "Export filter settings as text" export as a new
        user profile (Rumble 3.8) — see rew_import.py. Returns `(None, [])`
        when the file holds no importable filter. Skip notes cover any
        `BW/60`-defined filter REW's own bandwidth convention there can't be
        translated to RUMBLE's Q/octave-bandwidth model."""
        source_path = Path(source_path)
        try:
            text = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None, []
        result = rew_import.parse_filter_settings_text(text)
        if not result.filters:
            return None, result.skipped
        section = apo_config.ChannelSection(channel="L R", filters=result.filters)
        config = apo_config.ProfileConfig(device="", preamp=None, channel_sections=[section])
        display_name = name or source_path.stem.replace("_", " ").title()
        meta = self.create_profile(
            display_name,
            description=f"Imported from REW filter settings ({source_path.name})",
            tags=["Imported", "REW"], config=config)
        return meta, result.skipped

    def import_rew_measurement(self, source_path: Path, *, name: str | None = None,
                               slope_db_per_oct: float = 0.0,
                               blend_curve: list[tuple[float, float]] | None = None,
                               ) -> tuple[ProfileMeta | None, eq_target.ClampSummary, list[str]]:
        """Import a REW frequency-response measurement export as a new user
        profile (Rumble 3.8) — see rew_import.py. The raw SPL curve is
        turned into a correction the same way SpinoramaProvider turns a raw
        Listening Window into one (eq_target.make_correction toward a flat/
        tilted target — see rumble.core.eq_target).

        `blend_curve` (optional) is an existing community curve (e.g. one
        already imported from AutoEQ/Spinorama) to weight-blend this REW
        measurement against, via rew_import.blend_with_community() — the
        "replace or blend with community data" choice the guide calls for,
        exposed here as simply whether the caller passes one. `None`
        (default) uses the REW measurement alone.

        Returns `(None, ClampSummary(), warnings)` when the file holds no
        usable data points.
        """
        source_path = Path(source_path)
        try:
            text = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None, eq_target.ClampSummary(), []
        result = rew_import.parse_measurement_text(text)
        if not result.curve:
            return None, eq_target.ClampSummary(), result.warnings
        measured = (rew_import.blend_with_community(result.curve, blend_curve)
                   if blend_curve else result.curve)
        curve, clamp = eq_target.make_correction(measured, slope_db_per_oct=slope_db_per_oct)
        sample_rate = self.effective_sample_rate()
        config = autoeq_merge.build_curve_config(curve, sample_rate=sample_rate)
        display_name = name or source_path.stem.replace("_", " ").title()
        blended_note = " (blended with existing community data)" if blend_curve else ""
        meta = self.create_profile(
            display_name,
            description=f"Imported from REW measurement ({source_path.name}){blended_note}. "
                        "Your own real measurement — refine further by ear or re-measure "
                        "after applying.",
            tags=["Imported", "REW"], config=config, fit_sample_rate=sample_rate)
        return meta, clamp, result.warnings

    def create_from_apo_text(self, name: str, text: str, *, description: str = "",
                             tags: list[str] | None = None) -> ProfileMeta | None:
        """Create a user profile directly from APO config text already in hand
        (e.g. fetched from the AutoEQ database) — no file or zip on disk.

        Foreign Device: lines are remapped to the active device just like a
        file import; AutoEQ files carry no Device line so this is a no-op for
        them. Returns the new profile, or None if the text holds no profile."""
        text, _ = _remap_unmatched_devices(text, audio_devices.list_apo_devices())
        doc = profile_file.parse_profile_text(text)
        if doc is None:
            return None
        _default_to_stereo(text, doc.config)
        return self.create_profile(
            name,
            description=description or (doc.description or ""),
            tags=list(tags if tags is not None else (doc.tags or [])),
            config=doc.config,
            created=doc.created,
        )

    def import_profiles(self, source_paths: list[Path]) -> ImportResult:
        """Import many profiles at once. Each path may be a plain `.txt`
        profile or a `.zip`; a zip may itself contain *several* profiles (a
        "pack"), every one of which is imported with its own impulse response.
        Returns a summary of what was created and what was skipped."""
        result = ImportResult()
        for raw in source_paths:
            path = Path(raw)
            docs, notes = self._read_import_docs(path)
            result.notes.extend(notes)
            if not docs:
                result.failures.append(f"{path.name}: no importable profile found")
                continue
            for doc, fallback in docs:
                meta = self._create_from_doc(doc, fallback, path.name)
                if meta is not None:
                    result.imported.append(meta)
                else:
                    result.failures.append(f"{path.name}: could not import '{fallback}'")
        return result

    def _read_import_docs(self, source_path: Path
                          ) -> tuple[list[tuple[ProfileDocument, str]], list[str]]:
        """Read every profile document from a source file as (doc, fallback
        name) pairs, plus any non-fatal import notes (e.g. a stripped VST
        plugin reference). A `.zip` yields one entry per contained `.txt`
        (each with its impulse response resolved); a plain `.txt` yields a
        single entry."""
        try:
            is_bundle = zipfile.is_zipfile(source_path)
        except OSError:
            return [], []
        if is_bundle:
            return self._read_bundle_docs(source_path)
        try:
            text = source_path.read_text(encoding="utf-8")
        except OSError:
            return [], []
        text, _ = _remap_unmatched_devices(text, audio_devices.list_apo_devices())
        doc = profile_file.parse_profile_text(text)
        if doc is None:
            return [], []
        fallback = source_path.stem.replace("_", " ").title()
        notes = []
        note = _strip_untrusted_vst(doc.config, doc.name or fallback)
        if note:
            notes.append(note)
        return [(doc, fallback)], notes

    def _create_from_doc(self, doc: ProfileDocument, fallback_name: str,
                         source_name: str, name_override: str | None = None) -> ProfileMeta | None:
        """Create one user profile from a parsed document. An explicit
        `name_override` wins over the document's own name (single-import
        rename); otherwise the document name, then the fallback, is used."""
        name = name_override or doc.name or fallback_name or "Imported Profile"
        description = doc.description if doc.description else f"Imported from {source_name}"
        return self.create_profile(name, description=description,
                                   tags=list(doc.tags or []), config=doc.config,
                                   created=doc.created)

    def _read_bundle_docs(self, source_path: Path
                          ) -> tuple[list[tuple[ProfileDocument, str]], list[str]]:
        """Parse every `.txt` profile inside a `.zip`. Each profile's
        Convolution impulse response (when present) is stored under
        `rumble/irs/` and the line repointed there. Returns (doc, fallback name)
        pairs ordered shallowest archive path first, plus any non-fatal import
        notes (e.g. a stripped VST plugin reference). One unreadable member
        never aborts the rest of the pack."""
        docs: list[tuple[ProfileDocument, str]] = []
        notes: list[str] = []
        try:
            with zipfile.ZipFile(source_path) as bundle:
                txt_members = sorted(
                    (m for m in bundle.namelist() if m.lower().endswith(".txt")),
                    key=lambda m: (m.count("/"), m.lower()))
                for member in txt_members:
                    raw = self._read_zip_member_capped(bundle, member, _MAX_TEXT_MEMBER_BYTES)
                    if raw is None:
                        continue
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    text, _ = _remap_unmatched_devices(text, audio_devices.list_apo_devices())
                    doc = profile_file.parse_profile_text(text)
                    if doc is None:
                        continue
                    self._rehome_bundle_irs(bundle, doc)
                    fallback = Path(member).stem.replace("_", " ").title()
                    note = _strip_untrusted_vst(doc.config, doc.name or fallback)
                    if note:
                        notes.append(note)
                    docs.append((doc, fallback))
        except (OSError, zipfile.BadZipFile):
            return docs, notes
        return docs, notes

    def _rehome_bundle_irs(self, bundle: zipfile.ZipFile, doc: ProfileDocument) -> None:
        """Copy every IR a bundled profile references into our own IR store and
        repoint the profile at the stored copies — the bare `Convolution:` line
        AND each branch of a sample-rate conditional. Missing members are left
        alone (the import still lands; only that branch's IR won't resolve)."""
        def store(ref: str) -> str | None:
            member = self._find_ir_member(bundle, ref)
            if member is None:
                return None
            payload = self._read_zip_member_capped(bundle, member, _MAX_IR_MEMBER_BYTES)
            if payload is None:
                return None
            stored = self._store_ir_file(Path(member).name, payload)
            if stored is None:
                return None
            return f"{IR_SUBDIR}/{stored.name}"

        if doc.config.convolution:
            rehomed = store(doc.config.convolution)
            if rehomed is not None:
                doc.config.convolution = rehomed

        def rewrite(match: re.Match[str]) -> str:
            rehomed = store(match.group(2).strip())
            return match.group(0) if rehomed is None else f"{match.group(1)}{rehomed}"

        doc.config.conditional_blocks = [
            re.sub(r"(?im)^(\s*Convolution\s*:\s*)(.+)$", rewrite, block)
            for block in doc.config.conditional_blocks
        ]

    @staticmethod
    def _find_ir_member(bundle: zipfile.ZipFile, convolution: str) -> str | None:
        """Locate the bundle member the Convolution line refers to — by exact
        archive path first, then by bare filename (hand-made zips)."""
        target = convolution.replace("\\", "/").strip()
        names = bundle.namelist()
        if target in names:
            return target
        base = target.rsplit("/", 1)[-1].lower()
        for member in names:
            if not member.endswith("/") and member.rsplit("/", 1)[-1].lower() == base:
                return member
        return None

    def _store_ir_file(self, filename: str, payload: bytes) -> Path | None:
        """Store an imported impulse response under `rumble/irs/`. An identical
        existing file is reused; a different file owning the name is never
        overwritten (other profiles may reference it) — the new one gets a
        numbered suffix instead."""
        ir_dir = self.rumble_dir / IR_SUBDIR
        try:
            ir_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        base = Path(filename)
        candidate = ir_dir / base.name
        counter = 2
        while True:
            try:
                if not candidate.exists():
                    candidate.write_bytes(payload)
                    return candidate
                if candidate.read_bytes() == payload:
                    return candidate
            except OSError:
                return None
            candidate = ir_dir / f"{base.stem}_{counter}{base.suffix}"
            counter += 1

    def resolve_ir_path(self, conv_value: str) -> Path | None:
        """Resolve a `Convolution:` value to its on-disk file the way APO
        resolves it — absolute paths as-is; relative paths against the
        directory of the file containing the command (`rumble/`) with the
        config dir as a fallback. None when unset or not found."""
        conv_value = (conv_value or "").strip()
        if not conv_value:
            return None
        raw = Path(conv_value)
        candidates = [raw] if raw.is_absolute() else [self.rumble_dir / raw, self.config_path / raw]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
        return None

    @staticmethod
    def _primary_convolution(config: "apo_config.ProfileConfig") -> str:
        """The IR path that represents this profile, wherever it lives: the
        bare `Convolution:` line if it has one, else the default-rate IR its
        sample-rate conditional falls back to (the `Else:` branch).

        A multi-rate profile deliberately has NO bare line (that duplicate is
        what double-convolved the signal -- see
        create_profile_with_convolution), so every consumer that just wants
        "this profile's IR" -- export, IR analysis, the render layers'
        preamp-headroom calculation -- has to look inside the conditional
        too, which is what this centralises."""
        if config.convolution:
            return config.convolution
        fallback = apo_config.fallback_convolution_path(config)
        if fallback:
            return fallback
        conditional = apo_config.conditional_convolution_paths(config)
        return conditional[0] if conditional else ""

    @staticmethod
    def _all_convolution_refs(config: "apo_config.ProfileConfig") -> list[str]:
        """Every IR path the profile can convolve at runtime — the bare line
        plus each branch of its sample-rate conditional. Export has to carry
        them all or the profile breaks on a machine running the other rate."""
        refs: list[str] = []
        if config.convolution:
            refs.append(config.convolution)
        for path in apo_config.conditional_convolution_paths(config):
            if path not in refs:
                refs.append(path)
        return refs

    def resolve_convolution_ir(self, profile_id: str) -> Path | None:
        """The on-disk impulse response file a profile convolves, or None
        when it convolves nothing or the file cannot be found."""
        config = self.load_profile_config(profile_id)
        if config is None:
            return None
        convolution = self._primary_convolution(config)
        if not convolution:
            return None
        return self.resolve_ir_path(convolution)

    def create_profile_with_convolution(
            self, name: str, ir_by_rate: dict[int, bytes], *,
            description: str = "", tags: list[str] | None = None,
            default_rate: int = 48000) -> ProfileMeta | None:
        """Create a profile whose Convolution: line is chosen at runtime by
        Equalizer APO's own `If: sampleRate == …` conditional, from a set of
        same-tuning impulse responses at different sample rates (e.g. AutoEQ's
        44.1/48 kHz minimum-phase pair). `default_rate` picks the IR the
        conditional's `Else:` branch falls back to. Returns None if no IR
        could be stored.

        DOUBLE-CONVOLUTION BUG (fixed 2026-07-13): this used to ALSO set the
        top-level `convolution` field on the multi-rate path, on the stated
        assumption that the field was "used only by RUMBLE's own preview/
        export bookkeeping". It is not -- `apo_config.render_config_body()`
        writes it out as a real, unconditional `Convolution:` line. So a
        multi-rate profile rendered a bare `Convolution:` AND an exhaustive
        `If/ElseIf/Else` block that convolved again, and APO "adds a
        convolver" per line: the IR was applied TWICE, doubling every dB of
        the correction (a -16.8 dB point became -33.6 dB). The conditional's
        `Else:` branch already covers every sample rate, so the bare line was
        pure duplication. Multi-rate profiles now carry the conditional ONLY;
        `_primary_convolution()` is how the bookkeeping finds their IR."""
        stored: dict[int, Path] = {}
        for rate, payload in ir_by_rate.items():
            path = self._store_ir_file(f"{_slugify(name)}_{rate}.wav", payload)
            if path is not None:
                stored[rate] = path
        if not stored:
            return None

        rates_sorted = sorted(stored)
        default_path = stored.get(default_rate) or stored[rates_sorted[0]]

        if len(stored) == 1:
            # One IR, no choice to make: a plain unconditional line.
            config = ProfileConfig(convolution=f"{IR_SUBDIR}/{default_path.name}")
        else:
            config = ProfileConfig()
            lines = []
            for i, rate in enumerate(rates_sorted):
                kw = "If" if i == 0 else "ElseIf"
                lines.append(f"{kw}: sampleRate == {rate}")
                lines.append(f"  Convolution: {IR_SUBDIR}/{stored[rate].name}")
            lines.append("Else:")
            lines.append(f"  Convolution: {IR_SUBDIR}/{default_path.name}")
            lines.append("EndIf:")
            config.conditional_blocks.append("\n".join(lines))

        return self.create_profile(name, description=description, tags=tags, config=config)

    def export_profile_bundle(self, profile_id: str, dest_path: Path) -> bool:
        """Export profile + impulse response(s) as a zip: `<id>.txt` at the root
        referencing `irs/<ir name>`. A multi-rate profile carries every IR its
        sample-rate conditional can pick, not just the default one. Fails when
        an IR file cannot be found — callers should offer plain
        `export_profile` in that case."""
        meta = self.get_profile(profile_id)
        config = self.load_profile_config(profile_id)
        if meta is None or config is None:
            return False
        ir_paths: list[Path] = []
        for ref in self._all_convolution_refs(config):
            resolved = self.resolve_ir_path(ref)
            if resolved is None:
                return False
            if resolved not in ir_paths:
                ir_paths.append(resolved)
        if not ir_paths:
            return False
        # Only normalise the bare line when there IS one: writing it on a
        # conditional-only profile would re-create the double-convolution.
        if config.convolution:
            config.convolution = f"{IR_SUBDIR}/{ir_paths[0].name}"
        doc = self._doc_for_meta(meta, config)
        doc.readonly = False  # the exported copy should never be locked
        doc.builtin = False   # ...nor system protected
        try:
            with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr(f"{meta.id}.txt", profile_file.render_profile_text(doc))
                for ir_path in ir_paths:
                    bundle.write(ir_path, f"{IR_SUBDIR}/{ir_path.name}")
        except OSError:
            return False
        return True

    def export_profile(self, profile_id: str, dest_path: Path) -> bool:
        meta = self.get_profile(profile_id)
        config = self.load_profile_config(profile_id)
        if meta is None or config is None:
            return False
        doc = self._doc_for_meta(meta, config)
        doc.readonly = False  # the exported copy should never be locked
        doc.builtin = False   # ...nor system protected
        try:
            Path(dest_path).write_text(profile_file.render_profile_text(doc), encoding="utf-8")
        except OSError:
            return False
        return True

    # ── listener slots ──────────────────────────────────────────────

    def get_active_correction(self) -> "listener_correction.EarCorrection":
        """The active Listener Slot's current correction curve, cached until
        invalidated (slot switch, or a caller reporting new Test A data via
        invalidate_listener_correction_cache()). Flat while the user has
        switched the correction off (settings toggle) -- the underlying slot
        data stays saved and cached."""
        if not self.settings.get("listener_correction_enabled", True):
            return listener_correction.NO_CORRECTION
        if (self._listener_correction_cache is None
                or self._listener_correction_cache_slot != self.active_listener_slot):
            curve = listener_slots.effective_imbalance_curve(self.data_dir, self.active_listener_slot)
            self._listener_correction_cache = listener_correction.derive_correction(curve)
            self._listener_correction_cache_slot = self.active_listener_slot
        return self._listener_correction_cache

    def invalidate_listener_correction_cache(self) -> None:
        """Call after saving new Test A data for the active slot (see
        hearing_dialog.py) so the next render picks up the fresh average."""
        self._listener_correction_cache = None

    def set_active_listener_slot(self, slot_id: int) -> None:
        """Switch the session's active Listener Slot. A session control, same
        category as Pre-Amp/Auto (design spec §6): updates the live
        render/preview immediately, never marks a profile dirty, never
        persists beyond this process (see _load() resetting to
        settings["default_listener_slot"] on every launch)."""
        if slot_id not in listener_slots.SLOT_IDS:
            raise ValueError(
                f"set_active_listener_slot(): invalid slot {slot_id!r}, "
                f"expected one of {listener_slots.SLOT_IDS}")
        if slot_id == self.active_listener_slot:
            return
        self.active_listener_slot = slot_id
        self._listener_correction_cache = None
        if not self._previewing:
            meta = self.get_profile(self._active_profile_id) if self._active_profile_id else None
            self._write_active_pointer(meta)
        for cb in list(self.on_listener_changed):
            try:
                cb(slot_id)
            except Exception:
                pass

    # ── B1 session tone-trim layer ────────────────────────────────────

    def set_session_trim(self, **values) -> None:
        """Session control (same category as set_active_listener_slot(),
        design spec §6): updates session_trim in place and refreshes the
        live render immediately. Never marks a profile dirty, never
        persisted -- resets to flat on every launch (see __init__)."""
        self.session_trim.update(values)
        self.refresh_render_layers()

    def _apply_tone_trim(self, config: "apo_config.ProfileConfig") -> "apo_config.ProfileConfig":
        """The one place render-time session tone trim happens -- called
        from _apply_render_layers(), same choke points as the other
        layers."""
        curve = tone_trim.derive_trim_curve(**self.session_trim)
        return tone_trim.compose_profile(
            config, curve, sample_rate=self.effective_sample_rate(),
            ir_response=self._resolve_ir_response(config))

    def bake_session_trim(self, config: "apo_config.ProfileConfig") -> "apo_config.ProfileConfig":
        """Applies the CURRENT session tone trim to *config* as real
        GraphicEQ content -- the "Bake into profile..." escape hatch
        (roadmap B1). Same composition _apply_tone_trim() uses at render
        time, exposed publicly so the UI can write it into a profile's
        saved editor state (S5) rather than only the live render, without
        reaching into the private IR-resolution/render-layer helpers
        directly."""
        return self._apply_tone_trim(config)

    def _resolve_ir_response(self, config: "apo_config.ProfileConfig") -> "np.ndarray | None":
        """Pre-sampled convolution IR response curve for *config*, or None --
        shared by every render-time layer that recomputes preamp via
        preamp_calc.calculate_auto_preamp() (listener correction, the ISO
        226 loudness layer)."""
        convolution = self._primary_convolution(config)
        if not convolution:
            return None
        ir_path = self.resolve_ir_path(convolution)
        if ir_path is None:
            return None
        result = ir_analysis.get_ir_response(ir_path)
        return result.response_db if result is not None else None

    def _apply_listener_correction(self, config: "apo_config.ProfileConfig") -> "apo_config.ProfileConfig":
        """The one place render-time listener correction happens -- called
        from _apply_render_layers() so live edits, ABX playback (via
        preview_config) and canonical activation all compose the same way
        (design spec §5, §5.2)."""
        correction = self.get_active_correction()
        if correction.is_flat:
            return config
        return listener_correction.compose_profile(
            config, correction, sample_rate=self.effective_sample_rate(),
            ir_response=self._resolve_ir_response(config))

    # ── ISO 226 loudness layer ───────────────────────────────────────

    def get_active_loudness_correction(self) -> "loudness_layer.LoudnessCorrection":
        """The current ISO 226 loudness-compensation curve. Unlike
        get_active_correction() this is never cached -- it reads the live
        WASAPI master volume on every call (a single cheap COM read) so it
        stays in sync with the user's volume knob, which is the entire
        point of a "volume-linked" layer (Rumble 3.5)."""
        if not self.settings.get("iso226_loudness_enabled"):
            return loudness_layer.NO_LOUDNESS_CORRECTION
        reference_volume_db = self.settings.get("iso226_reference_volume_db")
        current_volume_db = master_volume.get_master_volume_db()
        return loudness_layer.derive_loudness_correction(
            reference_phon=float(self.settings.get("iso226_reference_phon", 80.0)),
            reference_volume_db=reference_volume_db,
            current_volume_db=current_volume_db)

    def calibrate_loudness_reference(self) -> bool:
        """Capture the current WASAPI master volume as the ISO 226 layer's
        reference point -- "this is my normal listening volume, matching
        settings['iso226_reference_phon']". Mirrors upstream
        LoudnessCorrection:'s own Reference-Level/Offset concept
        (docs/RUMBLE_APP_SPECIFIC_KB.md §14.1) but sourced from a live
        measurement rather than a typed-in number. Returns False (no-op,
        setting left unchanged) if the current volume can't be read."""
        current = master_volume.get_master_volume_db()
        if current is None:
            return False
        self.settings["iso226_reference_volume_db"] = current
        self.save()
        return True

    def calibrate_loudness_reference_from_conversation(
            self, conversation_volume_db: float | None = None,
            measured_level_db: float | None = None) -> bool:
        """Anchor the ISO 226 layer from the Ear Calibration volume step:
        the listener has just adjusted their volume until the reference
        clip's target segment matched (~75 phon, see
        loudness_layer.REFERENCE_CLIP_TARGET_PHON), so the configured
        mix-reference level (settings['iso226_reference_phon'], default 80)
        sits (reference_phon - anchor_phon) dB further up the volume scale
        (1 dB ~= 1 phon), where anchor_phon is *measured_level_db* if the
        listener had a real SPL meter reading of that segment, else the
        assumed REFERENCE_CLIP_TARGET_PHON default. Extrapolating rather
        than capturing directly keeps reference_phon's "level the content
        was mixed to sound balanced at" meaning intact -- and is
        better-anchored than the plain Settings button, since the volume
        was just deliberately set against a known-level clip rather than
        being whatever it happened to be. Reads the live volume when
        *conversation_volume_db* is None; returns False (no-op) when no
        reading is available either way."""
        if conversation_volume_db is None:
            conversation_volume_db = master_volume.get_master_volume_db()
        if conversation_volume_db is None:
            return False
        reference_phon = float(self.settings.get("iso226_reference_phon", 80.0))
        self.settings["iso226_reference_volume_db"] = (
            loudness_layer.reference_volume_from_conversation(
                conversation_volume_db, reference_phon, measured_phon=measured_level_db))
        self.save()
        return True

    def refresh_render_layers(self) -> bool:
        """Re-render the active profile's composed output so the
        volume-linked loudness layer picks up the CURRENT Windows volume --
        the volume-poll adapter's entry point (see
        rumble/ui/loudness_volume_adapter.py). Refuses (returns False)
        while a preview is live (re-writing the active pointer would
        clobber a live-tuning preview or an ABX trial mid-listen), while
        bypassed (the pointer intentionally holds the bypass render), or
        with no active profile."""
        if self._previewing or self.settings.get("bypassed"):
            return False
        meta = self.get_active_profile()
        if meta is None:
            return False
        self._write_active_pointer(meta)
        return True

    def _apply_loudness_correction(self, config: "apo_config.ProfileConfig") -> "apo_config.ProfileConfig":
        """The one place render-time ISO 226 loudness compensation happens --
        called from _apply_render_layers(), same choke points as listener
        correction."""
        correction = self.get_active_loudness_correction()
        if correction.is_flat:
            return config
        return loudness_layer.compose_profile(
            config, correction, sample_rate=self.effective_sample_rate(),
            ir_response=self._resolve_ir_response(config))

    def _apply_render_layers(self, config: "apo_config.ProfileConfig") -> "apo_config.ProfileConfig":
        """The one place every render-time correction/compensation layer is
        composed, in order -- called from both preview_config() and
        _write_active_pointer() so live edits, ABX playback (via
        preview_config) and canonical activation all reflect the same
        stack. Listener correction runs first (it may expand a linked "all"
        section into L/R); the tone-trim (B1) and ISO 226 loudness layers
        then apply uniformly to whatever sections exist at that point --
        all three are additive dB curves merged via
        autoeq_merge.sum_curves(), so composition order among them doesn't
        change the physical result, only which section shapes get expanded
        first. Loudness matching (A2) runs last: a flat, uniform preamp
        delta on top of whatever curve the earlier layers already produced,
        so it adjusts absolute level without touching their shape."""
        config = self._apply_listener_correction(config)
        config = self._apply_tone_trim(config)
        config = self._apply_loudness_correction(config)
        config = self._apply_loudness_matching(config)
        return config

    # ── A2 profile-switch loudness matching ──────────────────────────

    # abx.estimated_gain_db() of a fully flat (EQ-off) config is exactly
    # 0 dB -- no channel sections means no power samples, so its
    # empty-config fallback returns the config's preamp, and a flat
    # config's preamp is 0. Anchoring here means every profile also
    # matches "EQ off", so a bypass toggle doesn't jump loudness either.
    _LOUDNESS_MATCH_ANCHOR_DB = 0.0

    # Matches abx.py's own _MATCH_TOLERANCE_DB: APO renders Preamp to a
    # single decimal place (preamp_calc.py quantises to the same 0.1 dB
    # grid), so a correction finer than this isn't representable in the
    # rendered config anyway -- nothing to gain from a tighter tolerance.
    _LOUDNESS_MATCH_TOLERANCE_DB = 0.1

    def _apply_loudness_matching(self, config: "apo_config.ProfileConfig") -> "apo_config.ProfileConfig":
        """The one place render-time profile-switch loudness matching
        happens -- always the LAST render layer (see _apply_render_layers()
        docstring), so the flat preamp delta it applies pulls down whatever
        absolute level the listener-correction and ISO 226 layers already
        produced without touching their shape (a uniform dB shift doesn't
        change relative frequency balance).

        Reduction-only, matching abx.level_match's own principle: a config
        already at or below the flat anchor is left untouched (never
        boosted -- boosting risks clipping and eats headroom).

        The broadband-gain estimate is cached against the composed config's
        own rendered text (a cheap string compare) so the volume-poller's
        refresh_render_layers() ticks don't re-run the numpy estimate every
        second when the composed content hasn't actually changed."""
        if not self.settings.get("profile_loudness_matching"):
            return config
        fingerprint = apo_config.render_config_body(config)
        if fingerprint != self._loudness_matching_cache_key:
            self._loudness_matching_cache_gain = abx.estimated_gain_db(
                config, self.effective_sample_rate())
            self._loudness_matching_cache_key = fingerprint
        diff = self._loudness_matching_cache_gain - self._LOUDNESS_MATCH_ANCHOR_DB
        if diff < self._LOUDNESS_MATCH_TOLERANCE_DB:
            return config
        return replace(config, preamp=(config.preamp or 0.0) - diff)

    # ── B2 headphone crossfeed ────────────────────────────────────────

    def _global_crossfeed_spec(self) -> "crossfeed.CrossfeedSpec | None":
        """The global crossfeed VALUE (preset/strength/custom, edited in
        Settings), or None when that value is inert (a named preset at zero
        strength). Whether it actually applies -- as a fallback for un-set
        profiles, as an override for every profile, or not at all -- is the
        `crossfeed_mode` question answered in _effective_crossfeed()."""
        spec = crossfeed.CrossfeedSpec(
            preset=str(self.settings.get("crossfeed_preset", crossfeed.DEFAULT_PRESET)),
            strength=float(self.settings.get("crossfeed_strength", 100)),
            custom_gain_db=float(self.settings.get("crossfeed_custom_gain_db", -6.0)),
            custom_freq_hz=float(self.settings.get("crossfeed_custom_freq_hz", 700.0)),
            custom_delay_us=float(self.settings.get("crossfeed_custom_delay_us", 300.0)),
        )
        return spec if spec.is_active() else None

    def _effective_crossfeed(self, own: "crossfeed.CrossfeedSpec | None") -> "crossfeed.CrossfeedSpec | None":
        """Which crossfeed the render layer applies for a profile whose own
        selection is *own*, decided by the machine-level `crossfeed_mode`:
          "off"     -> nothing,
          "global"  -> the global value for every profile (override),
          "profile" -> the profile's own value, falling back to the global
                       value when it sets none (the default)."""
        mode = str(self.settings.get("crossfeed_mode", "profile"))
        if mode == "off":
            return None
        if mode == "global":
            return self._global_crossfeed_spec()
        return own if own is not None else self._global_crossfeed_spec()

    def _render_crossfeed_block(self, config: "apo_config.ProfileConfig",
                                spec: "crossfeed.CrossfeedSpec | None") -> str:
        """The one place render-time headphone crossfeed happens. Unlike
        every other layer above, this is not composed into the
        ProfileConfig itself -- it is raw text appended after the rest of
        the rendered body (crossfeed.py's module docstring; KB §17.4).
        Render-only, storage never mutated. *spec* is the specific profile's
        own crossfeed selection (per-profile, not a machine setting). Returns
        "" when *spec* is None/inert, or *config*'s channel sections aren't a
        plain stereo layout -- silently skipped, like every other layer's
        flat/no-op case."""
        if spec is None or not spec.is_active():
            return ""
        if not crossfeed.can_crossfeed(config.channel_sections):
            return ""
        return crossfeed.render_spec(spec)

    def _render_layered_body(self, config: "apo_config.ProfileConfig",
                             crossfeed_spec: "crossfeed.CrossfeedSpec | None" = None) -> str:
        """Composes every ProfileConfig-level render layer
        (_apply_render_layers()) and renders it to text, then appends the
        crossfeed block (if any) after that text -- the one place both
        halves of "the fully rendered active/preview body" come together.
        Shared by preview_config() and _write_active_pointer() so both
        reflect the same stack. *crossfeed_spec* is the profile being
        rendered's own crossfeed (per-profile); None ⇒ no crossfeed."""
        composed = self._apply_render_layers(config)
        body = apo_config.render_config_body(composed)
        crossfeed_block = self._render_crossfeed_block(composed, crossfeed_spec)
        if crossfeed_block:
            body = body.rstrip("\n") + "\n\n" + crossfeed_block
        return body

    # ── D1a per-device profile dispatch ───────────────────────────────
    #
    # KB §20.3 (verify_device_dispatch_live.py, confirmed on real hardware):
    # Equalizer APO claims a device by the FIRST matching `Device:` block in
    # file order -- not the last -- so assignment blocks MUST render before
    # the global-fallback `Device: all` block. That ordering is what the
    # code below already does; do not reorder it.

    def _any_render_layer_active(self, meta: "ProfileMeta | None" = None) -> bool:
        """"Could any render layer produce something" check, shared by the
        single-profile fast path and every dispatch branch. The machine-level
        layers (correction, trim, ISO 226 loudness, loudness matching) are the
        same for every branch; crossfeed is per-profile, so pass the branch's
        *meta* to test its own crossfeed (defaults to the active profile)."""
        own = (meta.crossfeed if meta is not None
               else (self.get_active_profile().crossfeed if self.get_active_profile() else None))
        cf = self._effective_crossfeed(own)
        return (not self.get_active_correction().is_flat
                or any(self.session_trim.values())
                or not self.get_active_loudness_correction().is_flat
                or bool(self.settings.get("profile_loudness_matching"))
                or (cf is not None and cf.is_active()))

    def _device_scratch_filename(self, guid: str) -> str:
        """rumble_device_<8-char-guid-hash>.txt -- a stable, filesystem-safe
        name for a per-device composed scratch file (endpoint GUIDs contain
        braces/hyphens, so the raw string isn't a safe filename)."""
        digest = hashlib.sha256(guid.encode("utf-8")).hexdigest()[:8]
        return f"{DEVICE_SCRATCH_PREFIX}{digest}.txt"

    def _device_for_guid(self, guid: str) -> "audio_devices.ApoDevice | None":
        """Resolve an endpoint GUID to its full ApoDevice entry (pattern +
        flow), scanning audio_devices.list_apo_devices() fresh each render --
        never cached, since a device can be unhooked/rehooked between
        renders. None when the endpoint isn't currently APO-hooked."""
        for device in audio_devices.list_apo_devices():
            if device.guid == guid:
                return device
        return None

    def _device_pattern_for_guid(self, guid: str) -> str:
        """Resolve an endpoint GUID to its `Device:` pattern via
        audio_devices.ApoDevice.pattern. Falls back to the raw GUID string
        when the endpoint can't be resolved (unhooked, removed, or no
        registry access) -- APO's own Device: matching haystack is
        "deviceName connectionName guid" (audio_devices.py's module
        docstring), so the bare GUID still selects the right endpoint on
        its own rather than silently matching nothing."""
        device = self._device_for_guid(guid)
        return device.pattern if device is not None else guid

    def _valid_device_assignments(self) -> dict[str, ProfileMeta]:
        """settings["device_profile_assignments"], resolved to ProfileMeta
        and dropped when the assigned profile no longer exists -- a stale
        assignment (deleted profile) falls through to the global active
        profile via the dispatch's own trailing `Device: all` block rather
        than erroring or rendering nothing for that endpoint.

        Capture (microphone) endpoints are dropped the same way while
        settings["show_microphone_profiles"] is off: hiding microphone
        profiles disables microphone EQ outright, it does not merely hide
        it from the library. The assignment itself is left untouched in
        settings, so re-enabling the toggle brings the mic's profile back
        exactly as it was. Doing it HERE (rather than in the dispatch
        renderer) also covers the no-assignments fast path in
        _write_active_pointer(): with the only assignment suppressed the
        dict goes empty and the render collapses to the plain single-
        profile pointer, with no capture branch left at all."""
        assignments = self.settings.get("device_profile_assignments") or {}
        show_mic = bool(self.settings.get("show_microphone_profiles", False))
        resolved: dict[str, ProfileMeta] = {}
        for guid, profile_id in assignments.items():
            dev_meta = self.get_profile(profile_id)
            if dev_meta is None:
                continue
            if not show_mic:
                device = self._device_for_guid(guid)
                # Unresolvable endpoints (unhooked/removed) keep D1's
                # flow-unknown handling: nothing is guessed, so nothing is
                # suppressed either.
                if device is not None and device.flow == "Capture":
                    continue
            resolved[guid] = dev_meta
        return resolved

    def _dispatch_branch_target(self, meta: ProfileMeta, scratch_filename: str) -> str:
        """Resolve the Include: target for one dispatch branch (the global
        fallback, or one per-device assignment): the profile's own file
        directly when no render layer is active (byte-identical to the
        pre-D1a single-profile fast path), else a composed scratch file
        under rumble/ -- LISTENER_ACTIVE_FILENAME for the fallback branch
        (unchanged name/header, so the zero-assignment render stays
        byte-identical), scratch_filename for a per-device branch. Every
        branch goes through this one path so render layers (listener
        correction, trim, ISO 226 loudness, loudness matching, crossfeed)
        compose identically regardless of which branch a device lands in."""
        if not self._any_render_layer_active(meta):
            return meta.file
        config = self.load_profile_config(meta.id)
        if config is None:
            return meta.file
        header = ("# RUMBLE listener-corrected scratch file — temporary, safe to delete\n\n"
                   if scratch_filename == LISTENER_ACTIVE_FILENAME
                   else "# RUMBLE per-device scratch file — temporary, safe to delete\n\n")
        scratch_path = self.rumble_dir / scratch_filename
        self.rumble_dir.mkdir(parents=True, exist_ok=True)
        scratch_path.write_text(
            header + self._render_layered_body(config, self._effective_crossfeed(meta.crossfeed)),
            encoding="utf-8")
        return f"{RUMBLE_SUBDIR}/{scratch_filename}"

    def _cleanup_stale_device_scratch(self, active_names: set[str]) -> None:
        """Remove rumble_device_*.txt scratch files that don't correspond to
        a branch this render actually produced (assignment removed, its
        endpoint unhooked, or render layers went flat so the branch now
        includes its profile file directly) -- always safe-to-delete render
        scratch, never user data."""
        if not self.rumble_dir.is_dir():
            return
        try:
            for path in self.rumble_dir.glob(f"{DEVICE_SCRATCH_PREFIX}*.txt"):
                if path.name not in active_names:
                    try:
                        path.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    def _render_dispatch(
            self, fallback_meta: ProfileMeta,
            assignments: dict[str, ProfileMeta]) -> str:
        """Render rumble_active.txt as a per-device dispatch: one
        `Device: <pattern>` block per assignment (each Include-ing that
        profile's branch target), then an explicit `Device: all` block
        Include-ing the global active profile's branch target as the
        fallback for every unassigned endpoint. Assignment blocks are
        written before the fallback -- required by the confirmed
        first-claim, file-order-first-wins semantics above; reordering
        would silently break per-device isolation.

        D2: an assignment whose endpoint currently resolves to a Capture
        (microphone) flow gets a `Stage: capture` line ahead of its own
        `Device:` line (apo_config.render_config_body()'s own canonical
        Stage-then-Device order, reused here since Stage: is parsed/
        rendered structurally per the S21->S22 Amendment) -- this scopes
        the assignment to the capture path only, so a mic profile never
        also applies to playback. A Render-flow (or unresolved) assignment
        gets no Stage: line, unchanged from D1. NOTE: Stage:+Device: used
        together in one block has not been confirmed on real hardware (KB
        Sec 20's spike only exercised Device: alone on Render endpoints) --
        see the S22 BLOCKING follow-up in the progress file."""
        blocks: list[str] = []
        active_scratch_names: set[str] = set()
        for guid, dev_meta in assignments.items():
            device = self._device_for_guid(guid)
            pattern = device.pattern if device is not None else guid
            scratch_name = self._device_scratch_filename(guid)
            target = self._dispatch_branch_target(dev_meta, scratch_name)
            if target == f"{RUMBLE_SUBDIR}/{scratch_name}":
                active_scratch_names.add(scratch_name)
            stage_prefix = "Stage: capture\n" if device is not None and device.flow == "Capture" else ""
            blocks.append(f"{stage_prefix}Device: {pattern}\nInclude: {target}\n")
        fallback_target = self._dispatch_branch_target(fallback_meta, LISTENER_ACTIVE_FILENAME)
        blocks.append(f"Device: all\nInclude: {fallback_target}\n")
        self._cleanup_stale_device_scratch(active_scratch_names)
        return (
            "# RUMBLE active profile dispatch — do not edit by hand\n"
            "# The first matching per-device block wins (file order, not\n"
            "# last) -- assignment blocks must stay before the trailing\n"
            "# fallback block. Capture (microphone) assignments carry a\n"
            "# leading Stage: capture line so they never apply to playback.\n\n"
            + "\n".join(blocks))

    # ── activation / bypass ──────────────────────────────────────────

    def _write_active_pointer(self, meta: ProfileMeta | None) -> None:
        self._previewing = False  # a canonical pointer write supersedes any preview
        pointer_path = self.config_path / ACTIVE_POINTER_FILENAME
        try:
            self.config_path.mkdir(parents=True, exist_ok=True)
            if self.settings.get("bypassed"):
                # Bypass replaces the whole dispatch -- no per-device blocks,
                # nothing to compose, so any prior device scratch is stale.
                pointer_path.write_text(apo_config.render_bypass(), encoding="utf-8")
                self._cleanup_stale_device_scratch(set())
            elif meta is not None:
                assignments = self._valid_device_assignments()
                if not assignments:
                    # No assignments: identical render to pre-D1a (single
                    # Include:, no Device: line) -- regression-critical.
                    target = self._dispatch_branch_target(meta, LISTENER_ACTIVE_FILENAME)
                    pointer_path.write_text(apo_config.render_include(target), encoding="utf-8")
                    self._cleanup_stale_device_scratch(set())
                else:
                    pointer_path.write_text(self._render_dispatch(meta, assignments), encoding="utf-8")
            else:
                pointer_path.write_text(apo_config.render_bypass(), encoding="utf-8")
                self._cleanup_stale_device_scratch(set())
        except OSError:
            pass

    def refresh_fits_for_sample_rate(self, new_rate: int | None = None) -> list[str]:
        """Re-solve every curve-fitted profile's residual for the current device
        sample rate, so an imported/measured profile stays exact when the rate
        changes under it.

        Only profiles carrying a `Fit Sample Rate` header are touched — a
        hand-authored profile's GraphicEQ line is deliberate tuning, not a
        residual, and must never be rewritten (see
        autoeq_merge.retune_config_for_sample_rate). Locked (readonly) profiles
        ARE refreshed: this preserves what the profile means rather than changing
        it, which is the whole point of the lock.

        Returns the ids actually rewritten. The active profile is re-applied so
        the change reaches APO immediately.
        """
        rate = int(new_rate or self.effective_sample_rate())
        changed: list[str] = []
        for meta in list(self.profiles):
            if not meta.fit_sample_rate or meta.fit_sample_rate == rate:
                continue
            config = self.load_profile_config(meta.id)
            if config is None:
                continue
            if not autoeq_merge.retune_config_for_sample_rate(
                    config, meta.fit_sample_rate, rate):
                continue
            meta.fit_sample_rate = rate
            if self._write_profile_file(meta, config):
                changed.append(meta.id)

        if changed:
            self.save()
            active = self.get_active_profile()
            if active is not None and active.id in changed:
                self._write_active_pointer(active)
        return changed

    def activate_profile(self, profile_id: str) -> bool:
        meta = self.get_profile(profile_id)
        if meta is None:
            return False
        for profile in self.profiles:
            profile.active = (profile.id == profile_id)
        self._active_profile_id = profile_id
        self.settings["bypassed"] = False
        self.settings["last_active_profile"] = profile_id
        self.save()
        self._write_active_pointer(meta)
        for cb in list(self.on_profile_activated):
            try:
                cb(profile_id)
            except Exception:
                pass
        return True

    # ── live preview ─────────────────────────────────────────────────

    @property
    def is_previewing(self) -> bool:
        return self._previewing

    def preview_config(self, config: ProfileConfig,
                       crossfeed_spec: "crossfeed.CrossfeedSpec | None" = None) -> bool:
        """Make Equalizer APO play *config* (typically unsaved editor state)
        immediately: write it to a scratch file in the rumble dir and point the
        active include at it. *crossfeed_spec* is the edited profile's own
        crossfeed (per-profile), so a live crossfeed change previews too.
        Nothing saved is touched — any canonical pointer
        write (activate, bypass, save) supersedes the preview. The active
        Listener Slot's correction and the ISO 226 loudness layer (if
        active) are composed in first, and the crossfeed block (if enabled
        and the layout is stereo) is appended after -- this is the same
        choke point ABXRunner.present() plays through, so ABX playback
        reflects all of them too (design spec §5.2; Rumble 3.5; roadmap B2)."""
        if self.settings.get("bypassed"):
            return False
        preview_path = self.rumble_dir / PREVIEW_FILENAME
        self._preview_seq += 1
        text = (f"# RUMBLE live preview — temporary scratch file, safe to delete "
                f"(seq {self._preview_seq})\n\n"
                + self._render_layered_body(config, self._effective_crossfeed(crossfeed_spec)))
        try:
            self.rumble_dir.mkdir(parents=True, exist_ok=True)
            preview_path.write_text(text, encoding="utf-8")
            pointer_path = self.config_path / ACTIVE_POINTER_FILENAME
            pointer_path.write_text(
                apo_config.render_include(f"{RUMBLE_SUBDIR}/{PREVIEW_FILENAME}"),
                encoding="utf-8")
        except OSError:
            return False
        self._previewing = True
        return True

    def end_preview(self) -> None:
        """Point Equalizer APO back at the saved active profile (or bypass)."""
        if self._previewing:
            self._write_active_pointer(self.get_active_profile())

    def is_bypassed(self) -> bool:
        return bool(self.settings.get("bypassed"))

    def set_bypass(self, bypassed: bool) -> None:
        self.settings["bypassed"] = bool(bypassed)
        self.save()
        self._write_active_pointer(self.get_active_profile())

    def set_detected_sample_rate(self, rate: int | None) -> None:
        """Populate (or clear, with None) the in-memory detected-sample-rate
        cache used by effective_sample_rate() in "auto" mode. Called by
        MainWindow._refresh_device_format() to keep the cache in sync with
        the real default device, reusing that single live query instead of
        letting effective_sample_rate() poll COM itself on every call."""
        self._detected_sample_rate = rate

    def effective_sample_rate(self) -> int:
        """Processing sample rate (Hz) to use for response/preamp maths and
        for displaying sample-denominated delays in ms.

        When "sample_rate_mode" is "auto" (the default), returns the cached
        detected rate (kept warm by set_detected_sample_rate(), populated
        from the live device-format watcher/adapter) when available — this
        is the common case and involves no COM call at all. If the cache is
        empty (nothing has populated it yet, e.g. very early at startup or a
        test that never wires up a watcher), falls back to a one-off live
        query via `device_format.get_default_device_format()` and, on
        success, warms the cache so subsequent calls on this instance are
        cache hits. Falls back to the manual "sample_rate" setting when
        detection fails (no default device, COM error, offscreen/CI
        environment with no audio stack) — a failed detection is NOT cached,
        so the next call retries live rather than baking in a transient
        hiccup. This method never raises. When "sample_rate_mode" is
        "manual", the manual "sample_rate" setting is used directly without
        ever touching device_format/COM."""
        manual_rate = int(self.settings.get("sample_rate", 48000) or 48000)
        if self.settings.get("sample_rate_mode") != "auto":
            return manual_rate
        if self._detected_sample_rate is not None:
            return self._detected_sample_rate
        detected = device_format.get_default_device_format()
        if detected is not None:
            self._detected_sample_rate = detected.sample_rate
            return detected.sample_rate
        return manual_rate

    # ── first-run / config.txt integration ───────────────────────────

    def _render_managed_config(self) -> str:
        """The config.txt RUMBLE owns: a marker block (for ownership
        detection + a user breadcrumb) plus the single active-profile include,
        so RUMBLE is the sole authority over the APO chain."""
        return (
            "# ----------------------------------------------\n"
            "# This Equalizer APO config is managed by RUMBLE.\n"
            "# Do not edit by hand - use the RUMBLE app.\n"
            f"# Your original config was saved as: {CONFIG_FILENAME}{CONFIG_BACKUP_SUFFIX}\n"
            f"{MANAGED_MARKER} {MANAGED_CONFIG_VERSION}\n"
            "# ----------------------------------------------\n"
            f"Include: {ACTIVE_POINTER_FILENAME}\n"
        )

    def ensure_first_run(self) -> dict:
        """Take (and keep) ownership of the Equalizer APO config.

        RUMBLE owns ``config.txt`` outright so its profiles truly override rather
        than stack on top of pre-existing APO content. On the first run the
        existing config is saved verbatim to ``config.txt.rumble_backup`` and
        replaced with a managed marker + the active-profile include. Safe to call
        every launch: if the marker is already present it is a no-op; if the
        marker is gone but a backup exists, config.txt was clobbered by another
        tool or an APO reinstall (drift) and ownership is re-asserted. The
        displaced foreign content is stashed for the boot reconcile to offer as
        an import. Returns a dict describing what happened.
        """
        actions: dict = {"backed_up": False, "owned_config": False,
                         "reasserted": False, "drift_backed_up": False,
                         "foreign_includes": [], "config_accessible": True}

        # The user handed the config back to APO (see release_control); don't
        # re-take ownership until they resume.
        if not self.settings.get("apo_managed", True):
            return actions

        config_path = self.config_path
        try:
            config_path.mkdir(parents=True, exist_ok=True)
            self.rumble_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # The APO config dir is missing and we can't create it — e.g. it
            # lives under Program Files and we lack the rights to recreate it
            # (as happens if the folder was deleted out from under us). Nothing
            # the app does works without it, so report up rather than crash.
            actions["config_accessible"] = False
            return actions

        config_txt = config_path / CONFIG_FILENAME
        backup_path = config_txt.with_suffix(config_txt.suffix + CONFIG_BACKUP_SUFFIX)
        pointer_path = config_path / ACTIVE_POINTER_FILENAME

        try:
            current = config_txt.read_text(encoding="utf-8") if config_txt.exists() else ""
        except OSError:
            current = ""

        owns_marker = MANAGED_MARKER in current
        foreign_body = _foreign_config_body(current)

        if not owns_marker or foreign_body:
            # We don't own config.txt cleanly: first run, drift (our marker was
            # overwritten), or a foreign tool wrote its own directives alongside
            # RUMBLE's block (marker present but extra content). Capture the
            # foreign content, preserve it, then (re-)assert sole ownership.
            displaced = _strip_rumble_includes(current)
            actions["foreign_includes"] = _foreign_includes(displaced)
            if displaced.strip():
                if not owns_marker and not backup_path.exists():
                    # Genuine first run (or config.txt clobbered back to a
                    # non-RUMBLE file with no backup yet): the current content is
                    # the user's true pre-RUMBLE config. Save it byte-for-byte as
                    # the sacred backup — restore on "Hand back to APO" / uninstall
                    # must reproduce that file exactly.
                    try:
                        shutil.copy2(config_txt, backup_path)
                        actions["backed_up"] = True
                    except OSError:
                        pass
                else:
                    # Later displacement: config.txt was overwritten after we
                    # already hold a sacred backup, or a tool appended to our
                    # block. Never touch the sacred backup; preserve the newly
                    # displaced content under a timestamped drift file so nothing
                    # is lost even if the user declines the import offer.
                    try:
                        drift_path = config_path / (
                            f"{CONFIG_FILENAME}{CONFIG_DRIFT_SUFFIX}-"
                            f"{datetime.now().strftime('%Y%m%d-%H%M%S')}")
                        drift_path.write_text(displaced, encoding="utf-8")
                        actions["drift_backed_up"] = True
                    except OSError:
                        pass
                self._displaced_config_text = displaced
            # Backup already present and we didn't just create it ⇒ this was a
            # re-assert after drift, not a first run.
            actions["reasserted"] = backup_path.exists() and not actions["backed_up"]
            try:
                config_txt.write_text(self._render_managed_config(), encoding="utf-8")
                actions["owned_config"] = True
            except OSError:
                pass

        if not pointer_path.exists():
            self._write_active_pointer(self.get_active_profile())

        return actions

    def release_control(self) -> bool:
        """Hand the Equalizer APO config back to its pre-RUMBLE state: restore
        the saved original ``config.txt`` and remove RUMBLE's pointer and
        render-scratch files (including any D1a per-device dispatch branches),
        and stop re-taking ownership on launch (until `resume_control`). Never
        touches APO's own files, the config folder, or the rumble/ profile
        library (defaults/, user/, irs/) -- unlike the uninstaller's own
        DelTree of the whole rumble/ tree, this is reversible via
        `resume_control()`, so user profiles must survive it. Exposed in-app
        as "Hand back to APO"."""
        config_path = self.config_path
        config_txt = config_path / CONFIG_FILENAME
        backup_path = config_txt.with_suffix(config_txt.suffix + CONFIG_BACKUP_SUFFIX)
        ok = True
        try:
            if backup_path.exists():
                shutil.copy2(backup_path, config_txt)
            else:
                # RUMBLE created config.txt from scratch (no prior original): leave
                # a valid empty config so APO still has something to load.
                config_txt.write_text("", encoding="utf-8")
        except OSError:
            ok = False
        try:
            (config_path / ACTIVE_POINTER_FILENAME).unlink(missing_ok=True)
        except OSError:
            ok = False
        # PREVIEW_FILENAME/LISTENER_ACTIVE_FILENAME/DEVICE_SCRATCH_PREFIX*.txt
        # all live under rumble_dir (see preview_config()/_dispatch_branch_target()/
        # _device_scratch_filename()), not the config-path root.
        for name in (PREVIEW_FILENAME, LISTENER_ACTIVE_FILENAME):
            try:
                (self.rumble_dir / name).unlink(missing_ok=True)
            except OSError:
                ok = False
        self._cleanup_stale_device_scratch(set())
        self.settings["apo_managed"] = False
        self.save()
        return ok

    def resume_control(self) -> dict:
        """Re-take ownership of the Equalizer APO config after a hand-back
        (re-runs the swap). Returns the same actions dict as ensure_first_run."""
        self.settings["apo_managed"] = True
        self.save()
        return self.ensure_first_run()

    def is_apo_managed(self) -> bool:
        return bool(self.settings.get("apo_managed", True))

    # ── config.txt drift-snapshot restore ──────────────────────────────

    def list_drift_snapshots(self) -> list[DriftSnapshot]:
        """List preserved `config.txt.rumble_drift-<ts>` files, newest first."""
        try:
            files = list(self.config_path.glob(
                f"{CONFIG_FILENAME}{CONFIG_DRIFT_SUFFIX}-*"))
        except OSError:
            files = []
        snapshots = []
        prefix = f"{CONFIG_FILENAME}{CONFIG_DRIFT_SUFFIX}-"
        for path in files:
            ts_str = path.name[len(prefix):]
            try:
                ts = datetime.strptime(ts_str, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            snapshots.append(DriftSnapshot(
                path=path, timestamp=ts, label=ts.strftime("%b %d, %Y %I:%M %p")))
        snapshots.sort(key=lambda s: s.timestamp, reverse=True)
        return snapshots

    def restore_drift_snapshot(self, path: Path) -> bool:
        """Write a drift snapshot's content back into config.txt verbatim and
        stop RUMBLE management (mirrors release_control) so it isn't
        immediately re-captured as a fresh drift event on next launch."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return False
        try:
            (self.config_path / CONFIG_FILENAME).write_text(text, encoding="utf-8")
        except OSError:
            return False
        self.settings["apo_managed"] = False
        self.save()
        return True

    # ── config health / effective-chain viewer ──────────────────────────

    def effective_config_report(self) -> EffectiveConfigReport:
        """Build the read-only config-health view: the live config.txt with
        every Include: chain fully inlined — including RUMBLE's own managed
        tree, unlike ``resolve_includes()``'s import-discovery use, since the
        point here is showing exactly what's actually applying — with each
        line traceable to its source file."""
        config_txt = self.config_path / CONFIG_FILENAME
        try:
            text = config_txt.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        lines = apo_config.resolve_includes_traced(text, self.config_path, CONFIG_FILENAME)
        resolved_text = "\n".join(line.text for line in lines)
        sources: list[str] = []
        for line in lines:
            if line.source not in sources:
                sources.append(line.source)
        conditional_block_count = len(apo_config.parse_config(resolved_text).conditional_blocks)
        return EffectiveConfigReport(
            resolved_text=resolved_text, sources=sources,
            conditional_block_count=conditional_block_count)

    # ── foreign-tool coexistence dashboard ──────────────────────────────

    def coexistence_report(self) -> foreign_tool_detection.CoexistenceReport:
        """Build the read-only coexistence-dashboard view: what other
        Equalizer-APO-consuming tools are (or have been) touching the same
        config, on demand — not live-refreshing, matching
        `effective_config_report()`'s snapshot convention."""
        config_txt = self.config_path / CONFIG_FILENAME
        try:
            current = config_txt.read_text(encoding="utf-8") if config_txt.exists() else ""
        except OSError:
            current = ""
        snapshots = self.list_drift_snapshots()
        return foreign_tool_detection.CoexistenceReport(
            foreign_includes=_foreign_includes(current),
            foreign_raw_directives=_foreign_raw_directives(current),
            drift_snapshot_count=len(snapshots),
            latest_drift_label=snapshots[0].label if snapshots else None,
            running_tools=foreign_tool_detection.detect_running_tools(),
        )

    # ── VST management ──────────────────────────────────────────────

    def vst_report(self) -> vst_registry.VstLibraryReport:
        """Aggregate every VST plugin path referenced across the whole
        library, on demand (not live-refreshing, matching
        `coexistence_report()`'s snapshot convention)."""
        triples = []
        for meta in self.list_profiles():
            config = self.load_profile_config(meta.id)
            if config is not None:
                triples.append((meta.id, meta.name, config))
        return vst_registry.aggregate_vst_usage(triples)

    def relink_vst_plugin(self, old_path: str, new_path: str) -> vst_registry.RelinkResult:
        """Replace *old_path* with *new_path* in every profile that
        references it. A profile that matches but is locked/built-in is
        left untouched and reported in `skipped_protected_ids` --
        `save_profile_config` already refuses protected profiles, this
        just sorts the outcome for the caller."""
        updated: list[str] = []
        skipped: list[str] = []
        for meta in self.list_profiles():
            config = self.load_profile_config(meta.id)
            if config is None:
                continue
            if not vst_registry.apply_relink(config, old_path, new_path):
                continue
            if self.save_profile_config(meta.id, config):
                updated.append(meta.id)
            else:
                skipped.append(meta.id)
        return vst_registry.RelinkResult(
            updated_profile_ids=updated, skipped_protected_ids=skipped)

    def remove_vst_plugin(self, path: str) -> vst_registry.RelinkResult:
        """Drop *path* from every profile that references it (all matching
        `VSTPlugin:` entries, across every channel). A locked/built-in
        profile that matches is left untouched and reported in
        `skipped_protected_ids`, same convention as `relink_vst_plugin`."""
        updated: list[str] = []
        skipped: list[str] = []
        for meta in self.list_profiles():
            config = self.load_profile_config(meta.id)
            if config is None:
                continue
            if not vst_registry.apply_remove(config, path):
                continue
            if self.save_profile_config(meta.id, config):
                updated.append(meta.id)
            else:
                skipped.append(meta.id)
        return vst_registry.RelinkResult(
            updated_profile_ids=updated, skipped_protected_ids=skipped)

    # ── existing-APO-config import (discover / reconcile) ─────────────

    def discover_importable_configs(self) -> list[ApoImportCandidate]:
        """Find existing Equalizer APO configs worth importing as profiles.

        Two sources: the config the swap displaced (captured as one "Current APO
        Setup"), and any standalone EQ `.txt` sitting in the config dir. APO's
        bundled samples, the multi-MB AutoEQ database dumps, RUMBLE's own files and
        configs with no real shaping (a lone preamp / flat GraphicEQ) are all
        ignored."""
        cfg_dir = self.config_path
        candidates: list[ApoImportCandidate] = []

        displaced = self._displaced_config_text
        if displaced.strip():
            flat = apo_config.resolve_includes(displaced, cfg_dir, skip_stems=_APO_SAMPLE_STEMS)
            if _has_real_eq(apo_config.parse_config(flat)):
                candidates.append(ApoImportCandidate(
                    source=CONFIG_FILENAME, name="Current APO Setup",
                    text=flat, source_hash=_text_hash(flat)))

        try:
            files = sorted(cfg_dir.glob("*.txt"))
        except OSError:
            files = []
        reserved = {CONFIG_FILENAME.lower(), ACTIVE_POINTER_FILENAME, PREVIEW_FILENAME}
        for path in files:
            if path.name.lower() in reserved or path.stem in _APO_SAMPLE_STEMS:
                continue
            try:
                if path.stat().st_size > 512 * 1024:  # AutoEQ DB dumps, not profiles
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            flat = apo_config.resolve_includes(text, cfg_dir, skip_stems=_APO_SAMPLE_STEMS)
            if not _has_real_eq(apo_config.parse_config(flat)):
                continue
            candidates.append(ApoImportCandidate(
                source=path.name, name=path.stem.replace("_", " ").title(),
                text=flat, source_hash=_text_hash(flat)))
        return candidates

    def import_apo_candidates(self, candidates: list[ApoImportCandidate]) -> list[ProfileMeta]:
        """Create a fully-compliant canonical profile from each candidate
        (device remapped to the active endpoint, stereo-defaulted), stamped with
        provenance so the reconcile can keep it current."""
        devices = audio_devices.list_apo_devices()
        created: list[ProfileMeta] = []
        for c in candidates:
            text, _ = _remap_unmatched_devices(c.text, devices)
            config = apo_config.parse_config(text)
            _default_to_stereo(text, config)
            meta = self.create_profile(
                c.name,
                description=f"Imported from Equalizer APO ({c.source})",
                tags=["Imported"], config=config,
                apo_source=c.source, apo_source_hash=c.source_hash,
                import_signature=_config_hash(config))
            created.append(meta)
        return created

    def ignore_apo_sources(self, sources: list[str]) -> None:
        """Remember declined sources so the reconcile won't re-offer them."""
        ignored = list(self.settings.get("ignored_apo_sources") or [])
        for s in sources:
            if s not in ignored:
                ignored.append(s)
        self.settings["ignored_apo_sources"] = ignored
        self.save()

    def dismiss_apo_coverage_warning(self, guid: str) -> None:
        """Remember that the user dismissed the A1 "default device isn't
        APO-hooked" banner for endpoint *guid*, so it doesn't re-nag every
        launch/device-change for that same endpoint (mirrors
        ignore_apo_sources above)."""
        dismissed = list(self.settings.get("apo_coverage_dismissed_guids") or [])
        if guid not in dismissed:
            dismissed.append(guid)
        self.settings["apo_coverage_dismissed_guids"] = dismissed
        self.save()

    def is_apo_coverage_dismissed(self, guid: str | None) -> bool:
        """True when *guid* is in the dismissed-endpoints list. A None guid
        (undeterminable endpoint identity) is never treated as dismissed."""
        if guid is None:
            return False
        return guid in (self.settings.get("apo_coverage_dismissed_guids") or [])

    def sync_apo_imports(self) -> list[ApoImportCandidate]:
        """Reconcile already-imported APO profiles with their sources, then
        return the NEW importable candidates (not yet imported, not previously
        declined) for the caller to offer. Idempotent and safe every boot:
        unchanged sources are no-ops, a changed source refreshes its profile only
        when the user hasn't edited it, and nothing is ever duplicated."""
        cfg_dir = self.config_path
        imported = {p.apo_source: p for p in self.profiles if p.apo_source}

        for src, meta in list(imported.items()):
            if src == CONFIG_FILENAME:
                continue  # one-time displaced snapshot — nothing live to re-read
            src_path = cfg_dir / src
            if not src_path.exists():
                continue  # source gone → leave the now-standalone profile
            try:
                text = src_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            flat = apo_config.resolve_includes(text, cfg_dir, skip_stems=_APO_SAMPLE_STEMS)
            new_hash = _text_hash(flat)
            if new_hash == meta.apo_source_hash:
                continue  # unchanged
            cur = self.load_profile_config(meta.id)
            if cur is None or _config_hash(cur) != meta.import_signature:
                continue  # user has edited the profile → don't clobber their work
            remapped, _ = _remap_unmatched_devices(flat, audio_devices.list_apo_devices())
            config = apo_config.parse_config(remapped)
            _default_to_stereo(remapped, config)
            meta.apo_source_hash = new_hash
            meta.import_signature = _config_hash(config)
            meta.modified = _now_iso()
            self._write_profile_file(meta, config)
            self.save()

        return self.importable_candidates()

    def importable_candidates(self) -> list[ApoImportCandidate]:
        """Discover importable APO configs, excluding any source already
        imported or previously declined. Shared by the boot reconcile
        (`sync_apo_imports`) and the manual Settings → Import action so neither
        re-offers a source the user already has a profile for or dismissed."""
        known = {p.apo_source for p in self.profiles if p.apo_source}
        ignored = set(self.settings.get("ignored_apo_sources") or [])
        return [c for c in self.discover_importable_configs()
                if c.source not in known and c.source not in ignored]

    def ensure_builtin_profiles(self) -> bool:
        """Reconcile the curated built-in profiles in rumble/defaults/.

        Regenerates every launch so embedded metadata (tags, descriptions) stays
        in sync, writing only on change. When the ``show_builtin_profiles``
        setting is off the built-ins are removed instead (the user prefers only
        their own); a built-in that was active falls back to bypass. Returns True
        when anything changed on disk.
        """
        defaults_dir = self.rumble_dir / "defaults"
        defaults_dir.mkdir(parents=True, exist_ok=True)

        enabled = bool(self.settings.get("show_builtin_profiles", True))
        # Ids that should exist on disk after this call: the full set when
        # enabled, none when disabled.
        wanted = set(BUILTIN_PROFILES) if enabled else set()

        changed = False
        now = _now_iso()

        if enabled:
            for profile_id, profile_data in BUILTIN_PROFILES.items():
                target_path = defaults_dir / f"{profile_id}.txt"

                doc = ProfileDocument(
                    name=profile_data['name'],
                    description=profile_data['description'],
                    tags=profile_data['tags'],
                    created=now,
                    modified=now,
                    builtin=True,
                    config=apo_config.parse_config(profile_data['body']),
                )
                try:
                    new_content = profile_file.render_profile_text(doc)
                    if not target_path.exists() or target_path.read_text(encoding="utf-8") != new_content:
                        target_path.write_text(new_content, encoding="utf-8")
                        changed = True
                except OSError:
                    pass

        # Remove any defaults/*.txt not wanted: stale built-ins from a previous
        # release (when enabled), or *all* of them (when disabled). defaults/ is
        # app-managed — user profiles live in user/ — so this never touches the
        # user's own work.
        active_removed = False
        for stale in defaults_dir.glob("*.txt"):
            if stale.stem not in wanted:
                if stale.stem == self._active_profile_id:
                    active_removed = True
                try:
                    stale.unlink()
                    changed = True
                except OSError:
                    pass

        # The active profile was a built-in that just got removed (disabling the
        # feature): drop to bypass so the include pointer doesn't dangle.
        if active_removed:
            self._active_profile_id = None
            self._write_active_pointer(None)
            self.save()  # persist the cleared active id so it survives restart
            changed = True

        if changed:
            self._scan_profile_folders()

        return changed

    # ── backup / restore ─────────────────────────────────────────────

    def backup_library(self, zip_path: Path) -> bool:
        """Full backup of everything RUMBLE owns: the app data directory
        (settings + active pointer in profiles.json, custom themes) and the
        entire `rumble/` tree inside the Equalizer APO config (all default and
        user profile files, plus any impulse-response files). The archive
        mirrors the on-disk layout under `data/` and `rumble/` so restore can
        put every file back exactly where it belongs."""
        zip_path = Path(zip_path).resolve()
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", json.dumps({
                    "format": BACKUP_FORMAT_VERSION,
                    "created": _now_iso(),
                    "app": "RUMBLE",
                }, indent=2))
                # App data dir → data/… (profiles.json, custom_themes.json, …).
                # data_dir/trash/ (the profile recycle bin) rides along here
                # too, deliberately -- a full backup should be able to restore
                # a not-yet-purged deleted profile too.
                for src in self._iter_backup_files(self.data_dir):
                    if src.resolve() == zip_path:
                        continue  # never zip the archive into itself
                    arc = src.relative_to(self.data_dir).as_posix()
                    zf.write(src, arcname=f"data/{arc}")
                # APO rumble/ tree → rumble/… (defaults/, user/, irs/). All
                # render scratch (preview, listener/loudness-composed
                # fallback, D1a per-device dispatch branches) is regenerated
                # on demand from settings + profiles, so skip it -- restoring
                # stale scratch (e.g. a device-GUID hash scratch file for an
                # endpoint that no longer exists on the restoring machine)
                # would just be dead weight until the next render sweeps it.
                for src in self._iter_backup_files(self.rumble_dir):
                    if (src.name == PREVIEW_FILENAME
                            or src.name == LISTENER_ACTIVE_FILENAME
                            or src.name.startswith(DEVICE_SCRATCH_PREFIX)):
                        continue
                    arc = src.relative_to(self.rumble_dir).as_posix()
                    zf.write(src, arcname=f"{RUMBLE_SUBDIR}/{arc}")
            return True
        except OSError:
            return False

    @staticmethod
    def _iter_backup_files(root: Path):
        """Yield every regular file under `root`, skipping temp/journal files."""
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix != ".tmp":
                yield path

    def restore_library(self, zip_path: Path) -> bool:
        """Replace the current library with a full-tree backup archive written
        by backup_library(). Existing profile/IR files are cleared first so the
        restored library matches the backup exactly."""
        zip_path = Path(zip_path)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                is_full = any(n.startswith("data/") for n in names) or any(
                    n.startswith(f"{RUMBLE_SUBDIR}/user/")
                    or n.startswith(f"{RUMBLE_SUBDIR}/defaults/") for n in names)
                if is_full:
                    self._restore_full(zf, names)
                else:
                    return False
            # Re-read settings + active pointer and rescan the profile folders,
            # then re-merge any restored custom themes into the runtime set.
            self._load()
            try:
                from rumble.core.custom_themes import load_custom_themes
                load_custom_themes(self.data_dir)
            except Exception:
                pass
            # The restored settings may carry a different active profile,
            # bypass state, or device_profile_assignments dict than whatever
            # was on disk before the restore -- rumble_active.txt itself was
            # never part of the backup (it lives at config_path root, not
            # under rumble_dir), so it's still the PRE-restore render until
            # explicitly rewritten here. Called directly (not via
            # refresh_render_layers()) since that refuses during a live
            # preview/bypass -- a restore is an authoritative state replace,
            # not a settings-changed nudge, and must win regardless.
            self._write_active_pointer(self.get_active_profile())
            return True
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError, KeyError):
            return False

    def _restore_full(self, zf: zipfile.ZipFile, names: list[str]) -> None:
        """Restore a full-tree archive: wipe the existing profile/IR files,
        then extract every `data/` and `rumble/` member to its real location.

        Member names come from the archive, not from us — a hand-edited or
        tampered zip could carry a `data/../../../whatever` entry, so every
        target is confined to its root via `_safe_extract_target()` (zip-slip
        guard, CWE-22) before anything is written."""
        # Clear the existing library so stale profiles/IRs don't linger.
        for sub in ("user", "defaults", IR_SUBDIR):
            folder = self.rumble_dir / sub
            if folder.is_dir():
                for f in folder.glob("*"):
                    if f.is_file():
                        try:
                            f.unlink()
                        except OSError:
                            pass
        for name in names:
            if name.startswith("data/"):
                target = self._safe_extract_target(self.data_dir, name[len("data/"):])
                cap = _MAX_TEXT_MEMBER_BYTES
            elif name.startswith(f"{RUMBLE_SUBDIR}/"):
                relative = name[len(f"{RUMBLE_SUBDIR}/"):]
                target = self._safe_extract_target(self.rumble_dir, relative)
                cap = (_MAX_IR_MEMBER_BYTES if relative.startswith(f"{IR_SUBDIR}/")
                       else _MAX_TEXT_MEMBER_BYTES)
            else:
                continue  # manifest.json and other root entries
            if target is None:
                continue  # escapes its root — skip rather than trust it
            payload = self._read_zip_member_capped(zf, name, cap)
            if payload is None:
                continue  # missing, unreadable, or past the size ceiling
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

    @staticmethod
    def _safe_extract_target(root: Path, relative: str) -> Path | None:
        """Resolve `relative` under `root`, or None if it would land outside
        `root` (an absolute path, a drive-letter path, or `../` traversal)."""
        candidate = (root / relative).resolve()
        root_resolved = root.resolve()
        if candidate != root_resolved and root_resolved not in candidate.parents:
            return None
        return candidate

    @staticmethod
    def _read_zip_member_capped(zf: zipfile.ZipFile, name: str, max_bytes: int) -> bytes | None:
        """Read one zip member as a stream, refusing past `max_bytes` of
        actual decompressed data — a zip-bomb guard that checks bytes as
        they come out, rather than trusting the archive's own (spoofable)
        declared size. None if the member is missing, unreadable, or would
        exceed the cap."""
        try:
            with zf.open(name) as fh:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except (KeyError, OSError, zipfile.BadZipFile):
            return None
