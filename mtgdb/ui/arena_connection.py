"""Linking this computer to Proxic Arena, inside the Proxic Arena Export window.

Proxic Arena's Account page makes a one-time code; the player types it into
the export window's link form with a name for this computer, and the Deck
Builder is given a device token (Arena specs 0021, 0022). From then on the
window checks every deck against the rules Arena plays by and sends decks to
the player's Deck Depot (ARN-012). The link, the token and the saved rules
belong to ``arena/connection.py``; this module owns only the form and the
linked line, runs the network work on a worker (DUI-017) and words every
outcome in plain language. Nothing a player signs in with is ever asked for here.
"""

import logging
import socket
import tkinter as tk
import urllib.parse

from mtgdb.arena.connection import (
    DEFAULT_ADDRESS, ArenaConnectionError, link_computer, normalize_address,
)
from mtgdb.core.secret_store import sealed
from mtgdb.deck.file_jobs import submit_deck_file_job
from mtgdb.ui.components import AppButton, AppEntry
from mtgdb.ui.tokens import FONT_BODY, FONT_HELPER, PALETTE

log = logging.getLogger("mtg")

ARENA_LINK_LEAD = (
    "Link this computer to send decks. You do this once. After that, every "
    "export goes straight to your Deck Depot.")
ARENA_LINK_STEPS = (
    "1. On Proxic Arena, open your Account page and choose Link a computer.\n"
    "2. Type the code it shows here.")
ARENA_UNSEALED_NOTE = (
    "On this system the link is stored in a file only you can read, not "
    "sealed to your Windows account.")


def arena_host(address):
    """``proxic.barnflix.net`` for ``https://proxic.barnflix.net``."""
    return urllib.parse.urlsplit(address).netloc or address


def arena_link_line(link):
    """The linked strip's sentence, composed apart from its widgets."""
    who = link.display_name or link.username
    return (f"Linked to Proxic Arena as {who} on \"{link.device_name}\" "
            f"· {arena_host(link.address)}")


class ArenaLinkForm:
    """The form that links this computer, drawn into ``parent``.

    ``on_linked(link)`` runs on Tk once the link is saved.
    """

    def __init__(self, app, parent, store, on_linked):
        self.app = app
        self.store = store
        self.on_linked = on_linked
        self.address = DEFAULT_ADDRESS
        p = PALETTE
        edge = tk.Frame(parent, bg=p["accent"])
        edge.pack(fill="x", pady=(0, 2))
        box = tk.Frame(edge, bg=p["surface2"], padx=12, pady=10)
        box.pack(fill="x", padx=1, pady=1)
        self.frame = edge
        tk.Label(box, text=ARENA_LINK_LEAD, bg=p["surface2"], fg=p["text"], font=FONT_BODY,
                 justify="left", anchor="w", wraplength=600).pack(anchor="w", fill="x")
        tk.Label(box, text=ARENA_LINK_STEPS, bg=p["surface2"], fg=p["muted"], font=FONT_HELPER,
                 justify="left", anchor="w", wraplength=600).pack(anchor="w", fill="x", pady=(6, 8))

        fields = tk.Frame(box, bg=p["surface2"])
        fields.pack(fill="x")
        fields.columnconfigure(1, weight=1)
        for column, text in ((0, "Link code"), (1, "Name for this computer")):
            tk.Label(fields, text=text, bg=p["surface2"], fg=p["muted"], font=FONT_HELPER,
                     anchor="w").grid(row=0, column=column, sticky="w", padx=(0, 8))
        self.code = AppEntry(fields, width=14)
        self.code.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.name = AppEntry(fields)
        self.name.insert(0, socket.gethostname() or "Deck Builder")
        self.name.grid(row=1, column=1, sticky="ew", padx=(0, 8))
        self.button = AppButton(fields, text="Link", role="compact_primary", command=self.start)
        self.button.grid(row=1, column=2, sticky="e")
        self.code.bind("<Return>", lambda _e: self.start())

        where = tk.Frame(box, bg=p["surface2"])
        where.pack(fill="x", pady=(8, 0))
        self.where_label = tk.Label(where, text="", bg=p["surface2"], fg=p["muted"],
                                    font=FONT_HELPER, anchor="w")
        self.where_label.pack(side="left")
        AppButton(where, text="Change", role="compact", command=self.change_address).pack(
            side="left", padx=(8, 0))
        self.address_entry = None
        self.where = where
        self._show_where()
        if not sealed():
            tk.Label(box, text=ARENA_UNSEALED_NOTE, bg=p["surface2"], fg=p["muted"],
                     font=FONT_HELPER, justify="left", anchor="w",
                     wraplength=600).pack(anchor="w", fill="x", pady=(6, 0))
        # Packed only once there is something to say, so an empty line adds no gap.
        self.message = tk.Label(box, text="", bg=p["surface2"], fg=p["bad"], font=FONT_HELPER,
                                justify="left", anchor="w", wraplength=600)

    def _show_where(self):
        self.where_label.configure(text=f"Arena address: {arena_host(self.address)}")

    def change_address(self):
        if self.address_entry is not None:
            return
        self.address_entry = AppEntry(self.where)
        self.address_entry.insert(0, self.address)
        self.address_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.address_entry.focus_set()

    def show(self, text, ok=False):
        try:
            self.message.configure(text=text, fg=PALETTE["muted"] if ok else PALETTE["bad"])
            if not self.message.winfo_ismapped():
                self.message.pack(anchor="w", fill="x", pady=(6, 0))
        except tk.TclError:
            pass

    def start(self):
        raw = self.address_entry.get() if self.address_entry is not None else self.address
        try:
            address = normalize_address(raw)
        except ValueError as exc:
            self.show(str(exc))
            return
        code = self.code.get().strip()
        if not code:
            self.show("Type the code Arena's Account page shows.")
            return
        self.button.state(["disabled"])
        self.show("Linking…", ok=True)
        future = submit_deck_file_job(
            _link_or_error, address, code, self.name.get(), name="mtg-arena-link")
        self._poll(future)

    def _poll(self, future):
        """Wait for the link on Tk without blocking it; a closed form drops the answer."""
        if not future.done():
            try:
                self.app.after(50, lambda: self._poll(future))
            except tk.TclError:
                pass
            return
        try:
            if not self.frame.winfo_exists():
                return
        except tk.TclError:
            return
        outcome = future.result()
        if isinstance(outcome, ArenaConnectionError):
            self.button.state(["!disabled"])
            self.show(str(outcome))
            return
        link, token = outcome
        self.store.save_link(link, token)
        log.info("Linked this computer to Proxic Arena at %s as %s", link.address, link.username)
        self.app._status(f"Linked to Proxic Arena as {link.display_name}")
        self.on_linked(link)


def _link_or_error(address, code, name):
    """Run on the worker: the link, or the error to show, never a raised one."""
    try:
        return link_computer(address, code, name)
    except ArenaConnectionError as exc:
        return exc
