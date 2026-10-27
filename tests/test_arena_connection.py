"""Proxic Arena connection contract: linking, the sealed token, the rules, and sending a deck (ARN-012, ARN-014)."""

import atexit
import gzip
import io
import json
import pathlib
import shutil
import sys
import tempfile
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mtgdb.arena import connection
from mtgdb.arena.connection import (
    DUPLICATE, NAME_TAKEN, REFUSED, SENT, SOURCE_ARENA, SOURCE_BUILT_IN,
    SOURCE_SAVED, UNAUTHORIZED, UNAVAILABLE, ArenaConnectionError,
    ArenaConnectionStore, ArenaLink, DeckNamesFetch, RulesFetch,
    fetch_deck_names, fetch_known_pictures, fetch_rules, link_computer,
    normalize_address, obtain_rules, send_deck,
)
from mtgdb.core import secret_store
from mtgdb.deck.arena_support import rules_from_document

RULES = {
    "rulesVersion": "v1",
    "format": {"minCards": 60, "maxCards": 65, "maxCopies": 4,
               "unlimitedCopies": ["plains"], "sideDecks": {}},
    "cards": [{"name": "Lightning Bolt", "faces": ["Lightning Bolt"], "printings": ["M10 146"]}],
    "editions": [{"code": "M10", "scryfallCode": "m10", "type": "CORE"}],
    "setCodes": {"M10": "M10"},
}


def _accept(payload):
    return rules_from_document(payload).known


def _folder():
    folder = tempfile.mkdtemp(prefix="mtg-arena-link-")
    atexit.register(shutil.rmtree, folder, True)
    return pathlib.Path(folder)


class _Response:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self, _limit=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Opener:
    """Stands in for urllib's opener: records each request, answers from a script."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _http_error(code, detail="", **fields):
    payload = dict(fields, detail=detail) if detail or fields else None
    body = json.dumps(payload).encode() if payload else b""
    return urllib.error.HTTPError("https://arena", code, "x", {}, io.BytesIO(body))


def _address_check():
    """ARN-012: https, or http only to this computer; a bare host is https."""
    def refused(text):
        try:
            normalize_address(text)
        except ValueError:
            return True
        return False

    return (
        normalize_address("proxic.barnflix.net") == "https://proxic.barnflix.net"
        and normalize_address("https://proxic.barnflix.net/") == "https://proxic.barnflix.net"
        and normalize_address("https://proxic.barnflix.net/lobby") == "https://proxic.barnflix.net"
        and normalize_address("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
        and normalize_address("http://localhost:8080") == "http://localhost:8080"
        # The token would cross the network unencrypted.
        and refused("http://proxic.barnflix.net")
        and refused("ftp://proxic.barnflix.net")
        and refused("file://host/share")
        and refused("")
        # Credentials typed into the address are not sent along.
        and normalize_address("https://user:pw@proxic.barnflix.net") == "https://proxic.barnflix.net")


def _link_check():
    """ARN-012: a code becomes a link; every refusal is a sentence for the player."""
    ok = _Opener(_Response(201, json.dumps(
        {"token": "secret-token", "user": {"username": "Pat", "displayName": "Pat P"}}).encode()))
    link, token = link_computer("proxic.example", "k7qx-m4pd", "Desk", opener=ok)
    sent = ok.requests[0]
    body = json.loads(sent.data.decode())

    def message(answer):
        try:
            link_computer("proxic.example", "AAAA-BBBB", "Desk", opener=_Opener(answer))
        except ArenaConnectionError as exc:
            return str(exc)
        return ""

    return (
        token == "secret-token"
        and (link.address, link.username, link.display_name, link.device_name)
        == ("https://proxic.example", "Pat", "Pat P", "Desk")
        and sent.full_url == "https://proxic.example/api/auth/device-link"
        and sent.get_method() == "POST"
        and body == {"code": "k7qx-m4pd", "deviceName": "Desk"}
        # No password is ever asked for or sent.
        and "password" not in sent.data.decode().lower()
        and "make a new one on Arena's Account page" in message(_http_error(410))
        and "Try again in 30 seconds" in message(_http_error(429, "Too many tries. Try again in 30 seconds."))
        and "could not be reached" in message(urllib.error.URLError("down")))


def _fetch_check():
    """ARN-012: the token goes as a bearer, the saved copy's ETag rides along, gzip is read."""
    body = json.dumps(RULES).encode()
    fresh = _Opener(_Response(200, gzip.compress(body), {"Content-Encoding": "gzip", "ETag": '"v1"'}))
    got = fetch_rules("https://proxic.example", "tok", '"v0"', opener=fresh)
    sent = fresh.requests[0]
    return (
        got == RulesFetch("fresh", body, '"v1"')
        and sent.get_header("Authorization") == "Bearer tok"
        and sent.get_header("If-none-match") == '"v0"'
        and fetch_rules("https://p.example", "tok", opener=_Opener(_http_error(304))).status == "unchanged"
        and fetch_rules("https://p.example", "tok", opener=_Opener(_http_error(401))).status == "unauthorized"
        and fetch_rules("https://p.example", "tok", opener=_Opener(_http_error(503))).status == "unavailable"
        and fetch_rules("https://p.example", "tok", opener=_Opener(OSError("down"))).status == "unavailable")


def _no_redirect_check():
    """ARN-012: the default opener never follows a redirect with the token."""
    handler = connection._NoRedirect()
    return handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere") is None


def _store(linked=True, saved=None):
    folder = _folder()
    store = ArenaConnectionStore(folder)
    if linked:
        store.save_link(ArenaLink("https://p.example", "Pat", "Pat", "Desk", 1.0), "tok")
    if saved is not None:
        store.save_rules(json.dumps(saved).encode(), '"v0"')
    built_in = folder / "built_in.json"
    built_in.write_text(json.dumps(dict(RULES, rulesVersion="built")), encoding="utf-8")
    return store, built_in


def _obtain_check():
    """ARN-012: fresh from Arena, else saved, else built in; a refused link is forgotten."""
    fresh_body = json.dumps(dict(RULES, rulesVersion="fresh")).encode()

    store, built_in = _store(saved=RULES)
    asked = []

    def fresh(address, token, etag):
        asked.append((address, token, etag))
        return RulesFetch("fresh", fresh_body, '"v2"')
    got_fresh = obtain_rules(store, built_in, _accept, fetch=fresh)
    saved_after, etag_after, _ = store.saved_rules()

    store2, built_in2 = _store(saved=RULES)
    unchanged = obtain_rules(store2, built_in2, _accept, fetch=lambda *a: RulesFetch("unchanged"))

    store3, built_in3 = _store(saved=RULES)
    offline = obtain_rules(store3, built_in3, _accept, fetch=lambda *a: RulesFetch("unavailable"))

    store4, built_in4 = _store(saved=RULES)
    garbage = obtain_rules(store4, built_in4, _accept, fetch=lambda *a: RulesFetch("fresh", b"{}", '"x"'))
    kept, kept_etag, _ = store4.saved_rules()

    store5, built_in5 = _store(saved=RULES)
    refused = obtain_rules(store5, built_in5, _accept, fetch=lambda *a: RulesFetch("unauthorized"))

    store6, built_in6 = _store(linked=False)
    never = obtain_rules(store6, built_in6, _accept, fetch=lambda *a: 1 / 0)

    return (
        # Linked: Arena asked as this computer, with the saved copy's ETag, and its answer kept.
        asked == [("https://p.example", "tok", '"v0"')]
        and got_fresh.source == SOURCE_ARENA
        and got_fresh.payload["rulesVersion"] == "fresh"
        and saved_after["rulesVersion"] == "fresh" and etag_after == '"v2"'
        and unchanged.source == SOURCE_ARENA and unchanged.payload["rulesVersion"] == "v1"
        # Arena unreachable: the saved copy, said so.
        and offline.source == SOURCE_SAVED and offline.payload["rulesVersion"] == "v1"
        # A broken answer never replaces a good copy.
        and kept["rulesVersion"] == "v1" and kept_etag == '"v0"'
        and garbage.source == SOURCE_SAVED
        # Arena refusing the token: unlinked here, the saved copy used.
        and refused.unlinked and refused.source == SOURCE_SAVED and store5.link() is None
        # Never linked: Arena is not asked at all, and the built-in copy is used.
        and never.source == SOURCE_BUILT_IN and never.payload["rulesVersion"] == "built")


def _secret_check():
    """ARN-012: the token is sealed on disk; a damaged or foreign file reads as not linked."""
    folder = _folder()
    store = ArenaConnectionStore(folder)
    store.save_link(ArenaLink("https://p.example", "Pat", "Pat", "Desk", 1.0), "the-token-value")
    raw = (folder / connection.TOKEN_FILE).read_bytes()
    linked_ok = store.link() is not None and store.token() == "the-token-value"
    (folder / connection.TOKEN_FILE).write_bytes(raw[:-4])
    damaged = store.link()
    store.forget()
    return (
        linked_ok
        and b"the-token-value" not in raw if secret_store.sealed() else linked_ok
    ) and damaged is None and not (folder / connection.TOKEN_FILE).exists()


def _sentences_check():
    """ARN-012: the window says who it is linked as and where the rules came from."""
    from mtgdb.arena.connection import ObtainedRules
    from mtgdb.ui.arena_connection import arena_link_line
    from mtgdb.ui.arena_export import arena_rules_note

    line = arena_link_line(ArenaLink("https://proxic.example", "pat", "Pat", "Desk", 1.0))
    return (
        line == 'Linked to Proxic Arena as Pat on "Desk" · proxic.example'
        and arena_rules_note(ObtainedRules({}, SOURCE_ARENA), 1_700_000_000).startswith(
            "Deck rules from Proxic Arena, checked ")
        and "built into this version" in arena_rules_note(ObtainedRules({}, SOURCE_BUILT_IN))
        and "couldn't be reached" in arena_rules_note(ObtainedRules({}, SOURCE_SAVED, 1_700_000_000))
        and "no longer accepts this computer" in arena_rules_note(
            ObtainedRules({}, SOURCE_SAVED, unlinked=True))
        # The old dialog's menu is gone, so nothing points at it.
        and "File >" not in arena_rules_note(ObtainedRules({}, SOURCE_BUILT_IN)))


def _form_contract_check():
    """ARN-012, ARN-006: linking is in the export window, on a worker, never with a password."""
    form = (ROOT / "mtgdb/ui/arena_connection.py").read_text(encoding="utf-8")
    app = (ROOT / "mtgdb/ui/app.py").read_text(encoding="utf-8")
    store = (ROOT / "mtgdb/arena/connection.py").read_text(encoding="utf-8")
    return (
        "class ArenaLinkForm" in form
        and "submit_deck_file_job(" in form
        and "password" not in form.lower()
        and "messagebox" not in form
        # No Proxic Arena item in the File menu, and no separate dialog.
        and "Proxic Arena Connection" not in app
        and "ArenaConnectionMixin" not in app
        and "_open_arena_connection" not in form
        # The token goes only through the sealed store.
        and "write_secret(" in store and "read_secret(" in store
        and "build_opener(_NoRedirect())" in store)


def _deck_names_check():
    """ARN-014: the Deck Depot's names, read with the token; a refusal is "unauthorized"."""
    opener = _Opener(_Response(200, json.dumps({"names": ["Ape Tribal", "Burn"]}).encode()))
    got = fetch_deck_names("https://p.example", "tok", opener=opener)
    sent = opener.requests[0]

    def status(answer):
        return fetch_deck_names("https://p.example", "tok", opener=_Opener(answer)).status

    return (
        got == DeckNamesFetch("ok", ("Ape Tribal", "Burn"))
        and sent.full_url == "https://p.example/api/deck-builder/deck-names"
        and sent.get_header("Authorization") == "Bearer tok"
        and status(_http_error(401)) == "unauthorized"
        and status(_http_error(503)) == "unavailable"
        and status(OSError("x")) == "unavailable"
        and status(_Response(200, b"{}")) == "unavailable")


def _known_pictures_check():
    """ARN-014: which pictures Arena holds; any failure means "none", so all are sent."""
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    opener = _Opener(_Response(200, json.dumps({"known": [b, "f" * 64]}).encode()))
    got = fetch_known_pictures("https://p.example", "tok", [c, b, a, b], opener=opener)
    sent = opener.requests[0]
    many = _Opener(_Response(200, b'{"known": []}'))
    fetch_known_pictures("https://p.example", "tok", [f"{n:064x}" for n in range(500)], opener=many)

    def known(answer):
        return fetch_known_pictures("https://p.example", "tok", [a], opener=_Opener(answer))

    return (
        # Only what was asked about counts, whatever Arena says.
        got == frozenset({b})
        and sent.full_url == "https://p.example/api/deck-builder/pictures/known"
        and sent.get_method() == "POST"
        and json.loads(sent.data.decode()) == {"sha256": [a, b, c]}
        and len(json.loads(many.requests[0].data.decode())["sha256"]) == 200
        and known(OSError("x")) == frozenset()
        and known(_http_error(401)) == frozenset()
        # Nothing to ask: no request at all.
        and fetch_known_pictures("https://p.example", "tok", [], opener=_Opener()) == frozenset())


def _send_check():
    """ARN-014: one multipart send with the typed name; every answer read in plain terms."""
    created = _Opener(_Response(201, json.dumps({
        "deckId": "d1", "displayName": "Ape Tribal 2", "totalCards": 60,
        "tokenPictures": 1}).encode()))
    sent = send_deck("https://p.example", "tok", b"PK-zip-bytes", "Ape Tribal.zip",
                     name="Ape Tribal 2", opener=created)
    request = created.requests[0]
    body = request.data
    unnamed = _Opener(_Response(201, b"{}"))
    send_deck("https://p.example", "tok", b"z", "d.zip", opener=unnamed)

    def answer(error):
        return send_deck("https://p.example", "tok", b"zip", "d.zip", opener=_Opener(error))

    duplicate = answer(_http_error(
        409, 'You already have this deck: "Burn".', errorCode="DUPLICATE_PACKAGE",
        problems=["id"]))
    taken = answer(_http_error(
        409, 'You already have a deck called "Burn". Choose another name.',
        errorCode="DECK_NAME_TAKEN", problems=["Burn"]))
    refused = answer(_http_error(
        422, "The deck is not legal in Open Format.", errorCode="DECK_NOT_LEGAL",
        problems=["58 cards", "5 Bolt"]))
    crlf = bytes([13, 10])
    return (
        sent.status == SENT and sent.deck_name == "Ape Tribal 2"
        and (sent.deck_id, sent.total_cards, sent.token_pictures) == ("d1", 60, 1)
        and request.full_url == "https://p.example/api/deck-builder/decks"
        and request.get_header("Authorization") == "Bearer tok"
        and request.get_header("Content-type").startswith("multipart/form-data; boundary=")
        and b'name="name"' + crlf + crlf + b"Ape Tribal 2" + crlf in body
        and b'name="package"; filename="Ape Tribal.zip"' in body
        and b"PK-zip-bytes" in body
        # No name typed: no name field at all.
        and b'name="name"' not in unnamed.requests[0].data
        and duplicate.status == DUPLICATE and duplicate.deck_name == "Burn"
        and taken.status == NAME_TAKEN and taken.deck_name == "Burn"
        and refused.status == REFUSED and refused.problems == ("58 cards", "5 Bolt")
        and "not legal" in refused.detail
        and answer(_http_error(401)).status == UNAUTHORIZED
        and answer(_http_error(503)).status == UNAVAILABLE
        and answer(urllib.error.URLError("down")).status == UNAVAILABLE)


def main():
    checks = {
        "an Arena address is https, or http only to this computer": _address_check(),
        "a code becomes a link, and every refusal is a plain sentence": _link_check(),
        "the rules are fetched with the token and the saved copy's ETag": _fetch_check(),
        "a redirect is never followed with the token": _no_redirect_check(),
        "an export uses Arena's rules, else the saved copy, else the built-in one": _obtain_check(),
        "the token is sealed, and a damaged one reads as not linked": _secret_check(),
        "the window says who it is linked as and where the rules came from": _sentences_check(),
        "linking is in the export window, on a worker, never with a password": _form_contract_check(),
        "the Deck Depot's names are read with the token": _deck_names_check(),
        "Arena is asked which pictures it holds, and a failure sends them all": _known_pictures_check(),
        "a deck is sent in one request, and every answer is read": _send_check(),
    }
    ok = True
    for label, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok &= bool(passed)
    print("\nARENA CONNECTION:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
