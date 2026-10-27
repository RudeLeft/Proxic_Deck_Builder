"""Bundle one deck as a Proxic Arena package: its decklist plus its card pictures.

Proxic Arena draws a card from a real picture only when the deck brought one,
so a deck goes to Arena as one package -- the decklist and the PNGs -- built in
memory and sent straight to the player's Deck Depot by ``arena/connection.py``
(Arena spec 0022). Nothing is saved to disk: there is no zip to keep (ARN-014).

    <DeckName>.txt
    real_cards/<Card Name> [SET].png
    pictures.json          (only when some pictures were left out, see below)

The names follow Arena's own rule: the trailing `[SET]` is stripped and what
remains is matched against the card name, lowercased, with no accent folding.
So accents are kept exactly as printed, and only characters a filesystem
refuses are removed. Arena keys a picture by name alone, which means two
printings of one name would collide, so this bundles ONE image per distinct
card name rather than per printing.

A card that turns over (transform, modal double-faced, battle) also bundles its
back face, filed under that face's own name in the same shape, because Arena
maps an image to a face by that face's name and draws a transformed permanent
from it. Split, adventure and flip cards are one picture and add nothing.

The decklist's Tokens section lists the token cards the deck holds, and each
brings its picture the same way: in a game, Arena dresses a token in the
picture of the same name (ARN-013).

Arena keeps every picture it has been sent. Before packing, the caller may ask
it which of this deck's pictures the player's decks already hold
(``known_pictures``); those are left out and named in ``pictures.json`` by
their SHA-256 instead, so a big deck stays under the upload limit and sending
a deck again is quick.

The PNGs come from `printing/service.py`, which already downloads, validates,
caches and atomically replaces Scryfall's high-quality PNG per exact printing,
including a separate image per face of a double-faced card. Nothing here
re-downloads what printing has already cached.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import io
import json
import os
import zipfile

from mtgdb.core.background_jobs import JobCancelled, check_cancel


class ArenaExportCancelled(JobCancelled):
    """Raised when the user cancels a send (BGJ-004: one catchable base)."""


# Where Arena's package files card pictures.
REAL_ART_DIRECTORY = "real_cards"
# Names the pictures a package leaves out because Arena already holds them.
PICTURES_MEMBER = "pictures.json"

# Characters a Windows or POSIX filename cannot hold. Accents and spaces are
# deliberately NOT touched: Arena matches the name as printed, case-folded
# only, so stripping accents here would make Eowyn's art unfindable.
_ILLEGAL_FILENAME_CHARACTERS = '<>:"/\\|?*'


@dataclass(frozen=True)
class ArenaExportJob:
    """A stable snapshot of one deck's package, safe to hand to a worker.

    ``cards`` are the distinct main-deck cards that bring a picture;
    ``token_cards`` the token cards of the decklist's Tokens section.
    """

    deck_name: str
    deck_text: str
    cards: tuple[dict, ...]
    cache_dir: str
    token_cards: tuple[dict, ...] = ()

    @classmethod
    def from_deck(cls, deck, deck_text, cache_dir, token_cards=()):
        return cls(
            deck_name=str(getattr(deck, "name", "Deck") or "Deck"),
            deck_text=str(deck_text),
            cards=tuple(copy.deepcopy(distinct_art_cards(deck))),
            cache_dir=os.fspath(cache_dir),
            token_cards=tuple(copy.deepcopy(list(token_cards))))


@dataclass(frozen=True)
class ArenaPackage:
    """One built package, and what went into it."""

    data: bytes
    filename: str
    card_count: int
    # Pictures the deck has, sent in the package or already held by Arena.
    image_count: int
    # Of those, the ones Arena already held, so left out (``pictures.json``).
    held_count: int = 0
    missing_art: tuple[str, ...] = ()
    # How many of ``image_count`` are the back faces of cards that turn over.
    back_face_count: int = 0
    # Token cards in the Tokens section that bring a picture.
    token_pictures: int = 0


def front_face_name(name):
    """Return the face Arena indexes, which is the one before any ` // `."""
    return str(name or "").split(" // ", 1)[0].strip()


def art_filename(card):
    """Return `<Card Name> [SET].png` for one card, or None without a name.

    The set code is upper-cased for legibility and plays no part in Arena's
    lookup, which strips it. A card with no stored set still gets a usable
    name, because the suffix is what Arena discards anyway.
    """
    name = front_face_name((card or {}).get("name"))
    if not name:
        return None
    cleaned = "".join(
        character for character in name
        if character not in _ILLEGAL_FILENAME_CHARACTERS).strip()
    # A name of nothing but illegal characters cannot be filed.
    if not cleaned:
        return None
    set_code = str((card or {}).get("set_code") or "").strip().upper()
    return f"{cleaned} [{set_code}].png" if set_code else f"{cleaned}.png"


def distinct_art_cards(deck):
    """Return one card per distinct name of the main deck, in deck order.

    Arena has no sideboard, so a card held only there is not exported and gets
    no art. Arena's art index is keyed by card name, so a second printing of
    the same name could only overwrite the first. The deck's own first printing
    of a name wins, which is the copy the player is holding.
    """
    seen = {}
    for entry in deck.entries("main"):
        card = entry.get("card") or {}
        name = front_face_name(card.get("name")).casefold()
        if name and name not in seen:
            seen[name] = card
    return list(seen.values())


def package_filename(deck_name):
    """The package's file name, as Arena records where a deck came from."""
    return f"{_safe_member(deck_name)}.zip"


def _safe_member(name):
    """Return a zip-safe stem for the decklist member."""
    cleaned = "".join(
        character for character in str(name or "Deck")
        if character not in _ILLEGAL_FILENAME_CHARACTERS).strip()
    return cleaned or "Deck"


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_package(job, image_paths, held=(), cancel_event=None):
    """Return the package's bytes: the decklist, each picture not ``held``, and ``pictures.json``.

    ``image_paths`` maps each `real_cards/...` member to a local PNG; ``held``
    maps a member Arena already holds to its SHA-256, to name instead of send.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{_safe_member(job.deck_name)}.txt", job.deck_text)
        held = dict(held)
        for member, source in image_paths.items():
            check_cancel(
                cancel_event, "Sending was cancelled.", ArenaExportCancelled)
            if member not in held:
                archive.write(source, member)
        if held:
            archive.writestr(PICTURES_MEMBER, json.dumps(held, sort_keys=True))
    return buffer.getvalue()


def build_package(
        job, ensure_image, progress_cb=None, cancel_event=None,
        back_faces_of=None, ensure_face=None, known_pictures=None):
    """Download each picture, ask which Arena holds, then pack the rest.

    `ensure_image` is injected rather than imported so this module states what
    it needs -- a validated local PNG for one card -- without owning the
    download, the cache, or the HTTP policy, all of which belong to
    `printing/service.py`. Back faces are the same: `back_faces_of(card)` names
    each turned-over face as ``(face index, face name)`` and
    `ensure_face(card, face_index, cache_dir)` fetches it, so which cards turn
    over and where a face's image lives stay with the owner of face handling.
    Without both, only front faces are bundled. `known_pictures(hashes)`
    returns the hashes Arena already holds; without it, or when it fails,
    every picture is sent.
    """
    # One unit per image: each card's front, then its back faces, then each
    # token card. A back face is filed under its own name but carries the
    # front's set code.
    units = []
    for card in job.cards:
        units.append((card, None, card, False))
        if back_faces_of and ensure_face:
            for face_index, face_name in back_faces_of(card):
                units.append((card, face_index, {
                    "name": face_name, "set_code": card.get("set_code")}, False))
    for token in job.token_cards:
        units.append((token, None, token, True))
    total = len(units)
    image_paths = {}
    missing = []
    back_faces = 0
    token_pictures = 0
    for index, (card, face_index, art_card, is_token) in enumerate(units, 1):
        check_cancel(
            cancel_event, "Sending was cancelled.", ArenaExportCancelled)
        member_name = art_filename(art_card)
        if progress_cb:
            progress_cb("download", index - 1, total, member_name or "")
        path = None
        if member_name:
            try:
                path = (
                    ensure_image(card, job.cache_dir) if face_index is None
                    else ensure_face(card, face_index, job.cache_dir))
            except Exception:
                path = None
        member = f"{REAL_ART_DIRECTORY}/{member_name}"
        if path and os.path.exists(path):
            # A name already filed keeps its first image: Arena's index is
            # keyed by name, so a second one could only overwrite it.
            if member not in image_paths:
                image_paths[member] = path
                if face_index is not None:
                    back_faces += 1
                if is_token:
                    token_pictures += 1
        elif not is_token:
            # A card whose art cannot be fetched MUST NOT fail the send: the
            # deck is still worth having, and Arena draws its own card for it.
            missing.append(front_face_name(art_card.get("name")) or "(unnamed)")
        if progress_cb:
            progress_cb("download", index, total, member_name or "")

    held = {}
    if known_pictures and image_paths:
        hashes = {member: _sha256(path) for member, path in image_paths.items()}
        try:
            known = set(known_pictures(sorted(set(hashes.values()))) or ())
        except Exception:
            known = set()
        held = {member: sha for member, sha in hashes.items() if sha in known}

    if progress_cb:
        progress_cb("package", total, total, package_filename(job.deck_name))
    data = write_package(job, image_paths, held, cancel_event=cancel_event)
    return ArenaPackage(
        data=data, filename=package_filename(job.deck_name),
        card_count=len(job.cards), image_count=len(image_paths),
        held_count=len(held), missing_art=tuple(missing),
        back_face_count=back_faces, token_pictures=token_pictures)
