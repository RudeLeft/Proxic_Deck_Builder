"""One deck checked against Proxic Arena's deck rules, for the export window.

Everything the Proxic Arena Export window shows about a deck is measured here,
off Tk, from a detached snapshot and the rules Arena published: which cards
travel and which token cards ride along for their pictures (``arena_tokens``),
which printing each card goes in (``arena_printings``), the size and copy
limits and repeated legendary cards (``arena_limits``), the cards Arena cannot
play (``arena_support``), and the tokens the deck's cards make with no picture
in the deck. The window only words and draws the answer.

A deck Arena's import would refuse is ``blocked``: too few or too many cards,
too many copies, a short side deck, or a card Arena cannot play, since one such
card refuses the whole import. Everything else -- a repeated legendary card,
printings swapped, tokens without a picture, a sideboard left out -- is
information and never stops a send.
"""

from __future__ import annotations

from dataclasses import dataclass

from mtgdb.deck.arena_limits import ArenaDeckLimits, arena_deck_limits
from mtgdb.deck.arena_printings import arena_export_deck
from mtgdb.deck.arena_support import ArenaRules, unsupported_deck_names
from mtgdb.deck.arena_tokens import ArenaDeckParts, split_for_arena, token_status
from mtgdb.deck.model import Deck


@dataclass(frozen=True)
class ArenaCheck:
    """A deck measured against Arena's rules. ``limits`` is None when the rules are unknown."""

    rules: ArenaRules
    parts: ArenaDeckParts
    # The cards as they would be sent: each in a printing Arena has.
    export_deck: Deck
    swaps: tuple = ()
    limits: ArenaDeckLimits | None = None
    unsupported: tuple = ()
    held_tokens: tuple = ()
    missing_tokens: tuple = ()
    sideboard_count: int = 0

    @property
    def blocked(self):
        """True when Arena's import would refuse the deck as it stands."""
        if not self.parts.cards.total("main"):
            return True
        if self.unsupported:
            return True
        return self.limits is not None and not self.limits.allowed


def check_for_arena(deck, rules, printings_of):
    """Return the ``ArenaCheck`` of a detached ``deck`` under ``rules``.

    ``printings_of(name)`` reads the Scryfall printings of a name; it is asked
    only for a card whose printing Arena does not have, and only here, on the
    worker that runs this.
    """
    parts = split_for_arena(deck)
    cards = parts.cards
    limits = arena_deck_limits(cards, rules.format) if rules.known else None
    unsupported = tuple(unsupported_deck_names(
        cards, rules.support.names, boards=("main",)))
    export_deck, swaps = arena_export_deck(cards, rules.support, printings_of)
    held, missing = token_status(deck)
    return ArenaCheck(
        rules=rules, parts=parts, export_deck=export_deck, swaps=tuple(swaps),
        limits=limits, unsupported=unsupported, held_tokens=held,
        missing_tokens=missing, sideboard_count=cards.total("side"))
