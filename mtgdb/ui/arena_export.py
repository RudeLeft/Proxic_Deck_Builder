"""Proxic Arena export presentation: the bundle action, its progress, its report.

The export reaches two feature packages the deck-file workflow may not touch --
`mtgdb.arena` for the bundle and `mtgdb.printing` for the validated card-art
download -- so it is its own UI module with its own cluster row rather than a
boundary break inside `ui/deck_files.py` (LAYER-006).

It owns no file format and no name rule: the zip layout and Arena's art naming
belong to `arena/export.py`, the playable-name comparison to
`deck/arena_support.py`, the size and copy limits to `deck/arena_limits.py`, the
tokens a deck can create to `deck/arena_tokens.py`, and the PNG download, cache,
face handling and HTTP policy to `printing/service.py`.
"""

import logging
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from mtgdb.arena.export import ArenaExportJob, build_export
from mtgdb.deck.arena_limits import (
    ARENA_COPY_LIMIT, ARENA_MAX_CARDS, arena_deck_limits,
)
from mtgdb.deck.arena_support import (
    load_supported_names, unsupported_deck_names,
)
from mtgdb.deck.arena_tokens import deck_tokens
from mtgdb.deck.file_jobs import submit_deck_file_job
from mtgdb.deck.io import deck_to_text
from mtgdb.printing.service import back_faces, ensure_face_png, ensure_png
from mtgdb.ui.assets import ARENA_SUPPORTED_CARDS_FILE, _asset_path
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
ARENA_LIMIT_LEAD = (
    "This deck can't be exported for Proxic Arena yet. Proxic Arena accepts "
    f"up to {ARENA_MAX_CARDS} cards, with no more than {ARENA_COPY_LIMIT} "
    "copies of any card (basic lands and cards that allow any number of "
    "copies are exempt).")
ARENA_LIMIT_CLOSING = "Nothing was exported. Adjust the deck and try again."
# Legendary cards named inline before the rest are counted: the warning is a
# paragraph in the report, not a list, so it is kept short.
ARENA_LEGENDARY_NAME_LIMIT = 6
ARENA_TOKEN_INTRO = (
    "These cards in your deck can create tokens. Tokens are not included in "
    "the export, so add card art for them yourself if you want it:")
# Deck cards named against each token before the rest are counted.
ARENA_TOKEN_MAKER_LIMIT = 3


def arena_limit_text(limits):
    """Return the refusal's lead line, listed problems, overflow count, closing.

    Composed apart from the widgets, like the unplayable-card notice, so the
    sentences are checked without a Tk root. Each listed line is one problem in
    plain words: the deck's size, then each over-limit card by name.
    """
    lines = []
    if limits.cards_over:
        lines.append(
            f"Deck size: {limits.total_cards} cards, {limits.cards_over} over "
            f"the {ARENA_MAX_CARDS}-card limit")
    for name, copies, allowed in limits.over_copy_limit:
        lines.append(
            f"{name}: {copies} copies, {copies - allowed} over the limit "
            f"of {allowed}")
    shown = lines[:ARENA_WARNING_NAME_LIMIT]
    return (ARENA_LIMIT_LEAD, shown, len(lines) - len(shown),
            ARENA_LIMIT_CLOSING)


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
        play. The sideboard is not exported, and the report says so.

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
        # Refused before asking where to save: a deck Arena will not accept
        # should never reach a file dialog. The measurement is in-memory, so it
        # needs no worker (DUI-017 concerns file work).
        detached = self._detached_deck_copy(deck)
        limits = arena_deck_limits(detached)
        if not limits.allowed:
            self._show_arena_list_dialog(*arena_limit_text(limits))
            return

        path = filedialog.asksaveasfilename(
            title="Proxic Arena Export", defaultextension=".zip",
            initialfile=f"{deck.name}.zip",
            filetypes=[("Zip Archives", "*.zip")])
        if not path:
            return

        sideboard_count = detached.total("side")
        legendary_repeats = limits.legendary_repeats
        tokens = deck_tokens(detached)
        job = ArenaExportJob.from_deck(
            detached, deck_to_text(detached, include_side=False), path,
            os.path.join(self.image_cache_dir, "print_png"))
        cancel_event = threading.Event()
        # Progress crosses threads through a queue rather than a Tk call from
        # the worker; Tk polls it (BEH-002).
        progress = queue.Queue()

        def run_export():
            def report(stage, current, total, detail):
                progress.put((stage, current, total, detail))
            supported = load_supported_names(
                _asset_path(ARENA_SUPPORTED_CARDS_FILE))
            unsupported = unsupported_deck_names(
                detached, supported, boards=("main",))
            result = build_export(
                job, ensure_png, progress_cb=report,
                cancel_event=cancel_event, back_faces_of=back_faces,
                ensure_face=ensure_face_png)
            return result, unsupported

        def exported(outcome):
            self._close_arena_export_progress()
            result, unsupported = outcome
            log.info(
                "Arena export wrote %s: %d images for %d cards, %d without art,"
                " %d unplayable", result.output_path, result.image_count,
                result.card_count, len(result.missing_art), len(unsupported))
            self._status(f"Exported {os.path.basename(result.output_path)}")
            self._report_arena_export(
                result, unsupported, sideboard_count, legendary_repeats,
                tokens)

        self._status("Exporting for Proxic Arena\u2026")
        popup = self._show_arena_export_progress(len(job.cards), cancel_event)
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
            legendary_repeats=(), tokens=()):
        """Report what the bundle holds, and what Arena still cannot play.

        One dialog rather than two: the write, the playability answer, the note
        that the sideboard was left out, the legend-rule warning and the tokens
        the deck can create belong to the same action, and a second popup to
        dismiss is not news.
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
        token_section = arena_token_section(tokens)
        self._show_arena_list_dialog(
            lead, shown, remaining, closing,
            sections=[token_section] if token_section else [])


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

        list_rows = 8 if sections else 12
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
