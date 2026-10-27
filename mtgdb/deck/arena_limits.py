"""Whether a deck is within the limits Proxic Arena's import accepts.

The limits are Arena's, read from its deck rules (``ArenaFormat``): a main deck
of ``min_cards`` to ``max_cards`` cards, at most ``max_copies`` of one card
name, the named basic lands exempt and nothing else, and the side decks the
Un-sets use (Contraptions, Attractions), which are not in the main deck's count
and hold a smallest number of cards and one copy of each. This module only
measures a deck against them; the refusal's wording and dialog belong to the
Arena export UI, and the name key belongs to ``arena_support``. It knows no
number of its own, so a change to Arena's format reaches the export with no
change here.

Only the main board counts, because the sideboard is not exported. Copies are
counted per name as Proxic Arena resolves it -- the front face, case-folded and
accent-stripped -- so two printings of one card add together.

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

from mtgdb.deck.arena_support import card_key

_PERMANENT_TYPES = frozenset({
    "artifact", "battle", "creature", "enchantment", "land", "planeswalker"})


@dataclass(frozen=True)
class ArenaDeckLimits:
    """One deck's measurements against Proxic Arena's deck limits."""

    # Cards in the main deck, side-deck cards left out.
    total_cards: int
    min_cards: int
    max_cards: int
    # (name as stored, copies in the deck, copies allowed), by name, main deck
    # and side decks alike.
    over_copy_limit: tuple[tuple[str, int, int], ...] = ()
    # (side deck, cards in it, fewest it may hold) for each side deck holding
    # some cards but too few.
    short_side_decks: tuple[tuple[str, int, int], ...] = ()
    # (name as stored, copies in the deck) for each legendary permanent held
    # more than once, by name. A warning, never a reason to refuse.
    legendary_repeats: tuple[tuple[str, int], ...] = ()

    @property
    def cards_over(self):
        """How many cards past the size limit, or 0."""
        return max(0, self.total_cards - self.max_cards)

    @property
    def cards_short(self):
        """How many cards short of the smallest deck, or 0."""
        return max(0, self.min_cards - self.total_cards)

    @property
    def allowed(self):
        return not (
            self.cards_over or self.cards_short or self.over_copy_limit
            or self.short_side_decks)


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


def arena_deck_limits(deck, rules_format):
    """Measure ``deck``'s main board against ``rules_format`` (an ``ArenaFormat``)."""
    groups = {}
    for entry in deck.entries("main"):
        card = entry.get("card") or {}
        quantity = int(entry.get("qty", 0))
        stored = str(card.get("name") or "").strip()
        # A card with no name still counts toward the deck's size.
        key = card_key(stored) or object()
        bucket = groups.setdefault(
            key, {"name": stored, "copies": 0, "cards": [], "named": stored != ""})
        bucket["copies"] += quantity
        bucket["cards"].append(card)

    total = 0
    side_totals = {}
    over = []
    repeats = []
    for key, bucket in groups.items():
        section = rules_format.sections.get(key) if bucket["named"] else None
        if section is None:
            total += bucket["copies"]
            limit = (None if key in rules_format.unlimited
                     else rules_format.max_copies)
        else:
            side_totals[section] = side_totals.get(section, 0) + bucket["copies"]
            limit = rules_format.side_decks[section][1]
        if bucket["named"] and limit is not None and bucket["copies"] > limit:
            over.append((bucket["name"], bucket["copies"], limit))
        if bucket["copies"] > 1 and any(
                _is_legendary_permanent(card) for card in bucket["cards"]):
            repeats.append((bucket["name"], bucket["copies"]))

    short = [
        (section, count, rules_format.side_decks[section][0])
        for section, count in sorted(side_totals.items())
        if count < rules_format.side_decks[section][0]]
    over.sort(key=lambda item: item[0].casefold())
    repeats.sort(key=lambda item: item[0].casefold())
    return ArenaDeckLimits(
        total_cards=total, min_cards=rules_format.min_cards,
        max_cards=rules_format.max_cards, over_copy_limit=tuple(over),
        short_side_decks=tuple(short), legendary_repeats=tuple(repeats))
