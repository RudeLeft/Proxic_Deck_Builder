"""Bundle one deck as a Proxic Arena package: its decklist plus its card pictures.

Proxic Arena draws a card from a real picture only when the deck brought one,
so a deck goes to Arena as one package -- the decklist and the PNGs -- built in
memory and sent straight to the player's Deck Depot by ``arena/connection.py``
(Arena spec 0022). Nothing is saved to disk: there is no zip to keep (ARN-014),
except the one File > Test Deck Export writes for testing (``ui/testing_export.py``,
ARN-019).

    <DeckName>.txt
    real_cards/<Card Name> [SET].png   (or .jpg, when Arena takes JPEGs)
    real_cards/<Card Name> [SET-NUM].png  (another printing of a name, see below)
    art_crops/<Card Name> [SET].jpg    (when Arena takes art crops)
    pictures.json          (only when some pictures were left out, see below)

What Arena takes is its deck rules' to say (`ArenaFormat.picture_formats`,
`art_crops`); `arena/pictures.py` makes the light JPEG and fetches the crop.
Pictures are stored in the zip, not compressed, and every member carries one
fixed time, so the same deck sent twice is the same package.

The names follow Arena's own rule: the trailing `[SET]` is stripped and what
remains is matched against the card name, lowercased, with no accent folding.
So accents are kept exactly as printed, and only characters a filesystem
refuses are removed. Arena keys a picture by name, so this bundles ONE image
per distinct card name: the deck's first printing of it. An Arena that takes
printing pictures (Arena spec 0053, ``ArenaFormat.printing_pictures``) also
gets each OTHER printing of a name the deck holds in more than one, as
`<Card Name> [SET-NUM]` with its art crop, so four Mountain printings look
like four Mountains; a deck with one printing per name sends the same as
before.

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


class ArenaExportStopped(Exception):
    """Raised by a caller's question to Arena to stop a send before it is packed.

    Arena refusing this computer's link is the one case: packing a deck it
    will refuse anyway only wastes the player's time.
    """


# Where Arena's package files card pictures.
REAL_ART_DIRECTORY = "real_cards"
# Where it files each picture's art crop (Arena spec 0051).
CROP_DIRECTORY = "art_crops"
# Names the pictures a package leaves out because Arena already holds them.
PICTURES_MEMBER = "pictures.json"

# Characters a Windows or POSIX filename cannot hold. Accents and spaces are
# deliberately NOT touched: Arena matches the name as printed, case-folded
# only, so stripping accents here would make Eowyn's art unfindable.
_ILLEGAL_FILENAME_CHARACTERS = '<>:"/\\|?*'
# A build unit that is another printing of a name (Arena spec 0053): neither a
# card's own picture nor a token's, and never reported as missing art.
PRINTING = "printing"


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
    # The other printings of names held in more than one (Arena spec 0053).
    printing_cards: tuple[dict, ...] = ()

    @classmethod
    def from_deck(cls, deck, deck_text, cache_dir, token_cards=(), printings=False):
        return cls(
            deck_name=str(getattr(deck, "name", "Deck") or "Deck"),
            deck_text=str(deck_text),
            cards=tuple(copy.deepcopy(distinct_art_cards(deck))),
            cache_dir=os.fspath(cache_dir),
            token_cards=tuple(copy.deepcopy(list(token_cards))),
            printing_cards=tuple(copy.deepcopy(
                other_printing_cards(deck) if printings else [])))


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
    # Art crops sent or named with the pictures (Arena spec 0051).
    crop_count: int = 0


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


def printing_filename(card):
    """Return `<Card Name> [SET-NUM].png` for one printing (Arena spec 0053).

    None without a name, set code or collector number: such a card can only
    wear its name's picture. Arena splits set from number at the first ``-``,
    which a set code never holds.
    """
    named = art_filename(card)
    set_code = str((card or {}).get("set_code") or "").strip().upper()
    number = "".join(
        character for character in str((card or {}).get("collector_number") or "")
        if character not in _ILLEGAL_FILENAME_CHARACTERS).strip()
    if not named or not set_code or not number:
        return None
    return f"{named[:-len(f' [{set_code}].png')]} [{set_code}-{number}].png"


def other_printing_cards(deck):
    """Return each main-deck printing of a name after the deck's first, in deck order.

    The first printing of a name is that name's picture
    (`distinct_art_cards`); these are the rest, one per printing, so a name
    held in one printing adds nothing.
    """
    first, others = {}, {}
    for entry in deck.entries("main"):
        card = entry.get("card") or {}
        name = front_face_name(card.get("name")).casefold()
        printing = (str(card.get("set_code") or "").casefold(),
                    str(card.get("collector_number") or "").casefold())
        if not name or not all(printing):
            continue
        if name not in first:
            first[name] = printing
        elif printing != first[name] and (name, printing) not in others:
            others[(name, printing)] = card
    return list(others.values())


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


# One fixed time for every member, so the same deck makes the same package
# and Arena can say it already holds it (zip members otherwise carry the time
# they were written).
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def _member(name, compress):
    info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    return info


def write_package(job, image_paths, held=(), cancel_event=None):
    """Return the package's bytes: the decklist, each picture not ``held``, and ``pictures.json``.

    ``image_paths`` maps each `real_cards/...` or `art_crops/...` member to a
    local picture; ``held`` maps a member Arena already holds to its SHA-256,
    to name instead of send. Pictures are stored, not compressed: they are
    compressed already, and squeezing them again only costs time.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(_member(f"{_safe_member(job.deck_name)}.txt", True), job.deck_text)
        held = dict(held)
        for member, source in image_paths.items():
            check_cancel(
                cancel_event, "Sending was cancelled.", ArenaExportCancelled)
            if member not in held:
                with open(source, "rb") as handle:
                    archive.writestr(_member(member, False), handle.read())
        if held:
            archive.writestr(
                _member(PICTURES_MEMBER, True), json.dumps(held, sort_keys=True))
    return buffer.getvalue()


def _sent_name(member_name, path):
    """The member name with the extension of the file actually sent (``.png`` or ``.jpg``)."""
    stem = member_name[:-len(".png")] if member_name.endswith(".png") else member_name
    extension = os.path.splitext(path)[1].casefold() or ".png"
    return f"{stem}{extension}"


def build_package(
        job, ensure_image, progress_cb=None, cancel_event=None,
        back_faces_of=None, ensure_face=None, known_pictures=None,
        prepare_picture=None, ensure_crop=None, fingerprint=None):
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
    every picture is sent -- except when it raises ``ArenaExportStopped``,
    which stops the send (the link was removed).

    What Arena's deck rules ask for is injected too (`arena/pictures.py`):
    `prepare_picture(path)` turns a PNG into the file to send (the light
    JPEG), `ensure_crop(card, face_index)` returns a picture's art crop or
    None, and `fingerprint(path)` its SHA-256. Each is optional; without them
    the PNGs go as they are, with no crops. Every download is told about
    `cancel_event`, so Cancel stops a picture mid-download.
    """
    # One unit per image: each card's front, then its back faces, then each
    # other printing (Arena spec 0053), then each token card. A back face is
    # filed under its own name but carries the front's set code.
    units = []
    for card in job.cards:
        units.append((card, None, card, False))
        if back_faces_of and ensure_face:
            for face_index, face_name in back_faces_of(card):
                units.append((card, face_index, {
                    "name": face_name, "set_code": card.get("set_code")}, False))
    for card in job.printing_cards:
        units.append((card, None, card, PRINTING))
    for token in job.token_cards:
        units.append((token, None, token, True))
    total = len(units)
    image_paths = {}
    missing = []
    back_faces = 0
    token_pictures = 0
    crops = 0
    for index, (card, face_index, art_card, is_token) in enumerate(units, 1):
        check_cancel(
            cancel_event, "Sending was cancelled.", ArenaExportCancelled)
        member_name = (printing_filename(art_card) if is_token is PRINTING
                       else art_filename(art_card))
        if progress_cb:
            progress_cb("download", index - 1, total, member_name or "")
        path = None
        if member_name:
            try:
                path = (
                    ensure_image(card, job.cache_dir, cancel_event=cancel_event)
                    if face_index is None
                    else ensure_face(card, face_index, job.cache_dir, cancel_event=cancel_event))
                if path and prepare_picture:
                    path = prepare_picture(path)
            except JobCancelled:
                raise ArenaExportCancelled("Sending was cancelled.") from None
            except Exception:
                path = None
        if path and os.path.exists(path):
            member = f"{REAL_ART_DIRECTORY}/{_sent_name(member_name, path)}"
            # A name already filed keeps its first image: Arena's index is
            # keyed by name, so a second one could only overwrite it.
            if not any(_same_name(member, filed) for filed in image_paths):
                image_paths[member] = path
                if face_index is not None:
                    back_faces += 1
                if is_token is True:
                    token_pictures += 1
                crop = _crop_for(ensure_crop, card, face_index, cancel_event)
                if crop:
                    image_paths[f"{CROP_DIRECTORY}/{_sent_name(member_name, crop)}"] = crop
                    crops += 1
        elif not is_token:
            # A card whose art cannot be fetched MUST NOT fail the send: the
            # deck is still worth having, and Arena draws its own card for it.
            # (An other printing is not missing: it wears its name's picture.)
            missing.append(front_face_name(art_card.get("name")) or "(unnamed)")
        if progress_cb:
            progress_cb("download", index, total, member_name or "")

    held = {}
    if known_pictures and image_paths:
        check_cancel(
            cancel_event, "Sending was cancelled.", ArenaExportCancelled)
        sha_of = fingerprint or _sha256
        hashes = {member: sha_of(path) for member, path in image_paths.items()}
        try:
            known = set(known_pictures(sorted(set(hashes.values()))) or ())
        except ArenaExportStopped:
            raise
        except Exception:
            known = set()
        held = {member: sha for member, sha in hashes.items() if sha in known}

    if progress_cb:
        progress_cb("package", total, total, package_filename(job.deck_name))
    data = write_package(job, image_paths, held, cancel_event=cancel_event)
    pictures = len(image_paths) - crops
    return ArenaPackage(
        data=data, filename=package_filename(job.deck_name),
        card_count=len(job.cards), image_count=pictures,
        held_count=sum(1 for member in held if member.startswith(f"{REAL_ART_DIRECTORY}/")),
        missing_art=tuple(missing),
        back_face_count=back_faces, token_pictures=token_pictures, crop_count=crops)


def _same_name(member, filed):
    """Whether two `real_cards/` members name one picture (``.png`` or ``.jpg`` alike)."""
    return (filed.startswith(f"{REAL_ART_DIRECTORY}/")
            and os.path.splitext(filed)[0] == os.path.splitext(member)[0])


def _crop_for(ensure_crop, card, face_index, cancel_event):
    """A picture's art crop when the caller fetches them, else None; a failure is only no crop."""
    if not ensure_crop:
        return None
    try:
        crop = ensure_crop(card, face_index)
    except JobCancelled:
        raise ArenaExportCancelled("Sending was cancelled.") from None
    except Exception:
        return None
    check_cancel(cancel_event, "Sending was cancelled.", ArenaExportCancelled)
    return crop if crop and os.path.exists(crop) else None
