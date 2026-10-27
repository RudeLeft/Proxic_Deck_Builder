"""Proxic Arena's deck rules as this application reads them, and the names a deck carries that Arena cannot play.

Proxic Arena publishes its deck rules (its specs 0021 and 0022): every card
it can play, every printing of each it accepts -- in the order Arena prefers
them -- the set codes it answers to, and Open Format's limits. The Deck Builder
follows that document and knows nothing of how Arena plays a card, so when
Arena's game engine changes, every export follows with no release here
(DECK-012, ARN-015).

The document arrives three ways, all in the one shape this module reads: fresh
from a linked Arena, as the copy saved from the last download, or as the copy
built into the program (``ARENA_DECK_RULES_FILE`` in ``ui/assets.py``) for a
computer never linked. Fetching and saving belong to ``arena/connection.py``;
which printing an export uses to ``deck/arena_printings.py``; the size and copy
limits to ``deck/arena_limits.py``.

The name key mirrors Arena's own lookup: accents are stripped, the name is
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

FACE_SEPARATOR = " // "

# One printing in the document: Arena's edition code and collector number,
# joined by a space, which neither ever contains.
PRINTING_SEPARATOR = " "

# The parts every rules document carries; one missing means it is not one.
RULES_DOCUMENT_PARTS = ("rulesVersion", "format", "cards", "editions", "setCodes")


def _empty_mapping():
    return MappingProxyType({})


@dataclass(frozen=True)
class ArenaSupport:
    """Which cards and printings Arena can play.

    ``names`` holds lookup keys, of every card and every face. ``printings``
    maps a card's key to the printings Arena has of it, each ``"<EDITION>
    <NUMBER>"``, in the order Arena prefers them (a tuple: the order is
    Arena's instruction, ARN-011). ``set_codes`` maps every set code Arena
    accepts, upper-cased, to the edition it finds, and ``editions`` gives each
    edition's ``(Scryfall set code, edition type, release date)``. Empty means
    "not known", and callers report and change nothing.
    """

    names: frozenset = frozenset()
    printings: object = field(default_factory=_empty_mapping)
    set_codes: object = field(default_factory=_empty_mapping)
    editions: object = field(default_factory=_empty_mapping)

    @property
    def knows_printings(self):
        return bool(self.printings)


@dataclass(frozen=True)
class ArenaFormat:
    """The deck limits Arena's import enforces, as its document states them.

    ``unlimited`` holds the keys of the cards exempt from ``max_copies`` (the
    basic lands, and only those). ``side_decks`` maps a side deck's name
    (``CONTRAPTIONS``, ``ATTRACTIONS``) to ``(fewest cards, copies of each)``,
    and ``sections`` maps a card's key to the side deck it goes to; such a card
    is not in the main deck's count.
    """

    min_cards: int
    max_cards: int
    max_copies: int
    unlimited: frozenset = frozenset()
    side_decks: object = field(default_factory=_empty_mapping)
    sections: object = field(default_factory=_empty_mapping)


@dataclass(frozen=True)
class ArenaRules:
    """One rules document, read: what Arena plays, its format, and where it came from."""

    support: ArenaSupport = field(default_factory=ArenaSupport)
    format: ArenaFormat | None = None
    rules_version: str = ""

    @property
    def known(self):
        return self.format is not None


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
        if lowered in ("'", "’", ","):
            continue
        if not ("a" <= lowered <= "z" or "0" <= lowered <= "9"):
            if letters and letters[-1] == "_":
                continue
            lowered = "_"
        letters.append(lowered)
    return "".join(letters).strip("_")


def card_key(name):
    """The key of a stored card name: its front face, normalized."""
    return normalize_card_name(front_face_name(name))


def rules_from_document(payload):
    """Read one rules document, or return an empty ``ArenaRules`` when it is not one.

    Anything malformed yields "not known" rather than an exception: a broken
    download or a damaged saved copy MUST NOT turn an export into a failure,
    and the caller falls back to the next copy it has.
    """
    if not isinstance(payload, dict) or any(
            part not in payload for part in RULES_DOCUMENT_PARTS):
        return ArenaRules()
    try:
        support = _support(payload["cards"], payload["setCodes"], payload["editions"])
        rules_format = _format(payload["format"], payload["cards"])
    except (TypeError, ValueError, KeyError, AttributeError):
        return ArenaRules()
    return ArenaRules(
        support=support, format=rules_format,
        rules_version=str(payload.get("rulesVersion") or ""))


def load_arena_rules(path):
    """Read a saved or built-in rules document; empty when missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return ArenaRules()
    return rules_from_document(payload)


def _support(cards, set_codes, editions):
    names = set()
    printings = {}
    faces_of = {}
    for card in cards:
        key = card_key(card["name"])
        if not key:
            continue
        names.add(key)
        # A card's own printings, never another card's: a face can share a
        # name with a card (Emeritus of Conflict turns over to Lightning Bolt).
        # In Arena's order, once each.
        printings[key] = tuple(dict.fromkeys(str(p) for p in card.get("printings") or ()))
        for face in card.get("faces") or ():
            face_key = card_key(face)
            if face_key:
                names.add(face_key)
                faces_of.setdefault(face_key, key)
    for face_key, owner in faces_of.items():
        printings.setdefault(face_key, printings.get(owner, ()))
    codes = {str(code).upper(): str(edition) for code, edition in set_codes.items()}
    table = {}
    for edition in editions:
        table[str(edition["code"])] = (
            str(edition.get("scryfallCode") or edition["code"]).casefold(),
            str(edition.get("type") or ""),
            str(edition.get("date") or ""))
    return ArenaSupport(
        names=frozenset(names), printings=MappingProxyType(printings),
        set_codes=MappingProxyType(codes), editions=MappingProxyType(table))


def _format(raw, cards):
    sides = {
        str(name): (int(rule["minCards"]), int(rule["maxCopies"]))
        for name, rule in (raw.get("sideDecks") or {}).items()}
    sections = {}
    for card in cards:
        section = card.get("section")
        if section in sides:
            sections[card_key(card["name"])] = section
    return ArenaFormat(
        min_cards=int(raw["minCards"]), max_cards=int(raw["maxCards"]),
        max_copies=int(raw["maxCopies"]),
        unlimited=frozenset(
            normalize_card_name(name) for name in raw.get("unlimitedCopies") or ()),
        side_decks=MappingProxyType(sides),
        sections=MappingProxyType(sections))


def unsupported_deck_names(deck, supported, boards=("main", "side")):
    """Return the deck's distinct card names Arena holds no script for.

    Names are returned as they are stored, so they can be shown to the person
    who recognizes them, and deduplicated by lookup key, so one card listed in
    both boards is reported once. An empty ``supported`` set means the rules
    were unavailable, and nothing is reported. ``boards`` narrows the look to
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
            key = card_key(stored)
            if not key or key in supported:
                continue
            found.setdefault(key, stored)
    return sorted(found.values(), key=str.casefold)
