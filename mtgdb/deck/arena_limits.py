"""Whether a deck is within the size and copy limits Proxic Arena export accepts.

Proxic Arena export refuses a deck with more than ``ARENA_MAX_CARDS`` cards in
all, or more than ``ARENA_COPY_LIMIT`` copies of one card name. This module only
measures the deck against those two numbers; the refusal's wording and dialog
belong to the Arena export UI, and the name key belongs to ``arena_support``.

Only the main deck counts, because the sideboard is not exported. Copies are
counted per name as Proxic Arena resolves it -- the front face, case-folded and
accent-stripped -- so two printings of one card add together. Basic lands and
cards whose own text allows any number of copies are exempt, by the same rule
the legality panel applies, so the two can never disagree about what is exempt.
"""

from __future__ import annotations

from dataclasses import dataclass

from mtgdb.deck.arena_support import front_face_name, normalize_card_name
from mtgdb.deck.legality import group_copy_limit

ARENA_MAX_CARDS = 65
ARENA_COPY_LIMIT = 4


@dataclass(frozen=True)
class ArenaDeckLimits:
    """One deck's measurements against Proxic Arena's export limits."""

    total_cards: int
    # (name as stored, copies in the deck, copies allowed), by name.
    over_copy_limit: tuple[tuple[str, int, int], ...]

    @property
    def cards_over(self):
        """How many cards past the size limit, or 0."""
        return max(0, self.total_cards - ARENA_MAX_CARDS)

    @property
    def allowed(self):
        return not self.cards_over and not self.over_copy_limit


def arena_deck_limits(deck):
    """Measure ``deck``'s main deck against Proxic Arena's size and copy limits."""
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
    for bucket in groups.values():
        limit = group_copy_limit(bucket["cards"], ARENA_COPY_LIMIT)
        if limit is not None and bucket["copies"] > limit:
            over.append((bucket["name"], bucket["copies"], limit))
    over.sort(key=lambda item: item[0].casefold())
    return ArenaDeckLimits(total_cards=total, over_copy_limit=tuple(over))
