"""Tag hierarchy builder for organizing profiles by tag hierarchy.

Profiles have comma-separated tags like "Genre, Rock, Bright" which form
a hierarchical tree structure. TagNode represents a node in this tree,
and TagHierarchyBuilder constructs the tree from profiles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# When two tags are shared by equally many profiles, the one whose leading
# `/` component appears here wins the tie — it stays as the grouping label
# instead of being dropped as a redundant trailing tag. Earlier = stronger
# preference; matched case-insensitively. So a profile tagged both
# "Music / Pop" and "Character / Vocal-Forward" groups under Music → Pop.
_PREFERRED_TAG_PREFIXES = ("music",)


def _tie_rank(tag: str) -> int:
    """Tie-break rank for a tag: preferred dimensions sort first (lower)."""
    head = tag.split('/', 1)[0].strip().lower()
    try:
        return _PREFERRED_TAG_PREFIXES.index(head)
    except ValueError:
        return len(_PREFERRED_TAG_PREFIXES)


@dataclass
class TagNode:
    """A node in the tag hierarchy tree."""

    name: str
    children: list[TagNode] = field(default_factory=list)
    profile_ids: set[str] = field(default_factory=set)

    @property
    def count(self) -> int:
        """Number of profiles at this node (leaf level only)."""
        return len(self.profile_ids)

    def get_child(self, name: str) -> Optional[TagNode]:
        """Find a child node by name."""
        for child in self.children:
            if child.name == name:
                return child
        return None

    def add_or_get_child(self, name: str) -> TagNode:
        """Get existing child or create a new one."""
        child = self.get_child(name)
        if child is None:
            child = TagNode(name=name)
            self.children.append(child)
        return child

    def sorted_children(self) -> list[TagNode]:
        """Return children in the order they were added."""
        return list(self.children)


class TagHierarchyBuilder:
    """Builds a tag hierarchy from a list of profiles."""

    def __init__(self):
        self.root = TagNode(name="")

    def build_from_profiles(self, profiles: list) -> TagNode:
        """Build a single-placement hierarchy from each profile's tags.

        Every profile lands at exactly **one** leaf, so it can never appear in
        more than one group. The order tags are written in the file does not
        matter: each profile's tags are ordered by how widely they are *shared*
        across the whole library (most common first; ties broken by preferred
        dimension — see _PREFERRED_TAG_PREFIXES — then alphabetically), so the
        most common tag becomes the top group and rarer tags nest deeper,
        batching profiles that share tags into the same groups and sub-groups.

        Nesting stops as soon as it stops being useful. A profile's path is
        truncated after the last tag that actually *subdivides* its group:
        trailing tags shared by everyone who has reached that node no longer
        separate anything, so they are dropped rather than spun out into a long
        single-profile chain. Shared tags that sit *above* a real split are kept
        as context.

        A tag token may still carry its own internal hierarchy via `/`
        (e.g. "Music/Jazz"); the token is counted as one unit for frequency and
        its `/` parts are expanded contiguously into the path.
        """
        self.root = TagNode(name="")

        # Global frequency of each tag token across all profiles.
        freq: dict[str, int] = {}
        for profile in profiles:
            for tag in self._profile_tags(profile):
                freq[tag] = freq.get(tag, 0) + 1

        # Order each profile's tags by shared frequency so that profiles sharing
        # tags produce identical prefixes (the grouping is order-independent).
        ordered: dict[str, list[str]] = {}
        for profile in profiles:
            tags = self._profile_tags(profile)
            if tags:
                ordered[profile.id] = sorted(
                    set(tags), key=lambda t: (-freq[t], _tie_rank(t), t.lower()))

        # Count how many profiles share each ordered token-prefix. A token
        # "subdivides" its group when its prefix is shared by fewer profiles than
        # the prefix before it; trailing tokens that don't subdivide are the
        # redundant tail we trim.
        total_tagged = len(ordered)
        prefix_count: dict[tuple, int] = {}
        for toks in ordered.values():
            for i in range(1, len(toks) + 1):
                key = tuple(toks[:i])
                prefix_count[key] = prefix_count.get(key, 0) + 1

        for profile in profiles:
            toks = ordered.get(profile.id)
            if not toks:
                continue

            # Keep tokens up to and including the last one that narrows the
            # group; tokens after it are shared by the whole group and add only
            # depth, so they're dropped (falling back to the first token when
            # nothing subdivides, e.g. a lone profile sharing all its tags).
            last_subdividing = -1
            for i, _tok in enumerate(toks):
                before = prefix_count[tuple(toks[:i])] if i else total_tagged
                after = prefix_count[tuple(toks[:i + 1])]
                if after < before:
                    last_subdividing = i
            keep = toks[:last_subdividing + 1] if last_subdividing >= 0 else toks[:1]

            # Expand each kept tag's internal `/` hierarchy into the overall
            # path; node reuse in _add_path merges shared components (so all
            # "Character / *" tags share one "Character" group).
            path: list[str] = []
            for tag in keep:
                path.extend(p.strip() for p in tag.split('/') if p.strip())

            if path:
                self._add_path(profile.id, path)

        return self.root

    @staticmethod
    def _profile_tags(profile) -> list[str]:
        """Return a profile's non-empty tag tokens (stripped)."""
        tags = getattr(profile, 'tags', None) or []
        return [t.strip() for t in tags if t and t.strip()]

    def _add_path(self, profile_id: str, path: list[str]) -> None:
        """Add a profile to a hierarchical path in the tree."""
        current = self.root

        for i, part in enumerate(path):
            # Get or create child node
            next_node = current.add_or_get_child(part)

            # If this is the leaf, add the profile ID
            if i == len(path) - 1:
                next_node.profile_ids.add(profile_id)

            current = next_node

    def find_profiles(self, node: TagNode) -> list[str]:
        """Find all profile IDs under a node (including descendants)."""
        profile_ids = set(node.profile_ids)
        for child in node.children:
            profile_ids.update(self.find_profiles(child))
        return sorted(list(profile_ids))
