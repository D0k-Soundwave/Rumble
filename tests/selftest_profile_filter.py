"""Regression test for the profile-panel tag tree + search filter.

The left panel is a QTreeWidget (no dropdown): profiles nest under top-level
**User** / **Built-in** branches by their tag hierarchy ("Music / Rock" ->
Music > Rock), and the search box filters the whole tree by name or tag. This
covers hierarchy key construction, branch/leaf counts, the untagged bucket,
search matching, and the User/Built-in split.

Run:  python tools/selftest_profile_filter.py
Exits non-zero on failure."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from rumble.core.profile_manager import ProfileMeta
from rumble.ui.profile_panel import ProfilePanel, _ROLE_BASE
from rumble.ui.theme.tokens import DEFAULT_THEME_NAME, get_theme

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def meta(pid: str, name: str, tags: list[str], builtin: bool = False) -> ProfileMeta:
    return ProfileMeta(id=pid, name=name, tags=tags, builtin=builtin)


def main() -> int:
    app = QApplication([])
    theme = get_theme(DEFAULT_THEME_NAME)

    profiles = [
        meta("rock1", "Rock One", ["Music / Rock"]),
        meta("rock2", "Rock Two", ["Music / Rock"]),
        meta("jazz1", "Jazz One", ["Music / Jazz"]),
        meta("bass1", "Bass Booster", ["Situation / Enhancement / Bass"]),
        meta("plain", "Plain Profile", []),
        meta("bflat", "Flat Reference", ["Situation / Reference"], builtin=True),
    ]
    panel = ProfilePanel(theme)
    panel.set_profiles(profiles)

    keys = set(panel._tag_items)
    check("root:user" in keys and "root:builtin" in keys,
          "both User and Built-in branches build")
    check("root:user>Music" in keys, "parent tag node 'Music' is offered")
    check("root:user>Music>Rock" in keys, "hierarchy key is a full path (Music > Rock)")
    check("root:user>Situation>Enhancement>Bass" in keys, "deep tag path resolves")
    check("root:builtin>Situation>Reference" in keys,
          "built-in profiles build their own branch hierarchy")
    check("root:user>__untagged__" in keys, "untagged profile gets an Untagged bucket")

    def tag_count(key: str) -> int:
        base = panel._tag_items[key].data(0, _ROLE_BASE)  # e.g. "Rock  (2)"
        return int(base.rsplit("(", 1)[1].rstrip(")"))

    check(tag_count("root:user>Music>Rock") == 2, "leaf 'Rock' counts both rock profiles")
    check(tag_count("root:user>Music") == 3, "parent 'Music' counts the whole branch")
    check(tag_count("root:user") == 5, "User branch counts every non-builtin profile")
    check(tag_count("root:builtin") == 1, "Built-in branch counts the builtin profile")

    check(len(panel._item_by_id) == len(profiles), "every profile gets a row")

    # ── search filters the whole tree by name or tag ──────────────────
    def search(text: str) -> set[str]:
        panel.search_box.setText(text)
        panel._apply_search()  # bypass the debounce timer
        return set(panel._item_by_id)

    check(search("rock") == {"rock1", "rock2"}, "search 'rock' shows only rock profiles")
    check(search("bass") == {"bass1"}, "search matches by tag, not just name")
    check(search("one") == {"rock1", "jazz1"}, "multi-result name search works")
    check(search("nonexistent") == set(), "no matches yields an empty tree")
    check(search("") == set(panel._item_by_id) and len(panel._item_by_id) == len(profiles),
          "clearing the search restores every profile")

    print(("\nFAILURES: " + ", ".join(FAILURES)) if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
