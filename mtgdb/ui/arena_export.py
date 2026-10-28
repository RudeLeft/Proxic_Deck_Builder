"""The Proxic Arena Export window: link, check, name, tokens and send, in one place.

The **Proxic Arena Export** button beside Print Deck opens one window that holds
everything a deck needs to reach Proxic Arena (owner decision 2026-10-27): this
computer's link (and the form that makes one), the deck's name on Arena with a
warning when the player's Deck Depot already uses it, the deck checked against
Arena's own deck rules, the tokens the deck's cards make and whether the deck
brings their pictures, and **Send to Arena**, which puts the deck straight into
the player's Deck Depot (Arena spec 0022). No file is saved on the way (ARN-014).

The window stays open while the player edits the deck and checks again whenever
the deck changes, so a red item can be fixed without closing it.

It reaches two feature packages the deck-file workflow may not touch --
`mtgdb.arena` for the package and the connection, `mtgdb.printing` for the
validated card-art download -- so it is its own UI module with its own cluster
row rather than a boundary break inside `ui/deck_files.py` (LAYER-006). It owns
the window and its words only: the check is `deck/arena_check.py`'s, the
package `arena/export.py`'s, the link, the rules and the send
`arena/connection.py`'s, and the PNG download `printing/service.py`'s. Every
piece of network or disk work runs on the deck-file worker (DUI-017).
"""

import logging
import os
import queue
import threading
import time
import tkinter as tk
import urllib.parse
import webbrowser
from tkinter import ttk

from mtgdb.arena.connection import (
    DUPLICATE, NAME_TAKEN, REFUSED, SENT, SOURCE_ARENA, SOURCE_BUILT_IN,
    UNAUTHORIZED, ArenaConnectionStore, fetch_deck_names, fetch_known_pictures,
    obtain_rules, send_deck,
)
from mtgdb.arena.export import ArenaExportCancelled, ArenaExportJob, build_package
from mtgdb.deck.arena_check import check_for_arena
from mtgdb.deck.arena_support import rules_from_document
from mtgdb.deck.file_jobs import submit_deck_file_job
from mtgdb.deck.io import deck_to_text
from mtgdb.printing.service import back_faces, ensure_face_png, ensure_png
from mtgdb.ui.arena_connection import ArenaLinkForm, arena_link_line
from mtgdb.ui.assets import ARENA_DECK_RULES_FILE, _asset_path
from mtgdb.ui.components import AppButton, AppEntry, ScrollZone
from mtgdb.ui.styles import scaled_pixels
from mtgdb.ui.tokens import FONT_BODY, FONT_HELPER, FONT_HELPER_BOLD, PALETTE

log = logging.getLogger("mtg")

ARENA_WINDOW_TITLE = "Proxic Arena Export"
ARENA_WINDOW_HEADING = "PROXIC ARENA EXPORT"
# Rows named one by one before the rest are counted in one more row.
ARENA_WARNING_NAME_LIMIT = 8
# Legendary cards and printing swaps named inline before the rest are counted.
ARENA_INLINE_NAME_LIMIT = 6
# Deck cards named against each token before the rest are counted.
ARENA_TOKEN_MAKER_LIMIT = 3
# How often the window looks for a changed deck, in milliseconds.
ARENA_RECHECK_MS = 700

# Row kinds, which pick the row's mark and colour.
OK, BAD, WARN, INFO = "ok", "bad", "warn", "info"

# A side deck's name, as one of its cards is called.
_SIDE_DECK_WORDS = {"CONTRAPTIONS": "Contraption", "ATTRACTIONS": "Attraction"}


def _plural(count, noun, many=None):
    return f"{count} {noun if count == 1 else (many or noun + 's')}"


def _named(names, limit=ARENA_INLINE_NAME_LIMIT):
    shown = list(names[:limit])
    if len(names) > limit:
        shown.append(f"and {len(names) - limit} more")
    return ", ".join(shown)


# -- the deck check, in words (composed apart from the widgets) ---------------

def arena_check_rows(check):
    """Return the DECK CHECK rows: ``(kind, title, detail)`` each.

    A ``bad`` row is one Arena's import would refuse, and while any shows the
    deck cannot be sent (ARN-007). Every other row is information.
    """
    rows = []
    cards = check.parts.cards
    if not cards.total("main"):
        rows.append((BAD, "No cards in the main deck yet",
                     "Add the deck's cards to send it."))
        return rows
    limits = check.limits
    rules_format = check.rules.format
    if limits is None:
        rows.append((INFO, f"{_plural(cards.total('main'), 'card')}",
                     "No deck rules could be read, so the deck can't be checked."))
    else:
        if limits.cards_short:
            rows.append((BAD, _plural(limits.total_cards, "card"),
                         f"Arena needs at least {limits.min_cards}. "
                         f"Add {limits.cards_short} more."))
        elif limits.cards_over:
            rows.append((BAD, _plural(limits.total_cards, "card"),
                         f"Arena plays decks of up to {limits.max_cards}. "
                         f"Take out {limits.cards_over}."))
        else:
            rows.append((OK, _plural(limits.total_cards, "card"),
                         f"Arena plays decks of {limits.min_cards} to "
                         f"{limits.max_cards} cards."))
        over = [
            (BAD, f"{name}: {copies} copies",
             f"Arena allows {allowed} of a card. Take out {copies - allowed}.")
            for name, copies, allowed in limits.over_copy_limit]
        rows.extend(_capped(over, "more cards over the copy limit"))
        if not over:
            rows.append((OK, "Copies are within Arena's limit",
                         f"Up to {rules_format.max_copies} of any card. "
                         "Basic lands can have any number."))
        for section, count, least in limits.short_side_decks:
            word = _SIDE_DECK_WORDS.get(section, section.title())
            rows.append((BAD, f"{word} deck: {_plural(count, 'card')}",
                         f"Arena needs at least {least} {word}s, or none."))
    unplayable = [
        (BAD, f"{name} doesn't play in Proxic Arena yet",
         "Arena doesn't have this card. Take it out to send the deck.")
        for name in check.unsupported]
    rows.extend(_capped(unplayable, "more cards Arena doesn't have"))
    if not check.unsupported and check.rules.support.names:
        rows.append((OK, "Every card plays in Proxic Arena", ""))
    if limits is not None:
        for name, copies in limits.legendary_repeats[:ARENA_INLINE_NAME_LIMIT]:
            rows.append((WARN, f"{name}: {copies} copies",
                         "Allowed, but it's legendary: you can control only one at "
                         "a time, so Arena asks you to discard the other."))
        extra = len(limits.legendary_repeats) - ARENA_INLINE_NAME_LIMIT
        if extra > 0:
            rows.append((WARN, f"{_plural(extra, 'more legendary card')} held twice", ""))
    if check.swaps:
        rows.append((INFO, f"{_plural(len(check.swaps), 'card goes', 'cards go')} "
                     "in a printing Arena has", arena_swap_detail(check.swaps)))
    if check.sideboard_count:
        rows.append((INFO, "Sideboard stays here",
                     f"Arena decks have no sideboard, so its "
                     f"{_plural(check.sideboard_count, 'card')} aren't sent."))
    if check.parts.left_out:
        rows.append((INFO, f"Left out: {_named(check.parts.left_out)}",
                     "Emblems and art cards aren't part of a deck."))
    return rows


def _capped(rows, more_noun):
    if len(rows) <= ARENA_WARNING_NAME_LIMIT:
        return rows
    shown = rows[:ARENA_WARNING_NAME_LIMIT]
    shown.append((rows[0][0], f"and {len(rows) - len(shown)} {more_noun}", ""))
    return shown


def arena_swap_line(swap):
    """One swap in plain words: ``Pongify: PLC 44 → TSR 87``."""
    before = " ".join(part for part in (swap.from_set, swap.from_number) if part)
    if not swap.to_set:
        return f"{swap.name}: {before} → by name only (Arena picks the printing)"
    after = " ".join(part for part in (swap.to_set, swap.to_number) if part)
    return f"{swap.name}: {before} → {after}"


def arena_swap_detail(swaps):
    """The printings a send changes, then why that is fine."""
    lines = [arena_swap_line(swap) for swap in swaps[:ARENA_INLINE_NAME_LIMIT]]
    if len(swaps) > ARENA_INLINE_NAME_LIMIT:
        lines.append(f"and {len(swaps) - ARENA_INLINE_NAME_LIMIT} more")
    return ("; ".join(lines) + ". Arena doesn't have the printings you chose; "
            "the cards play the same, and your saved deck keeps its own.")


def arena_token_label(token):
    """A token as a player reads it: ``Goblin (Token Creature — Goblin), from Krenko``."""
    makers = list(token.makers[:ARENA_TOKEN_MAKER_LIMIT])
    if len(token.makers) > ARENA_TOKEN_MAKER_LIMIT:
        makers.append(f"and {len(token.makers) - ARENA_TOKEN_MAKER_LIMIT} more")
    label = f"{token.name} ({token.type_line})" if token.type_line else token.name
    return f"{label}, from {', '.join(makers)}" if makers else label


ARENA_TOKEN_MISSING = "Not in your deck: Arena shows it without a picture."
ARENA_TOKEN_NOTE = (
    "Token cards go along for their pictures only. They don't count toward the "
    "deck's size, and you can send without them.")


def arena_token_rows(check):
    """Return the TOKENS rows: ``(kind, title, detail, token or None)`` each.

    A token the deck's cards make that the deck has no picture for is a
    warning and never stops a send (ARN-010); the token is returned with its
    row so the window can offer to find it.
    """
    rows = [(OK, arena_token_label(token), "", None) for token in check.held_tokens]
    rows.extend(
        (WARN, arena_token_label(token), ARENA_TOKEN_MISSING, token)
        for token in check.missing_tokens)
    made = {token.name.casefold() for token in check.held_tokens}
    for card, quantity in check.parts.tokens:
        name = str(card.get("name") or "")
        if name.split(" // ", 1)[0].strip().casefold() not in made:
            rows.append((OK, f"{quantity} {name}", "", None))
    return rows


# -- the name, the rules and the send, in words -------------------------------

def _name_key(name):
    return " ".join(str(name or "").split()).casefold()


def next_free_name(name, taken):
    """``Ape Tribal 2``, or the first number after it that is free."""
    keys = {_name_key(other) for other in taken}
    number = 2
    while _name_key(f"{name} {number}") in keys:
        number += 1
    return f"{name} {number}"


def arena_name_status(name, names, linked):
    """Return ``(kind, sentence, suggested name or "")`` for the deck's name on Arena.

    A name is taken as Arena compares names: case and surrounding spaces
    ignored. ``names`` is None when the Deck Depot could not be read.
    """
    name = str(name or "").strip()
    if not name:
        return BAD, "Give the deck a name to send it.", ""
    if names is None:
        if not linked:
            return (INFO, "Once this computer is linked, this checks whether your Deck "
                    "Depot already has a deck with this name.", "")
        return (INFO, "Your Deck Depot couldn't be checked just now. Press Check again, "
                "or send anyway: Arena checks the name too.", "")
    if _name_key(name) in {_name_key(other) for other in names}:
        return (WARN,
                f"Your Deck Depot already has a deck named {name}. Sending this one "
                f"makes a second deck, so give it a different name. Your other {name} "
                "stays as it is.",
                next_free_name(name, names))
    return (OK, f"Checked your Deck Depot: you don't have a deck named {name} yet, "
            "so this name is free.", "")


def arena_rules_note(obtained, checked_at=None):
    """Where the deck rules came from, in plain words."""
    if obtained is None:
        return "Reading Proxic Arena's deck rules…"
    if obtained.source == SOURCE_ARENA:
        when = time.strftime("%I:%M %p", time.localtime(checked_at or time.time())).lstrip("0")
        return f"Deck rules from Proxic Arena, checked {when}."
    if obtained.unlinked:
        return ("Proxic Arena no longer accepts this computer's link (it was removed "
                "on Arena), so it was unlinked here. Link it again to send.")
    if obtained.source == SOURCE_BUILT_IN:
        return "Checked with the deck rules built into this version. Linking checks against Arena right away."
    when = ""
    if obtained.saved_at:
        when = " on " + time.strftime("%b %d", time.localtime(obtained.saved_at))
    return f"Proxic Arena couldn't be reached, so this uses the deck rules saved{when}."


def arena_sent_text(result, package):
    """The sentence after a send that put the deck in the Deck Depot."""
    cards = package.image_count - package.token_pictures
    pictures = _plural(cards, "card picture")
    if package.token_pictures:
        pictures += f" and {_plural(package.token_pictures, 'token picture')}"
    text = (f"Sent {result.deck_name} to your Deck Depot: "
            f"{_plural(result.total_cards, 'card')}, {pictures}. "
            "It's ready to pick in a game room.")
    if package.held_count:
        text += (f" Arena already had {package.held_count} of the pictures, so "
                 "they weren't sent again.")
    if package.missing_art:
        text += (f" No picture could be downloaded for "
                 f"{_plural(len(package.missing_art), 'card')}; Arena draws its own.")
    return text


def arena_refused_text(result):
    """Arena's own reason for refusing a send, with its listed problems."""
    text = result.detail or "Proxic Arena refused the deck."
    if result.problems:
        text += "\n" + "\n".join(f"• {problem}" for problem in result.problems[:ARENA_WARNING_NAME_LIMIT])
        if len(result.problems) > ARENA_WARNING_NAME_LIMIT:
            text += f"\n• and {len(result.problems) - ARENA_WARNING_NAME_LIMIT} more"
    return text


def arena_footer_hint(check, linked, name_kind, busy):
    """Why Send to Arena is off, or "" when it is on."""
    if busy:
        return ""
    if not linked:
        return "Link this computer to send."
    if check is None:
        return "Checking the deck…"
    if check.blocked:
        return "Fix the red items in your deck to send it. This window checks again as you edit."
    if name_kind in (BAD, WARN):
        return "Change the name to send."
    return ""


# -- the window -----------------------------------------------------------------

def _mark(parent, kind, background):
    """A small drawn mark: a tick, a cross, or a ringed ! or i, in the kind's colour."""
    p = PALETTE
    colour = {OK: p["good"], BAD: p["bad"], WARN: p["search_hover_glow"], INFO: p["muted"]}[kind]
    # Drawn on a 16-unit grid and grown with display scaling, as the text is.
    size = scaled_pixels(parent, 16)
    unit = size / 16
    canvas = tk.Canvas(parent, width=size, height=size, bg=background,
                       highlightthickness=0, bd=0, takefocus=0)

    def at(*points):
        return [value * unit for value in points]

    if kind == OK:
        canvas.create_line(*at(3, 9, 7, 13, 14, 4), fill=colour, width=2.4 * unit)
    elif kind == BAD:
        canvas.create_line(*at(4, 4, 12, 12), fill=colour, width=2.4 * unit)
        canvas.create_line(*at(12, 4, 4, 12), fill=colour, width=2.4 * unit)
    else:
        canvas.create_oval(*at(1, 1, 15, 15), outline=colour, width=1.6 * unit)
        canvas.create_text(*at(8, 8), text="!" if kind == WARN else "i",
                           fill=colour, font=FONT_HELPER_BOLD)
    return canvas


class ArenaExportWindow:
    """One Proxic Arena Export window, following the deck the player is working on."""

    WRAP = 600

    def __init__(self, app):
        self.app = app
        self.store = ArenaConnectionStore(app.data_dir)
        self.link = self.store.link()
        self.obtained = None
        self.rules = None
        self.rules_checked_at = None
        self.check = None
        self.names = None
        self.signature = None
        self.checking = False
        self.recheck_wanted = False
        self.phase = "ready"            # ready | sending | sent
        self.banner = None              # (kind, text) above everything
        self.cancel_event = None
        self.progress = None
        self.name_edited = False
        self.refreshing = False
        self.after_handles = {}
        self._build()

    # -- frame ----------------------------------------------------------------
    def _build(self):
        p = PALETTE
        app = self.app
        popup = app._create_hidden_popup(ARENA_WINDOW_TITLE, transient=app)
        popup.configure(bg=p["border"])
        popup.protocol("WM_DELETE_WINDOW", self.close)
        popup.bind("<Escape>", lambda _e: self.close())
        self.popup = popup
        shell = tk.Frame(popup, bg=p["surface"], padx=18, pady=14)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        self.shell = shell

        head = tk.Frame(shell, bg=p["surface"])
        head.pack(fill="x", pady=(0, 8))
        ttk.Label(head, text=ARENA_WINDOW_HEADING, style="DialogTitle.TLabel").pack(side="left")

        self.footer = tk.Frame(shell, bg=p["surface"])
        self.footer.pack(side="bottom", fill="x", pady=(10, 0))
        zone = ScrollZone(shell, scroll_step=scaled_pixels(shell, 48))
        zone.pack(fill="both", expand=True)
        app._register_scrollable(zone.canvas, target=zone.wheel_target())
        self.body = tk.Frame(zone.inner, bg=p["surface"])
        self.body.pack(fill="both", expand=True)

        name_frame = tk.Frame(self.body, bg=p["surface"])
        self.name_var = tk.StringVar(master=popup)
        self.name_entry = AppEntry(name_frame, textvariable=self.name_var)
        self.name_entry.bind("<KeyRelease>", lambda _e: self._name_typed(), add="+")
        self.name_frame = name_frame

        self._follow_deck(initial=True)
        self._render()
        app._present_hidden_popup(
            popup, preferred_width=720, preferred_height=780, min_width=560,
            min_height=520, grab=False, fit_content=False)
        self._refresh_rules()
        self._schedule("watch", ARENA_RECHECK_MS, self._watch)

    def lift(self):
        try:
            self.popup.deiconify()
            self.popup.lift()
            self.popup.focus_force()
        except tk.TclError:
            pass

    def alive(self):
        try:
            return bool(self.popup.winfo_exists())
        except tk.TclError:
            return False

    def close(self):
        if self.cancel_event is not None:
            self.cancel_event.set()
        for handle in self.after_handles.values():
            try:
                self.app.after_cancel(handle)
            except (tk.TclError, ValueError):
                pass
        self.after_handles.clear()
        try:
            self.popup.destroy()
        except tk.TclError:
            pass
        if getattr(self.app, "_arena_export_window", None) is self:
            self.app._arena_export_window = None

    def _schedule(self, key, delay, callback):
        try:
            self.after_handles[key] = self.app.after(delay, callback)
        except tk.TclError:
            pass

    # -- the deck, followed ------------------------------------------------------
    def _deck(self):
        sessions = self.app.deck_sessions
        index = sessions.active_index
        if not sessions.is_valid_index(index):
            return None
        return sessions[index].deck

    def _deck_signature(self, deck):
        # The deck counts its own changes, so this costs nothing per tick.
        if deck is None:
            return None
        return (id(deck), deck.generation, deck.name)

    def _follow_deck(self, initial=False):
        """Notice a changed deck; return True when a check should run."""
        deck = self._deck()
        signature = self._deck_signature(deck)
        if signature == self.signature and not initial:
            return False
        previous = self.signature
        self.signature = signature
        name = deck.name if deck is not None else ""
        switched = previous is None or previous[0] != (signature or (None,))[0]
        if initial or switched or not self.name_edited:
            self.name_var.set(name)
            if switched:
                self.name_edited = False
        if switched and self.phase == "sent":
            self.phase, self.banner = "ready", None
        return True

    def _watch(self):
        if not self.alive():
            return
        try:
            self.app._sync_deck_meta()
        except (tk.TclError, AttributeError):
            pass
        if self._follow_deck() and self.phase != "sending":
            if self.phase == "sent":
                self.phase, self.banner = "ready", None
            self._recheck()
        self._schedule("watch", ARENA_RECHECK_MS, self._watch)

    # -- work on the worker -------------------------------------------------------
    def _run(self, key, job, done, *args):
        future = submit_deck_file_job(job, *args, name=f"mtg-arena-{key}")
        self._await(key, future, done)

    def _await(self, key, future, done):
        if not self.alive():
            return
        if not future.done():
            self._schedule(key, 40, lambda: self._await(key, future, done))
            return
        try:
            outcome = future.result()
        except Exception as exc:  # reported in the window, never a crash dialog
            log.exception("Proxic Arena export: %s failed", key)
            done(exc)
            return
        done(outcome)

    def _check_again(self):
        """Ask Arena again: its deck rules, and the names in the Deck Depot."""
        if self.refreshing or self.phase == "sending":
            return
        self._refresh_rules()
        self._render_name_status()
        self._render_footer()

    def _refresh_rules(self):
        self.refreshing = True
        store = self.store
        built_in = _asset_path(ARENA_DECK_RULES_FILE)

        def job():
            obtained = obtain_rules(
                store, built_in, lambda payload: rules_from_document(payload).known)
            link = store.link()
            names = None
            unlinked = obtained.unlinked
            if link is not None:
                answer = fetch_deck_names(link.address, store.token())
                if answer.status == "ok":
                    names = answer.names
                elif answer.status == "unauthorized":
                    store.forget()
                    unlinked, link = True, None
            return obtained, rules_from_document(obtained.payload), link, names, unlinked

        def done(outcome):
            self.refreshing = False
            if isinstance(outcome, Exception):
                self.banner = (BAD, "Proxic Arena's deck rules couldn't be read. Press "
                               "Check again, or close this window and try again.")
                self._render()
                return
            self.obtained, self.rules, self.link, self.names, unlinked = outcome
            self.rules_checked_at = time.time()
            if unlinked:
                log.info("Proxic Arena refused this computer's link; unlinked")
            self._recheck()

        self._run("rules", job, done)

    def _recheck(self):
        if self.rules is None:
            return
        if self.checking:
            self.recheck_wanted = True
            return
        deck = self._deck()
        if deck is None:
            self.check = None
            self._render()
            return
        self.checking = True
        self.recheck_wanted = False
        detached = self.app._detached_deck_copy(deck)
        rules = self.rules
        printings_named = self.app.db.printings_named

        def done(outcome):
            self.checking = False
            self.check = None if isinstance(outcome, Exception) else outcome
            self._render()
            if self.recheck_wanted:
                self._recheck()

        self._run("check", check_for_arena, done, detached, rules, printings_named)

    # -- sending ------------------------------------------------------------------
    def _send(self):
        check, link = self.check, self.link
        name = self.name_var.get().strip()
        if check is None or link is None or check.blocked or not name:
            return
        self.phase, self.banner = "sending", None
        self.cancel_event = threading.Event()
        self.progress = queue.Queue()
        store = self.store
        cancel_event, progress = self.cancel_event, self.progress
        cache_dir = os.path.join(self.app.image_cache_dir, "print_png")
        export_deck = check.export_deck
        tokens = check.parts.tokens

        def job():
            def report(stage, current, total, detail):
                progress.put((stage, current, total))
            text = deck_to_text(export_deck, include_side=False, tokens=tokens)
            package_job = ArenaExportJob.from_deck(
                export_deck, text, cache_dir, token_cards=[card for card, _qty in tokens])
            token = store.token()
            package = build_package(
                package_job, ensure_png, progress_cb=report, cancel_event=cancel_event,
                back_faces_of=back_faces, ensure_face=ensure_face_png,
                known_pictures=lambda hashes: fetch_known_pictures(link.address, token, hashes))
            progress.put(("send", 1, 1))
            # The name typed here names the Arena copy only; the deck here keeps its own.
            result = send_deck(link.address, token, package.data, package.filename, name=name)
            return package, result

        self._render()
        self._drain_progress()
        self._run("send", job, self._sent)

    def _drain_progress(self):
        if self.phase != "sending" or not self.alive():
            return
        latest = None
        try:
            while True:
                latest = self.progress.get_nowait()
        except (queue.Empty, AttributeError):
            pass
        if latest is not None:
            stage, current, total = latest
            self._show_progress(stage, current, total)
        self._schedule("progress", 120, self._drain_progress)

    def _sent(self, outcome):
        self.phase = "ready"
        self.cancel_event = None
        if isinstance(outcome, ArenaExportCancelled):
            self.banner = (INFO, "Sending was cancelled. Nothing reached your Deck Depot.")
            self._render()
            return
        if isinstance(outcome, Exception):
            self.banner = (BAD, "The deck couldn't be sent. Try again; the log has the details.")
            self._render()
            return
        package, result = outcome
        log.info("Proxic Arena send: %s (%s), %d pictures, %d held, %d token pictures",
                 result.status, result.deck_name, package.image_count,
                 package.held_count, package.token_pictures)
        if result.status == SENT:
            self.phase = "sent"
            self.banner = (OK, arena_sent_text(result, package))
            self.names = tuple(self.names or ()) + (result.deck_name,)
            self.app._status(f"Sent {result.deck_name} to Proxic Arena")
        elif result.status == DUPLICATE:
            self.phase = "sent"
            self.banner = (INFO, f"Your Deck Depot already has this exact deck"
                           f"{', as ' + result.deck_name if result.deck_name else ''}. "
                           "Nothing new was sent.")
        elif result.status == NAME_TAKEN:
            taken = result.deck_name or self.name_var.get().strip()
            self.names = tuple(self.names or ()) + (taken,)
        elif result.status == REFUSED:
            self.banner = (BAD, arena_refused_text(result))
        elif result.status == UNAUTHORIZED:
            self.store.forget()
            self.link, self.names = None, None
            self.banner = (BAD, "Proxic Arena no longer accepts this computer's link (it "
                           "was removed on Arena), so it was unlinked here. Link it again "
                           "to send.")
        else:
            self.banner = (BAD, "Proxic Arena couldn't be reached. Check your connection "
                           "and try again.")
        self._render()

    def _cancel(self):
        if self.cancel_event is not None:
            self.cancel_event.set()

    # -- the player's actions -----------------------------------------------------
    def _name_typed(self):
        deck = self._deck()
        self.name_edited = deck is None or self.name_var.get() != deck.name
        self._render_name_status()
        self._render_footer()

    def _use_name(self, name):
        self.name_var.set(name)
        self.name_edited = True
        self._render_name_status()
        self._render_footer()

    def _linked(self, link):
        self.link = link
        self.banner = None
        self._refresh_rules()
        self._render()

    def _unlink(self):
        self.store.forget()
        log.info("Unlinked this computer from Proxic Arena")
        self.link, self.names = None, None
        self.banner = (INFO, "This computer is unlinked. To stop the link on Arena too, "
                       "remove this computer on your Account page.")
        self._render()

    def _open_depot(self):
        if self.link is not None:
            webbrowser.open(urllib.parse.urljoin(self.link.address + "/", "decks"), new=2)

    def _find_tokens(self, tokens):
        self.app._search_for_token_names([token.name for token in tokens])

    # -- drawing ----------------------------------------------------------------------
    def _clear(self, frame):
        for child in frame.winfo_children():
            if child is not self.name_frame:
                child.destroy()

    def _heading(self, parent, text, right=None):
        p = PALETTE
        row = tk.Frame(parent, bg=p["surface"])
        row.pack(fill="x", pady=(12, 5))
        tk.Label(row, text=text, bg=p["surface"], fg=p["accent"],
                 font=FONT_HELPER_BOLD).pack(side="left")
        if right:
            tk.Label(row, text=right[1], bg=p["surface"], fg=right[0],
                     font=FONT_HELPER).pack(side="right")
        return row

    def _text(self, parent, text, fg=None, font=FONT_HELPER, **pack):
        p = PALETTE
        label = tk.Label(parent, text=text, bg=parent.cget("bg"), fg=fg or p["muted"],
                         font=font, justify="left", anchor="w", wraplength=self.WRAP)
        label.pack(anchor="w", fill="x", **pack)
        return label

    def _rows(self, parent, rows):
        """A bordered dark list of ``(kind, title, detail, action)`` rows."""
        p = PALETTE
        box = tk.Frame(parent, bg=p["border"])
        box.pack(fill="x")
        for kind, title, detail, action in rows:
            # One pixel of the border colour between rows draws the dividers.
            row = tk.Frame(box, bg=p["input"], padx=10, pady=6)
            row.pack(fill="x", pady=(1, 0), padx=1)
            _mark(row, kind, p["input"]).pack(side="left", anchor="n", pady=(2, 0))
            words = tk.Frame(row, bg=p["input"])
            words.pack(side="left", fill="x", expand=True, padx=(9, 0))
            tk.Label(words, text=title, bg=p["input"], fg=p["text"], font=FONT_BODY,
                     justify="left", anchor="w", wraplength=self.WRAP - 120).pack(anchor="w", fill="x")
            if detail:
                tk.Label(words, text=detail, bg=p["input"], fg=p["muted"], font=FONT_HELPER,
                         justify="left", anchor="w", wraplength=self.WRAP - 120).pack(anchor="w", fill="x")
            if action is not None:
                label, command = action
                AppButton(row, text=label, role="compact", command=command).pack(
                    side="right", anchor="center", padx=(8, 0))
        tk.Frame(box, bg=p["border"], height=1).pack(fill="x")
        return box

    def _banner(self, parent, kind, text):
        p = PALETTE
        colours = {OK: ("#1B2A20", "#2F5A3A"), BAD: ("#2E1C1C", "#6A3434"),
                   WARN: ("#2B2618", "#6B5527"), INFO: (p["surface2"], p["border"])}
        fill, edge = colours[kind]
        frame = tk.Frame(parent, bg=edge)
        frame.pack(fill="x", pady=(0, 6))
        inner = tk.Frame(frame, bg=fill, padx=10, pady=8)
        inner.pack(fill="x", padx=1, pady=1)
        _mark(inner, kind, fill).pack(side="left", anchor="n", pady=(2, 0))
        tk.Label(inner, text=text, bg=fill, fg=p["text"], font=FONT_BODY, justify="left",
                 anchor="w", wraplength=self.WRAP - 60).pack(side="left", fill="x",
                                                              expand=True, padx=(9, 0))

    def _render(self):
        if not self.alive():
            return
        body = self.body
        self._clear(body)
        self.name_frame.pack_forget()
        if self.banner:
            self._banner(body, *self.banner)
        self._render_link(body)
        self._render_name(body)
        self._render_check(body)
        self._render_tokens(body)
        self._render_footer()

    def _render_link(self, body):
        p = PALETTE
        if self.link is not None:
            strip = tk.Frame(body, bg=p["border"])
            strip.pack(fill="x", pady=(0, 2))
            inner = tk.Frame(strip, bg=p["surface2"], padx=10, pady=7)
            inner.pack(fill="x", padx=1, pady=1)
            dot = tk.Canvas(inner, width=10, height=10, bg=p["surface2"], highlightthickness=0)
            dot.create_oval(1, 1, 9, 9, fill=p["good"], outline=p["good"])
            dot.pack(side="left")
            tk.Label(inner, text=arena_link_line(self.link), bg=p["surface2"], fg=p["text"],
                     font=FONT_HELPER, anchor="w").pack(side="left", padx=(8, 0))
            AppButton(inner, text="Unlink", role="compact", command=self._unlink).pack(side="right")
            return
        ArenaLinkForm(self.app, body, self.store, self._linked)

    def _render_name(self, body):
        self._heading(body, "DECK NAME ON ARENA")
        self.name_frame.pack(in_=body, fill="x")
        self._render_name_status()

    def _render_name_status(self):
        p = PALETTE
        for child in self.name_frame.winfo_children():
            if child is not self.name_entry:
                child.destroy()
        kind, sentence, suggestion = arena_name_status(
            self.name_var.get(), self.names, self.link is not None)
        line = tk.Frame(self.name_frame, bg=p["surface"])
        line.pack(fill="x")
        self.name_entry.pack(in_=line, side="left", fill="x", expand=True)
        # The row was made after the entry, so it would be drawn over it.
        self.name_entry.lift(line)
        if suggestion:
            AppButton(line, text=f"Use “{suggestion}”", role="compact",
                      command=lambda: self._use_name(suggestion)).pack(side="left", padx=(8, 0))
        if self.link is not None:
            again = AppButton(line, text="Check again", role="compact", command=self._check_again)
            again.pack(side="left", padx=(8, 0))
            if self.refreshing:
                again.state(["disabled"])
                kind, sentence = INFO, "Checking your Deck Depot…"
        status = tk.Frame(self.name_frame, bg=p["surface"])
        status.pack(fill="x", pady=(5, 0))
        _mark(status, kind, p["surface"]).pack(side="left", anchor="n", pady=(1, 0))
        tk.Label(status, text=sentence, bg=p["surface"],
                 fg=p["text"] if kind in (BAD, WARN) else p["muted"], font=FONT_HELPER,
                 justify="left", anchor="w", wraplength=self.WRAP - 30).pack(
            side="left", fill="x", expand=True, padx=(8, 0))
        self.name_kind = kind

    def _render_check(self, body):
        p = PALETTE
        check = self.check
        rows = arena_check_rows(check) if check is not None else []
        problems = sum(1 for row in rows if row[0] == BAD)
        right = (p["bad"], f"{_plural(problems, 'thing')} to fix") if problems else None
        self._heading(body, "DECK CHECK", right)
        if check is None:
            self._text(body, "Checking the deck against Proxic Arena's deck rules…")
            return
        self._rows(body, [(kind, title, detail, None) for kind, title, detail in rows])

    def _render_tokens(self, body):
        check = self.check
        if check is None:
            return
        rows = arena_token_rows(check)
        if not rows:
            return
        self._heading(body, "TOKENS YOUR CARDS MAKE")
        self._rows(body, [
            (kind, title, detail,
             ("Find token", lambda t=token: self._find_tokens([t])) if token else None)
            for kind, title, detail, token in rows])
        self._text(body, ARENA_TOKEN_NOTE, pady=(6, 0))

    def _render_footer(self):
        p = PALETTE
        footer = self.footer
        for child in footer.winfo_children():
            child.destroy()
        tk.Frame(footer, bg=p["border"], height=1).pack(fill="x", pady=(0, 10))
        if self.phase == "sending":
            self.progress_label = tk.Label(
                footer, text="Getting the deck's pictures ready…", bg=p["surface"],
                fg=p["text"], font=FONT_BODY, anchor="w")
            self.progress_label.pack(fill="x")
            self.progress_bar = ttk.Progressbar(
                footer, orient="horizontal", mode="determinate", maximum=1,
                style="Gold.Horizontal.TProgressbar")
            self.progress_bar.pack(fill="x", pady=(6, 8))
            self.cancel_button = AppButton(footer, text="Cancel", role="compact",
                                           command=self._cancel)
            self.cancel_button.pack(side="right")
            return
        row = tk.Frame(footer, bg=p["surface"])
        row.pack(fill="x")
        AppButton(row, text="Close", role="compact", command=self.close).pack(side="right")
        hint = ""
        if self.phase == "sent":
            AppButton(row, text="Open Deck Depot", role="compact_primary",
                      command=self._open_depot).pack(side="right", padx=(0, 8))
        else:
            hint = arena_footer_hint(
                self.check, self.link is not None, getattr(self, "name_kind", OK), False)
            if self.refreshing and not hint:
                # Send waits for Check again, so it never goes on an old answer.
                hint = "Checking your Deck Depot…"
            send = AppButton(row, text="Send to Arena", role="compact_primary",
                             command=self._send)
            send.pack(side="right", padx=(0, 8))
            if hint:
                send.state(["disabled"])
        words = tk.Frame(row, bg=p["surface"])
        words.pack(side="left", fill="x", expand=True)
        if hint:
            blocked = self.check is not None and self.check.blocked
            tk.Label(words, text=hint, bg=p["surface"], fg=p["bad"] if blocked else p["text"],
                     font=FONT_HELPER, justify="left", anchor="w",
                     wraplength=self.WRAP - 260).pack(anchor="w", fill="x")
        tk.Label(words, text=arena_rules_note(self.obtained, self.rules_checked_at),
                 bg=p["surface"], fg=p["muted"], font=FONT_HELPER, justify="left",
                 anchor="w", wraplength=self.WRAP - 260).pack(anchor="w", fill="x")

    def _show_progress(self, stage, current, total):
        try:
            if stage == "download":
                text = f"Getting the deck's pictures ready ({current} of {total})…"
            elif stage == "package":
                text = "Packing the deck…"
            else:
                text = "Sending the deck to your Deck Depot…"
                self.cancel_button.state(["disabled"])
            self.progress_label.configure(text=text)
            self.progress_bar.configure(maximum=max(total, 1), value=current)
        except (tk.TclError, AttributeError):
            pass


class ArenaExportMixin:
    """Own the Proxic Arena Export button's window."""

    def _export_arena(self):
        """Open the Proxic Arena Export window, or bring it forward.

        It returns nothing: everything the window does runs on a worker, so a
        value returned here could only mean "opened", which DUI-020 forbids a
        caller from mistaking for "done".
        """
        window = getattr(self, "_arena_export_window", None)
        if window is not None and window.alive():
            window.lift()
            return
        self._sync_deck_meta()
        self._arena_export_window = ArenaExportWindow(self)
