"""Which tokens the cards of a deck can create, from each card's related parts.

Scryfall links a card to the tokens it makes: the card's ``related_parts`` hold
an entry whose component is ``token`` for each one. Proxic Arena export carries
card art for the deck's own cards only, so a token made in play has none unless
the player adds it. This module finds those tokens so the export can tell the
player which ones to find art for; the wording and the dialog belong to the Arena
export UI, and the name key belongs to ``arena_support``.

Only the main deck counts, because the sideboard is not exported. Tokens are
grouped by name and type line, so several Scryfall tokens of one kind (a 1/1 and
a 2/2 Goblin, say) read as one line and name every card that can make it.
"""

from __future__ import annotations

from dataclasses import dataclass

from mtgdb.core.scryfall_json import json_list
from mtgdb.deck.arena_support import front_face_name


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
