"""Which printing of each card a Proxic Arena export uses.

Since its spec 0024 Proxic Arena keeps any printing of a card it plays, so
the export sends every card as the deck holds it, the player's own picture
with it. The exception is a card whose printing changes what it does (the
Attractions, Everythingamajig...), which Arena's rules mark: Arena takes only a
printing it lists for those, and so does an older Arena, whose rules do not say
``anyPrinting``, for every card. There a decklist line ``N Name [SET:NUM]`` is
accepted only when Arena holds that exact printing -- the set code one its
rules list, the collector number equal -- and one line that is not refuses the
whole import, so the export picks a printing Arena has instead.

A card whose printing Arena takes is exported exactly as the deck holds it.
Otherwise the export uses, in order of preference:

1. the same set, with Arena's collector number, when Arena has that set;
2. otherwise the first printing in the order Arena's rules list them -- Arena
   decides which printing it prefers, never this module (ARN-011, ARN-015) --
   among the printings Scryfall also has, so its picture can be fetched and
   bundled;
3. otherwise the line names the card alone, which Arena resolves by name.

Only the export changes. The deck the player saved keeps its printings, and
``Save Deck As`` never comes here. This module does no I/O: the rules arrive
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

# Scryfall treatments that make a printing an alternate look of the card.
_TREATMENT_FRAMES = ("showcase", "extendedart", "inverted", "etched")


@dataclass(frozen=True)
class ArenaPrintingSwap:
    """One deck printing the export replaced, for the report.

    ``to_set`` and ``to_number`` are empty when the export names the card
    alone because no printing Arena has could be pictured.
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

    A card with no set code is written by name and is always accepted, and so
    is any printing when Arena takes any (``any_printing``) of a card whose
    printing does not change it. A name the rules have no printings for is not
    this module's question -- the name check reports it -- so it is left as it
    is.
    """
    key = _key(card)
    printings = support.printings.get(key)
    if not printings:
        return True
    if support.any_printing and key not in support.printing_matters:
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


def choose_printing(support, card, rows):
    """Return ``(row, set code, number)`` for the printing to export, or None.

    ``rows`` are the Scryfall printings of the card's name. Only a printing
    Arena has and Scryfall can picture is a candidate: the deck's own set
    first, then Arena's order. The set code returned is one Arena accepts for
    that printing's edition: the Scryfall code when Arena accepts it,
    otherwise Arena's own.
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
    for order, printing in enumerate(printings):
        edition, _separator, number = printing.partition(PRINTING_SEPARATOR)
        scryfall_code = support.editions.get(edition, (edition.casefold(),))[0]
        row = (pictured.get((scryfall_code, number))
               or pictured.get((edition.casefold(), number)))
        if row is None:
            continue
        candidates.append(((edition not in same_set, order), row, edition, number))
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
    rules list no printings, it is returned as it is, so such a deck exports
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
            # Nothing Arena has can be pictured: name the card alone, which
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
