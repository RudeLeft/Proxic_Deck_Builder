"""Proxic Arena export contract: art naming, bundle contents, durability (ARN-*)."""

import ast
import json
import pathlib
import re
import sys
import tempfile
import threading
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mtgdb.arena.export import (
    REAL_ART_DIRECTORY, ArenaExportCancelled, ArenaExportJob, art_filename,
    build_export, distinct_art_cards, front_face_name,
)
from mtgdb.core.background_jobs import JobCancelled
from mtgdb.deck.io import deck_to_text
from mtgdb.deck.model import Deck
from mtgdb.printing.service import back_faces

# A minimal valid-looking PNG header. These tests never reach the network:
# the downloader is injected (ARN-003), so the stub stands in for it.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _card(identifier, name, set_code="tst", png="https://example/art.png"):
    return {
        "id": identifier, "name": name, "set_code": set_code,
        "collector_number": "1", "image_png": png,
    }


def _stub_downloader(calls=None):
    """Stand in for printing.service.ensure_png without a network."""
    def ensure(card, cache_dir):
        if calls is not None:
            calls.append(card.get("id"))
        if not card.get("image_png"):
            return None
        path = pathlib.Path(cache_dir) / f"{card['id']}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_PNG_BYTES)
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
        path.write_bytes(_PNG_BYTES)
        return str(path)
    return ensure


def _back_face_check():
    """ARN-002/ARN-003: a card that turns over also bundles its back face."""
    delver = _two_faced(
        "d1", "Delver of Secrets", "Insectile Aberration", "isd")
    valki = _two_faced(
        "m1", "Valki, God of Lies", "Tibalt, Cosmic Impostor", "khm",
        layout="modal_dfc")
    # One picture for both halves: nothing turns over.
    giant = {
        "id": "g1", "name": "Bonecrusher Giant // Stomp", "set_code": "eld",
        "collector_number": "1", "image_png": "https://example/g1.png",
        "card_faces": [{"name": "Bonecrusher Giant"}, {"name": "Stomp"}]}
    deck = Deck("Faces", "modern")
    deck.add(delver, "main", 2)
    deck.add(valki, "main", 1)
    deck.add(giant, "main", 1)
    deck.add(_card("b", "Lightning Bolt", "lea"), "main", 4)
    # The sideboard is not exported, so neither is its back face.
    deck.add(_two_faced("s1", "Side Front", "Side Back", "sss"), "side", 1)

    def export(base, name, ensure_face, inject=True):
        output = base / f"{name}.zip"
        job = ArenaExportJob.from_deck(
            deck, deck_to_text(deck, include_side=False), output,
            base / "cache")
        events = []
        extra = (
            {"back_faces_of": back_faces, "ensure_face": ensure_face}
            if inject else {})
        result = build_export(
            job, _stub_downloader(),
            progress_cb=lambda *args: events.append(args), **extra)
        with zipfile.ZipFile(output) as archive:
            members = sorted(archive.namelist())
            # Named after the deck, not the archive, whatever it was saved as.
            decklist = archive.read("Faces.txt").decode("utf-8")
        return result, members, decklist, events

    with tempfile.TemporaryDirectory() as directory:
        base = pathlib.Path(directory)
        calls = []
        result, members, decklist, events = export(
            base, "Faces", _stub_face_downloader(calls))
        failed, failed_members, _text, _events = export(
            base, "Failing", _stub_face_downloader(
                fail={("d1", 1)}))
        bare, bare_members, _text, _events = export(
            base, "Fronts", None, inject=False)

    art = [m for m in members if m.startswith(f"{REAL_ART_DIRECTORY}/")]
    return (
        # Each face under its own name, the back with the front's set code.
        art == sorted([
            f"{REAL_ART_DIRECTORY}/Delver of Secrets [ISD].png",
            f"{REAL_ART_DIRECTORY}/Insectile Aberration [ISD].png",
            f"{REAL_ART_DIRECTORY}/Valki, God of Lies [KHM].png",
            f"{REAL_ART_DIRECTORY}/Tibalt, Cosmic Impostor [KHM].png",
            f"{REAL_ART_DIRECTORY}/Bonecrusher Giant [ELD].png",
            f"{REAL_ART_DIRECTORY}/Lightning Bolt [LEA].png"])
        and result.image_count == 6 and result.card_count == 4
        and result.back_face_count == 2 and result.missing_art == ()
        # One download per back face, however many copies are in the deck.
        and sorted(calls) == [("d1", 1), ("m1", 1)]
        # The decklist still names a double-faced card by both faces.
        and "Delver of Secrets // Insectile Aberration" in decklist
        and "Side Front" not in decklist
        # Progress counts every image, then the packaging step.
        and events[-1] == ("package", 6, 6, "Faces.zip")
        # A back face that cannot be fetched is reported, and the rest ships.
        and failed.missing_art == ("Insectile Aberration",)
        and failed.back_face_count == 1 and failed.image_count == 5
        and f"{REAL_ART_DIRECTORY}/Delver of Secrets [ISD].png" in failed_members
        # Without the two injected functions, only fronts are bundled.
        and bare.image_count == 4 and bare.back_face_count == 0)


def _arena_index_key(filename):
    """Arena's own rule, from `_real_art_path` in its client.

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
        # Every one of them is found by Arena's own index rule.
        and all(
            _arena_index_key(names[card["id"]])
            == front_face_name(card["name"]).lower()
            for card in cards[:4])
        # A card with no usable name cannot be filed.
        and art_filename({"name": ""}) is None
        and art_filename({"name": "///"}) is None
        and art_filename(None) is None
        # No set code still yields a name Arena can match.
        and art_filename({"name": "Nameless Set"}) == "Nameless Set.png")


def _one_image_per_name_check():
    """ARN-002: Arena's name-keyed index gets exactly one image per name."""
    deck = Deck("Collision", "commander")
    first = _card("bolt-lea", "Lightning Bolt", "lea")
    second = _card("bolt-m10", "Lightning Bolt", "m10")
    deck.add(first, "main", 1)
    deck.add(second, "main", 1)
    deck.add(_card("mind", "Mind Stone", "m20"), "main", 1)
    # A card held only in the sideboard is not exported, so it gets no art,
    # and a sideboard printing of a main-deck name cannot displace the main one.
    deck.add(_card("side", "Subgoyf", "mb2"), "side", 2)
    deck.add(_card("bolt-side", "Lightning Bolt", "4ed"), "side", 1)
    chosen = distinct_art_cards(deck)
    names = [front_face_name(card["name"]) for card in chosen]
    return (
        names == ["Lightning Bolt", "Mind Stone"]
        # The deck's own first printing of the name wins.
        and chosen[0]["id"] == "bolt-lea"
        and len(chosen) == 2)


def _bundle_layout_check():
    """ARN-001/ARN-004: contents, tolerated missing art, nothing left behind."""
    deck = Deck("Layout Probe", "commander")
    deck.add(_card("a", "Lightning Bolt", "lea"), "main", 4)
    deck.add(_card("c", "Éowyn, Lady of Rohan", "ltr"), "main", 1)
    # No upstream art for this one.
    deck.add(_card("x", "Subgoyf", "mb2", png=None), "main", 1)
    # Arena has no sideboard: this card must reach neither list nor art.
    deck.add(_card("s", "Sideboard Only", "sbo"), "side", 3)

    with tempfile.TemporaryDirectory() as directory:
        base = pathlib.Path(directory)
        output = base / "Layout Probe.zip"
        job = ArenaExportJob.from_deck(
            deck, deck_to_text(deck, include_side=False), output,
            base / "cache")
        events = []
        result = build_export(
            job, _stub_downloader(),
            progress_cb=lambda *args: events.append(args))
        with zipfile.ZipFile(output) as archive:
            members = sorted(archive.namelist())
            decklist = archive.read("Layout Probe.txt").decode("utf-8")
        leftovers = [
            item.name for item in base.iterdir()
            if item.is_file() and item.suffix != ".zip"]
        return (
            output.is_file()
            # Decklist at the root, art under real_cards/.
            and members == [
                "Layout Probe.txt",
                f"{REAL_ART_DIRECTORY}/Lightning Bolt [LEA].png",
                f"{REAL_ART_DIRECTORY}/Éowyn, Lady of Rohan [LTR].png",
            ]
            and decklist.startswith("// Layout Probe (commander)")
            and "Lightning Bolt" in decklist
            # The sideboard is left out of the decklist and of the art.
            and "Sideboard" not in decklist
            and "Sideboard Only" not in decklist
            # The art-less card is reported, and did not fail the bundle.
            and result.missing_art == ("Subgoyf",)
            and result.image_count == 2
            and result.card_count == 3
            # Progress is reported per card, plus the packaging step.
            and events
            and events[-1][0] == "package"
            and leftovers == [])


def _durability_check():
    """ARN-004: a cancelled export leaves the destination and folder alone."""
    deck = Deck("Cancelled", "commander")
    for index in range(4):
        deck.add(_card(f"c{index}", f"Card {index}"), "main", 1)

    with tempfile.TemporaryDirectory() as directory:
        base = pathlib.Path(directory)
        output = base / "Cancelled.zip"
        output.write_bytes(b"an archive that was already here")
        original = output.read_bytes()
        job = ArenaExportJob.from_deck(
            deck, deck_to_text(deck), output, base / "cache")
        cancel = threading.Event()
        cancel.set()
        raised = None
        try:
            build_export(job, _stub_downloader(), cancel_event=cancel)
        except JobCancelled as exc:
            raised = exc
        leftovers = [
            item.name for item in base.iterdir()
            if item.is_file() and item.suffix != ".zip"]
        return (
            # Cancellation is catchable as the shared base (BGJ-004).
            isinstance(raised, ArenaExportCancelled)
            and isinstance(raised, JobCancelled)
            # The archive already at the destination is untouched.
            and output.read_bytes() == original
            and leftovers == [])


def _injected_downloader_check():
    """ARN-003: the export package reaches neither printing nor the network."""
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
        base = pathlib.Path(directory)
        job = ArenaExportJob.from_deck(
            deck, deck_to_text(deck), base / "out.zip", base / "cache")
        build_export(job, _stub_downloader(calls))
    return (
        # Naming and packaging only: no printing, no net, no Tk, no sqlite.
        not any(
            name.startswith(("mtgdb.printing", "mtgdb.ui", "tkinter", "sqlite3"))
            or name == "mtgdb.core.net"
            for name in imported)
        and {"mtgdb.core.atomic_files", "mtgdb.core.background_jobs"} <= imported
        # The downloader it was handed is the one it used.
        and calls == ["a"])


def _export_ui_check():
    """ARN-005/ARN-006: off Tk, cancellable, one app-owned dark report."""
    source = (ROOT / "mtgdb/ui/arena_export.py").read_text(encoding="utf-8")
    app_source = (ROOT / "mtgdb/ui/app.py").read_text(encoding="utf-8")
    deck_files = (ROOT / "mtgdb/ui/deck_files.py").read_text(encoding="utf-8")
    report = source.split("def _report_arena_export")[1].split("\n    def ")[0]
    return (
        # Runs on a deck-file worker, not on Tk.
        'submit_deck_file_job(run_export, name="mtg-arena-export")' in source
        and "ensure_png" in source
        # Progress crosses threads through a queue.
        and "queue.Queue()" in source
        and "progress.put(" in source
        and "progress.get_nowait()" in source
        # Cancel exists, and closing the popup means the same thing.
        and "threading.Event()" in source
        and 'text="Cancel"' in source
        and 'popup.protocol("WM_DELETE_WINDOW", cancel_event.set)' in source
        # The popup comes down however the job ends.
        and "on_error=self._close_arena_export_progress" in source
        and "def _close_arena_export_progress" in source
        and "after_cancel" in source
        # One app-owned dark dialog, never a native one. The prohibition is on
        # calls (`messagebox.`) rather than the word, which a docstring names
        # to say what this dialog is not.
        and "messagebox." not in source
        and "from tkinter import filedialog, ttk" in source
        and 'style="DialogTitle.TLabel"' in source
        and "PALETTE" in source
        and "_show_arena_list_dialog(" in report
        # Composed onto the app, and Save Deck As says nothing about Arena.
        and "ArenaExportMixin" in app_source
        and 'label="Proxic Arena Export..."' in app_source
        and "_export_arena" not in deck_files
        and "arena_support" not in deck_files
        and "save_deck_text, path, detached" in deck_files)


def _arena_warning_sentences_check():
    """DECK-012: the notice's sentences, composed apart from its widgets.

    Checked here without a Tk root. The lead carries no count, so it reads the
    same however many cards are listed and needs no subject/verb agreement --
    the defect an earlier count-carrying lead could produce was "1 card ...
    are not", which lives in the sentence rather than in any value a
    structural check can see.
    """
    from mtgdb.ui.arena_export import (
        ARENA_WARNING_NAME_LIMIT, arena_warning_text,
    )

    expected_lead = (
        "The following cards in your deck are not in the Proxic Arena card "
        "pool and will not play properly")
    one_lead, one_shown, one_rest, one_closing = arena_warning_text(["Subgoyf"])
    many = ["Subgoyf", "Cecily, Haunted Mage", "Sly Spy"]
    many_lead, many_shown, many_rest, _closing = arena_warning_text(many)
    overflowing = [
        f"Card {index}" for index in range(ARENA_WARNING_NAME_LIMIT + 5)]
    over_lead, over_shown, over_rest, _over_closing = arena_warning_text(
        overflowing)
    return (
        one_lead == expected_lead
        and one_shown == ["Subgoyf"]
        and one_rest == 0
        and one_closing == "The rest of the deck will play without issue."
        # One sentence at every count, so no agreement can drift.
        and many_lead == expected_lead
        and over_lead == expected_lead
        and many_shown == many
        and many_rest == 0
        # The heading says the deck was saved; the lead does not repeat it.
        and not one_lead.startswith("Deck Saved")
        # The listing is capped and the remainder counted, not dropped.
        and len(over_shown) == ARENA_WARNING_NAME_LIMIT
        and over_rest == 5)


def _limit_refusal_check():
    """ARN-007: a deck over Arena's limits is refused before any save prompt."""
    source = (ROOT / "mtgdb/ui/arena_export.py").read_text(encoding="utf-8")
    deck_files = (ROOT / "mtgdb/ui/deck_files.py").read_text(encoding="utf-8")
    body = source.split("def _export_arena")[1].split("\n    def ")[0]
    snapshot = body.find("detached = self._detached_deck_copy(deck)")
    measured = body.find("arena_deck_limits(detached)")
    refused = body.find("_show_arena_list_dialog(*arena_limit_text(")
    prompted = body.find("filedialog.asksaveasfilename(")
    return (
        # Snapshotted, measured, refused, and only then asked where to save.
        0 <= snapshot < measured < refused < prompted
        # The snapshot that was measured is the one exported: it is taken
        # once, and the job is built from it rather than from the live deck.
        and body.count("_detached_deck_copy(") == 1
        and "arena_export_deck(\n                detached," in body
        and "ArenaExportJob.from_deck(\n                export_deck," in body
        # The limits are not judged in the UI, and Save Deck As never asks.
        and "arena_limits" not in deck_files
        and "ARENA_MAX_CARDS" not in deck_files
        and "messagebox." not in source)


def _sideboard_exclusion_check():
    """ARN-008: the sideboard is left out, checked against the exported deck."""
    source = (ROOT / "mtgdb/ui/arena_export.py").read_text(encoding="utf-8")
    deck_files = (ROOT / "mtgdb/ui/deck_files.py").read_text(encoding="utf-8")
    body = source.split("def _export_arena")[1].split("\n    def ")[0]
    with_side = Deck("With Side", "modern")
    with_side.add(_card("a", "Lightning Bolt"), "main", 4)
    with_side.add(_card("b", "Mind Stone"), "side", 2)
    return (
        # The decklist and the playability check both look at the main deck.
        "include_side=False" in body
        and 'boards=("main",)' in body
        and "arena_sideboard_note(" in source
        and "sideboard_count" in body
        # Save Deck As keeps the sideboard, as ever.
        and "include_side" not in deck_files
        # The default still writes it, so every other save is unchanged.
        and "Sideboard" in deck_to_text(with_side)
        and "Sideboard" not in deck_to_text(with_side, include_side=False))


def _arena_sideboard_sentence_check():
    """ARN-008: the note's sentence, composed apart from its widgets."""
    from mtgdb.ui.arena_export import arena_sideboard_note

    return (
        # No sideboard, no mention of one.
        arena_sideboard_note(0) == ""
        and arena_sideboard_note(1) == (
            "Sideboard cards are not included in a Proxic Arena export. This "
            "deck's sideboard (1 card) was left out.")
        # The noun agrees with the count.
        and arena_sideboard_note(15).endswith("(15 cards) was left out."))


def _arena_legendary_note_check():
    """ARN-009: the legend-rule warning, composed apart from its widgets."""
    from mtgdb.ui.arena_export import (
        ARENA_LEGENDARY_NAME_LIMIT, arena_legendary_note,
    )

    one = arena_legendary_note((("Krenko, Mob Boss", 2),))
    two = arena_legendary_note((
        ("Krenko, Mob Boss", 2), ("Ragavan, Nimble Pilferer", 4)))
    crowd = tuple(
        (f"Legend {index}", 2) for index in range(ARENA_LEGENDARY_NAME_LIMIT + 3))
    long_note = arena_legendary_note(crowd)
    source = (ROOT / "mtgdb/ui/arena_export.py").read_text(encoding="utf-8")
    report = source.split("def _report_arena_export")[1].split("\n    def ")[0]
    body = source.split("def _export_arena")[1].split("\n    def ")[0]
    return (
        # Nothing repeats, nothing is said about the legend rule.
        arena_legendary_note(()) == ""
        # The rule, what Arena does, and each card with its copy count.
        and one == (
            "Heads up: you can only control one legendary permanent with a "
            "given name at a time. If you play a second copy, Proxic Arena "
            "will ask you to discard one. Your deck has more than one of: "
            "Krenko, Mob Boss (2 copies).")
        and two.endswith(
            "Krenko, Mob Boss (2 copies); Ragavan, Nimble Pilferer (4 copies).")
        # The listing is capped and the remainder counted, not dropped.
        and long_note.endswith("; and 3 more.")
        and long_note.count("copies)") == ARENA_LEGENDARY_NAME_LIMIT
        # It rides in the one report dialog, and never blocks the export.
        and "arena_legendary_note(" in report
        and "limits.legendary_repeats" in body
        and "if not limits.allowed:" in body
        and "legendary" not in body.split("if not limits.allowed:")[1].split(
            "return")[0])


def _arena_token_notice_check():
    """ARN-002/ARN-010: token and back-face wording, composed apart from widgets."""
    from mtgdb.deck.arena_tokens import ArenaToken
    from mtgdb.ui.arena_export import (
        ARENA_WARNING_NAME_LIMIT, arena_back_face_phrase, arena_token_section,
    )

    goblin = ArenaToken(
        "Goblin", "Token Creature — Goblin", ("Hordeling Outburst",))
    treasure = ArenaToken(
        "Treasure", "Token Artifact — Treasure", ("A", "B", "C", "D", "E"))
    mystery = ArenaToken("Mystery", "", ("Some Card",))
    crowd = tuple(
        ArenaToken(f"Token {index}", "Token Creature — Thing", ("Maker",))
        for index in range(ARENA_WARNING_NAME_LIMIT + 4))
    intro, lines, rest = arena_token_section((goblin,))
    crowded = arena_token_section(crowd)
    source = (ROOT / "mtgdb/ui/arena_export.py").read_text(encoding="utf-8")
    body = source.split("def _export_arena")[1].split("\n    def ")[0]
    report = source.split("def _report_arena_export")[1].split("\n    def ")[0]
    return (
        # No token, no mention of tokens at all.
        arena_token_section(()) == ()
        # Plain words: not in the export, and the player can add the art.
        and "can create tokens" in intro
        and "not included in the export" in intro
        and "add card art for them yourself" in intro
        and lines == [
            "Goblin (Token Creature — Goblin), from Hordeling Outburst"]
        and rest == 0
        # The makers are capped with the remainder counted, not dropped.
        and arena_token_section((treasure,))[1] == [
            "Treasure (Token Artifact — Treasure), from A, B, C, and 2 more"]
        # A token with no type line still reads cleanly.
        and arena_token_section((mystery,))[1] == ["Mystery, from Some Card"]
        # The list is capped and the remainder counted.
        and len(crowded[1]) == ARENA_WARNING_NAME_LIMIT and crowded[2] == 4
        # An image count past the card count says why.
        and arena_back_face_phrase(0) == ""
        and arena_back_face_phrase(1) == ", including 1 back face"
        and arena_back_face_phrase(3) == ", including 3 back faces"
        # Wired: found on the snapshot, shown in the one report, fetched through
        # the printing owner's face functions, and never a reason to refuse.
        and "tokens = deck_tokens(detached)" in body
        and "back_faces_of=back_faces" in body
        and "ensure_face=ensure_face_png" in body
        and "arena_token_section(" in report
        and "sections=" in report
        and "arena_back_face_phrase(" in report
        and "token" not in body.split("if not limits.allowed:")[1].split(
            "return")[0])


def _arena_limit_sentences_check():
    """ARN-007: the refusal's sentences, composed apart from its widgets."""
    from mtgdb.deck.arena_limits import ArenaDeckLimits
    from mtgdb.ui.arena_export import (
        ARENA_WARNING_NAME_LIMIT, arena_limit_text,
    )

    both = ArenaDeckLimits(
        total_cards=107, over_copy_limit=(("Lightning Bolt", 6, 4),))
    lead, shown, rest, closing = arena_limit_text(both)
    many = ArenaDeckLimits(
        total_cards=40, over_copy_limit=tuple(
            (f"Card {index}", 5, 4)
            for index in range(ARENA_WARNING_NAME_LIMIT + 3)))
    _lead, over_shown, over_rest, _closing = arena_limit_text(many)
    return (
        # The limits are named in the lead, whichever one was broken.
        "100 cards" in lead and "4 copies" in lead
        # A legendary card may repeat, so the refusal never mentions the legend
        # rule: that is a warning in the report, not a reason to refuse.
        and "legendary" not in lead.lower()
        and shown == [
            "Deck size: 107 cards, 7 over the 100-card limit",
            "Lightning Bolt: 6 copies, 2 over the limit of 4"]
        and rest == 0
        and closing == "Nothing was exported. Adjust the deck and try again."
        # The listing is capped and the remainder counted, not dropped.
        and len(over_shown) == ARENA_WARNING_NAME_LIMIT
        and over_rest == 3)


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
    """A small asset: what Forge has, by Forge's own edition rule."""
    from mtgdb.deck.arena_support import ArenaSupport

    return ArenaSupport(
        names=frozenset({
            "island", "lightning_bolt", "ugin_the_spirit_dragon",
            "llanowar_elves", "nameless_wonder", "shock"}),
        printings={
            "island": frozenset({"M21 263", "TRK 319", "C21 400"}),
            "lightning_bolt": frozenset({"M10 146", "2X2 117"}),
            "ugin_the_spirit_dragon": frozenset({"UGF 1"}),
            "llanowar_elves": frozenset({"M19 314", "DOM 168"}),
            "nameless_wonder": frozenset({"OLD 9"}),
            "shock": frozenset({"SHW 5", "SHW 300"}),
        },
        # CON is Conflux's second code; UGIN is only Ugin's Fate's Scryfall
        # code, which Forge's lookup does not accept.
        set_codes={
            "M21": "M21", "TRK": "TRK", "C21": "C21", "M10": "M10",
            "2X2": "2X2", "UGF": "UGF", "M19": "M19", "DOM": "DOM",
            "OLD": "OLD", "SHW": "SHW"},
        editions={
            "M21": ("m21", "Core", "2020-07-03"),
            "TRK": ("trk", "Expansion", "2026-11-13"),
            "C21": ("c21", "Commander", "2026-12-01"),
            "M10": ("m10", "Core", "2009-07-17"),
            "2X2": ("2x2", "Reprint", "2022-07-08"),
            "UGF": ("ugin", "Promo", "2015-01-01"),
            "M19": ("m19", "Core", "2018-07-13"),
            "DOM": ("dom", "Expansion", "2018-04-27"),
            "OLD": ("old", "Expansion", "1999-01-01"),
            "SHW": ("shw", "Expansion", "2024-02-09"),
        })


def _printing_swap_check():
    """ARN-011: a printing Arena lacks is exported as one it has, and only then."""
    from mtgdb.deck.arena_printings import (
        arena_export_deck, choose_printing, printing_accepted,
    )
    from mtgdb.deck.arena_support import ArenaSupport

    support = _printing_support()
    rows = {
        "Island": [
            _row("island-mdd", "Island", "mdd", "294"),
            _row("island-m21", "Island", "m21", "263"),
            # Newer, but Universes Beyond, so after the in-universe printing.
            _row("island-trk", "Island", "trk", "319", universes_beyond=1),
            # Newer still, but a commander edition.
            _row("island-c21", "Island", "c21", "400"),
        ],
        "Lightning Bolt": [
            _row("bolt-m10", "Lightning Bolt", "m10", "146"),
            _row("bolt-m10-promo", "Lightning Bolt", "m10", "146p", promo=1),
            _row("bolt-2x2", "Lightning Bolt", "2x2", "117"),
        ],
        "Ugin, the Spirit Dragon": [
            _row("ugin", "Ugin, the Spirit Dragon", "ugin", "1"),
        ],
        "Llanowar Elves": [
            _row("elves-promo", "Llanowar Elves", "pdom", "168s"),
        ],
        "Nameless Wonder": [_row("wonder", "Nameless Wonder", "xyz", "9")],
        # One release, two looks: the plain card wins over the lower number.
        "Shock": [
            _row("shock-xyz", "Shock", "xyz", "1"),
            _row("shock-showcase", "Shock", "shw", "5",
                 frame_effects='["showcase"]'),
            _row("shock-plain", "Shock", "shw", "300"),
        ],
    }
    asked = []

    def printings_of(name):
        asked.append(name)
        return rows.get(name, [])

    island_mdd = rows["Island"][0]
    bolt_promo = rows["Lightning Bolt"][1]
    accepted = [rows["Island"][1], rows["Lightning Bolt"][0]]

    # A deck of printings Forge has comes back as the same deck, untouched.
    fine = Deck("Fine", "modern")
    for card in accepted:
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
    # A name the asset does not know is the name check's business: left alone.
    deck.add(_row("mystery", "Mystery Card", "zzz", "1"), "main", 1)
    deck.add(bolt_promo, "side", 1)
    before = deck.snapshot_entries()
    exported, swaps = arena_export_deck(deck, support, printings_of)
    text = deck_to_text(exported, include_side=False)
    by_name = {swap.name: swap for swap in swaps}

    # The art downloaded is the printing exported.
    downloaded = []
    with tempfile.TemporaryDirectory() as tmp:
        job = ArenaExportJob.from_deck(
            exported, text, pathlib.Path(tmp) / "Mixed.zip", tmp)
        build_export(job, _stub_downloader(downloaded))

    version_one = ArenaSupport(names=support.names)
    v1_out, v1_swaps = arena_export_deck(deck, version_one, printings_of)
    return (
        fine_out is fine and fine_swaps == () and asked_for_fine == []
        and printing_accepted(support, rows["Island"][1])
        # Forge's lookup takes any case, and a set with no number by set alone.
        and printing_accepted(support, {"name": "Island", "set_code": "m21",
                                         "collector_number": "263"})
        and printing_accepted(support, {"name": "Island", "set_code": "M21"})
        and printing_accepted(support, {"name": "Island"})
        and not printing_accepted(support, island_mdd)
        and not printing_accepted(support, bolt_promo)
        # An Island Forge lacks becomes the in-universe ordinary printing.
        and by_name["Island"].to_set == "M21"
        and by_name["Island"].to_number == "263"
        and (by_name["Island"].from_set, by_name["Island"].from_number)
        == ("MDD", "294")
        # The same set wins when Forge has it, at Forge's own number.
        and (by_name["Lightning Bolt"].to_set,
             by_name["Lightning Bolt"].to_number) == ("M10", "146")
        # A set Forge knows only by another code is written in Forge's code.
        and (by_name["Ugin, the Spirit Dragon"].to_set,
             by_name["Ugin, the Spirit Dragon"].to_number) == ("UGF", "1")
        and "1 Ugin, the Spirit Dragon [UGF:1]" in text
        # Nothing Forge has can be pictured: the line names the card alone.
        and by_name["Llanowar Elves"].to_set == ""
        and "4 Llanowar Elves\n" in text
        and "Mystery Card" not in by_name
        and "1 Mystery Card [ZZZ:1]" in text
        and "22 Island [M21:263]" in text
        and "4 Lightning Bolt [M10:146]" in text
        and "4 Shock [SHW:300]" in text
        and "MDD" not in text and "146p" not in text
        # Each name was looked up once at most.
        and asked.count("Island") == 1
        and "Mystery Card" not in asked
        # The picture bundled is the chosen printing's, not the deck's own.
        and "island-m21" in downloaded and "island-mdd" not in downloaded
        and "bolt-m10" in downloaded and "bolt-m10-promo" not in downloaded
        # The saved deck keeps every printing, sideboard included.
        and deck.snapshot_entries() == before
        and island_mdd["set_code"] == "mdd"
        and deck.total("side") == 1
        # A version 1 asset knows no printings: nothing changes.
        and v1_out is deck and v1_swaps == ()
        and choose_printing(version_one, island_mdd, rows["Island"]) is None)


def _shipped_asset_printing_check():
    """ARN-011: the real refusals of 2026-10-23, against the shipped asset.

    Arena refused Island MDD 294 and Space Beleren PLST UNF-178 under Forge
    2.0.15, which lacked both printings; later Forge added them. So the
    shipped asset is checked both ways: as it ships, where a printing Forge
    has is kept, and with those two printings taken out, as 2.0.15 had it,
    where each must become a printing Forge does have.
    """
    from mtgdb.deck.arena_printings import arena_export_deck, printing_accepted
    from mtgdb.deck.arena_support import ArenaSupport, load_arena_support

    shipped = load_arena_support(ROOT / "assets/arena/supported_cards.json")
    lacking = dict(shipped.printings)
    lacking["island"] = shipped.printings["island"] - {"MDD 294"}
    lacking["space_beleren"] = (
        shipped.printings["space_beleren"] - {"PLST UNF-178"})
    older = ArenaSupport(
        names=shipped.names, printings=lacking,
        set_codes=shipped.set_codes, editions=shipped.editions)

    island_rows = [
        _row("island-mdd", "Island", "mdd", "294"),
        _row("island-m21", "Island", "m21", "263"),
        _row("island-m21-showcase", "Island", "m21", "310",
             frame_effects='["showcase"]'),
    ]
    beleren_rows = [
        _row("beleren-plst", "Space Beleren", "plst", "UNF-178"),
        _row("beleren-178", "Space Beleren", "unf", "178"),
        _row("beleren-276", "Space Beleren", "unf", "276",
             frame_effects='["showcase"]'),
    ]
    rows = {"Island": island_rows, "Space Beleren": beleren_rows}
    deck = Deck("Refused", "vintage")
    deck.add(island_rows[0], "main", 11)
    deck.add(beleren_rows[0], "main", 2)

    def export(support):
        exported, swaps = arena_export_deck(
            deck, support, lambda name: rows.get(name, []))
        accepted = all(printing_accepted(support, entry["card"])
                       for entry in exported.entries("main"))
        return {swap.name: swap for swap in swaps}, accepted

    swapped, older_accepted = export(older)
    kept, shipped_accepted = export(shipped)
    island = swapped.get("Island")
    beleren = swapped.get("Space Beleren")
    return (
        shipped.knows_printings
        # Every printing of the exported deck is one Forge has, either way.
        and older_accepted and shipped_accepted
        # As 2.0.15 had it: Island lands on a printing in Forge's list, the
        # ordinary one, and PLST UNF-178 exports as UNF 178.
        and island is not None and beleren is not None
        and f"{island.to_set} {island.to_number}" in older.printings["island"]
        and (island.to_set, island.to_number) == ("M21", "263")
        and (beleren.to_set, beleren.to_number) == ("UNF", "178")
        # Wherever Forge has the deck's own printing, it is kept as it is.
        and all(
            (name in kept) != printing_accepted(shipped, card)
            for name, card in (("Island", island_rows[0]),
                               ("Space Beleren", beleren_rows[0]))))


def _unchanged_export_check():
    """ARN-011: a deck of printings Forge has exports byte-for-byte as before."""
    from mtgdb.deck.arena_printings import arena_export_deck

    support = _printing_support()
    deck = Deck("Same", "modern")
    deck.add(_row("island-m21", "Island", "m21", "263"), "main", 20)
    deck.add(_row("bolt-m10", "Lightning Bolt", "m10", "146"), "main", 4)

    def contents(target_deck, folder, name):
        job = ArenaExportJob.from_deck(
            target_deck, deck_to_text(target_deck, include_side=False),
            pathlib.Path(folder) / name, folder)
        build_export(job, _stub_downloader())
        with zipfile.ZipFile(job.output_path) as archive:
            return {
                member: archive.read(member) for member in archive.namelist()}

    with tempfile.TemporaryDirectory() as tmp:
        before = contents(deck, tmp, "before.zip")
        exported, swaps = arena_export_deck(
            deck, support, lambda _name: [])
        after = contents(exported, tmp, "after.zip")
    return swaps == () and exported is deck and before == after


def _printing_sentences_check():
    """ARN-011: each changed printing in plain words, apart from the widgets."""
    from mtgdb.deck.arena_printings import ArenaPrintingSwap
    from mtgdb.ui.arena_export import (
        ARENA_PRINTING_INTRO, ARENA_WARNING_NAME_LIMIT, arena_printing_line,
        arena_printing_section,
    )

    island = ArenaPrintingSwap("Island", "MDD", "294", "M21", "263")
    by_name = ArenaPrintingSwap("Llanowar Elves", "PDOM", "168s", "", "")
    many = [
        ArenaPrintingSwap(f"Card {index}", "AAA", "1", "BBB", "2")
        for index in range(ARENA_WARNING_NAME_LIMIT + 2)]
    intro, shown, remaining = arena_printing_section(many)
    return (
        arena_printing_line(island)
        == "Island: MDD 294 -> M21 263 (the printing Proxic Arena has)"
        and arena_printing_line(by_name) == (
            "Llanowar Elves: PDOM 168s -> listed by name only "
            "(Proxic Arena picks the printing)")
        # Nothing changed, nothing said.
        and arena_printing_section(()) == ()
        and intro == ARENA_PRINTING_INTRO
        and "saved deck keeps" in intro
        and len(shown) == ARENA_WARNING_NAME_LIMIT and remaining == 2)


def _printing_ui_check():
    """ARN-011: printings are chosen on the export worker, from the snapshot."""
    source = (ROOT / "mtgdb/ui/arena_export.py").read_text(encoding="utf-8")
    deck_files = (ROOT / "mtgdb/ui/deck_files.py").read_text(encoding="utf-8")
    body = source.split("def _export_arena")[1].split("\n    def ")[0]
    worker = body.split("def run_export")[1].split("def exported")[0]
    report = source.split("def _report_arena_export")[1].split("\n    def ")[0]
    return (
        # On the worker, from the measured snapshot, before the job is built.
        "arena_export_deck(\n                detached, support, printings_named)"
        in worker
        and worker.find("arena_export_deck(") < worker.find(
            "ArenaExportJob.from_deck(")
        and "ArenaExportJob.from_deck(\n                export_deck," in worker
        and "printings_named = self.db.printings_named" in body
        # Reported in the one dialog, and never on Save Deck As.
        and "arena_printing_section(swaps)" in report
        and "arena_printings" not in deck_files)


def main():
    checks = {
        "art filenames resolve through Arena's own index rule":
            _art_naming_check(),
        "one image per distinct card name reaches the bundle":
            _one_image_per_name_check(),
        "the bundle holds the decklist beside real_cards art":
            _bundle_layout_check(),
        "a cancelled export preserves the destination and leaves no temporary":
            _durability_check(),
        "the export package owns naming only and is handed its downloader":
            _injected_downloader_check(),
        "the export runs off Tk, can be cancelled, and reports once":
            _export_ui_check(),
        "the notice reads the same at every count":
            _arena_warning_sentences_check(),
        "a deck over Arena's limits is refused before the save prompt":
            _limit_refusal_check(),
        "the refusal names the limits and lists each problem in plain words":
            _arena_limit_sentences_check(),
        "the sideboard is left out of the export and every check on it":
            _sideboard_exclusion_check(),
        "the user is told how many sideboard cards were left out":
            _arena_sideboard_sentence_check(),
        "repeated legendary cards are warned about, never refused":
            _arena_legendary_note_check(),
        "a card that turns over also bundles its back face":
            _back_face_check(),
        "tokens a deck can create are listed in plain words, never refused":
            _arena_token_notice_check(),
        "a printing Arena lacks is exported as one it has, and only then":
            _printing_swap_check(),
        "the real Island and Space Beleren refusals export as Forge's printings":
            _shipped_asset_printing_check(),
        "a deck of printings Forge has exports byte-for-byte as before":
            _unchanged_export_check(),
        "each changed printing is listed in plain words":
            _printing_sentences_check(),
        "printings are chosen on the export worker from the measured snapshot":
            _printing_ui_check(),
    }
    ok = True
    for label, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok &= bool(passed)
    print("\nARENA EXPORT:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
