"""Which cards and printings Proxic Arena can play, and which names a deck carries that it cannot.

Proxic Arena resolves a card by name: it normalizes the name, looks for a card
script of that name, and plays whatever that script says. A name it holds no
script for is not a failed cast but a card it never finds, so a deck exported
from here can lose cards on import with nothing said about it.

It also checks the printing. A decklist line that names a set and collector
number is accepted only when Forge holds that exact printing of the card, and
one mismatch refuses the whole import. So the asset also carries, per name,
the printings Forge has, and the set codes Forge's own lookup accepts; which
printing an export then uses is decided in ``deck/arena_printings.py``.

This module answers the name question and owns the asset. The asset is
generated, located by ``ARENA_SUPPORTED_CARDS_FILE`` in ``ui/assets.py``,
because the two applications are separate projects and neither reads the
other's tree at runtime; it is regenerated when the Forge card scripts or
editions Arena plays with are updated, per DECK-012.

The normalization mirrors Arena's own lookup: accents are stripped, the name is
lowercased, apostrophes and commas are dropped, and every other run of
non-alphanumeric characters becomes one underscore. Both the curly and the
straight apostrophe are dropped, which Arena's own rule does not do, because
the two spellings of one name must not read as two different cards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from types import MappingProxyType
import unicodedata

# The asset's payload shape. A future generator that changes the meaning of
# `names` raises this, and a reader that does not recognize a version declines
# the asset rather than guessing at its contents. Version 2 added printings;
# a version 1 asset still answers the name question and knows no printings,
# so an export from it keeps every printing as the deck holds it.
ASSET_VERSION = 2
_READABLE_VERSIONS = (1, ASSET_VERSION)

FACE_SEPARATOR = " // "

# One printing in the asset: Forge's edition code and collector number, joined
# by a space, which neither ever contains.
PRINTING_SEPARATOR = " "


@dataclass(frozen=True)
class ArenaSupport:
    """What the asset says Proxic Arena can play.

    ``names`` holds lookup keys. ``printings`` maps a key to the printings
    Forge has of it, each ``"<EDITION> <NUMBER>"``. ``set_codes`` maps every
    set code Forge's lookup accepts, upper-cased, to the edition it finds, and
    ``editions`` gives each edition's ``(Scryfall set code, Forge type,
    release date)``. A version 1 asset, or none, leaves the last three empty.
    """

    names: frozenset = frozenset()
    printings: object = field(default_factory=lambda: MappingProxyType({}))
    set_codes: object = field(default_factory=lambda: MappingProxyType({}))
    editions: object = field(default_factory=lambda: MappingProxyType({}))

    @property
    def knows_printings(self):
        return bool(self.printings)


def front_face_name(name):
    """Return the face a card is played from, which is the one Arena looks up.

    A stored name holds every face ("Delver of Secrets // Insectile
    Aberration"); Arena files such a card under its front face alone.
    """
    text = str(name or "")
    head = text.split(FACE_SEPARATOR, 1)[0]
    return head.strip()


def normalize_card_name(name):
    """Return the lookup key Arena would derive from ``name``."""
    decomposed = unicodedata.normalize("NFKD", str(name or ""))
    letters = []
    for character in decomposed:
        if unicodedata.combining(character):
            continue
        lowered = character.lower()
        if lowered in ("'", "\u2019", ","):
            continue
        if not ("a" <= lowered <= "z" or "0" <= lowered <= "9"):
            if letters and letters[-1] == "_":
                continue
            lowered = "_"
        letters.append(lowered)
    return "".join(letters).strip("_")


def load_arena_support(path):
    """Return what the asset says Arena can play, or an empty answer when unavailable.

    A missing, unreadable, or unrecognized asset yields an empty
    ``ArenaSupport`` rather than an exception: a broken asset MUST NOT turn an
    export into a failure. Empty means "not known", and callers report and
    change nothing. A malformed printing table is dropped on its own, keeping
    the names, because the two questions are independent.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return ArenaSupport()
    if (not isinstance(payload, dict)
            or payload.get("version") not in _READABLE_VERSIONS):
        return ArenaSupport()
    names = payload.get("names")
    if not isinstance(names, list):
        return ArenaSupport()
    supported = frozenset(
        name for name in names if isinstance(name, str) and name)
    if payload.get("version") == 1:
        return ArenaSupport(names=supported)
    tables = _printing_tables(payload)
    if tables is None:
        return ArenaSupport(names=supported)
    printings, set_codes, editions = tables
    return ArenaSupport(
        names=supported, printings=MappingProxyType(printings),
        set_codes=MappingProxyType(set_codes),
        editions=MappingProxyType(editions))


def _printing_tables(payload):
    """Return the version 2 printing tables, or None when any is malformed."""
    raw_printings = payload.get("printings")
    raw_codes = payload.get("set_codes")
    raw_editions = payload.get("editions")
    if not (isinstance(raw_printings, dict) and isinstance(raw_codes, dict)
            and isinstance(raw_editions, dict)):
        return None
    printings = {}
    for key, entries in raw_printings.items():
        if not isinstance(entries, list):
            return None
        printings[key] = frozenset(
            entry for entry in entries if isinstance(entry, str))
    set_codes = {
        str(code).upper(): edition for code, edition in raw_codes.items()
        if isinstance(edition, str)}
    editions = {}
    for code, details in raw_editions.items():
        if not (isinstance(details, list) and len(details) == 3):
            return None
        editions[code] = tuple(str(value or "") for value in details)
    return printings, set_codes, editions


def load_supported_names(path):
    """Return the normalized names Arena can play, or an empty set when unavailable.

    The name half of ``load_arena_support``: a missing, unreadable, or
    unrecognized asset yields no names, and callers report nothing.
    """
    return load_arena_support(path).names


def unsupported_deck_names(deck, supported, boards=("main", "side")):
    """Return the deck's distinct card names Arena holds no script for.

    Names are returned as they are stored, so they can be shown to the person
    who recognizes them, and deduplicated by lookup key, so one card listed in
    both boards is reported once. An empty ``supported`` set means the asset
    was unavailable, and nothing is reported. ``boards`` narrows the look to
    what is actually exported, so a card left out is never reported as lost.
    """
    if not supported:
        return []
    found = {}
    for board in boards:
        for entry in deck.entries(board):
            card = entry.get("card") or {}
            stored = str(card.get("name") or "").strip()
            if not stored:
                continue
            key = normalize_card_name(front_face_name(stored))
            if not key or key in supported:
                continue
            found.setdefault(key, stored)
    return sorted(found.values(), key=str.casefold)
