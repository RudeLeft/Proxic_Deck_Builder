"""File > Test Deck Export: save the open deck as a Proxic Arena package, for testing.

This is the one place the Deck Builder saves a zip (ARN-019, owner decision
2026-11-04); a deck reaches Arena only through the Proxic Arena Export window,
which saves nothing (ARN-014). The zip is Arena's own package shape, so Arena
and its tests read it as it is:

    <DeckName>.txt                        the main deck, with its Tokens section
    real_cards/<Card Name> [SET].png      one picture per card name
    real_cards/<Card Name> [SET-NUM].png  each other printing of a name

with back faces and token pictures as a send carries them. It always holds
every picture, as full PNGs: no Arena is asked what it already holds, and no
light JPEG or art crop is made. The deck keeps its own printings; no printing
is swapped for one Arena has.

The package is `arena/export.py`'s and the PNGs `printing/service.py`'s; this
owns the file dialog, the progress popup and the words. The work runs on the
deck-file worker (DUI-017) and can be cancelled.
"""

import logging
import os
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from mtgdb.arena.export import (
    ArenaExportCancelled, ArenaExportJob, build_package, package_filename,
)
from mtgdb.core.atomic_files import TEMP_SUFFIX, temp_prefix
from mtgdb.deck.arena_tokens import split_for_arena
from mtgdb.deck.file_jobs import submit_deck_file_job
from mtgdb.deck.io import deck_to_text
from mtgdb.printing.service import back_faces, ensure_face_png, ensure_png
from mtgdb.ui.components import AppButton
from mtgdb.ui.tokens import FONT_BODY, FONT_HELPER, PALETTE

log = logging.getLogger("mtg")

TEST_DECK_EXPORT_TITLE = "Test Deck Export"


def test_deck_job(deck, cache_dir):
    """Return the ``ArenaExportJob`` of a detached ``deck``: main deck, tokens, every printing."""
    parts = split_for_arena(deck)
    text = deck_to_text(parts.cards, include_side=False, tokens=parts.tokens)
    return ArenaExportJob.from_deck(
        parts.cards, text, cache_dir,
        token_cards=[card for card, _quantity in parts.tokens], printings=True)


def save_test_deck(job, path, progress_cb=None, cancel_event=None,
                   ensure_image=ensure_png, ensure_face=ensure_face_png):
    """Build ``job``'s package with every picture as a PNG and write it to ``path`` atomically.

    Nothing is written when the build is cancelled or fails.
    """
    package = build_package(
        job, ensure_image, progress_cb=progress_cb, cancel_event=cancel_event,
        back_faces_of=back_faces, ensure_face=ensure_face)
    folder = os.path.dirname(os.path.abspath(path))
    handle, temporary = tempfile.mkstemp(
        prefix=temp_prefix(os.path.basename(path)), suffix=TEMP_SUFFIX, dir=folder)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(package.data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    return package


def test_deck_saved_text(package, path):
    """What the popup says once the zip is saved."""
    pictures = package.image_count
    text = (f"Saved {os.path.basename(path)} with {pictures} "
            f"picture{'' if pictures == 1 else 's'}.")
    missing = len(package.missing_art)
    if missing:
        text += (f" {missing} card{' has' if missing == 1 else 's have'} no picture: "
                 + ", ".join(package.missing_art[:6])
                 + ("…" if missing > 6 else "") + ".")
    return text


class TestingExportMixin:
    """Own File > Test Deck Export."""

    def _export_test_deck(self):
        deck = self.deck
        if deck is None or deck.total("main") <= 0:
            messagebox.showinfo(TEST_DECK_EXPORT_TITLE,
                                "Add cards to the main deck before exporting a test deck.")
            return
        if getattr(self, "_test_deck_popup", None) is not None:
            return
        path = filedialog.asksaveasfilename(
            title=TEST_DECK_EXPORT_TITLE, defaultextension=".zip",
            initialfile=package_filename(deck.name),
            filetypes=[("Zip files", "*.zip")])
        if not path:
            return
        job = test_deck_job(self._detached_deck_copy(deck),
                            os.path.join(self.image_cache_dir, "print_png"))
        cancel_event = threading.Event()
        progress = queue.Queue()

        def report(stage, current, total, _detail):
            progress.put((stage, current, total))

        future = submit_deck_file_job(
            save_test_deck, job, path, report, cancel_event, name="mtg-test-deck-export")
        self._show_test_deck_popup(cancel_event)
        self._poll_test_deck(future, progress, path)

    def _show_test_deck_popup(self, cancel_event):
        palette = PALETTE
        popup = self._create_hidden_popup(TEST_DECK_EXPORT_TITLE, transient=self,
                                          resizable=False)
        popup.configure(bg=palette["border"])
        popup.protocol("WM_DELETE_WINDOW", cancel_event.set)
        shell = tk.Frame(popup, bg=palette["surface"], padx=24, pady=20)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        ttk.Label(shell, text="TEST DECK EXPORT", style="DialogTitle.TLabel").pack(anchor="w")
        label = tk.Label(shell, text="Getting the deck's pictures ready…",
                         bg=palette["surface"], fg=palette["text"], font=FONT_BODY,
                         anchor="w", justify="left", wraplength=480)
        label.pack(fill="x", pady=(6, 8))
        bar = ttk.Progressbar(shell, mode="determinate", maximum=1, value=0,
                              style="Gold.Horizontal.TProgressbar", length=480)
        bar.pack(fill="x")
        tk.Label(shell, text="Every picture is saved as a full PNG beside the decklist.",
                 bg=palette["surface"], fg=palette["muted"], font=FONT_HELPER,
                 anchor="w").pack(fill="x", pady=(8, 10))
        button = AppButton(shell, text="Cancel", role="compact", command=cancel_event.set)
        button.pack(side="right")
        self._test_deck_popup = (popup, label, bar, button)
        self._present_hidden_popup(popup, preferred_width=540, min_width=480,
                                   min_height=200, lock_size=True, grab=True,
                                   fit_content=True)

    def _poll_test_deck(self, future, progress, path):
        widgets = getattr(self, "_test_deck_popup", None)
        if widgets is None:
            return
        _popup, label, bar, _button = widgets
        latest = None
        try:
            while True:
                latest = progress.get_nowait()
        except queue.Empty:
            pass
        try:
            if latest is not None:
                stage, current, total = latest
                label.configure(text=(
                    f"Getting the deck's pictures ready ({current} of {total})…"
                    if stage == "download" else "Saving the zip…"))
                bar.configure(maximum=max(total, 1), value=current)
            if not future.done():
                self.after(80, lambda: self._poll_test_deck(future, progress, path))
                return
        except tk.TclError:
            return
        try:
            package = future.result()
        except ArenaExportCancelled:
            self._finish_test_deck("Test Deck Export was cancelled. No file was saved.")
            return
        except Exception as exc:
            log.exception("Test Deck Export failed")
            self._close_test_deck_popup()
            messagebox.showerror(TEST_DECK_EXPORT_TITLE,
                                 f"The test deck couldn't be saved.\n\n{exc}")
            return
        log.info("Test Deck Export: %s, %d pictures, %d back faces, %d token pictures, "
                 "%d missing, %d bytes", path, package.image_count, package.back_face_count,
                 package.token_pictures, len(package.missing_art), len(package.data))
        self._status(f"Saved test deck {os.path.basename(path)}")
        self._finish_test_deck(test_deck_saved_text(package, path))

    def _finish_test_deck(self, text):
        """Say how it ended in the popup and turn Cancel into Close."""
        widgets = getattr(self, "_test_deck_popup", None)
        if widgets is None:
            return
        popup, label, bar, button = widgets
        try:
            label.configure(text=text)
            bar.configure(maximum=1, value=1)
            button.configure(text="Close", command=self._close_test_deck_popup)
            popup.protocol("WM_DELETE_WINDOW", self._close_test_deck_popup)
        except tk.TclError:
            self._close_test_deck_popup()

    def _close_test_deck_popup(self):
        widgets = getattr(self, "_test_deck_popup", None)
        self._test_deck_popup = None
        if widgets is None:
            return
        popup = widgets[0]
        try:
            popup.grab_release()
            popup.destroy()
        except tk.TclError:
            pass
