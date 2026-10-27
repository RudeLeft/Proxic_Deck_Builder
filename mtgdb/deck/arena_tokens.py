"""Tokens and a Proxic Arena deck: the token cards a deck holds, and the tokens its cards make.

A token is not a card a deck can hold. Proxic Arena makes tokens itself during
a game, and its import refuses a decklist that names one ("Forge does not know
a card named Ape"). So a token, emblem or art card the player put in the deck
is left out of the export and out of its card count (ARN-013); the stored
``content_kind`` -- the card database's own classification of every printing
-- says which entries those are, never a second reading of the type line.

Scryfall also links a card to the tokens it makes: the card's ``related_parts``
hold an entry whose component is ``token`` for each one. The export lists those
so the player knows what to expect in play (ARN-010); Arena makes them, so the
player has nothing to add. The wording and the dialog belong to the Arena
export UI, and the name key belongs to ``arena_support``.

Only the main deck counts, because the sideboard is not exported. Tokens are
grouped by name and type line, so several Scryfall tokens of one kind (a 1/1 and
a 2/2 Goblin, say) read as one line and name every card that can make it.
"""

from __future__ import annotations

from dataclasses import dataclass

from mtgdb.core.scryfall_json import json_list
from mtgdb.deck.arena_support import front_face_name
from mtgdb.deck.model import Deck

# The card database's content kinds that are not cards a deck holds.
_GAME_PIECE_KINDS = frozenset({"token", "emblem", "art"})


def is_game_piece(card):
    """True for a token, emblem or art card: something a game makes, not a deck card."""
    return str((card or {}).get("content_kind") or "").casefold() in _GAME_PIECE_KINDS


def without_game_pieces(deck):
    """Return ``(deck, names left out)``: ``deck`` without its tokens, emblems and art cards.

    ``deck`` is not changed. When it holds none, it is returned as it is. The
    names are the main deck's, as stored, once each, in order of name.
    """
    pieces = [entry for entry in deck.iter_entries() if is_game_piece(entry.get("card"))]
    if not pieces:
        return deck, ()
    kept = Deck(name=deck.name, fmt=deck.fmt)
    for card, quantity, board in deck.snapshot_entries():
        if quantity > 0 and not is_game_piece(card):
            kept.add(card, board, quantity)
    names = {}
    for entry in pieces:
        if entry.get("board") != "main":
            continue
        name = str((entry.get("card") or {}).get("name") or "").strip()
        if name:
            names.setdefault(name.casefold(), name)
    return kept, tuple(sorted(names.values(), key=str.casefold))


@dataclass(frozen=True)
class ArenaToken:
    """One kind of token, and the deck cards that can create it."""

    name: str
    type_line: str
    # Front-face names of the deck's cards that can make it, in deck order.
    makers: tuple[str, ...]


def deck_tokens(deck):
    """Return the tokens the main deck's cards can create, by token name."""
    found = {}
    for entry in deck.entries("main"):
        card = entry.get("card") or {}
        # A token card does not make itself; it is left out of the export.
        if is_game_piece(card):
            continue
        maker = front_face_name(card.get("name"))
        for part in json_list(card.get("related_parts")):
            if not isinstance(part, dict) or part.get("component") != "token":
                continue
            name = str(part.get("name") or "").strip()
            if not name:
                continue
            type_line = str(part.get("type_line") or "").strip()
            token = found.setdefault(
                (name.casefold(), type_line.casefold()),
                {"name": name, "type_line": type_line, "makers": []})
            if maker and maker not in token["makers"]:
                token["makers"].append(maker)
    ordered = sorted(
        found.values(),
        key=lambda item: (item["name"].casefold(), item["type_line"].casefold()))
    return tuple(
        ArenaToken(item["name"], item["type_line"], tuple(item["makers"]))
        for item in ordered)
