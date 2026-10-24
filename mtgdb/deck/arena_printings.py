"""Which printing of each card a Proxic Arena export uses.

Proxic Arena checks the printing as well as the name. A decklist line
``N Name [SET:NUM]`` is accepted only when Forge holds that exact printing --
the set code found by Forge's own edition lookup, the collector number equal
-- and one line that is not refuses the whole import. Arena stays strict on
purpose, so the export picks a printing Forge has instead.

A card whose printing Forge has is exported exactly as the deck holds it.
Otherwise the export uses, in order of preference:

1. the same set, with Forge's collector number, when Forge has that set;
2. otherwise the newest ordinary printing -- core, expansion, funny, draft and
   reprint editions ahead of commander and collector editions, ahead of
   everything else, and a Universes Beyond printing after the in-universe
   ones, as deck import ranks them -- among the printings Scryfall also has,
   so its picture can be fetched and bundled;
3. otherwise the line names the card alone, which Arena resolves by name.

Only the export changes. The deck the player saved keeps its printings, and
``Save Deck As`` never comes here. This module does no I/O: the asset arrives
as an ``ArenaSupport`` and the Scryfall printings of a name through an
injected ``printings_of`` callable, which the export runs off Tk.
"""

from __future__ import annotations

from dataclasses import dataclass

from mtgdb.core.scryfall_json import card_faces, json_list
from mtgdb.deck.arena_support import (
    PRINTING_SEPARATOR, front_face_name, normalize_card_name,
)
from mtgdb.deck.model import Deck

# Forge edition types, by preference. An ordinary printing is the card as it
# came in a normal release.
_ORDINARY_EDITION_TYPES = ("core", "expansion", "funny", "draft", "reprint")
_SUPPLEMENTAL_EDITION_TYPES = (
    "commander", "collector_edition", "duel_deck", "starter", "boxed_set",
    "multiplayer")
# Scryfall treatments that make a printing an alternate look of the card.
_TREATMENT_FRAMES = ("showcase", "extendedart", "inverted", "etched")


@dataclass(frozen=True)
class ArenaPrintingSwap:
    """One deck printing the export replaced, for the report.

    ``to_set`` and ``to_number`` are empty when the export names the card
    alone because no printing Forge has could be pictured.
    """

    name: str
    from_set: str
    from_number: str
    to_set: str
    to_number: str


def _key(card):
    return normalize_card_name(front_face_name((card or {}).get("name")))


def _set_code(card):
    return str((card or {}).get("set_code") or "").strip()


def _collector(card):
    return str((card or {}).get("collector_number") or "").strip()


def printing_accepted(support, card):
    """Whether Proxic Arena accepts ``card`` as the deck holds it.

    A card with no set code is written by name and is always accepted. A name
    the asset has no printings for is not this module's question -- the name
    check reports it -- so it is left as it is.
    """
    printings = support.printings.get(_key(card))
    if not printings:
        return True
    set_code = _set_code(card)
    if not set_code:
        return True
    edition = support.set_codes.get(set_code.upper())
    if edition is None:
        return False
    number = _collector(card)
    if not number:
        prefix = f"{edition}{PRINTING_SEPARATOR}"
        return any(found.startswith(prefix) for found in printings)
    return f"{edition}{PRINTING_SEPARATOR}{number}" in printings


def _has_picture(row):
    if row.get("image_png"):
        return True
    return any(
        isinstance(face.get("image_uris"), dict)
        and face["image_uris"].get("png")
        for face in card_faces(row))


def _row_rank(row):
    """Prefer the English, paper, untreated printing among same-numbered rows."""
    frames = [str(value) for value in json_list(row.get("frame_effects"))]
    treated = (
        bool(row.get("promo"))
        or any(frame in _TREATMENT_FRAMES for frame in frames)
        or bool(json_list(row.get("promo_types"))))
    return (
        str(row.get("lang") or "en").casefold() != "en",
        not row.get("paper"),
        treated,
    )


def _numeric(number):
    digits = "".join(character for character in number if character.isdigit())
    return int(digits) if digits else 0


def _date_value(date):
    digits = "".join(character for character in str(date) if character.isdigit())
    return int(digits) if digits else 0


def choose_printing(support, card, rows):
    """Return ``(row, set code, number)`` for the printing to export, or None.

    ``rows`` are the Scryfall printings of the card's name. Only a printing
    Forge has and Scryfall can picture is a candidate. The set code returned
    is one Forge's lookup accepts for that printing's edition: the Scryfall
    code when Forge accepts it, otherwise Forge's own.
    """
    printings = support.printings.get(_key(card))
    if not printings:
        return None
    pictured = {}
    for row in rows:
        if not _has_picture(row):
            continue
        place = (_set_code(row).casefold(), _collector(row))
        best = pictured.get(place)
        if best is None or _row_rank(row) < _row_rank(best):
            pictured[place] = row

    deck_set = _set_code(card).casefold()
    same_set = {support.set_codes.get(deck_set.upper())}
    same_set.update(
        code for code, details in support.editions.items()
        if details[0] == deck_set)
    same_set.discard(None)

    candidates = []
    for printing in printings:
        edition, _separator, number = printing.partition(PRINTING_SEPARATOR)
        details = support.editions.get(edition, ("", "", ""))
        scryfall_code, edition_type, date = details
        row = (pictured.get((scryfall_code, number))
               or pictured.get((edition.casefold(), number)))
        if row is None:
            continue
        kind = edition_type.casefold()
        tier = (0 if kind in _ORDINARY_EDITION_TYPES
                else 1 if kind in _SUPPLEMENTAL_EDITION_TYPES else 2)
        rank = (
            edition not in same_set,
            bool(row.get("universes_beyond")),
            tier,
            -_date_value(date),
            _row_rank(row),
            _numeric(number),
            number,
            edition,
        )
        candidates.append((rank, row, edition, number))
    if not candidates:
        return None
    _rank, row, edition, number = min(candidates, key=lambda item: item[0])
    written = _set_code(row)
    if support.set_codes.get(written.upper()) != edition:
        written = edition
    return row, written, number


def arena_export_deck(deck, support, printings_of):
    """Return ``(deck to export, swaps)`` with every main-deck printing accepted.

    ``deck`` is not changed. When every printing is already accepted, or the
    asset knows no printings, it is returned as it is, so such a deck exports
    exactly as before. ``printings_of(name)`` returns the Scryfall printings
    of a stored card name and is asked at most once per name.
    """
    if not support.knows_printings:
        return deck, ()
    entries = list(deck.iter_entries("main"))
    if all(printing_accepted(support, entry["card"]) for entry in entries):
        return deck, ()

    rows_by_name = {}
    swaps = []
    exported = Deck(name=deck.name, fmt=deck.fmt)
    for entry in entries:
        card = entry["card"]
        if printing_accepted(support, card):
            exported.add(card, "main", entry["qty"])
            continue
        name = str(card.get("name") or "")
        if name not in rows_by_name:
            rows_by_name[name] = list(printings_of(name) or ())
        chosen = choose_printing(support, card, rows_by_name[name])
        if chosen is None:
            # Nothing Forge has can be pictured: name the card alone, which
            # Arena resolves by name, and keep the deck's own picture.
            replacement = dict(card)
            replacement["set_code"] = ""
            replacement["collector_number"] = ""
            replacement.pop("set", None)
            to_set = to_number = ""
        else:
            row, to_set, to_number = chosen
            replacement = dict(row)
            replacement["set_code"] = to_set
            replacement["collector_number"] = to_number
        exported.add(replacement, "main", entry["qty"])
        swaps.append(ArenaPrintingSwap(
            name=front_face_name(name), from_set=_set_code(card).upper(),
            from_number=_collector(card), to_set=to_set.upper(),
            to_number=to_number))
    return exported, tuple(swaps)
