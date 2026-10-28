"""Tokens and a Proxic Arena deck: the token cards a deck holds, and the tokens its cards make.

A token is not one of a deck's cards, but its picture is worth sending: in a
game, Proxic Arena dresses a token in the picture of a token card a deck
brought, matched by name (Arena spec 0022). So a token card the player put in
the main deck travels in the decklist's Tokens section, for its picture only:
it is not counted toward the deck's size and is never checked as a card
(ARN-013). An emblem or art card is neither a card nor a token Arena draws, so
it is left out. Which entries are which is the card database's own
``content_kind``, never a second reading of the type line.

Scryfall also links a card to the tokens it makes: the card's
``related_parts`` hold an entry whose component is ``token`` for each one. A
token the deck's cards make with no token card of that name in the deck still
plays, without a picture, so the export window tells the player and never
stops the send (ARN-010). The wording belongs to the UI,
and the name key to ``arena_support``.

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
_TOKEN_KIND = "token"


def is_game_piece(card):
    """True for a token, emblem or art card: something a game makes, not a deck card."""
    return str((card or {}).get("content_kind") or "").casefold() in _GAME_PIECE_KINDS


def is_token_card(card):
    """True for a token card, whose picture a Proxic Arena deck can bring."""
    return str((card or {}).get("content_kind") or "").casefold() == _TOKEN_KIND


def _token_name_key(name):
    return front_face_name(name).casefold()


@dataclass(frozen=True)
class ArenaDeckParts:
    """A deck as Proxic Arena takes it: its cards, its token cards, and what is left out.

    ``cards`` is a detached deck without any game piece. ``tokens`` holds the
    main deck's token cards, one per token name (the first printing of a name,
    as for any picture), each ``(card, quantity)``. ``left_out`` names the
    main deck's emblems and art cards, which travel nowhere.
    """

    cards: Deck
    tokens: tuple = ()
    left_out: tuple = ()


def split_for_arena(deck):
    """Return the ``ArenaDeckParts`` of ``deck``, which is not changed."""
    cards = Deck(name=deck.name, fmt=deck.fmt)
    tokens = {}
    left_out = {}
    for card, quantity, board in deck.snapshot_entries():
        if quantity <= 0:
            continue
        if not is_game_piece(card):
            cards.add(card, board, quantity)
            continue
        if board != "main":
            continue
        name = str((card or {}).get("name") or "").strip()
        if not name:
            continue
        if is_token_card(card):
            key = _token_name_key(name)
            held = tokens.get(key)
            tokens[key] = (held[0], held[1] + quantity) if held else (card, quantity)
        else:
            left_out.setdefault(name.casefold(), name)
    return ArenaDeckParts(
        cards=cards,
        tokens=tuple(tokens.values()),
        left_out=tuple(sorted(left_out.values(), key=str.casefold)))


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
        # A token card does not make itself.
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


def token_status(deck):
    """Return ``(held, missing)``: the tokens the main deck's cards make, split
    by whether a token card of that name is in the main deck.

    Matched by name alone, as Arena matches a picture to a token in play
    (owner decision S4): any Ape token card dresses every Ape.
    """
    held_names = {
        _token_name_key((entry.get("card") or {}).get("name"))
        for entry in deck.entries("main") if is_token_card(entry.get("card"))}
    held, missing = [], []
    for token in deck_tokens(deck):
        (held if _token_name_key(token.name) in held_names else missing).append(token)
    return tuple(held), tuple(missing)
