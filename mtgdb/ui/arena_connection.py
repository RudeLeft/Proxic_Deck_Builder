"""The Proxic Arena connection dialog: link this computer, see the link, unlink it.

Proxic Arena's Account page makes a one-time code; the player types it here
with Arena's address and a name for this computer, and the Deck Builder is
given a device token that can download Arena's deck rules and nothing else.
From then on every Proxic Arena export follows the rules that Arena plays by
(ARN-012). The link, the token and the saved rules belong to
``arena/connection.py``; this module owns only the dialog, which runs the
network work on a worker (DUI-017) and words every outcome in plain language.
"""

import logging
import socket
import time
import tkinter as tk
from tkinter import ttk

from mtgdb.arena.connection import (
    DEFAULT_ADDRESS, ArenaConnectionError, ArenaConnectionStore, link_computer,
    normalize_address,
)
from mtgdb.core.secret_store import sealed
from mtgdb.deck.file_jobs import submit_deck_file_job
from mtgdb.ui.components import AppButton, AppEntry
from mtgdb.ui.tokens import FONT_BODY, PALETTE

log = logging.getLogger("mtg")

ARENA_CONNECTION_TITLE = "Proxic Arena Connection"
ARENA_CONNECTION_HEADING = "PROXIC ARENA CONNECTION"
ARENA_LINK_LEAD = (
    "Link this computer to Proxic Arena so every Proxic Arena export follows "
    "the deck rules Arena plays by: which cards and printings it accepts, and "
    "how big a deck may be. On Arena, open your Account page and choose Link "
    "a computer, then type the code it shows here.")
ARENA_UNSEALED_NOTE = (
    "On this system the link is stored in a file only you can read, not "
    "sealed to your Windows account.")


def arena_linked_text(link, saved_at=None):
    """Return the linked dialog's sentences, composed apart from its widgets."""
    who = link.display_name
    if link.username and link.username.casefold() != link.display_name.casefold():
        who = f"{link.display_name} ({link.username})"
    lines = [
        f"Linked to {link.address} as {who}. This computer is \"{link.device_name}\".",
        "Every Proxic Arena export downloads Arena's current deck rules first.",
    ]
    if saved_at:
        when = time.strftime("%b %d, %I:%M %p", time.localtime(saved_at))
        lines.append(f"The rules were last downloaded {when}.")
    lines.append(
        "Unlinking forgets the link here. To stop it working on Arena too, "
        "remove this computer from Arena's Account page.")
    return "\n\n".join(lines)


class ArenaConnectionMixin:
    """Own the Proxic Arena connection dialog."""

    def _arena_store(self):
        return ArenaConnectionStore(self.data_dir)

    def _open_arena_connection(self):
        """Show the link, or the form that makes one."""
        store = self._arena_store()
        link = store.link()
        p = PALETTE
        popup = self._create_hidden_popup(ARENA_CONNECTION_TITLE, transient=self)
        popup.configure(bg=p["border"])
        popup.protocol("WM_DELETE_WINDOW", popup.destroy)
        shell = tk.Frame(popup, bg=p["surface"], padx=18, pady=16)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        ttk.Label(
            shell, text=ARENA_CONNECTION_HEADING, style="DialogTitle.TLabel"
        ).pack(anchor="w", pady=(0, 10))
        if link is None:
            self._build_arena_link_form(popup, shell, store)
        else:
            self._build_arena_linked_view(popup, shell, store, link)
        popup.bind("<Escape>", lambda _e: popup.destroy())
        self._present_hidden_popup(
            popup, preferred_width=560, min_width=460, min_height=260,
            grab=True, fit_content=True)
        return popup

    def _arena_text(self, parent, text, **options):
        p = PALETTE
        label = tk.Label(
            parent, text=text, bg=p["surface"], fg=options.pop("fg", p["text"]),
            font=FONT_BODY, justify="left", anchor="w", wraplength=520)
        label.pack(anchor="w", fill="x", **options)
        return label

    def _build_arena_linked_view(self, popup, shell, store, link):
        _payload, _etag, saved_at = store.saved_rules()
        self._arena_text(shell, arena_linked_text(link, saved_at))
        foot = tk.Frame(shell, bg=PALETTE["surface"])
        foot.pack(fill="x", pady=(14, 0))
        AppButton(foot, text="Close", role="compact", command=popup.destroy).pack(side="right")

        def unlink():
            store.forget()
            log.info("Unlinked this computer from Proxic Arena at %s", link.address)
            self._status("This computer is no longer linked to Proxic Arena")
            popup.destroy()

        AppButton(foot, text="Unlink", role="compact", command=unlink).pack(
            side="right", padx=(0, 8))

    def _build_arena_link_form(self, popup, shell, store):
        p = PALETTE
        self._arena_text(shell, ARENA_LINK_LEAD, pady=(0, 12))
        fields = {}
        for key, label, value in (
                ("address", "Proxic Arena address", DEFAULT_ADDRESS),
                ("name", "This computer's name", socket.gethostname() or "Deck Builder"),
                ("code", "Code from Arena", "")):
            tk.Label(
                shell, text=label, bg=p["surface"], fg=p["muted"],
                font=FONT_BODY, anchor="w").pack(anchor="w", fill="x")
            entry = AppEntry(shell)
            entry.insert(0, value)
            entry.pack(fill="x", pady=(2, 10))
            fields[key] = entry
        if not sealed():
            self._arena_text(shell, ARENA_UNSEALED_NOTE, fg=p["muted"], pady=(0, 6))
        message = self._arena_text(shell, "", fg=p["muted"])

        foot = tk.Frame(shell, bg=p["surface"])
        foot.pack(fill="x", pady=(10, 0))
        AppButton(foot, text="Close", role="compact", command=popup.destroy).pack(side="right")
        link_button = AppButton(foot, text="Link", role="compact_primary")
        link_button.pack(side="right", padx=(0, 8))

        def show(text, ok=False):
            message.configure(text=text, fg=p["text"] if ok else p["muted"])

        def linked(outcome):
            link, token = outcome
            store.save_link(link, token)
            log.info("Linked this computer to Proxic Arena at %s as %s", link.address, link.username)
            self._status(f"Linked to Proxic Arena as {link.display_name}")
            popup.destroy()
            self._open_arena_connection()

        def failed(error):
            link_button.state(["!disabled"])
            show(str(error))

        def start():
            try:
                address = normalize_address(fields["address"].get())
            except ValueError as exc:
                show(str(exc))
                return
            code = fields["code"].get().strip()
            if not code:
                show("Type the code Arena's Account page shows.")
                return
            link_button.state(["disabled"])
            show("Linking…", ok=True)
            future = submit_deck_file_job(
                _link_or_error, address, code, fields["name"].get(), name="mtg-arena-link")
            self._poll_arena_link(future, popup, linked, failed)

        link_button.configure(command=start)
        fields["code"].bind("<Return>", lambda _e: start())
        fields["code"].focus_set()

    def _poll_arena_link(self, future, popup, linked, failed):
        """Wait for the link on Tk without blocking it; a closed dialog drops the answer."""
        if not future.done():
            try:
                self.after(50, lambda: self._poll_arena_link(future, popup, linked, failed))
            except tk.TclError:
                pass
            return
        if not popup.winfo_exists():
            return
        outcome = future.result()
        if isinstance(outcome, ArenaConnectionError):
            failed(outcome)
        else:
            linked(outcome)


def _link_or_error(address, code, name):
    """Run on the worker: the link, or the error to show, never a raised one."""
    try:
        return link_computer(address, code, name)
    except ArenaConnectionError as exc:
        return exc
