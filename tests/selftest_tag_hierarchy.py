"""Self-test for the tag hierarchy builder (core only, no Qt).

Run:  python tests/selftest_tag_hierarchy.py

Characterizes TagNode / TagHierarchyBuilder behavior that must survive
refactors — in particular sorted_children() returning children in a stable,
predictable order. Exits non-zero on failure.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rumble.core.tag_hierarchy import TagHierarchyBuilder, TagNode

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


def _profile(pid: str, tags: list[str]):
    return SimpleNamespace(id=pid, tags=tags)


def main() -> int:
    print("[1] TagNode.sorted_children returns children in insertion order")
    root = TagNode(name="")
    first = root.add_or_get_child("Zebra")
    second = root.add_or_get_child("Apple")
    third = root.add_or_get_child("Mango")
    check(root.sorted_children() == [first, second, third],
          "children come back in the order they were added, not alphabetical")

    print("\n[2] TagHierarchyBuilder groups profiles by shared tags")
    profiles = [
        _profile("p1", ["Music", "Rock"]),
        _profile("p2", ["Music", "Rock"]),
        _profile("p3", ["Music", "Jazz"]),
    ]
    builder = TagHierarchyBuilder()
    tree = builder.build_from_profiles(profiles)
    music = tree.get_child("Music")
    check(music is not None, "top-level 'Music' group created")
    rock = music.get_child("Rock") if music else None
    check(rock is not None and rock.profile_ids == {"p1", "p2"},
          "Rock subgroup holds both rock profiles")
    check(builder.find_profiles(tree) == ["p1", "p2", "p3"],
          "find_profiles recovers every profile id under the root")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S)")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
