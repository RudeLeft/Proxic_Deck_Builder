"""Proxic Arena export contract: the package, the printings, the tokens and the window (ARN-*)."""

import ast
import hashlib
import io
import json
import pathlib
import re
import sys
import tempfile
import threading
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mtgdb.arena.connection import SENT, SendResult
from mtgdb.arena.export import (
    PICTURES_MEMBER, REAL_ART_DIRECTORY, ArenaExportCancelled, ArenaExportJob,
    ArenaPackage, art_filename, build_package, distinct_art_cards,
    front_face_name,
)
from mtgdb.core.background_jobs import JobCancelled
from mtgdb.deck.arena_check import check_for_arena
from mtgdb.deck.arena_support import load_arena_rules, rules_from_document
from mtgdb.deck.arena_tokens import split_for_arena
from mtgdb.deck.io import deck_to_text
from mtgdb.deck.model import Deck
from mtgdb.printing.service import back_faces

# A minimal valid-looking PNG header. These tests never reach the network:
# the downloader is injected (ARN-003), so the stub stands in for it.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _card(identifier, name, set_code="tst", png="https://example/art.png",
          number="1", type_line="Instant", content_kind="card"):
    return {
        "id": identifier, "name": name, "set_code": set_code,
        "collector_number": number, "image_png": png, "type_line": type_line,
        "content_kind": content_kind,
    }


def _stub_downloader(calls=None):
    """Stand in for printing.service.ensure_png without a network.

    Each card gets its own bytes, so each picture has its own hash.
    """
    def ensure(card, cache_dir):
        if calls is not None:
            calls.append(card.get("id"))
        if not card.get("image_png"):
            return None
        path = pathlib.Path(cache_dir) / f"{card['id']}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_PNG_BYTES + str(card["id"]).encode())
        return str(path)
    return ensure


def _two_faced(identifier, front, back, set_code, layout="transform"):
    """A card with its own image on each face, shaped like stored card data."""
    return {
        "id": identifier, "name": f"{front} // {back}", "set_code": set_code,
        "collector_number": "1", "layout": layout,
        "image_png": f"https://example/{identifier}-front.png",
        "card_faces": [
            {"name": front, "image_uris": {
                "png": f"https://example/{identifier}-front.png"}},
            {"name": back, "image_uris": {
                "png": f"https://example/{identifier}-back.png"}},
        ],
    }


def _stub_face_downloader(calls=None, fail=()):
    """Stand in for printing.service.ensure_face_png without a network."""
    def ensure(card, face_index, cache_dir):
        if calls is not None:
            calls.append((card.get("id"), face_index))
        if (card.get("id"), face_index) in fail:
            raise OSError("no art for this face")
        path = pathlib.Path(cache_dir) / f"{card['id']}.face{face_index}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_PNG_BYTES + f"{card['id']}-{face_index}".encode())
        return str(path)
    return ensure


def _members(package):
    with zipfile.ZipFile(io.BytesIO(package.data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _package(deck, cache, tokens=(), **options):
    parts_text = deck_to_text(deck, include_side=False, tokens=tokens)
    job = ArenaExportJob.from_deck(
        deck, parts_text, cache, token_cards=[card for card, _qty in tokens])
    return build_package(job, options.pop("ensure", _stub_downloader()), **options)


# -- the package (arena/export.py) --------------------------------------------

def _arena_index_key(filename):
    """Arena's own rule: the trailing [SET] stripped, the rest case-folded.

    Written out here rather than imported: Proxic Arena is a separate project
    this repository cannot reach, so the rule the names must satisfy is
    restated as an independent expectation (VER-010).
    """
    stem = pathlib.Path(filename).stem
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", stem).strip().lower()


def _art_naming_check():
    """ARN-002: every name resolves back to the card Arena looks up."""
    cards = [
        _card("a", "Lightning Bolt", "lea"),
        _card("b", "Delver of Secrets // Insectile Aberration", "isd"),
        _card("c", "Éowyn, Lady of Rohan", "ltr"),
        _card("d", "Ach! Hans, Run!", "unh"),
        # A name carrying characters no filesystem accepts.
        _card("e", 'Ko:tar? "Quote"/Slash', "tst"),
    ]
    names = {card["id"]: art_filename(card) for card in cards}
    return (
        names["a"] == "Lightning Bolt [LEA].png"
        # Filed under the front face alone.
        and names["b"] == "Delver of Secrets [ISD].png"
        # Accents survive: Arena case-folds but does not strip them.
        and names["c"] == "Éowyn, Lady of Rohan [LTR].png"
        and names["d"] == "Ach! Hans, Run! [UNH].png"
        and not any(
            character in names["e"] for character in '<>:"/\\|?*'[:-1])
        and all(
            _arena_index_key(names[card["id"]])
            == front_face_name(card["name"]).lower()
            for card in cards[:4])
        and art_filename({"name": ""}) is None
        and art_filename({"name": "///"}) is None
        and art_filename(None) is None
        and art_filename({"name": "Nameless Set"}) == "Nameless Set.png")


def _one_image_per_name_check():
    """ARN-002: Arena's name-keyed index gets exactly one image per name."""
    deck = Deck("Collision", "commander")
    deck.add(_card("bolt-lea", "Lightning Bolt", "lea"), "main", 1)
    deck.add(_card("bolt-m10", "Lightning Bolt", "m10"), "main", 1)
    deck.add(_card("mind", "Mind Stone", "m20"), "main", 1)
    deck.add(_card("side", "Subgoyf", "mb2"), "side", 2)
    deck.add(_card("bolt-side", "Lightning Bolt", "4ed"), "side", 1)
    chosen = distinct_art_cards(deck)
    return (
        [front_face_name(card["name"]) for card in chosen] == ["Lightning Bolt", "Mind Stone"]
        and chosen[0]["id"] == "bolt-lea" and len(chosen) == 2)


def _package_layout_check():
    """ARN-001, ARN-004, ARN-014: decklist and pictures, built in memory, nothing on disk."""
    deck = Deck("Layout Probe", "commander")
    deck.add(_card("a", "Lightning Bolt", "lea"), "main", 4)
    deck.add(_card("c", "Éowyn, Lady of Rohan", "ltr"), "main", 1)
    deck.add(_card("x", "Subgoyf", "mb2", png=None), "main", 1)
    deck.add(_card("s", "Sideboard Only", "sbo"), "side", 3)
    with tempfile.TemporaryDirectory() as directory:
        base = pathlib.Path(directory)
        events = []
        package = _package(deck, base / "cache", progress_cb=lambda *args: events.append(args))
        written = sorted(
            str(path.relative_to(base)) for path in base.rglob("*") if path.is_file())
    members = _members(package)
    decklist = members.get("Layout Probe.txt", b"").decode("utf-8")
    return (
        isinstance(package, ArenaPackage)
        and package.filename == "Layout Probe.zip"
        and sorted(members) == [
            "Layout Probe.txt",
            f"{REAL_ART_DIRECTORY}/Lightning Bolt [LEA].png",
            f"{REAL_ART_DIRECTORY}/Éowyn, Lady of Rohan [LTR].png"]
        and decklist.startswith("// Layout Probe (commander)")
        and "Sideboard" not in decklist and "Sideboard Only" not in decklist
        # The art-less card is counted, and did not stop the package.
        and package.missing_art == ("Subgoyf",)
        and (package.image_count, package.card_count, package.held_count) == (2, 3, 0)
        and events and events[-1] == ("package", 3, 3, "Layout Probe.zip")
        # Only the picture cache was written: no package on disk.
        and all(name.startswith("cache") for name in written))


def _held_pictures_check():
    """ARN-014: pictures Arena already holds are named in pictures.json, not sent."""
    deck = Deck("Held", "modern")
    deck.add(_card("a", "Lightning Bolt", "lea"), "main", 4)
    deck.add(_card("b", "Shock", "m21"), "main", 4)
    asked = []
    with tempfile.TemporaryDirectory() as directory:
        cache = pathlib.Path(directory)
        bolt_hash = hashlib.sha256(_PNG_BYTES + b"a").hexdigest()
        shock_hash = hashlib.sha256(_PNG_BYTES + b"b").hexdigest()

        def known(hashes):
            asked.append(list(hashes))
            return {bolt_hash, "f" * 64}

        def broken(_hashes):
            raise OSError("Arena went away")

        held = _package(deck, cache, known_pictures=known)
        fallback = _package(deck, cache, known_pictures=broken)
    members = _members(held)
    named = json.loads(members.get(PICTURES_MEMBER, b"{}"))
    return (
        asked == [sorted([bolt_hash, shock_hash])]
        and f"{REAL_ART_DIRECTORY}/Lightning Bolt [LEA].png" not in members
        and f"{REAL_ART_DIRECTORY}/Shock [M21].png" in members
        and named == {f"{REAL_ART_DIRECTORY}/Lightning Bolt [LEA].png": bolt_hash}
        and (held.image_count, held.held_count) == (2, 1)
        # When the question fails, every picture goes.
        and PICTURES_MEMBER not in _members(fallback)
        and fallback.held_count == 0
        and f"{REAL_ART_DIRECTORY}/Lightning Bolt [LEA].png" in _members(fallback))


def _tokens_in_package_check():
    """ARN-013: token cards ride in the Tokens section with their pictures."""
    deck = Deck("Ape Tribal", "modern")
    pongify = _card("po", "Pongify", "plc", number="44")
    pongify["related_parts"] = json.dumps([
        {"component": "token", "name": "Ape", "type_line": "Token Creature — Ape"}])
    deck.add(pongify, "main", 4)
    deck.add(_card("fo", "Forest", "m21", number="274", type_line="Basic Land — Forest"), "main", 56)
    deck.add(_card("ape", "Ape", "totc", number="15", type_line="Token Creature — Ape",
                   content_kind="token"), "main", 1)
    deck.add(_card("pictureless", "Treasure", "tfra", png=None, type_line="Token Artifact",
                   content_kind="token"), "main", 1)
    parts = split_for_arena(deck)
    with tempfile.TemporaryDirectory() as directory:
        package = _package(parts.cards, pathlib.Path(directory), tokens=parts.tokens)
    members = _members(package)
    decklist = members["Ape Tribal.txt"].decode("utf-8")
    return (
        f"{REAL_ART_DIRECTORY}/Ape [TOTC].png" in members
        and decklist.endswith("// Tokens\n1 Ape [TOTC:15]\n1 Treasure [TFRA:1]\n")
        and "56 Forest" in decklist and "Ape [TOTC:15]" not in decklist.split("// Tokens")[0]
        and package.token_pictures == 1
        # A token without a picture is not a card without art.
        and package.missing_art == ()
        and package.card_count == 2)


def _cancel_check():
    """ARN-005: a cancelled package raises the shared cancel and writes nothing."""
    deck = Deck("Cancelled", "commander")
    for index in range(4):
        deck.add(_card(f"c{index}", f"Card {index}"), "main", 1)
    cancel = threading.Event()
    cancel.set()
    raised = None
    with tempfile.TemporaryDirectory() as directory:
        try:
            _package(deck, pathlib.Path(directory), cancel_event=cancel)
        except JobCancelled as exc:
            raised = exc
        written = list(pathlib.Path(directory).rglob("*"))
    return isinstance(raised, ArenaExportCancelled) and written == []


def _back_face_check():
    """ARN-002/ARN-003: a card that turns over also bundles its back face."""
    deck = Deck("Faces", "modern")
    deck.add(_two_faced("d1", "Delver of Secrets", "Insectile Aberration", "isd"), "main", 2)
    deck.add(_two_faced("m1", "Valki, God of Lies", "Tibalt, Cosmic Impostor", "khm",
                        layout="modal_dfc"), "main", 1)
    deck.add({"id": "g1", "name": "Bonecrusher Giant // Stomp", "set_code": "eld",
              "collector_number": "1", "image_png": "https://example/g1.png",
              "card_faces": [{"name": "Bonecrusher Giant"}, {"name": "Stomp"}]}, "main", 1)
    deck.add(_card("b", "Lightning Bolt", "lea"), "main", 4)
    deck.add(_two_faced("s1", "Side Front", "Side Back", "sss"), "side", 1)
    calls = []
    with tempfile.TemporaryDirectory() as directory:
        cache = pathlib.Path(directory)
        full = _package(deck, cache, back_faces_of=back_faces,
                        ensure_face=_stub_face_downloader(calls))
        failed = _package(deck, cache, back_faces_of=back_faces,
                          ensure_face=_stub_face_downloader(fail={("d1", 1)}))
        bare = _package(deck, cache)
    art = sorted(m for m in _members(full) if m.startswith(f"{REAL_ART_DIRECTORY}/"))
    return (
        art == sorted([
            f"{REAL_ART_DIRECTORY}/Delver of Secrets [ISD].png",
            f"{REAL_ART_DIRECTORY}/Insectile Aberration [ISD].png",
            f"{REAL_ART_DIRECTORY}/Valki, God of Lies [KHM].png",
            f"{REAL_ART_DIRECTORY}/Tibalt, Cosmic Impostor [KHM].png",
            f"{REAL_ART_DIRECTORY}/Bonecrusher Giant [ELD].png",
            f"{REAL_ART_DIRECTORY}/Lightning Bolt [LEA].png"])
        and (full.image_count, full.card_count, full.back_face_count) == (6, 4, 2)
        and sorted(calls) == [("d1", 1), ("m1", 1)]
        and "Delver of Secrets // Insectile Aberration" in _members(full)["Faces.txt"].decode()
        and failed.missing_art == ("Insectile Aberration",)
        and (failed.image_count, failed.back_face_count) == (5, 1)
        and (bare.image_count, bare.back_face_count) == (4, 0))


def _injected_downloader_check():
    """ARN-003: the package module reaches neither printing nor the network."""
    source = (ROOT / "mtgdb/arena/export.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    calls = []
    deck = Deck("Injected", "commander")
    deck.add(_card("a", "Lightning Bolt"), "main", 1)
    with tempfile.TemporaryDirectory() as directory:
        _package(deck, pathlib.Path(directory), ensure=_stub_downloader(calls))
    return (
        not any(
            name.startswith(("mtgdb.printing", "mtgdb.ui", "tkinter", "sqlite3"))
            or name == "mtgdb.core.net"
            for name in imported)
        and "mtgdb.core.background_jobs" in imported
        and calls == ["a"])


# -- the printings (deck/arena_printings.py) ----------------------------------

def _row(identifier, name, set_code, number, **extra):
    """A stored Scryfall printing, shaped like a `cards` row."""
    row = {
        "id": identifier, "name": name, "set_code": set_code,
        "collector_number": number, "lang": "en", "paper": 1, "promo": 0,
        "frame_effects": "[]", "promo_types": "[]", "universes_beyond": 0,
        "image_png": f"https://example/{identifier}.png",
    }
    row.update(extra)
    return row


def _printing_support():
    """A small rules document's support: printings in Arena's own order."""
    from mtgdb.deck.arena_support import ArenaSupport

    return ArenaSupport(
        names=frozenset({
            "island", "lightning_bolt", "ugin_the_spirit_dragon",
            "llanowar_elves", "nameless_wonder", "shock"}),
        printings={
            # Arena lists TRK first: Arena decides, never this program.
            "island": ("TRK 319", "C21 400", "M21 263"),
            "lightning_bolt": ("2X2 117", "M10 146"),
            "ugin_the_spirit_dragon": ("UGF 1",),
            "llanowar_elves": ("M19 314", "DOM 168"),
            "nameless_wonder": ("OLD 9",),
            "shock": ("SHW 5", "SHW 300"),
        },
        # CON is Conflux's second code; UGIN is only Ugin's Fate's Scryfall
        # code, which Arena's set codes do not accept.
        set_codes={
            "M21": "M21", "TRK": "TRK", "C21": "C21", "M10": "M10",
            "2X2": "2X2", "UGF": "UGF", "M19": "M19", "DOM": "DOM",
            "OLD": "OLD", "SHW": "SHW"},
        editions={
            "M21": ("m21", "", ""), "TRK": ("trk", "", ""), "C21": ("c21", "", ""),
            "M10": ("m10", "", ""), "2X2": ("2x2", "", ""), "UGF": ("ugin", "", ""),
            "M19": ("m19", "", ""), "DOM": ("dom", "", ""), "OLD": ("old", "", ""),
            "SHW": ("shw", "", "")})


def _printing_swap_check():
    """ARN-011: a printing Arena lacks goes as one it has, in Arena's order, and only then."""
    from mtgdb.deck.arena_printings import (
        arena_export_deck, choose_printing, printing_accepted,
    )
    from mtgdb.deck.arena_support import ArenaSupport

    support = _printing_support()
    rows = {
        "Island": [
            _row("island-mdd", "Island", "mdd", "294"),
            _row("island-m21", "Island", "m21", "263"),
            _row("island-trk", "Island", "trk", "319", universes_beyond=1),
            _row("island-c21", "Island", "c21", "400"),
        ],
        "Lightning Bolt": [
            _row("bolt-m10", "Lightning Bolt", "m10", "146"),
            _row("bolt-m10-promo", "Lightning Bolt", "m10", "146p", promo=1),
            _row("bolt-2x2", "Lightning Bolt", "2x2", "117"),
        ],
        "Ugin, the Spirit Dragon": [_row("ugin", "Ugin, the Spirit Dragon", "ugin", "1")],
        "Llanowar Elves": [_row("elves-promo", "Llanowar Elves", "pdom", "168s")],
        "Nameless Wonder": [_row("wonder", "Nameless Wonder", "xyz", "9")],
        "Shock": [
            _row("shock-xyz", "Shock", "xyz", "1"),
            _row("shock-showcase", "Shock", "shw", "5", frame_effects='["showcase"]'),
            _row("shock-plain", "Shock", "shw", "300"),
        ],
    }
    asked = []

    def printings_of(name):
        asked.append(name)
        return rows.get(name, [])

    island_mdd = rows["Island"][0]
    bolt_promo = rows["Lightning Bolt"][1]
    fine = Deck("Fine", "modern")
    for card in (rows["Island"][1], rows["Lightning Bolt"][0]):
        fine.add(card, "main", 4)
    fine_out, fine_swaps = arena_export_deck(fine, support, printings_of)
    asked_for_fine = list(asked)

    deck = Deck("Mixed", "modern")
    deck.add(island_mdd, "main", 20)
    deck.add(rows["Island"][1], "main", 2)
    deck.add(bolt_promo, "main", 4)
    deck.add(rows["Ugin, the Spirit Dragon"][0], "main", 1)
    deck.add(rows["Llanowar Elves"][0], "main", 4)
    deck.add(rows["Nameless Wonder"][0], "main", 1)
    deck.add(rows["Shock"][0], "main", 4)
    deck.add(_row("mystery", "Mystery Card", "zzz", "1"), "main", 1)
    deck.add(bolt_promo, "side", 1)
    before = deck.snapshot_entries()
    exported, swaps = arena_export_deck(deck, support, printings_of)
    text = deck_to_text(exported, include_side=False)
    by_name = {swap.name: swap for swap in swaps}
    downloaded = []
    with tempfile.TemporaryDirectory() as tmp:
        _package(exported, pathlib.Path(tmp), ensure=_stub_downloader(downloaded))
    version_one = ArenaSupport(names=support.names)
    v1_out, v1_swaps = arena_export_deck(deck, version_one, printings_of)
    return (
        fine_out is fine and fine_swaps == () and asked_for_fine == []
        and printing_accepted(support, {"name": "Island", "set_code": "m21",
                                         "collector_number": "263"})
        and printing_accepted(support, {"name": "Island", "set_code": "M21"})
        and printing_accepted(support, {"name": "Island"})
        and not printing_accepted(support, island_mdd)
        and not printing_accepted(support, bolt_promo)
        # Arena's first printing that can be pictured, whatever this program
        # might think of it (a Universes Beyond printing, here).
        and (by_name["Island"].to_set, by_name["Island"].to_number) == ("TRK", "319")
        and (by_name["Island"].from_set, by_name["Island"].from_number) == ("MDD", "294")
        # The same set wins when Arena has it, at Arena's own number.
        and (by_name["Lightning Bolt"].to_set, by_name["Lightning Bolt"].to_number) == ("M10", "146")
        # A set Arena knows only by another code is written in Arena's code.
        and (by_name["Ugin, the Spirit Dragon"].to_set,
             by_name["Ugin, the Spirit Dragon"].to_number) == ("UGF", "1")
        and "1 Ugin, the Spirit Dragon [UGF:1]" in text
        # Nothing Arena has can be pictured: the line names the card alone.
        and by_name["Llanowar Elves"].to_set == ""
        and "4 Llanowar Elves\n" in text
        and "Mystery Card" not in by_name
        and "1 Mystery Card [ZZZ:1]" in text
        and "22 Island [TRK:319]" not in text and "20 Island [TRK:319]" in text
        and "4 Lightning Bolt [M10:146]" in text
        # Within one set, Arena's order decides too.
        and "4 Shock [SHW:5]" in text
        and "MDD" not in text and "146p" not in text
        and asked.count("Island") == 1 and "Mystery Card" not in asked
        and "island-trk" in downloaded and "island-mdd" not in downloaded
        and "bolt-m10" in downloaded and "bolt-m10-promo" not in downloaded
        and deck.snapshot_entries() == before
        and island_mdd["set_code"] == "mdd" and deck.total("side") == 1
        and v1_out is deck and v1_swaps == ()
        and choose_printing(version_one, island_mdd, rows["Island"]) is None)


def _shipped_rules_printing_check():
    """ARN-011: the real refusals of 2026-10-23, against the shipped rules.

    Arena once refused Island MDD 294 and Space Beleren PLST UNF-178, which it
    lacked; it has both today. So the shipped rules are checked both ways: as
    they ship, where a printing Arena has is kept, and with those two taken
    out, where each must become a printing Arena does have.
    """
    from mtgdb.deck.arena_printings import arena_export_deck, printing_accepted
    from mtgdb.deck.arena_support import ArenaSupport, load_arena_rules

    shipped = load_arena_rules(ROOT / "assets/arena/deck_rules.json").support
    lacking = dict(shipped.printings)
    lacking["island"] = tuple(p for p in shipped.printings["island"] if p != "MDD 294")
    lacking["space_beleren"] = tuple(
        p for p in shipped.printings["space_beleren"] if p != "PLST UNF-178")
    older = ArenaSupport(
        names=shipped.names, printings=lacking,
        set_codes=shipped.set_codes, editions=shipped.editions)
    island_rows = [
        _row("island-mdd", "Island", "mdd", "294"),
        _row("island-m21", "Island", "m21", "263"),
        _row("island-m21-showcase", "Island", "m21", "310", frame_effects='["showcase"]'),
    ]
    beleren_rows = [
        _row("beleren-plst", "Space Beleren", "plst", "UNF-178"),
        _row("beleren-178", "Space Beleren", "unf", "178"),
        _row("beleren-276", "Space Beleren", "unf", "276", frame_effects='["showcase"]'),
    ]
    rows = {"Island": island_rows, "Space Beleren": beleren_rows}
    deck = Deck("Refused", "vintage")
    deck.add(island_rows[0], "main", 11)
    deck.add(beleren_rows[0], "main", 2)

    def export(support):
        exported, swaps = arena_export_deck(deck, support, lambda name: rows.get(name, []))
        accepted = all(printing_accepted(support, entry["card"])
                       for entry in exported.entries("main"))
        return {swap.name: swap for swap in swaps}, accepted

    swapped, older_accepted = export(older)
    kept, shipped_accepted = export(shipped)
    island, beleren = swapped.get("Island"), swapped.get("Space Beleren")
    return (
        shipped.knows_printings and older_accepted and shipped_accepted
        and island is not None and beleren is not None
        and f"{island.to_set} {island.to_number}" in older.printings["island"]
        and (island.to_set, island.to_number) == ("M21", "263")
        and (beleren.to_set, beleren.to_number) == ("UNF", "178")
        and all(
            (name in kept) != printing_accepted(shipped, card)
            for name, card in (("Island", island_rows[0]),
                               ("Space Beleren", beleren_rows[0]))))


def _keep_printing_check():
    """ARN-011: an Arena that takes any printing gets the deck's own, but for the cards it marks."""
    from dataclasses import replace

    from mtgdb.deck.arena_printings import arena_export_deck, printing_accepted

    support = replace(
        _printing_support(), any_printing=True,
        printing_matters=frozenset({"balloon_stand"}),
        names=_printing_support().names | {"balloon_stand"},
        printings=dict(_printing_support().printings, balloon_stand=("UNF 1",)),
        set_codes=dict(_printing_support().set_codes, UNF="UNF"),
        editions=dict(_printing_support().editions, UNF=("unf", "", "")))
    island_mdd = _row("island-mdd", "Island", "mdd", "294")
    bolt_promo = _row("bolt-promo", "Lightning Bolt", "plst", "M10-146")
    stand_promo = _row("stand-promo", "Balloon Stand", "punf", "1p")
    rows = {"Balloon Stand": [stand_promo, _row("stand-unf", "Balloon Stand", "unf", "1")]}
    deck = Deck("Mine", "modern")
    deck.add(island_mdd, "main", 20)
    deck.add(bolt_promo, "main", 4)
    deck.add(stand_promo, "main", 1)
    exported, swaps = arena_export_deck(deck, support, lambda name: rows.get(name, []))
    text = deck_to_text(exported, include_side=False)

    document = json.loads(json.dumps(_RULES_DOCUMENT))
    document["format"]["anyPrinting"] = True
    document["cards"].append({"name": "Balloon Stand", "faces": ["Balloon Stand"],
                              "printings": ["TST 4"], "printingMatters": True})
    read = rules_from_document(document).support
    older = rules_from_document(_RULES_DOCUMENT).support
    shipped = load_arena_rules(ROOT / "assets/arena/deck_rules.json").support
    return (
        # The player's printings and pictures go as the deck holds them...
        printing_accepted(support, island_mdd) and printing_accepted(support, bolt_promo)
        and "20 Island [MDD:294]" in text and "4 Lightning Bolt [PLST:M10-146]" in text
        # ...but a card whose printing changes it still goes as one Arena has.
        and not printing_accepted(support, stand_promo)
        and [(swap.name, swap.to_set, swap.to_number) for swap in swaps]
        == [("Balloon Stand", "UNF", "1")]
        and "1 Balloon Stand [UNF:1]" in text
        # The rules document says so; an older Arena's, which does not, swaps.
        and read.any_printing and read.printing_matters == {"balloon_stand"}
        and not older.any_printing and older.printing_matters == frozenset()
        # The shipped copy is Arena 1.13.0's: any printing, 24 cards strict.
        and shipped.any_printing and len(shipped.printing_matters) == 24
        and {"everythingamajig", "garbage_elemental", "sly_spy", "balloon_stand"}
        <= shipped.printing_matters
        and printing_accepted(shipped, bolt_promo))


def _unchanged_package_check():
    """ARN-011: a deck of printings Arena has goes exactly as the deck holds it."""
    from mtgdb.deck.arena_printings import arena_export_deck

    deck = Deck("Same", "modern")
    deck.add(_row("island-m21", "Island", "m21", "263"), "main", 20)
    deck.add(_row("bolt-m10", "Lightning Bolt", "m10", "146"), "main", 4)
    with tempfile.TemporaryDirectory() as tmp:
        before = _members(_package(deck, pathlib.Path(tmp)))
        exported, swaps = arena_export_deck(deck, _printing_support(), lambda _name: [])
        after = _members(_package(exported, pathlib.Path(tmp)))
    return swaps == () and exported is deck and before == after


# -- the window's words (ui/arena_export.py) -----------------------------------

_RULES_DOCUMENT = {
    "rulesVersion": "test",
    "format": {"minCards": 60, "maxCards": 65, "maxCopies": 4,
               "unlimitedCopies": ["Forest"], "sideDecks": {}},
    "cards": [
        {"name": "Forest", "faces": ["Forest"], "printings": ["TST 1"]},
        {"name": "Lightning Bolt", "faces": ["Lightning Bolt"], "printings": ["TST 2"]},
        {"name": "Krenko, Mob Boss", "faces": ["Krenko, Mob Boss"], "printings": ["TST 3"]},
    ],
    "editions": [{"code": "TST", "scryfallCode": "tst", "type": "", "date": ""}],
    "setCodes": {"TST": "TST"},
}


def _rows_check():
    """DECK-012, ARN-007 to ARN-011: the deck check in plain words, red for what Arena refuses."""
    from mtgdb.ui.arena_export import BAD, INFO, OK, WARN, arena_check_rows

    rules = rules_from_document(_RULES_DOCUMENT)
    forest = _card("fo", "Forest", number="1", type_line="Basic Land — Forest")
    bolt = _card("bo", "Lightning Bolt", number="2")
    krenko = _card("kr", "Krenko, Mob Boss", number="3",
                   type_line="Legendary Creature — Goblin Warrior")
    clean = Deck("Clean", "modern")
    clean.add(forest, "main", 56)
    clean.add(bolt, "main", 4)
    clean_check = check_for_arena(clean, rules, lambda _name: [])

    broken = Deck("Broken", "modern")
    broken.add(forest, "main", 50)
    broken.add(bolt, "main", 5)
    broken.add(krenko, "main", 2)
    broken.add(_card("my", "Mystery Card", number="9"), "main", 1)
    broken.add(bolt, "side", 3)
    broken.add(_card("em", "Elspeth Emblem", content_kind="emblem"), "main", 1)
    broken_check = check_for_arena(broken, rules, lambda _name: [])

    # Within every limit, but one card Arena cannot play refuses the import.
    unplayable = Deck("Unplayable", "modern")
    unplayable.add(forest, "main", 59)
    unplayable.add(_card("my", "Mystery Card", number="9"), "main", 1)
    unplayable_check = check_for_arena(unplayable, rules, lambda _name: [])

    # Two token cards ride along for their pictures: still 60 cards.
    with_tokens = Deck("With Tokens", "modern")
    with_tokens.add(forest, "main", 56)
    with_tokens.add(bolt, "main", 4)
    with_tokens.add(_card("gob", "Goblin", type_line="Token Creature — Goblin",
                          content_kind="token"), "main", 2)
    tokens_check = check_for_arena(with_tokens, rules, lambda _name: [])

    swapped = Deck("Swapped", "modern")
    swapped.add(forest, "main", 56)
    swapped.add(_card("bz", "Lightning Bolt", "zzz", number="9"), "main", 4)
    swap_check = check_for_arena(
        swapped, rules, lambda name: [_row("bt", "Lightning Bolt", "tst", "2")])
    return (
        not clean_check.blocked
        and arena_check_rows(clean_check) == [
            (OK, "60 cards", "Arena plays decks of 60 to 65 cards."),
            (OK, "Copies are within Arena's limit",
             "Up to 4 of any card. Basic lands can have any number."),
            (OK, "Every card plays in Proxic Arena", "")]
        and broken_check.blocked
        and arena_check_rows(broken_check) == [
            (BAD, "58 cards", "Arena needs at least 60. Add 2 more."),
            (BAD, "Lightning Bolt: 5 copies", "Arena allows 4 of a card. Take out 1."),
            (BAD, "Mystery Card doesn't play in Proxic Arena yet",
             "Arena doesn't have this card. Take it out to send the deck."),
            (WARN, "Krenko, Mob Boss: 2 copies",
             "Allowed, but it's legendary: you can control only one at a time, so "
             "Arena asks you to discard the other."),
            (INFO, "Sideboard stays here",
             "Arena decks have no sideboard, so its 3 cards aren't sent."),
            (INFO, "Left out: Elspeth Emblem", "Emblems and art cards aren't part of a deck.")]
        and arena_check_rows(tokens_check)[0] == (
            OK, "60 cards", "Arena plays decks of 60 to 65 cards.")
        and not tokens_check.blocked
        and unplayable_check.blocked
        and arena_check_rows(unplayable_check)[0][0] == OK
        and (BAD, "Mystery Card doesn't play in Proxic Arena yet",
             "Arena doesn't have this card. Take it out to send the deck.")
        in arena_check_rows(unplayable_check)
        and not swap_check.blocked
        and arena_check_rows(swap_check)[-1] == (
            INFO, "1 card goes in a printing Arena has",
            "Lightning Bolt: ZZZ 9 → TST 2. Arena doesn't have the printings you "
            "chose; the cards play the same, and your saved deck keeps its own.")
        # An empty main deck cannot be sent.
        and check_for_arena(Deck("Empty", "modern"), rules, lambda _n: []).blocked)


def _name_check():
    """ARN-014: the name on Arena, checked as Arena compares names, with the next free one offered."""
    from mtgdb.ui.arena_export import BAD, INFO, OK, WARN, arena_name_status, next_free_name

    names = ("Ape Tribal", "Ape Tribal 2", "Burn")
    taken_kind, taken_text, suggestion = arena_name_status("  ape TRIBAL ", names, True)
    return (
        arena_name_status("", names, True) == (BAD, "Give the deck a name to send it.", "")
        and arena_name_status("Ape Tribal", None, False)[:2] == (
            INFO, "Once this computer is linked, this checks whether your Deck Depot "
            "already has a deck with this name.")
        and arena_name_status("Ape Tribal", None, True)[0] == INFO
        and "Check again" in arena_name_status("Ape Tribal", None, True)[1]
        and taken_kind == WARN and suggestion == "ape TRIBAL 3"
        and taken_text.startswith("Your Deck Depot already has a deck named ape TRIBAL.")
        and "makes a second deck" in taken_text
        and "stays as it is" in taken_text
        and arena_name_status("Gruul", names, True) == (
            OK, "Checked your Deck Depot: you don't have a deck named Gruul yet, so this "
            "name is free.", "")
        and next_free_name("Burn", names) == "Burn 2")


def _token_words_check():
    """ARN-010: tokens in plain words, a warning and never a refusal."""
    from mtgdb.ui.arena_export import (
        ARENA_TOKEN_MISSING, OK, WARN, arena_token_rows,
    )

    pongify = _card("po", "Pongify")
    pongify["related_parts"] = json.dumps([
        {"component": "token", "name": "Ape", "type_line": "Token Creature — Ape"}])
    krenko = _card("kr", "Krenko, Mob Boss")
    krenko["related_parts"] = json.dumps([
        {"component": "token", "name": "Goblin", "type_line": "Token Creature — Goblin"}])
    deck = Deck("Tokens", "modern")
    deck.add(pongify, "main", 4)
    deck.add(krenko, "main", 4)
    deck.add(_card("ape", "Ape", content_kind="token"), "main", 1)
    deck.add(_card("cl", "Clue", content_kind="token"), "main", 2)
    rows = arena_token_rows(check_for_arena(deck, rules_from_document(_RULES_DOCUMENT), lambda _n: []))
    return (
        [row[:3] for row in rows] == [
            # A token the deck has a picture for needs no explaining.
            (OK, "Ape (Token Creature — Ape), from Pongify", ""),
            (WARN, "Goblin (Token Creature — Goblin), from Krenko, Mob Boss",
             ARENA_TOKEN_MISSING),
            (OK, "2 Clue", "")]
        # The missing token rides with its row, for Find token.
        and rows[1][3] is not None and rows[1][3].name == "Goblin"
        and rows[0][3] is None)


def _send_words_check():
    """ARN-014, ARN-007: what reached the Deck Depot, Arena's refusal, and why Send is off."""
    from mtgdb.arena.connection import REFUSED
    from mtgdb.ui.arena_export import (
        BAD, OK, WARN, arena_footer_hint, arena_refused_text, arena_sent_text,
    )

    result = SendResult(SENT, deck_name="Ape Tribal", total_cards=60)
    package = ArenaPackage(b"", "Ape Tribal.zip", card_count=7, image_count=8, token_pictures=1)
    held = ArenaPackage(b"", "x.zip", card_count=7, image_count=8, held_count=8,
                        missing_art=("Subgoyf",), token_pictures=1)
    refused = SendResult(REFUSED, detail="The deck is not legal in Open Format.",
                         problems=tuple(f"Problem {n}" for n in range(10)))
    rules = rules_from_document(_RULES_DOCUMENT)
    blocked = check_for_arena(Deck("Empty", "modern"), rules, lambda _n: [])
    fine_deck = Deck("Fine", "modern")
    fine_deck.add(_card("fo", "Forest", number="1"), "main", 60)
    fine = check_for_arena(fine_deck, rules, lambda _n: [])
    return (
        arena_sent_text(result, package) == (
            "Sent Ape Tribal to your Deck Depot: 60 cards, 7 card pictures and 1 "
            "token picture. It's ready to pick in a game room.")
        and arena_sent_text(result, held).endswith(
            "Arena already had 8 of the pictures, so they weren't sent again. No "
            "picture could be downloaded for 1 card; Arena draws its own.")
        and arena_refused_text(refused).startswith("The deck is not legal in Open Format.\n• Problem 0")
        and arena_refused_text(refused).endswith("• and 2 more")
        and arena_footer_hint(fine, False, OK, False) == "Link this computer to send."
        and arena_footer_hint(None, True, OK, False) == "Checking the deck…"
        and arena_footer_hint(blocked, True, OK, False).startswith("Fix the red items")
        and arena_footer_hint(fine, True, WARN, False) == "Change the name to send."
        and arena_footer_hint(fine, True, BAD, False) == "Change the name to send."
        and arena_footer_hint(fine, True, OK, False) == "")


# -- the window, the button and the deck panel ---------------------------------

def _window_contract_check():
    """ARN-005, ARN-006, ARN-014: one window, off Tk, cancellable, no file saved."""
    source = (ROOT / "mtgdb/ui/arena_export.py").read_text(encoding="utf-8")
    app = (ROOT / "mtgdb/ui/app.py").read_text(encoding="utf-8")
    deck_files = (ROOT / "mtgdb/ui/deck_files.py").read_text(encoding="utf-8")
    menu = app.split("def _build_menu")[1].split("\n    def ")[0]
    file_menu = menu.split("filemenu = ")[1].split("file_btn.pack")[0]
    print_at = menu.find('text="Print Deck"')
    button_at = menu.find('text="Proxic Arena Export", role="menu_action"')
    return (
        # The button sits beside Print Deck; File holds no Proxic Arena item.
        0 < print_at < button_at
        and "command=self._export_arena" in menu[button_at:]
        and "Proxic" not in file_menu
        # One window, brought forward rather than opened twice.
        and "class ArenaExportWindow" in source
        and "window.lift()" in source
        # Rules, check and send on the deck-file worker; progress by queue.
        and "submit_deck_file_job(job" in source
        and "obtain_rules(" in source and "check_for_arena" in source
        and "queue.Queue()" in source and "progress.put(" in source
        and "self.progress.get_nowait()" in source
        and "threading.Event()" in source and 'text="Cancel"' in source
        # The upload cannot be cancelled once it starts.
        and 'self.cancel_button.state(["disabled"])' in source
        # It checks again when the deck changes, by the deck's own count.
        and "deck.generation" in source and "ARENA_RECHECK_MS" in source
        # No file is saved, and nothing native.
        and "filedialog" not in source and "asksaveasfilename" not in source
        and "zipfile" not in source and "messagebox." not in source
        and 'style="DialogTitle.TLabel"' in source and "ScrollZone(" in source
        # The heading is the window's name alone; the deck's name is in its field.
        and "deck_label" not in source
        # Check again asks Arena afresh for the rules and the Deck Depot's names.
        and 'text="Check again"' in source and "def _check_again" in source
        # Send waits for Check again, so it never goes on an old answer.
        and "if self.refreshing and not hint:" in source
        and "PALETTE" in source
        # Save Deck As says nothing about Arena.
        and "_export_arena" not in deck_files and "arena_support" not in deck_files
        and "ArenaExportMixin" in app)


def _only_the_window_names_arena_check():
    """ARN-016: Proxic Arena is named to a player only by the export window and its button.

    Every string a UI module could show is read from its syntax tree
    (docstrings left out, comments are not in the tree); outside the
    window's two modules the only one naming Proxic Arena is the menu bar
    button. Wizards' MTG Arena (the Paper / Arena / MTGO printing filter) is
    a different game and is not matched.
    """
    window = {"arena_export.py", "arena_connection.py"}
    found = []
    for path in sorted((ROOT / "mtgdb/ui").glob("*.py")):
        if path.name in window:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings and "Proxic Arena" in node.value):
                found.append((path.name, node.value))
    deck = (ROOT / "mtgdb/ui/deck.py").read_text(encoding="utf-8")
    search = (ROOT / "mtgdb/ui/search.py").read_text(encoding="utf-8")
    finder = search.split("def _search_for_token_names")[1].split("\n    def ")[0]
    return (
        found == [("app.py", "Proxic Arena Export")]
        # The deck panel counts and sorts every card as it always has.
        and "arena" not in deck.lower()
        and "card_count_text(self.deck.total('main'))" in deck
        and "_cards_before_tokens" not in deck
        # Find token is the window's; it searches Tokens by name.
        and 'self._selected_traits = {"content_tokens"}' in finder
        and "self._set_exact_name_batch(names)" in finder)


def main():
    checks = {
        "art filenames resolve through Arena's own rule": _art_naming_check(),
        "one image per distinct card name reaches the package": _one_image_per_name_check(),
        "the package holds the decklist and pictures, built in memory": _package_layout_check(),
        "pictures Arena already holds are named, not sent": _held_pictures_check(),
        "token cards ride in the Tokens section with their pictures": _tokens_in_package_check(),
        "a cancelled package writes nothing": _cancel_check(),
        "a card that turns over also bundles its back face": _back_face_check(),
        "the package module owns naming only and is handed its downloader":
            _injected_downloader_check(),
        "a printing Arena lacks goes as one it has, in Arena's order": _printing_swap_check(),
        "the real Island and Space Beleren refusals go as printings Arena has":
            _shipped_rules_printing_check(),
        "an Arena that takes any printing gets the deck's own, but for the cards it marks":
            _keep_printing_check(),
        "a deck of printings Arena has goes exactly as it is": _unchanged_package_check(),
        "the deck check reads in plain words, red for what Arena refuses": _rows_check(),
        "the name on Arena is checked as Arena compares names": _name_check(),
        "tokens are named in plain words, a warning and never a refusal": _token_words_check(),
        "the window says what reached the Deck Depot and why Send is off": _send_words_check(),
        "one window, off Tk, cancellable, saving no file": _window_contract_check(),
        "only the export window and its button name Proxic Arena to a player":
            _only_the_window_names_arena_check(),
    }
    ok = True
    for label, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok &= bool(passed)
    print("\nARENA EXPORT:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
