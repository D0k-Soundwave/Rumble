"""Library-wide VST plugin aggregation, relinking and best-effort discovery.

This is an audit/path-management layer, not a VST host: it never loads a
plugin, never decodes or edits `VSTPlugin Chunks:` preset data (that opaque
blob only ever comes from Equalizer APO Configurator and is carried through
untouched), and never changes whether/where a VST block appears in a
profile's rendered config. It only tracks which plugin paths profiles
reference, whether those paths currently resolve on disk, and offers a
manual "replace this path everywhere it's used" relink plus a folder scan
to help find candidate DLLs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from rumble.core.apo_config import ProfileConfig


def _norm(path: str) -> str:
    """Dedup key for a Windows filesystem path: case-insensitive, slash-tolerant."""
    return os.path.normcase(os.path.normpath(path.strip()))


@dataclass
class VstUsage:
    """One distinct VST plugin path referenced somewhere in the library."""
    path: str
    profile_names: list[str]
    profile_ids: list[str]
    exists: bool


@dataclass
class VstLibraryReport:
    entries: list[VstUsage] = field(default_factory=list)

    @property
    def profile_count(self) -> int:
        ids: set[str] = set()
        for entry in self.entries:
            ids.update(entry.profile_ids)
        return len(ids)


@dataclass
class DiscoveredPlugin:
    path: str
    filename: str


@dataclass
class RelinkResult:
    updated_profile_ids: list[str] = field(default_factory=list)
    skipped_protected_ids: list[str] = field(default_factory=list)


def aggregate_vst_usage(
        profile_configs: list[tuple[str, str, ProfileConfig]]) -> VstLibraryReport:
    """Group every VST plugin path referenced across *profile_configs*
    (``(profile_id, profile_name, config)`` triples) by normalized path,
    checking each distinct path's existence once."""
    by_key: dict[str, VstUsage] = {}
    for profile_id, profile_name, config in profile_configs:
        for vst in config.vst_plugins:
            path = vst.path.strip()
            if not path:
                continue
            key = _norm(path)
            usage = by_key.get(key)
            if usage is None:
                by_key[key] = VstUsage(
                    path=path, profile_names=[profile_name],
                    profile_ids=[profile_id], exists=Path(path).is_file())
            elif profile_id not in usage.profile_ids:
                usage.profile_names.append(profile_name)
                usage.profile_ids.append(profile_id)

    entries = sorted(by_key.values(), key=lambda u: (u.exists, u.path.lower()))
    return VstLibraryReport(entries=entries)


def apply_relink(config: ProfileConfig, old_path: str, new_path: str) -> bool:
    """Replace `.path` in place on every entry matching *old_path*, leaving
    `.chunks` untouched. Returns whether anything changed."""
    key = _norm(old_path)
    changed = False
    for vst in config.vst_plugins:
        if _norm(vst.path) == key:
            vst.path = new_path
            changed = True
    return changed


def apply_remove(config: ProfileConfig, path: str) -> bool:
    """Drop every entry matching *path* from `config.vst_plugins` in place.
    Returns whether anything was removed."""
    key = _norm(path)
    before = len(config.vst_plugins)
    config.vst_plugins = [v for v in config.vst_plugins if _norm(v.path) != key]
    return len(config.vst_plugins) != before


#: Short list of conventional VST2 install locations. Not exhaustive, not a
#: registry/CLSID scan -- just enough to make discovery useful without
#: walking the whole filesystem.
_CANDIDATE_ENV_SUBDIRS = [
    ("ProgramFiles", "Steinberg/VstPlugins"),
    ("ProgramFiles", "VstPlugins"),
    ("CommonProgramFiles", "VST2"),
    ("ProgramFiles(x86)", "Steinberg/VstPlugins"),
    ("ProgramFiles(x86)", "VstPlugins"),
    ("CommonProgramFiles(x86)", "VST2"),
]


def _candidate_roots() -> list[Path]:
    roots = []
    for env_var, subdir in _CANDIDATE_ENV_SUBDIRS:
        base = os.environ.get(env_var)
        if base:
            roots.append(Path(base) / subdir)
    return roots


def scan_common_vst_folders() -> list[DiscoveredPlugin]:
    """Best-effort scan of conventional VST2 install folders for `*.dll`,
    root plus one level of subdirectories deep (VST2 installs are typically
    flat or one vendor-folder deep; deeper scans risk a slow walk through
    unrelated nested content). Never raises -- an unreadable or missing
    folder is silently skipped."""
    found: dict[str, DiscoveredPlugin] = {}
    for root in _candidate_roots():
        _scan_one_level(root, found)
    return sorted(found.values(), key=lambda p: p.filename.lower())


def _scan_one_level(directory: Path, found: dict[str, DiscoveredPlugin]) -> None:
    try:
        if not directory.is_dir():
            return
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir():
                for dll in entry.glob("*.dll"):
                    _add_discovered(dll, found)
            elif entry.suffix.lower() == ".dll":
                _add_discovered(entry, found)
        except OSError:
            continue


def _add_discovered(dll: Path, found: dict[str, DiscoveredPlugin]) -> None:
    key = _norm(str(dll))
    if key not in found:
        found[key] = DiscoveredPlugin(path=str(dll), filename=dll.name)
