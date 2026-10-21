"""Whether a deck is within the limits Proxic Arena export accepts.

Proxic Arena export refuses a deck with more than ``ARENA_MAX_CARDS`` cards in
all, or more than ``ARENA_COPY_LIMIT`` copies of one card name. This module only
measures the deck against those two numbers; the refusal's wording and dialog
belong to the Arena export UI, and the name key belongs to ``arena_support``.

Only the main deck counts, because the sideboard is not exported. Copies are
counted per name as Proxic Arena resolves it -- the front face, case-folded and
accent-stripped -- so two printings of one card add together. Basic lands and
cards whose own text allows any number of copies are exempt from the copy limit,
by the same rule the legality panel applies, so the two can never disagree about
what is exempt.

It also finds the legendary permanents a deck holds more than once. That never
refuses an export, because a deck may hold several; it is a warning to give the
player. A player can only control one legendary permanent of a given name, so if
they play a second copy Proxic Arena asks them to discard one. That is Magic's
legend rule working correctly. Only permanents are affected: a legendary instant
or sorcery never enters play, so it is not listed.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from mtgdb.deck.arena_support import front_face_name, normalize_card_name
from mtgdb.deck.legality import group_copy_limit

ARENA_MAX_CARDS = 65
ARENA_COPY_LIMIT = 4

_PERMANENT_TYPES = frozenset({
    "artifact", "battle", "creature", "enchantment", "land", "planeswalker"})


@dataclass(frozen=True)
class ArenaDeckLimits:
    """One deck's measurements against Proxic Arena's export limits."""

    total_cards: int
    # (name as stored, copies in the deck, copies allowed), by name.
    over_copy_limit: tuple[tuple[str, int, int], ...]
    # (name as stored, copies in the deck) for each legendary permanent held
    # more than once, by name. A warning, never a reason to refuse.
    legendary_repeats: tuple[tuple[str, int], ...] = ()

    @property
    def cards_over(self):
        """How many cards past the size limit, or 0."""
        return max(0, self.total_cards - ARENA_MAX_CARDS)

    @property
    def allowed(self):
        return not self.cards_over and not self.over_copy_limit


def _is_legendary_permanent(card):
    """True when any face is Legendary and a permanent type, not just a spell.

    Every face is read: a double-faced card that is only legendary on its back
    still meets the legend rule once it transforms.
    """
    type_line = str(card.get("type_line") or "")
    for face in type_line.split("//"):
        left = re.split(r"\s+(?:—|–|-)\s+", face.strip(), maxsplit=1)[0]
        words = {word.casefold() for word in left.split()}
        if "legendary" in words and words & _PERMANENT_TYPES:
            return True
    return False


def arena_deck_limits(deck):
    """Measure ``deck``'s main deck against Proxic Arena's export limits."""
    total = 0
    groups = {}
    for entry in deck.entries("main"):
        card = entry.get("card") or {}
        quantity = int(entry.get("qty", 0))
        total += quantity
        stored = str(card.get("name") or "").strip()
        key = normalize_card_name(front_face_name(stored))
        if not key:
            continue
        bucket = groups.setdefault(
            key, {"name": stored, "copies": 0, "cards": []})
        bucket["copies"] += quantity
        bucket["cards"].append(card)

    over = []
    repeats = []
    for bucket in groups.values():
        limit = group_copy_limit(bucket["cards"], ARENA_COPY_LIMIT)
        if limit is not None and bucket["copies"] > limit:
            over.append((bucket["name"], bucket["copies"], limit))
        if bucket["copies"] > 1 and any(
                _is_legendary_permanent(card) for card in bucket["cards"]):
            repeats.append((bucket["name"], bucket["copies"]))
    over.sort(key=lambda item: item[0].casefold())
    repeats.sort(key=lambda item: item[0].casefold())
    return ArenaDeckLimits(
        total_cards=total, over_copy_limit=tuple(over),
        legendary_repeats=tuple(repeats))
