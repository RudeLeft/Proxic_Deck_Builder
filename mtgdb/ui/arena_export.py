"""Proxic Arena export presentation: the bundle action, its progress, its report.

The export reaches two feature packages the deck-file workflow may not touch --
`mtgdb.arena` for the bundle and `mtgdb.printing` for the validated card-art
download -- so it is its own UI module with its own cluster row rather than a
boundary break inside `ui/deck_files.py` (LAYER-006).

It owns no file format and no name rule: the zip layout and Arena's art naming
belong to `arena/export.py`, which deck rules apply (downloaded from a linked
Arena, saved, or built in) to `arena/connection.py`, reading them and the
playable-name comparison to `deck/arena_support.py`, which printing each card
is exported as to `deck/arena_printings.py`, the size and copy limits to
`deck/arena_limits.py`, the tokens a deck can create to `deck/arena_tokens.py`,
and the PNG download, cache, face handling and HTTP policy to
`printing/service.py`.
"""

import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

from mtgdb.arena.connection import (
    SOURCE_ARENA, SOURCE_BUILT_IN, ArenaConnectionStore, obtain_rules,
)
from mtgdb.arena.export import (
    ArenaExportJob, build_export, distinct_art_cards,
)
from mtgdb.deck.arena_limits import arena_deck_limits
from mtgdb.deck.arena_printings import arena_export_deck
from mtgdb.deck.arena_support import (
    rules_from_document, unsupported_deck_names,
)
from mtgdb.deck.arena_tokens import deck_tokens, without_game_pieces
from mtgdb.deck.file_jobs import submit_deck_file_job
from mtgdb.deck.io import deck_to_text
from mtgdb.printing.service import back_faces, ensure_face_png, ensure_png
from mtgdb.ui.assets import ARENA_DECK_RULES_FILE, _asset_path
from mtgdb.ui.components import AppButton, autohide_scrollbar
from mtgdb.ui.tokens import FONT_BODY, PALETTE

log = logging.getLogger("mtg")

ARENA_WARNING_NAME_LIMIT = 30
ARENA_WARNING_HEADING = "PROXIC ARENA EXPORT"
ARENA_WARNING_WINDOW_TITLE = "Proxic Arena Export"
ARENA_WARNING_LEAD = (
    "The following cards in your deck are not in the Proxic Arena card pool "
    "and will not play properly")
ARENA_WARNING_CLOSING = "The rest of the deck will play without issue."
ARENA_LIMIT_CLOSING = "Nothing was exported. Adjust the deck and try again."
# Legendary cards named inline before the rest are counted: the warning is a
# paragraph in the report, not a list, so it is kept short.
ARENA_LEGENDARY_NAME_LIMIT = 6
ARENA_TOKEN_INTRO = (
    "Good to know: these cards make tokens during a game. Proxic Arena makes "
    "the tokens itself, so there is nothing to add:")
# Deck cards named against each token before the rest are counted.
ARENA_TOKEN_MAKER_LIMIT = 3
ARENA_PRINTING_INTRO = (
    "Proxic Arena doesn't have these printings, so the export uses one it "
    "does have. Your saved deck keeps your own printings:")


# A side deck's name, as one of its cards is called.
_SIDE_DECK_WORDS = {"CONTRAPTIONS": "Contraption", "ATTRACTIONS": "Attraction"}


def arena_limit_lead(rules_format):
    """The refusal's first sentence, with Arena's own numbers in it."""
    return (
        "This deck can't be exported for Proxic Arena yet. Proxic Arena "
        f"accepts {rules_format.min_cards} to {rules_format.max_cards} cards, "
        f"with no more than {rules_format.max_copies} copies of any card "
        "(basic lands are exempt).")


def arena_limit_text(limits, rules_format):
    """Return the refusal's lead line, listed problems, overflow count, closing.

    Composed apart from the widgets, like the unplayable-card notice, so the
    sentences are checked without a Tk root. Each listed line is one problem in
    plain words: the deck's size, each over-limit card by name, then each side
    deck that is too small.
    """
    lines = []
    if limits.cards_over:
        lines.append(
            f"Deck size: {limits.total_cards} cards, {limits.cards_over} over "
            f"the {limits.max_cards}-card limit")
    if limits.cards_short:
        lines.append(
            f"Deck size: {limits.total_cards} cards, {limits.cards_short} short "
            f"of the {limits.min_cards}-card minimum")
    for name, copies, allowed in limits.over_copy_limit:
        lines.append(
            f"{name}: {copies} copies, {copies - allowed} over the limit "
            f"of {allowed}")
    for section, count, least in limits.short_side_decks:
        word = _SIDE_DECK_WORDS.get(section, section.title())
        lines.append(
            f"{word} deck: {count} cards; Proxic Arena needs at least {least} "
            f"{word}s, or none")
    shown = lines[:ARENA_WARNING_NAME_LIMIT]
    return (arena_limit_lead(rules_format), shown, len(lines) - len(shown),
            ARENA_LIMIT_CLOSING)


def arena_game_piece_note(names):
    """The sentence saying which tokens were left out of the deck, or "".

    Arena makes tokens during the game and refuses a decklist that names one,
    so they are never exported and never count toward the deck's size.
    """
    if not names:
        return ""
    listed = ", ".join(names[:ARENA_LEGENDARY_NAME_LIMIT])
    if len(names) > ARENA_LEGENDARY_NAME_LIMIT:
        listed += f" and {len(names) - ARENA_LEGENDARY_NAME_LIMIT} more"
    return (
        "Tokens aren't cards in a Proxic Arena deck: Arena makes them during "
        f"the game. So these were left out, and don't count toward the deck's "
        f"size: {listed}.")


def arena_rules_note(obtained):
    """Where this export's deck rules came from, in plain words, or "".

    Nothing is said when Arena itself answered, which is the ordinary case.
    """
    if obtained is None or obtained.source == SOURCE_ARENA:
        return ""
    if obtained.unlinked:
        return (
            "Proxic Arena no longer accepts this computer's link (it was "
            "removed on Arena), so it was unlinked here and this export used "
            "the deck rules saved earlier. Link it again under File > Proxic "
            "Arena Connection.")
    if obtained.source == SOURCE_BUILT_IN:
        return (
            "This computer is not linked to Proxic Arena, so this export used "
            "the deck rules built into this version. Link it under File > "
            "Proxic Arena Connection to always use Arena's current rules.")
    when = ""
    if obtained.saved_at:
        when = " on " + time.strftime(
            "%b %d", time.localtime(obtained.saved_at))
    return (
        "Proxic Arena could not be reached, so this export used the deck "
        f"rules downloaded{when}.")


def arena_sideboard_note(count):
    """Return the sentence telling the user their sideboard was left out.

    Proxic Arena decks have no sideboard, so none is exported. An empty string
    means there was nothing to leave out, and the report says nothing at all
    about a sideboard. The count's noun agrees with it ("1 card", "7 cards").
    """
    if count <= 0:
        return ""
    noun = "card" if count == 1 else "cards"
    return (
        "Sideboard cards are not included in a Proxic Arena export. This "
        f"deck's sideboard ({count} {noun}) was left out.")


def arena_legendary_note(repeats):
    """Return the warning about legendary cards held more than once, or "".

    A deck may hold several copies, so this warns and never refuses. Each card
    is named with its copy count, capped with the remainder counted rather than
    dropped. An empty string means no legendary permanent repeats, and the
    report says nothing about the legend rule at all.
    """
    if not repeats:
        return ""
    named = [
        f"{name} ({copies} copies)"
        for name, copies in repeats[:ARENA_LEGENDARY_NAME_LIMIT]]
    remaining = len(repeats) - len(named)
    if remaining:
        named.append(f"and {remaining} more")
    return (
        "Heads up: you can only control one legendary permanent with a given "
        "name at a time. If you play a second copy, Proxic Arena will ask you "
        "to discard one. Your deck has more than one of: "
        + "; ".join(named) + ".")


def arena_back_face_phrase(count):
    """Return ``, including N back face(s)`` for the written line, or "".

    A card that turns over ships two images, so the image count runs past the
    card count; this says why rather than leaving the numbers to look wrong.
    """
    if count <= 0:
        return ""
    noun = "back face" if count == 1 else "back faces"
    return f", including {count} {noun}"


def arena_token_section(tokens):
    """Return ``(intro, lines, remaining)`` for the tokens a deck can create.

    An empty tuple means no card in the deck makes a token, and the report says
    nothing about tokens at all. Tokens are not in the export, so the intro says
    so and asks the player to add their art; each line names the token, its type
    line, and the deck cards that can make it, capped with the remainder counted
    rather than dropped.
    """
    if not tokens:
        return ()
    lines = []
    for token in tokens:
        makers = list(token.makers[:ARENA_TOKEN_MAKER_LIMIT])
        extra = len(token.makers) - len(makers)
        if extra:
            makers.append(f"and {extra} more")
        label = (f"{token.name} ({token.type_line})" if token.type_line
                 else token.name)
        lines.append(f"{label}, from {', '.join(makers)}")
    shown = lines[:ARENA_WARNING_NAME_LIMIT]
    return (ARENA_TOKEN_INTRO, shown, len(lines) - len(shown))


def arena_printing_line(swap):
    """Return one swap in plain words: ``Island: MDD 294 -> M21 263 (...)``.

    A card exported by name alone says so, because no printing Proxic Arena
    has could be pictured and Arena picks its own.
    """
    before = " ".join(part for part in (swap.from_set, swap.from_number) if part)
    if not swap.to_set:
        return (f"{swap.name}: {before} -> listed by name only (Proxic Arena "
                "picks the printing)")
    after = " ".join(part for part in (swap.to_set, swap.to_number) if part)
    return f"{swap.name}: {before} -> {after} (the printing Proxic Arena has)"


def arena_printing_section(swaps):
    """Return ``(intro, lines, remaining)`` for the printings the export changed.

    An empty tuple means every printing was exported as the deck holds it, and
    the report says nothing about printings at all. Capped with the remainder
    counted rather than dropped.
    """
    if not swaps:
        return ()
    lines = [arena_printing_line(swap) for swap in swaps]
    shown = lines[:ARENA_WARNING_NAME_LIMIT]
    return (ARENA_PRINTING_INTRO, shown, len(lines) - len(shown))


def arena_warning_text(unsupported):
    """Return the notice's lead line, listed names, overflow count, and closing.

    The wording lives apart from the widgets so the sentences can be checked
    without a Tk root. The lead carries no count and so needs no subject/verb
    agreement, and it does not repeat that the deck was saved: the dialog's
    own heading says that, and both together read it twice.
    """
    shown = list(unsupported[:ARENA_WARNING_NAME_LIMIT])
    return (
        ARENA_WARNING_LEAD, shown, len(unsupported) - len(shown),
        ARENA_WARNING_CLOSING)


class ArenaExportMixin:
    """Own the Proxic Arena bundle action and the dialogs that report it."""

    def _export_arena(self):
        """Bundle the active deck and its card art for Proxic Arena.

        Arena draws real art only for files its client already holds, so a
        decklist on its own plays through generated art. This writes both as
        one zip -- the decklist, plus `real_cards/<Card Name> [SET].png` for
        every distinct main-deck card -- and reports which cards Arena cannot
        play. A printing Arena does not have is exported as one it does, and
        the report lists each; the saved deck keeps its own printings. The
        sideboard is not exported, and the report says so.

        It returns nothing: the bundle is written on a worker, so a value
        returned here could only mean "started", which DUI-020 forbids a
        caller from mistaking for "done".
        """
        index = self.deck_sessions.active_index
        if not self.deck_sessions.is_valid_index(index):
            return
        self._sync_deck_meta()
        deck = self.deck_sessions[index].deck
        if not deck.total("main"):
            self._show_arena_list_dialog(
                "This deck has no cards in its main deck to export yet.",
                [], 0, "")
            return

        # One snapshot is measured and then exported, so the deck that passed
        # is the deck that is written, however long the save prompt stays open.
        # Tokens the player put in the deck are left out of both (ARN-013).
        detached, game_pieces = without_game_pieces(self._detached_deck_copy(deck))
        if not detached.total("main"):
            self._show_arena_list_dialog(
                "This deck has no cards in its main deck to export yet.",
                [], 0, arena_game_piece_note(game_pieces))
            return
        store = ArenaConnectionStore(self.data_dir)
        built_in = _asset_path(ARENA_DECK_RULES_FILE)

        def read_rules():
            # Asking Arena is network work, and reading the document is a few
            # megabytes of JSON: both on the worker, never on Tk (DUI-017).
            obtained = obtain_rules(
                store, built_in, lambda payload: rules_from_document(payload).known)
            return obtained, rules_from_document(obtained.payload)

        def checked(outcome):
            obtained, rules = outcome
            if obtained.unlinked:
                log.info("Proxic Arena refused this computer's link; unlinked")
            self._status("")
            self._continue_arena_export(deck, detached, rules, obtained, game_pieces)

        self._status("Checking Proxic Arena's deck rules\u2026")
        future = submit_deck_file_job(read_rules, name="mtg-arena-rules")
        self._poll_deck_file_job(
            future, checked, error_title="Proxic Arena export failed")

    def _continue_arena_export(self, deck, detached, rules, obtained, game_pieces=()):
        """Refuse, or ask where to save and write the bundle, by the rules just read."""
        note = "\n\n".join(
            part for part in (arena_game_piece_note(game_pieces), arena_rules_note(obtained))
            if part)
        legendary_repeats = ()
        # Refused before asking where to save: a deck Arena will not accept
        # should never reach a file dialog. With no rules at all (a damaged
        # install) nothing is refused, as nothing is known.
        if rules.known:
            limits = arena_deck_limits(detached, rules.format)
            if not limits.allowed:
                lead, shown, remaining, closing = arena_limit_text(limits, rules.format)
                if note:
                    closing = f"{closing}\n\n{note}"
                self._show_arena_list_dialog(lead, shown, remaining, closing)
                return
            legendary_repeats = limits.legendary_repeats

        path = filedialog.asksaveasfilename(
            title="Proxic Arena Export", defaultextension=".zip",
            initialfile=f"{deck.name}.zip",
            filetypes=[("Zip Archives", "*.zip")])
        if not path:
            return

        sideboard_count = detached.total("side")
        tokens = deck_tokens(detached)
        cache_dir = os.path.join(self.image_cache_dir, "print_png")
        printings_named = self.db.printings_named
        support = rules.support
        cancel_event = threading.Event()
        # Progress crosses threads through a queue rather than a Tk call from
        # the worker; Tk polls it (BEH-002).
        progress = queue.Queue()

        def run_export():
            def report(stage, current, total, detail):
                progress.put((stage, current, total, detail))
            unsupported = unsupported_deck_names(
                detached, support.names, boards=("main",))
            # The printings Arena checks are chosen here, on the worker,
            # because finding a replacement reads the card database.
            export_deck, swaps = arena_export_deck(
                detached, support, printings_named)
            job = ArenaExportJob.from_deck(
                export_deck, deck_to_text(export_deck, include_side=False),
                path, cache_dir)
            result = build_export(
                job, ensure_png, progress_cb=report,
                cancel_event=cancel_event, back_faces_of=back_faces,
                ensure_face=ensure_face_png)
            return result, unsupported, swaps

        def exported(outcome):
            self._close_arena_export_progress()
            result, unsupported, swaps = outcome
            log.info(
                "Arena export wrote %s: %d images for %d cards, %d without art,"
                " %d unplayable, %d printings changed, rules from %s",
                result.output_path, result.image_count, result.card_count,
                len(result.missing_art), len(unsupported), len(swaps),
                obtained.source)
            self._status(f"Exported {os.path.basename(result.output_path)}")
            self._report_arena_export(
                result, unsupported, sideboard_count, legendary_repeats,
                tokens, swaps, note)

        self._status("Exporting for Proxic Arena\u2026")
        popup = self._show_arena_export_progress(
            len(distinct_art_cards(detached)), cancel_event)
        self._arena_export_poll(progress, popup)
        future = submit_deck_file_job(run_export, name="mtg-arena-export")
        self._poll_deck_file_job(
            future, exported, error_title="Proxic Arena export failed",
            on_error=self._close_arena_export_progress)

    def _show_arena_export_progress(self, total, cancel_event):
        """Open the dark export progress popup and return it."""
        p = PALETTE
        popup = self._create_hidden_popup(
            "Proxic Arena Export", transient=self, resizable=False)
        popup.configure(bg=p["border"])
        # Closing the window carries the same intent as Cancel.
        popup.protocol("WM_DELETE_WINDOW", cancel_event.set)

        shell = tk.Frame(popup, bg=p["surface"], padx=18, pady=16)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        ttk.Label(
            shell, text="PROXIC ARENA EXPORT", style="DialogTitle.TLabel"
        ).pack(anchor="w", pady=(0, 10))
        self._arena_export_label = tk.Label(
            shell, text=f"Downloading card art for {total} cards\u2026",
            bg=p["surface"], fg=p["text"], font=FONT_BODY,
            justify="left", anchor="w", wraplength=430)
        self._arena_export_label.pack(anchor="w", fill="x")
        self._arena_export_bar = ttk.Progressbar(
            shell, orient="horizontal", mode="determinate",
            maximum=max(total, 1),
            style="Gold.Horizontal.TProgressbar", length=430)
        self._arena_export_bar.pack(fill="x", pady=(12, 0))

        foot = tk.Frame(shell, bg=p["surface"])
        foot.pack(fill="x", pady=(12, 0))
        AppButton(
            foot, text="Cancel", role="compact", command=cancel_event.set
        ).pack(side="right")

        self._arena_export_popup = popup
        self._present_hidden_popup(
            popup, preferred_width=500, min_width=460, min_height=200,
            grab=False, fit_content=True)
        return popup


    def _arena_export_poll(self, progress, popup):
        """Drain worker progress onto Tk until the popup is gone."""
        if getattr(self, "_arena_export_popup", None) is not popup:
            return
        latest = None
        try:
            while True:
                latest = progress.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            stage, current, total, _detail = latest
            try:
                self._arena_export_bar.configure(
                    maximum=max(total, 1), value=current)
                self._arena_export_label.configure(
                    text="Writing the zip\u2026" if stage == "package"
                    else f"Downloading card art ({current}/{total})\u2026")
            except (tk.TclError, AttributeError):
                return
        try:
            self._arena_export_after = self.after(
                120, lambda: self._arena_export_poll(progress, popup))
        except tk.TclError:
            pass


    def _close_arena_export_progress(self):
        """Tear the progress popup down once, cancelling its own poll (BEH-006)."""
        handle = getattr(self, "_arena_export_after", None)
        if handle:
            try:
                self.after_cancel(handle)
            except (tk.TclError, ValueError):
                pass
        self._arena_export_after = None
        popup = getattr(self, "_arena_export_popup", None)
        self._arena_export_popup = None
        self._arena_export_bar = None
        self._arena_export_label = None
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass


    def _report_arena_export(
            self, result, unsupported, sideboard_count=0,
            legendary_repeats=(), tokens=(), swaps=(), rules_note=""):
        """Report what the bundle holds, and what Arena still cannot play.

        One dialog rather than two: the write, the playability answer, the note
        that the sideboard was left out, the legend-rule warning, the printings
        the export changed and the tokens the deck can create belong to the
        same action, and a second popup to dismiss is not news.
        """
        written = (
            f"Wrote {os.path.basename(result.output_path)}: "
            f"{result.image_count} card images for {result.card_count} cards"
            f"{arena_back_face_phrase(result.back_face_count)}.")
        for note in (arena_sideboard_note(sideboard_count),
                     arena_legendary_note(legendary_repeats)):
            if note:
                written = f"{written}\n\n{note}"
        if unsupported:
            lead, shown, remaining, closing = arena_warning_text(unsupported)
            lead = f"{written}\n\n{lead}"
        else:
            lead, shown, remaining = written, [], 0
            closing = "Every card in this deck will play in Proxic Arena."
        if result.missing_art:
            closing = (
                f"{closing}\n\nNo art could be downloaded for "
                f"{len(result.missing_art)} card(s); Proxic Arena draws its "
                "own for those.")
        if rules_note:
            closing = f"{closing}\n\n{rules_note}"
        sections = [
            section for section in (
                arena_printing_section(swaps), arena_token_section(tokens))
            if section]
        self._show_arena_list_dialog(
            lead, shown, remaining, closing, sections=sections)


    def _build_arena_list(self, shell, shown, remaining, max_rows):
        """Draw one bordered dark list whose scrollbar appears only on overflow."""
        p = PALETTE
        list_shell = tk.Frame(
            shell, bg=p["border"], highlightthickness=1,
            highlightbackground=p["border"])
        list_shell.pack(fill="both", expand=True)
        list_shell.rowconfigure(0, weight=1)
        list_shell.columnconfigure(0, weight=1)

        names = tk.Listbox(
            list_shell, activestyle="none", exportselection=False,
            background=p["input"], foreground=p["text"],
            selectbackground=p["accent"],
            selectforeground=p["on_accent"],
            highlightthickness=0, relief="flat", bd=0,
            height=min(max(len(shown), 3), max_rows),
            font=FONT_BODY)
        scroll = ttk.Scrollbar(
            list_shell, orient="vertical", command=names.yview,
            style="Dark.Vertical.TScrollbar")
        names.configure(yscrollcommand=autohide_scrollbar(scroll))
        names.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self._register_scrollable(names)

        for name in shown:
            names.insert("end", f"  {name}")
        if remaining:
            # Counted rather than dropped silently.
            names.insert("end", f"  +{remaining} more")

    def _show_arena_list_dialog(
            self, lead, shown, remaining, closing, sections=()):
        """Present an Arena notice as an app-owned dark dialog, not a native one.

        A native `messagebox` paints the host platform's own grey chrome and
        reads as a different application beside the charcoal/gold popups, the
        way comparison notices did before CMP-012. This mirrors the Basic
        Format Check dialog: one gold `DialogTitle.TLabel`, a bordered dark
        list whose scrollbar appears only when it overflows (UI-016), and a
        single compact secondary `Close` (UI-012). With nothing to list the
        list is omitted rather than drawn empty. Each ``(intro, lines,
        remaining)`` in ``sections`` adds a further labelled list after the
        closing sentence, shorter so two lists fit a small screen.
        """
        p = PALETTE
        popup = self._create_hidden_popup(
            ARENA_WARNING_WINDOW_TITLE, transient=self)
        popup.configure(bg=p["border"])
        popup.protocol("WM_DELETE_WINDOW", popup.destroy)

        shell = tk.Frame(popup, bg=p["surface"], padx=18, pady=16)
        shell.pack(fill="both", expand=True, padx=1, pady=1)

        ttk.Label(
            shell, text=ARENA_WARNING_HEADING, style="DialogTitle.TLabel"
        ).pack(anchor="w", pady=(0, 10))

        # UI-017: a left-justified classic label is anchored west, or the text
        # block is centered inside a label wider than itself.
        tk.Label(
            shell, text=lead, bg=p["surface"], fg=p["text"], font=FONT_BODY,
            justify="left", anchor="w", wraplength=520
        ).pack(anchor="w", fill="x", pady=(0, 10))

        # Shorter lists as more of them share the dialog, so all of them fit
        # a small screen.
        list_rows = (12, 8, 6)[min(len(sections), 2)]
        if shown:
            self._build_arena_list(shell, shown, remaining, list_rows)

        if closing:
            tk.Label(
                shell, text=closing, bg=p["surface"], fg=p["text"],
                font=FONT_BODY, justify="left", anchor="w", wraplength=520
            ).pack(anchor="w", fill="x", pady=(10, 0))

        for intro, lines, more in sections:
            tk.Label(
                shell, text=intro, bg=p["surface"], fg=p["text"],
                font=FONT_BODY, justify="left", anchor="w", wraplength=520
            ).pack(anchor="w", fill="x", pady=(10, 6))
            self._build_arena_list(shell, lines, more, list_rows)

        foot = tk.Frame(shell, bg=p["surface"])
        foot.pack(fill="x", pady=(12, 0))
        AppButton(
            foot, text="Close", role="compact", command=popup.destroy
        ).pack(side="right")

        popup.bind("<Escape>", lambda _e: popup.destroy())
        self._present_hidden_popup(
            popup, preferred_width=560, preferred_height=420,
            min_width=460, min_height=300, grab=True, fit_content=True)
