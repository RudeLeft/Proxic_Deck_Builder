"""This computer's link to a Proxic Arena server: the deck rules it downloads and the decks it sends.

Proxic Arena publishes its deck rules (the cards and printings it plays, and
Open Format's limits) to signed-in players. The Deck Builder is not a browser
and never holds the player's password, so a player links this computer once:
Arena's Account page shows a one-time code, the player types it in the Proxic
Arena Export window, and Arena answers with a device token (Arena spec 0021).
The token downloads the rules, reads the player's deck names, asks which
pictures their decks already hold, and sends a deck to their Deck Depot (spec
0022) -- nothing else. It is kept sealed to this Windows user
(``core/secret_store.py``).

Before each Proxic Arena export the rules are asked for again. Arena answers
"unchanged" for the copy already saved, so this usually costs one small
request; a new copy replaces the saved one only once it reads as a rules
document. When Arena cannot be reached, the saved copy is used, and with none
saved, the copy built into the program -- so an export never waits on, or fails
for, a server.

Tk-free and deck-free: the caller hands in how to read a document
(``accept``), so this module moves bytes and the deck layer judges them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import gzip
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from mtgdb.core.secret_store import (
    delete_secret, read_secret, write_secret,
)
from mtgdb.core.atomic_files import TEMP_SUFFIX, temp_prefix
from mtgdb.core.version import app_version

DEFAULT_ADDRESS = "https://proxic.barnflix.net"
LINK_FILE = "arena_link.json"
TOKEN_FILE = "arena_link.token"
RULES_FILE = "arena_rules.json"
RULES_META_FILE = "arena_rules.meta.json"
# The first request after an Arena update builds its card list, which takes
# several seconds; later ones are answered at once.
REQUEST_TIMEOUT = 60
# A send carries every picture of a deck and Arena checks every card before it
# answers; on a slow connection that is minutes.
SEND_TIMEOUT = 600
# Arena answers about at most this many pictures at once: a package's limit.
KNOWN_PICTURES_LIMIT = 200
MAX_RULES_BYTES = 64 * 1024 * 1024

# Where the rules came from, for the export report.
SOURCE_ARENA = "arena"
SOURCE_SAVED = "saved"
SOURCE_BUILT_IN = "built-in"


class ArenaConnectionError(Exception):
    """A plain-words reason linking failed, fit to show the player as it is."""


@dataclass(frozen=True)
class ArenaLink:
    """Who this computer is linked as, and to which Arena."""

    address: str
    username: str
    display_name: str
    device_name: str
    linked_at: float


@dataclass(frozen=True)
class RulesFetch:
    """One request for the rules: ``fresh`` (with ``body``), ``unchanged``,
    ``unauthorized`` (the link was removed), or ``unavailable``."""

    status: str
    body: bytes = b""
    etag: str = ""


@dataclass(frozen=True)
class ObtainedRules:
    """The rules an export will use, and where they came from.

    ``payload`` is the parsed document, or None when no copy at all could be
    read. ``source`` is one of the ``SOURCE_*`` names; ``saved_at`` is when
    the saved copy was downloaded; ``unlinked`` says Arena refused this
    computer's token, so the link was forgotten.
    """

    payload: object
    source: str
    saved_at: float | None = None
    unlinked: bool = False


_LOOPBACK_NAMES = {"localhost"}


def _is_loopback(host):
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_address(text):
    """Return ``scheme://host[:port]`` for an Arena address, or raise ``ValueError``.

    A bare host is taken as https. Plain http is allowed only to this computer
    itself (a local Arena under development): anywhere else it would send the
    device token unencrypted.
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("Enter the Proxic Arena address.")
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urllib.parse.urlsplit(raw)
    host = (parts.hostname or "").casefold()
    if not host:
        raise ValueError("That is not a web address.")
    if parts.scheme == "http" and not _is_loopback(host):
        raise ValueError("Use an https address; http is only for an Arena on this computer.")
    if parts.scheme not in ("https", "http"):
        raise ValueError("Use an https address.")
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return f"{parts.scheme}://{netloc}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the token must not be sent anywhere it was not addressed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    return urllib.request.build_opener(_NoRedirect())


def _request(url, method="GET", body=None, headers=None):
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("User-Agent", f"ProxicDeckBuilder/{app_version()}")
    request.add_header("Accept", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    return request


def _read_body(response):
    raw = response.read(MAX_RULES_BYTES + 1)
    if len(raw) > MAX_RULES_BYTES:
        raise ValueError("response too large")
    if (response.headers.get("Content-Encoding") or "").strip().casefold() == "gzip":
        raw = gzip.decompress(raw)
    return raw


def _detail(error):
    try:
        return str(json.loads(error.read().decode("utf-8")).get("detail") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def link_computer(address, code, device_name, opener=None):
    """Trade a one-time code for a device token. Returns ``(ArenaLink, token)``.

    Raises ``ArenaConnectionError`` with a sentence fit to show the player.
    """
    base = normalize_address(address)
    body = json.dumps({"code": str(code or ""), "deviceName": str(device_name or "")})
    request = _request(
        f"{base}/api/auth/device-link", method="POST", body=body.encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with (opener or _opener()).open(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(_read_body(response).decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 410:
            raise ArenaConnectionError(
                "That code is not valid. Codes work once, for 10 minutes: make a new "
                "one on Arena's Account page.") from None
        if error.code == 429:
            raise ArenaConnectionError(
                _detail(error) or "Too many tries. Wait a few minutes and try again.") from None
        raise ArenaConnectionError(
            f"Proxic Arena refused the link ({error.code}). Check the address.") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise ArenaConnectionError(
            f"Proxic Arena could not be reached at {base}. Check the address and "
            "your connection.") from None
    try:
        token = str(payload["token"])
        user = payload["user"]
        link = ArenaLink(
            address=base, username=str(user["username"]),
            display_name=str(user.get("displayName") or user["username"]),
            device_name=str(device_name or "").strip() or "Deck Builder",
            linked_at=time.time())
    except (KeyError, TypeError):
        raise ArenaConnectionError("Proxic Arena sent an answer this version cannot read.") from None
    if not token:
        raise ArenaConnectionError("Proxic Arena sent an answer this version cannot read.")
    return link, token


def fetch_rules(address, token, etag="", opener=None):
    """Ask Arena for its deck rules, offering the saved copy's ``etag``."""
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}
    if etag:
        headers["If-None-Match"] = etag
    try:
        request = _request(f"{normalize_address(address)}/api/deck-rules", headers=headers)
        with (opener or _opener()).open(request, timeout=REQUEST_TIMEOUT) as response:
            return RulesFetch(
                "fresh", _read_body(response), response.headers.get("ETag") or "")
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return RulesFetch("unchanged")
        if error.code == 401:
            return RulesFetch("unauthorized")
        return RulesFetch("unavailable")
    except (urllib.error.URLError, OSError, ValueError):
        return RulesFetch("unavailable")


@dataclass(frozen=True)
class DeckNamesFetch:
    """Your Deck Depot's deck names: ``ok`` (with ``names``), ``unauthorized``, or ``unavailable``."""

    status: str
    names: tuple = ()


def fetch_deck_names(address, token, opener=None):
    """Ask Arena for the names of the decks in this player's Deck Depot (spec 0022)."""
    try:
        request = _request(
            f"{normalize_address(address)}/api/deck-builder/deck-names",
            headers={"Authorization": f"Bearer {token}"})
        with (opener or _opener()).open(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(_read_body(response).decode("utf-8"))
        names = tuple(str(name) for name in payload["names"])
    except urllib.error.HTTPError as error:
        return DeckNamesFetch("unauthorized" if error.code == 401 else "unavailable")
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return DeckNamesFetch("unavailable")
    return DeckNamesFetch("ok", names)


def fetch_known_pictures(address, token, hashes, opener=None):
    """Of ``hashes``, the pictures this player's decks on Arena already hold.

    Any failure answers "none": every picture is then sent, which is only
    slower.
    """
    wanted = sorted(set(hashes))[:KNOWN_PICTURES_LIMIT]
    if not wanted:
        return frozenset()
    body = json.dumps({"sha256": wanted}).encode("utf-8")
    try:
        request = _request(
            f"{normalize_address(address)}/api/deck-builder/pictures/known",
            method="POST", body=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with (opener or _opener()).open(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(_read_body(response).decode("utf-8"))
        return frozenset(str(sha) for sha in payload["known"]) & frozenset(wanted)
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return frozenset()


# Statuses of one send.
SENT = "sent"
DUPLICATE = "duplicate"
NAME_TAKEN = "name_taken"
REFUSED = "refused"
UNAUTHORIZED = "unauthorized"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SendResult:
    """What Arena answered one send.

    ``sent``: the deck is in the Deck Depot as ``deck_name``. ``duplicate``:
    this exact package is already there, as ``deck_name``. ``name_taken``: a
    deck of yours already has the name. ``refused``: Arena's import said why,
    in ``detail`` and ``problems``. ``unauthorized``: the link was removed.
    ``unavailable``: Arena could not be reached.
    """

    status: str
    deck_name: str = ""
    detail: str = ""
    problems: tuple = ()
    deck_id: str = ""
    total_cards: int = 0
    token_pictures: int = 0


def _multipart(fields, file_field, filename, data):
    """Encode ``fields`` and one file as ``multipart/form-data``: ``(body, content type)``."""
    boundary = f"----ProxicDeckBuilder{os.urandom(12).hex()}"
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))
    safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{safe}"\r\nContent-Type: application/zip\r\n\r\n'.encode("utf-8"))
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send_deck(address, token, data, filename, name=None, opener=None):
    """Send one deck package to this player's Deck Depot (Arena spec 0022)."""
    fields = {"name": name} if name else {}
    body, content_type = _multipart(fields, "package", filename, data)
    try:
        request = _request(
            f"{normalize_address(address)}/api/deck-builder/decks", method="POST",
            body=body, headers={
                "Authorization": f"Bearer {token}", "Content-Type": content_type})
        with (opener or _opener()).open(request, timeout=SEND_TIMEOUT) as response:
            payload = json.loads(_read_body(response).decode("utf-8"))
        return SendResult(
            SENT, deck_name=str(payload.get("displayName") or ""),
            deck_id=str(payload.get("deckId") or ""),
            total_cards=int(payload.get("totalCards") or 0),
            token_pictures=int(payload.get("tokenPictures") or 0))
    except urllib.error.HTTPError as error:
        if error.code == 401:
            return SendResult(UNAUTHORIZED)
        refusal = _refusal(error)
        code = str(refusal.get("errorCode") or "")
        detail = str(refusal.get("detail") or "")
        problems = tuple(str(problem) for problem in refusal.get("problems") or ())
        if code == "DUPLICATE_PACKAGE":
            return SendResult(DUPLICATE, deck_name=_quoted(detail), detail=detail)
        if code == "DECK_NAME_TAKEN":
            return SendResult(
                NAME_TAKEN, deck_name=problems[0] if problems else "", detail=detail)
        if error.code in (400, 409, 413, 422):
            return SendResult(REFUSED, detail=detail, problems=problems)
        return SendResult(UNAVAILABLE, detail=detail)
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        return SendResult(UNAVAILABLE)


def _refusal(error):
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (OSError, ValueError, AttributeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _quoted(detail):
    """The name inside the first pair of straight quotes, or ""."""
    parts = detail.split('"')
    return parts[1] if len(parts) >= 3 else ""


class ArenaConnectionStore:
    """The link, its sealed token and the saved rules, in the data folder."""

    def __init__(self, data_dir):
        self.folder = Path(data_dir)

    def _path(self, name):
        return self.folder / name

    def link(self):
        """The link, or None when this computer is not linked (or its token is unreadable)."""
        try:
            raw = json.loads(self._path(LINK_FILE).read_text(encoding="utf-8"))
            link = ArenaLink(**{key: raw[key] for key in ArenaLink.__dataclass_fields__})
        except (OSError, ValueError, TypeError, KeyError):
            return None
        return link if self.token() else None

    def token(self):
        return read_secret(self._path(TOKEN_FILE))

    def save_link(self, link, token):
        write_secret(self._path(TOKEN_FILE), token)
        self._write(LINK_FILE, json.dumps(asdict(link), indent=2).encode("utf-8"))

    def forget(self):
        """Unlink this computer. The saved rules stay: they are still the last known."""
        delete_secret(self._path(TOKEN_FILE))
        self._path(LINK_FILE).unlink(missing_ok=True)

    def saved_rules(self):
        """``(payload, etag, saved_at)`` of the saved rules, or ``(None, "", None)``."""
        try:
            payload = json.loads(self._path(RULES_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, "", None
        try:
            meta = json.loads(self._path(RULES_META_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        meta = meta if isinstance(meta, dict) else {}
        return payload, str(meta.get("etag") or ""), meta.get("savedAt")

    def save_rules(self, body, etag):
        self._write(RULES_FILE, body)
        self._write(
            RULES_META_FILE,
            json.dumps({"etag": etag, "savedAt": time.time()}).encode("utf-8"))

    def touch_rules(self):
        """Note that Arena confirmed the saved copy is current."""
        _payload, etag, _saved = self.saved_rules()
        self._write(
            RULES_META_FILE,
            json.dumps({"etag": etag, "savedAt": time.time()}).encode("utf-8"))

    def _write(self, name, data):
        """Write ``data`` beside its destination and move it over, so a crash keeps the old copy."""
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self._path(name)
        descriptor, temporary = tempfile.mkstemp(
            prefix=temp_prefix(target.name), suffix=TEMP_SUFFIX, dir=self.folder)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)


def obtain_rules(store, built_in_path, accept, fetch=fetch_rules):
    """Return the deck rules an export should use, as an ``ObtainedRules``.

    ``accept(payload)`` says whether a parsed document reads as rules. Linked:
    ask Arena, offering the saved copy; keep a fresh copy only once it is
    accepted. Arena unreachable, or not linked: the saved copy. None saved: the
    copy built into the program. Arena refusing the token means the player
    removed this computer, so the link is forgotten and the saved copy used.
    """
    saved, etag, saved_at = store.saved_rules()
    saved = saved if saved is not None and accept(saved) else None
    link = store.link()
    if link is not None:
        answer = fetch(link.address, store.token(), etag if saved is not None else "")
        if answer.status == "fresh":
            try:
                payload = json.loads(answer.body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                payload = None
            if payload is not None and accept(payload):
                store.save_rules(answer.body, answer.etag)
                return ObtainedRules(payload, SOURCE_ARENA, time.time())
        elif answer.status == "unchanged" and saved is not None:
            store.touch_rules()
            return ObtainedRules(saved, SOURCE_ARENA, time.time())
        elif answer.status == "unauthorized":
            store.forget()
            if saved is not None:
                return ObtainedRules(saved, SOURCE_SAVED, saved_at, unlinked=True)
            return ObtainedRules(_built_in(built_in_path, accept), SOURCE_BUILT_IN, unlinked=True)
    if saved is not None:
        return ObtainedRules(saved, SOURCE_SAVED, saved_at)
    return ObtainedRules(_built_in(built_in_path, accept), SOURCE_BUILT_IN)


def _built_in(path, accept):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if accept(payload) else None
