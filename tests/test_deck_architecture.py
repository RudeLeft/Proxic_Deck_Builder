"""Deck model, TXT I/O, analysis, legality, and facade contracts."""

import ast
import atexit
import json
import math
from pathlib import Path
import shutil
import tempfile

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mtgdb.deck.analysis import (
    analyze_deck, average_mana_value, card_draw_odds, classify_type,
    color_pips,
    color_sources, curve_breakdown, deck_stats, hyper_at_least, hyper_between,
    opening_land_stats, sample_hand,
)
from mtgdb.deck.io import deck_from_text, deck_to_text
from mtgdb.deck.legality import (
    _card_copy_limit,
    _oracle_text as _legality_oracle_text,
)
from mtgdb.deck.legality import legality_problems
from mtgdb.core.format_names import FORMAT_WORD_LABELS, format_display_name
from mtgdb.deck.analysis import front_face, is_land
from mtgdb.deck.io import read_deck_text
from mtgdb.deck.model import BOARDS, Deck
from mtgdb.deck.arena_support import (
    ArenaFormat, front_face_name, load_arena_rules, normalize_card_name,
    rules_from_document, unsupported_deck_names,
)
from mtgdb.deck.arena_limits import arena_deck_limits
from mtgdb.deck.arena_tokens import (
    deck_tokens, split_for_arena, token_status,
)


def _card(card_id, name, *, oracle_id=None, type_line="Creature",
          cmc=2, mana_cost="{1}{G}", identity="G", produced="",
          set_code="tst", collector="1", legal="legal"):
    return {
        "id": card_id,
        "oracle_id": oracle_id or f"oracle-{card_id}",
        "name": name,
        "type_line": type_line,
        "cmc": cmc,
        "mana_cost": mana_cost,
        "color_identity": identity,
        "produced_mana": produced,
        "set_code": set_code,
        "set_name": set_code.upper(),
        "collector_number": collector,
        "legalities": {"modern": legal, "commander": legal, "vintage": legal},
    }


class Resolver:
    def __init__(self, cards):
        self.cards = list(cards)
        self.calls = []

    def get_by_name(self, name, **options):
        self.calls.append((name, options))
        candidates = [card for card in self.cards if card["name"] == name]
        set_codes = options.get("allowed_set_codes")
        if set_codes is not None:
            candidates = [
                card for card in candidates
                if card.get("set_code") in set_codes]
        collectors = options.get("allowed_collector_numbers")
        if collectors is not None:
            candidates = [
                card for card in candidates
                if card.get("collector_number") in collectors]
        return candidates[0] if candidates else None


def _decklist_encoding_check():
    """A decklist this app did not write still opens, and keeps its format.

    Decks arrive from other builders and editors. A byte-order mark is the
    dangerous one: it is invisible, so the cards still import and nothing looks
    wrong, but it sits in front of the "// Name (format)" line and stops that
    line being read as the header. The deck then falls back to the Commander
    default, and a 60-card Modern deck is reported as needing 100 cards with
    every playset over the singleton limit. UTF-16 and Windows-1252 do not
    import at all without this.
    """
    body = "// Burn (modern)\n\n4 Test Card\n"
    folder = Path(tempfile.mkdtemp(prefix="mtgdb-encodings-"))
    atexit.register(shutil.rmtree, str(folder), True)

    resolver = Resolver([_card("printing-a", "Test Card")])
    outcomes = {}
    for label, encoding in (("utf-8", "utf-8"), ("bom", "utf-8-sig"),
                            ("utf-16", "utf-16"), ("cp1252", "cp1252")):
        path = folder / ("deck-%s.txt" % label)
        path.write_bytes(body.encode(encoding))
        try:
            deck, missing = deck_from_text(read_deck_text(path), resolver,
                                           name="Fallback Name")
            outcomes[label] = (deck.name, deck.fmt, deck.total("main"),
                               tuple(missing))
        except Exception as exc:                            # noqa: BLE001
            outcomes[label] = type(exc).__name__

    # A name only Windows-1252 can spell must survive that fallback.
    latin = folder / "latin.txt"
    latin.write_text("4 Lim-D\u00fbl's Vault\n", encoding="cp1252")
    latin_text = read_deck_text(latin)

    # A tool that re-saves an already-marked file writes the mark twice, and
    # utf-8-sig strips only one. The second is still enough to hide the header,
    # so removing it is the reader's job rather than the codec's.
    doubled = folder / "doubled-bom.txt"
    doubled.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8-sig"))
    doubled_deck, doubled_missing = deck_from_text(
        read_deck_text(doubled), resolver, name="Fallback Name")

    expected = ("Burn", "modern", 4, ())
    return (
        all(outcomes[label] == expected for label in outcomes)
        and "Lim-D\u00fbl" in latin_text
        and (doubled_deck.name, doubled_deck.fmt, doubled_deck.total("main"),
             tuple(doubled_missing)) == expected
        # The mark itself is removed, not merely decoded around.
        and not read_deck_text(
            folder / "deck-bom.txt").startswith("\ufeff")
        and not read_deck_text(doubled).startswith("\ufeff"))


def _paren_printing_check():
    """DECK-007: "Name (SET) 123" keeps the printing it names.

    MTG Arena, Moxfield and Archidekt export this style. It used to resolve by
    name alone, so a pasted Duel Decks: Elves vs. Goblins list came in as each
    card's usual printing. A set the library lacks (MTG Arena writes some of its
    own codes) falls back to the name, and a real name ending in "(Word)" still
    finds its card.
    """
    from mtgdb.database.queries import _name_candidates

    class SuffixResolver(Resolver):
        # The real resolver also tries the name without a "(SET) 123" suffix.
        def get_by_name(self, name, **options):
            for candidate in _name_candidates(name):
                card = super().get_by_name(candidate, **options)
                if card is not None:
                    return card
            return None

    cards = [
        _card("mtn-usual", "Mountain", set_code="usu", collector="1"),
        _card("mtn-59", "Mountain", set_code="dd1", collector="59"),
        _card("mtn-61", "Mountain", set_code="dd1", collector="61"),
        _card("sgc-usual", "Siege-Gang Commander", set_code="usu", collector="2"),
        _card("sgc-dd1", "Siege-Gang Commander", set_code="dd1", collector="32"),
        _card("bolt-usual", "Lightning Bolt", set_code="usu", collector="3"),
        _card("bolt-2x2", "Lightning Bolt", set_code="2x2", collector="117"),
        _card("hazmat", "Hazmat Suit (Used)", set_code="ust", collector="57"),
    ]
    deck, missing = deck_from_text(
        "6 Mountain (DD1) 59\n5 Mountain (dd1) 61\n"
        "1 Siege-Gang Commander (DD1) 32 *F*\n"
        "1 Lightning Bolt (2X2) 999 *E*\n"
        "1 Lightning Bolt (DAR) 254\n"
        "1 Hazmat Suit (Used)\n",
        SuffixResolver(cards), paper_only=False, lang=None)
    held = {entry["card"]["id"]: entry["qty"] for entry in deck.entries("main")}
    return missing == [] and held == {
        # The printing named, each Mountain its own...
        "mtn-59": 6, "mtn-61": 5, "sgc-dd1": 1,
        # ...the set, when that number is not in it...
        "bolt-2x2": 1,
        # ...the name, when the library has no such set...
        "bolt-usual": 1,
        # ...and a name that ends in "(Word)".
        "hazmat": 1}


def _oversized_sideboard_deck():
    """A legal-sized Modern deck whose sideboard is one card too many."""
    deck = Deck("Sideboarded", "modern")
    deck.add(_card("main-card", "Main Card"), "main", 60)
    deck.add(_card("side-card", "Side Card"), "side", 16)
    return deck


def _format_naming_check():
    """One format is named the same way everywhere the user can see it.

    Scryfall identifies a format by a bare key. Those keys read wrong in prose
    -- "commander decks have no sideboard" -- and made the legality report call
    a format something different from the Format picker and the card preview:
    "paupercommander" against "Pauper Commander". The readable name is shared,
    and the rule lookup still uses the stored key, because normalizing the
    display name would turn "duel" into "duelcommander" and silently fail every
    construction check closed.
    """
    ui_source = (ROOT / "mtgdb/ui/components.py").read_text(encoding="utf-8")
    legality_source = (ROOT / "mtgdb/deck/legality.py").read_text(
        encoding="utf-8")

    commander = Deck("Sideboarded", "commander")
    commander.add(_card("side-card", "Side Card"), "side", 1)
    commander_problems = legality_problems(commander)

    duel = Deck("Duel", "duel")
    duel_problems = legality_problems(duel)

    unverified = Deck("Pauper EDH", "paupercommander")
    unverified_problems = legality_problems(unverified)

    unknown = Deck("Future", "timewalkcube")
    unknown_problems = legality_problems(unknown)

    return (
        # Every problem sentence names the format the readable way.
        any("Commander decks have no sideboard" in problem
            for problem in commander_problems)
        and not any("commander decks have no sideboard" in problem
                    for problem in commander_problems)
        and any("Duel Commander requires exactly 100" in problem
                for problem in duel_problems)
        and any("not verified for Pauper Commander" in problem
                for problem in unverified_problems)
        # All four construction sentences name the format, not just the two
        # that happened to already: a minimum and a sideboard cap read as
        # anonymous numbers otherwise.
        and any("Modern requires at least 60" in problem
                for problem in legality_problems(Deck("Short", "modern")))
        and any("Modern allows at most 15" in problem
                for problem in legality_problems(_oversized_sideboard_deck()))
        # The lookup key still comes from the stored format, so a format whose
        # display name differs from its key keeps its construction rules.
        and any("exactly 100" in problem for problem in duel_problems)
        and not any("not verified" in problem for problem in duel_problems)
        # A format with no readable name still names itself and stays usable.
        and any("Timewalkcube" in problem for problem in unknown_problems)
        # One mapping, shared: the UI does not keep a second copy.
        and format_display_name("paupercommander") == "Pauper Commander"
        and "from mtgdb.core.format_names import" in ui_source
        and ui_source.count('"paupercommander":') == 0
        and "from mtgdb.core.format_names import" in legality_source
        and set(FORMAT_WORD_LABELS) >= {"duel", "paupercommander"})


def _double_faced_statistics_check():
    """A card is classified by the face it is played from, not both faces.

    A stored type line holds every face at once, so a transforming permanent
    with a land back reads "Legendary Enchantment // Legendary Land" and a
    whole-string land test counted it -- and 81 other real cards -- as a land.
    That removed the card from the mana curve, took it out of the average mana
    value, moved it from its real type row into Lands, and inflated both
    opening-hand land figures, all for a three-mana enchantment that can never
    be played as a land.
    """
    growing_rites = _card(
        "rites", "Growing Rites of Itlimoc", cmc=3, mana_cost="{2}{G}",
        type_line="Legendary Enchantment // Legendary Land")
    mammoth = _card(
        "mammoth", "Kazandu Mammoth", cmc=3, mana_cost="{2}{G}",
        type_line="Creature — Elephant // Land")
    abbey = _card(
        "abbey", "Westvale Abbey", cmc=0, mana_cost="", identity="",
        produced="C", type_line="Land // Legendary Creature — Demon")
    forest = _card(
        "forest", "Forest", cmc=0, mana_cost="", identity="", produced="G",
        type_line="Basic Land — Forest")

    deck = Deck("Faces", "modern")
    deck.add(growing_rites, "main", 4)
    deck.add(mammoth, "main", 4)
    deck.add(abbey, "main", 4)
    deck.add(forest, "main", 8)

    snapshot = analyze_deck(deck)
    legacy = deck_stats(deck)
    _labels, buckets = curve_breakdown(deck, "type")

    return (
        # The helper reports the played face, and tolerates a missing one.
        front_face("Sorcery // Land") == "Sorcery"
        and front_face("Basic Land — Forest") == "Basic Land — Forest"
        and front_face(None) == ""
        and not is_land({"type_line": "Sorcery // Land"})
        and is_land({"type_line": "Land // Artifact Creature"})
        # A land back does not make a spell a land: only the 4 Abbeys and the
        # 8 Forests fill land slots, and the other 8 cards stay spells.
        and snapshot.stats["types"] == {
            "Lands": 12, "Creatures": 4, "Enchantments": 4}
        and snapshot.stats["curve"][3] == 8
        and snapshot.opening_land_stats[:2] == (20, 12)
        and snapshot.average_mana_value == 3.0
        # A card kept out of the curve must be kept out of its breakdown too.
        and buckets[3] == {
            "Creatures": 4, "Instants": 0, "Sorceries": 0, "Artifacts": 0,
            "Enchantments": 4, "Planeswalkers": 0, "Battles": 0, "Other": 0}
        # The one-pass analysis and the legacy aggregate never disagree.
        and legacy["curve"] == snapshot.stats["curve"]
        and legacy["types"] == snapshot.stats["types"])


def _probability_guard_check():
    """Every probability call returns a number instead of raising.

    Both summations index math.comb with a term that goes negative once a
    caller asks for more successes than it draws, or names a lower bound below
    zero. hyper_between guarded one of the three terms and hyper_at_least
    guarded neither, so a future "chance of two or more copies" figure would
    have crashed the stats panel rather than reading 0%.
    """
    return (
        hyper_at_least(60, 4, 1, want=3) == 0.0
        and hyper_at_least(60, 4, 7, want=9) == 0.0
        and hyper_between(60, 4, 7, -2, 1) == hyper_between(60, 4, 7, 0, 1)
        and hyper_between(60, 4, 7, 3, 99) == hyper_between(60, 4, 7, 3, 4)
        # Asking for at least one copy is still the ordinary calculation.
        and math.isclose(hyper_at_least(60, 4, 7, want=1),
                         1 - math.comb(56, 7) / math.comb(60, 7)))


def _arena_name_key_check():
    """DECK-012: the lookup key mirrors Proxic Arena's own name rule.

    Every expectation is written out rather than derived from the function
    under test (VER-010), because a key computed by calling the same code it
    checks moves with any defect it should have caught.
    """
    curly = normalize_card_name("Yawgmoth\u2019s Will")
    return (
        normalize_card_name("Lightning Bolt") == "lightning_bolt"
        and normalize_card_name("Jace, the Mind Sculptor")
        == "jace_the_mind_sculptor"
        # Accents are stripped, so a deck naming Eowyn either way still matches.
        and normalize_card_name("\u00c9owyn, Lady of Rohan")
        == "eowyn_lady_of_rohan"
        # A comma inside a number is dropped rather than made a separator.
        and normalize_card_name("Borrowing 100,000 Arrows")
        == "borrowing_100000_arrows"
        and normalize_card_name("Yawgmoth's Will") == "yawgmoths_will"
        # Both apostrophe spellings reach one key; two spellings of one name
        # must not read as two cards.
        and curly == "yawgmoths_will"
        and normalize_card_name("Mothers of Runes  --  Promo")
        == "mothers_of_runes_promo"
        and normalize_card_name("") == ""
        and normalize_card_name("???") == ""
        # A stored multi-face name is filed under its front face.
        and front_face_name("Delver of Secrets // Insectile Aberration")
        == "Delver of Secrets"
        and front_face_name("Lightning Bolt") == "Lightning Bolt"
        and normalize_card_name(front_face_name(
            "Delver of Secrets // Insectile Aberration")) == "delver_of_secrets")


def _arena_unreadable_rules_check():
    """DECK-012: a rules document that cannot be read reports nothing instead of raising."""
    directory = tempfile.mkdtemp(prefix="mtg-arena-rules-")
    atexit.register(shutil.rmtree, directory, True)
    missing = Path(directory) / "absent.json"
    malformed = Path(directory) / "malformed.json"
    malformed.write_text("{not json", encoding="utf-8")
    good = {
        "rulesVersion": "v1",
        "format": {"minCards": 60, "maxCards": 65, "maxCopies": 4,
                   "unlimitedCopies": ["plains"], "sideDecks": {}},
        "cards": [{"name": "Lightning Bolt", "faces": ["Lightning Bolt"],
                   "printings": ["M10 146"]}],
        "editions": [{"code": "M10", "scryfallCode": "m10", "type": "CORE"}],
        "setCodes": {"M10": "M10"},
    }
    part_missing = {key: value for key, value in good.items() if key != "setCodes"}
    bad_format = dict(good, format={"minCards": "sixty"})
    bad_cards = dict(good, cards="Lightning Bolt")

    deck = Deck("Arena Unknown", "modern")
    deck.add(_card("unknown", "Totally Unknown Card"), "main", 1)
    return (
        not load_arena_rules(missing).known
        and not load_arena_rules(malformed).known
        and not rules_from_document(None).known
        and not rules_from_document(part_missing).known
        and not rules_from_document(bad_format).known
        and not rules_from_document(bad_cards).known
        and rules_from_document(good).known
        and rules_from_document(good).support.names == frozenset({"lightning_bolt"})
        # No names known means nothing is claimed unplayable.
        and unsupported_deck_names(deck, frozenset()) == []
        and unsupported_deck_names(
            deck, load_arena_rules(missing).support.names) == [])


def _arena_deck_report_check():
    """DECK-012: only the names Proxic Arena has no script for are reported."""
    supported = frozenset({
        "lightning_bolt", "delver_of_secrets", "yawgmoths_will"})
    deck = Deck("Arena Mixed", "modern")
    deck.add(_card("bolt", "Lightning Bolt"), "main", 4)
    # Matched on the front face, which is the face Arena files it under.
    deck.add(
        _card("delver", "Delver of Secrets // Insectile Aberration"), "main", 4)
    # The apostrophe spelling differs from the key's source name.
    deck.add(_card("will", "Yawgmoth\u2019s Will"), "main", 1)
    deck.add(_card("cecily", "Cecily, Haunted Mage"), "main", 1)
    deck.add(_card("subgoyf", "Subgoyf"), "side", 2)
    # The same unplayable card in both boards is one reported name.
    deck.add(_card("subgoyf-alt", "Subgoyf"), "main", 1)
    reported = unsupported_deck_names(deck, supported)

    nameless = Deck("Arena Nameless", "modern")
    nameless.add(_card("blank", ""), "main", 1)

    clean = Deck("Arena Clean", "modern")
    clean.add(_card("bolt-only", "Lightning Bolt"), "main", 4)

    side_only = Deck("Arena Side", "modern")
    side_only.add(_card("bolt-main", "Lightning Bolt"), "main", 4)
    side_only.add(_card("subgoyf-side", "Subgoyf"), "side", 2)
    return (
        reported == ["Cecily, Haunted Mage", "Subgoyf"]
        # A card only in the sideboard is not exported, so it is not "lost".
        and unsupported_deck_names(
            side_only, supported, boards=("main",)) == []
        and unsupported_deck_names(side_only, supported) == ["Subgoyf"]
        # A deck Arena can play in full says nothing at all.
        and unsupported_deck_names(clean, supported) == []
        # A stored entry with no name cannot be reported as a missing card.
        and unsupported_deck_names(nameless, supported) == [])


def _arena_limits_check():
    """ARN-007: a deck is measured against Arena's format, as its rules state it.

    The numbers are written out rather than read from the rules, so a rule
    read wrongly fails here instead of agreeing with itself (VER-010).
    """
    rules_format = ArenaFormat(
        min_cards=60, max_cards=65, max_copies=4,
        unlimited=frozenset({"plains", "island"}),
        side_decks={"CONTRAPTIONS": (15, 1), "ATTRACTIONS": (10, 1)},
        sections={f"gadget_{n}": "CONTRAPTIONS" for n in range(20)})
    plains = _card("plains", "Plains", type_line="Basic Land — Plains")
    rats = _card("rats", "Relentless Rats")
    rats["oracle_text"] = (
        "A deck can have any number of cards named Relentless Rats.")
    dwarves = _card("dwarves", "Seven Dwarves")
    dwarves["oracle_text"] = (
        "A deck can have up to seven cards named Seven Dwarves.")

    def deck_of(*parts):
        deck = Deck("Probe", "modern")
        for card, board, quantity in parts:
            deck.add(card, board, quantity)
        return arena_deck_limits(deck, rules_format)

    bolt = _card("bolt", "Lightning Bolt")
    smallest = deck_of((bolt, "main", 4), (plains, "main", 56))
    largest = deck_of((bolt, "main", 4), (plains, "main", 61))
    one_over = deck_of((plains, "main", 66))
    one_short = deck_of((plains, "main", 59))
    # The sideboard is not exported: a 60-card deck with 15 more beside it.
    split = deck_of((plains, "main", 60), (bolt, "side", 15))
    five = deck_of(
        (_card("bolt-a", "Lightning Bolt", set_code="lea"), "main", 4),
        (_card("bolt-b", "Lightning Bolt", set_code="m10"), "main", 1),
        (plains, "main", 55))
    faces = deck_of(
        (_card("delver", "Delver of Secrets // Insectile Aberration"), "main", 3),
        (_card("delver-front", "Delver of Secrets"), "main", 2),
        (plains, "main", 55))
    # Owner 2026-10-27: Arena's rule, basic lands only. Relentless Rats and
    # Seven Dwarves are held to four, as Arena holds them.
    any_number = deck_of((rats, "main", 20), (plains, "main", 40))
    seven = deck_of((dwarves, "main", 7), (plains, "main", 53))
    # Side decks: not in the 60, at least 15 Contraptions, one of each.
    gadgets = [_card(f"g{n}", f"Gadget {n}") for n in range(20)]
    with_side = deck_of((plains, "main", 60), *[(g, "main", 1) for g in gadgets[:15]])
    short_side = deck_of((plains, "main", 60), *[(g, "main", 1) for g in gadgets[:14]])
    doubled = deck_of(
        (plains, "main", 60), (gadgets[0], "main", 2),
        *[(g, "main", 1) for g in gadgets[1:15]])
    nameless = deck_of((_card("blank", ""), "main", 9), (plains, "main", 51))

    def legend(card_id, name, type_line, set_code="tst"):
        return _card(card_id, name, type_line=type_line, set_code=set_code)

    mob_boss = "Krenko, Mob Boss"
    goblin = "Legendary Creature — Goblin Warrior"
    legend_two = deck_of((legend("k1", mob_boss, goblin), "main", 2), (plains, "main", 58))
    legend_printings = deck_of(
        (legend("k1", mob_boss, goblin, "m20"), "main", 1),
        (legend("k2", mob_boss, goblin, "ima"), "main", 1), (plains, "main", 58))
    legend_five = deck_of((legend("k1", mob_boss, goblin), "main", 5), (plains, "main", 55))
    legend_side = deck_of(
        (legend("k1", mob_boss, goblin), "main", 1),
        (legend("k1", mob_boss, goblin), "side", 3), (plains, "main", 59))
    spell = deck_of(
        (legend("arc", "Legendary Spell", "Legendary Sorcery — Arcane"), "main", 4),
        (plains, "main", 56))
    land = deck_of((legend("land", "Legend Land", "Legendary Land"), "main", 2), (plains, "main", 58))
    back = deck_of(
        (legend("back", "Hunter // Beast",
                "Creature — Human // Legendary Creature — Werewolf"), "main", 2),
        (plains, "main", 58))

    return (
        # Both ends of the size are allowed, and one past either is refused.
        smallest.allowed and smallest.total_cards == 60
        and largest.allowed and largest.total_cards == 65
        and not one_over.allowed and one_over.cards_over == 1
        and not one_short.allowed and one_short.cards_short == 1
        and (one_over.min_cards, one_over.max_cards) == (60, 65)
        and split.allowed and split.total_cards == 60
        # A fifth copy is refused, across printings and faces.
        and five.over_copy_limit == (("Lightning Bolt", 5, 4),)
        and len(faces.over_copy_limit) == 1 and faces.over_copy_limit[0][1:] == (5, 4)
        # Basic lands only are exempt.
        and largest.over_copy_limit == ()
        and any_number.over_copy_limit == (("Relentless Rats", 20, 4),)
        and seven.over_copy_limit == (("Seven Dwarves", 7, 4),)
        # Side decks: out of the main count, a smallest size, one of each.
        and with_side.allowed and with_side.total_cards == 60
        and short_side.short_side_decks == (("CONTRAPTIONS", 14, 15),)
        and not short_side.allowed
        and doubled.over_copy_limit == (("Gadget 0", 2, 1),)
        # Legendary permanents may repeat: allowed, and reported by name.
        and legend_two.allowed and legend_two.legendary_repeats == ((mob_boss, 2),)
        and legend_printings.legendary_repeats == ((mob_boss, 2),)
        and legend_five.over_copy_limit == ((mob_boss, 5, 4),)
        and legend_side.allowed and legend_side.legendary_repeats == ()
        and spell.allowed and spell.legendary_repeats == ()
        and land.legendary_repeats == (("Legend Land", 2),)
        and back.legendary_repeats == (("Hunter // Beast", 2),)
        and five.legendary_repeats == ()
        # A nameless entry counts toward size and cannot be listed as a card.
        and nameless.total_cards == 60 and nameless.allowed)


def _arena_shipped_rules_check():
    """DECK-012: the built-in copy is Arena's own document, read in full."""
    path = ROOT / "assets/arena/deck_rules.json"
    if not path.is_file():
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    rules = load_arena_rules(path)
    support = rules.support
    rules_format = rules.format
    return (
        rules.known
        # As Arena served it, with its own version.
        and payload["rulesVersion"] == rules.rules_version != ""
        and len(support.names) > 30000
        and "lightning_bolt" in support.names
        # A back face's name is a playable name too.
        and "insectile_aberration" in support.names
        # A card's own printings, never another card's that shares a face name.
        and "M10 146" in support.printings["lightning_bolt"]
        and "M21 263" in support.printings["island"]
        # In Arena's order, kept as served (ARN-011): the first is Arena's own
        # first choice, as the document lists it.
        and isinstance(support.printings["lightning_bolt"], tuple)
        and support.printings["lightning_bolt"][0] == next(
            card for card in payload["cards"]
            if card["name"] == "Lightning Bolt")["printings"][0]
        # Arena's set codes: a second code finds its edition, a Scryfall code alone does not.
        and support.set_codes.get("CON") == "CFX"
        and "UGIN" not in support.set_codes
        and support.editions["UGF"][0] == "ugin"
        # The format, with the basic lands the only exemption.
        and (rules_format.min_cards, rules_format.max_cards, rules_format.max_copies) == (60, 65, 4)
        and {"plains", "island", "snow_covered_forest"} <= rules_format.unlimited
        and "relentless_rats" not in rules_format.unlimited
        and rules_format.side_decks.get("CONTRAPTIONS") == (15, 1)
        and "CONTRAPTIONS" in set(rules_format.sections.values()))


def _no_engine_knowledge_check():
    """ARN-015, DECK-012: the program knows nothing of the engine Arena runs.

    Every instruction comes from Arena, so no source under mtgdb/ names the
    engine or reads its files, and nothing here rebuilds a card list or
    watches the engine.
    """
    import re
    engine = re.compile(r"\b" + "for" + r"ge\b", re.IGNORECASE)
    named = sorted(
        str(path.relative_to(ROOT)) for path in (ROOT / "mtgdb").rglob("*.py")
        if engine.search(path.read_text(encoding="utf-8")))
    sources = "".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "mtgdb").rglob("*.py"))
    workflows = "".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / ".github/workflows").glob("*.yml"))
    return (
        named == []
        and not (ROOT / "tests/arena_support_refresh.py").exists()
        and not (ROOT / "assets/arena/supported_cards.json").exists()
        and not engine.search(workflows)
        and "cardsfolder" not in sources
        and "res/editions" not in sources
        and "tokenscripts" not in sources)


def _arena_token_parts_check():
    """ARN-013, ARN-010: token cards travel as pictures, counted apart from the cards.

    Expectations are written out rather than derived from the functions under
    test (VER-010).
    """
    def kind(card, content_kind):
        card["content_kind"] = content_kind
        return card

    pongify = _card("po", "Pongify")
    pongify["related_parts"] = json.dumps([
        {"component": "token", "name": "Ape", "type_line": "Token Creature — Ape"}])
    krenko = _card("kr", "Krenko, Mob Boss")
    krenko["related_parts"] = json.dumps([
        {"component": "token", "name": "Goblin", "type_line": "Token Creature — Goblin"}])
    ape = kind(_card("ape1", "Ape"), "token")
    ape_again = kind(_card("ape2", "Ape", set_code="alt"), "token")
    emblem = kind(_card("em", "Elspeth Emblem"), "emblem")
    side_token = kind(_card("sd", "Zombie"), "token")
    deck = Deck("Ape Tribal", "modern")
    deck.add(pongify, "main", 4)
    deck.add(krenko, "main", 4)
    deck.add(_card("fo", "Forest"), "main", 52)
    deck.add(ape, "main", 1)
    deck.add(ape_again, "main", 2)
    deck.add(emblem, "main", 1)
    deck.add(side_token, "side", 1)
    deck.add(_card("bo", "Lightning Bolt"), "side", 3)
    parts = split_for_arena(deck)
    held, missing = token_status(deck)
    text = deck_to_text(parts.cards, include_side=False, tokens=parts.tokens)
    lines = text.splitlines()
    return (
        # Cards only, both boards; no game piece anywhere in them.
        parts.cards.total("main") == 60
        and parts.cards.total("side") == 3
        # One token per name: the first printing, with every copy counted.
        and [(card["id"], quantity) for card, quantity in parts.tokens] == [("ape1", 3)]
        and parts.left_out == ("Elspeth Emblem",)
        # Matched by name alone: any Ape token card dresses the Ape.
        and [token.name for token in held] == ["Ape"]
        and [token.name for token in missing] == ["Goblin"]
        and missing[0].makers == ("Krenko, Mob Boss",)
        # The Tokens section follows the deck, after a blank line.
        and lines[-2:] == ["// Tokens", "3 Ape [TST:1]"]
        and lines[-3] == ""
        and "Sideboard" not in text
        # A deck with no token cards writes no section.
        and "// Tokens" not in deck_to_text(parts.cards, include_side=False))


def _arena_tokens_check():
    """ARN-010: the tokens a deck's cards can create, from their related parts.

    Expectations are written out rather than derived from the function under
    test (VER-010).
    """
    def part(component, name, type_line, identifier="x"):
        return {"component": component, "name": name,
                "type_line": type_line, "id": identifier}

    def maker(card_id, name, parts, as_text=True):
        card = _card(card_id, name)
        card["related_parts"] = json.dumps(parts) if as_text else parts
        return card

    goblin = "Token Creature — Goblin"
    outburst = maker("ho", "Hordeling Outburst", [
        part("token", "Goblin", goblin, "g1"),
        part("combo_piece", "Hordeling Outburst", "Sorcery")])
    # Already-decoded related parts read the same as stored JSON text, and a
    # second Scryfall Goblin token is the same kind of token.
    krenko = maker(
        "kr", "Krenko, Mob Boss", [part("token", "Goblin", goblin, "g2")],
        as_text=False)
    elspeth = maker("el", "Elspeth, Sun's Champion", [
        part("token", "Soldier", "Token Creature — Soldier"),
        # An emblem is linked as a combo piece, and is not a token.
        part("combo_piece", "Elspeth, Sun's Champion Emblem",
             "Emblem — Elspeth")])
    turns_over = maker("df", "Front Side // Back Side", [
        part("token", "Clue", "Token Artifact — Clue")])
    plain = _card("pl", "Lightning Bolt")
    broken = _card("br", "Broken Parts")
    broken["related_parts"] = "{not json"
    sideboard_only = maker("sd", "Sideboard Maker", [
        part("token", "Zombie", "Token Creature — Zombie")])

    deck = Deck("Tokens", "modern")
    for card in (outburst, krenko, elspeth, turns_over, plain, broken):
        deck.add(card, "main", 2)
    # Another printing of a card already listed is not named twice.
    deck.add(maker("ho2", "Hordeling Outburst", [
        part("token", "Goblin", goblin, "g3")]), "main", 1)
    deck.add(sideboard_only, "side", 3)
    found = deck_tokens(deck)

    empty = Deck("No Tokens", "modern")
    empty.add(plain, "main", 4)
    empty.add(broken, "main", 1)
    return (
        # By token name, whatever order the deck lists its cards in.
        [token.name for token in found] == ["Clue", "Goblin", "Soldier"]
        # One entry for the Goblin, naming each card able to make it, once.
        and found[1].makers == ("Hordeling Outburst", "Krenko, Mob Boss")
        and found[1].type_line == goblin
        # A card that turns over is named by its front face.
        and found[0].makers == ("Front Side",)
        # No emblem, no sideboard token, and unreadable data fails closed.
        and "Zombie" not in [token.name for token in found]
        and "Emblem" not in " ".join(token.type_line for token in found)
        and deck_tokens(empty) == ())


def _import_roots(source):
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def main():
    first = _card(
        "spell-a", "Hybrid Spell", oracle_id="oracle-spell",
        mana_cost="{W/U}{2/G}", identity=["W", "U"], collector="10")
    second = _card(
        "spell-b", "Hybrid Spell", oracle_id="oracle-spell",
        mana_cost="{W/U}{2/G}", identity="W,U", set_code="alt",
        collector="44")
    land = _card(
        "land", "Test Forest", type_line="Basic Land — Forest", cmc=0,
        mana_cost="", identity="G", produced=["G"], collector="2")
    artifact = _card(
        "rock", "Mana Rock", type_line="Artifact", cmc=3,
        mana_cost="{3}", identity="", produced="W,U", collector="3")
    banned = _card(
        "banned", "Banned Card", legal="banned", collector="4")

    model = Deck("Domain Test", "modern")
    model.add(first, "main", 2)
    model.add(second, "main", 3)
    model.add(land, "main", 10)
    model.add(artifact, "side", 2)
    model.change_qty("spell-a", "main", 1)
    model.move("rock", "side", "main")
    model.remove("missing", "main")

    stats = deck_stats(model)
    method_stats = model.stats()
    pips = color_pips(model)
    sources, source_total = color_sources(model)
    type_labels, type_curve = curve_breakdown(model, "type")
    color_labels, color_curve = curve_breakdown(model, "color")
    lands = opening_land_stats(model)
    average_mv = average_mana_value(model)
    draw_odds = card_draw_odds(model, "Hybrid Spell")
    hand = sample_hand(model, 7)

    serialized = deck_to_text(model)
    method_serialized = model.to_text()
    resolver = Resolver([first, second, land, artifact, banned])
    imported, missing = deck_from_text(serialized, resolver)
    method_imported, method_missing = Deck.from_text(serialized, resolver)
    sectioned, section_missing = deck_from_text(
        "// Sections (modern)\n"
        "Mainboard:\n1 Hybrid Spell [TST:10]\n"
        "Sideboard:\n2 Mana Rock [TST:3]\n"
        "Maybeboard:\n4 Banned Card [TST:4]\n"
        "Main:\n1 Missing Card [TST:999]\n",
        resolver, allowed_set_types={"expansion"},
        allowed_set_codes={"alt"})

    # Importer robustness: a "0 Cardname" line must not abort the whole file,
    # and a bare unquantified line must import as one copy instead of vanishing.
    tolerant, tolerant_missing = deck_from_text(
        "// Tolerant (modern)\n"
        "4 Hybrid Spell [TST:10]\n"
        "0 Mana Rock [TST:3]\n"
        "2 Mana Rock [TST:3]\n",
        resolver)
    bare, bare_missing = deck_from_text(
        "// Bare (modern)\n"
        "Creatures (2)\n"
        "Hybrid Spell\n"
        "Mana Rock\n"
        "3 Banned Card\n"
        "2 Totally Absent Card\n",
        resolver)
    # Other builders put a card count in the header parens ("// Lands (37)"); a
    # purely numeric value is not a format and must not overwrite deck.fmt (which
    # would leave a bare "37" on the format button). A real format still applies.
    count_header, _count_missing = deck_from_text(
        "// Lands (37)\n4 Hybrid Spell [TST:10]\n", resolver, fmt="commander")
    real_header, _real_missing = deck_from_text(
        "// Duels (pauper)\n4 Hybrid Spell [TST:10]\n", resolver, fmt="commander")

    # card_faces arrives as JSON text from the database; iterating the raw
    # column walks characters and silently drops every face's rules text.
    faced_card = _card(
        "faced-1", "Faced Card // Faced Back", type_line="Creature // Creature")
    faced_card["oracle_text"] = ""
    faced_card["card_faces"] = json.dumps([
        {"name": "Faced Card",
         "oracle_text":
             "A deck can have any number of cards named "
             "Faced Card // Faced Back."},
        {"name": "Faced Back", "oracle_text": "Trample."},
    ])
    faced_limit = _card_copy_limit(faced_card, 4)
    faced_text = _legality_oracle_text(faced_card)

    # _FORMAT_RULES copy limits drive user-facing legality verdicts, so pin the
    # boundary itself: four copies are legal in Constructed and five are not.
    standard_legal = Deck("Standard Legal", "standard")
    standard_legal.add(first, "main", 4)
    standard_four_problems = [
        problem for problem in legality_problems(standard_legal)
        if "copies" in problem]
    standard_over = Deck("Standard Over", "standard")
    standard_over.add(first, "main", 5)
    standard_five_problems = [
        problem for problem in legality_problems(standard_over)
        if "copies" in problem]
    # Singleton formats must reject a second copy through the same table.
    singleton = Deck("Singleton", "commander")
    singleton.add(first, "main", 2)
    singleton_problems = [
        problem for problem in legality_problems(singleton)
        if "copies" in problem]

    # Deck.add must refuse non-positive quantities: a zero-quantity entry would
    # count toward unique() but not total() and serialize as "0 Cardname".
    def _rejects_quantity(value):
        try:
            Deck().add(first, "main", value)
        except ValueError:
            return True
        return False

    quantity_guard = (
        _rejects_quantity(0)
        and _rejects_quantity(-1)
        and not _rejects_quantity(1))

    # DECK-001 / VER-012: every mutation must reject an unknown board before
    # touching deck state. Deleting the guard in add() is silent data loss --
    # a mistyped board reports success while the card lands in neither
    # mainboard nor sideboard and is unreachable from both.
    def _rejects_board(mutate):
        try:
            mutate(Deck())
        except ValueError:
            return True
        return False

    bad = "sideboadr"
    board_guard = (
        _rejects_board(lambda deck: deck.add(first, bad, 1))
        and _rejects_board(lambda deck: deck.set_qty(first["id"], bad, 2))
        and _rejects_board(lambda deck: deck.change_qty(first["id"], bad, 1))
        and _rejects_board(lambda deck: deck.remove(first["id"], bad))
        and _rejects_board(lambda deck: deck.move(first["id"], bad, "side"))
        and _rejects_board(lambda deck: deck.move(first["id"], "main", bad))
        and not _rejects_board(lambda deck: deck.add(first, "side", 1)))

    # An accepted bad board would not raise, so also prove the card cannot go
    # missing: whatever add() accepts has to be reachable from a real board.
    swallow_check = Deck()
    try:
        swallow_check.add(first, bad, 2)
    except ValueError:
        card_never_swallowed = True
    else:
        card_never_swallowed = (
            swallow_check.total("main") + swallow_check.total("side") == 2)

    illegal = Deck("Illegal", "modern")
    illegal.add(first, "main", 3)
    illegal.add(second, "side", 2)
    illegal.add(banned, "side", 1)
    illegal_problems = legality_problems(illegal)
    commander = Deck("Commander", "commander")
    commander.add(first, "main", 2)
    commander.add(artifact, "side", 1)
    commander_problems = legality_problems(commander)

    brawl_basic = _card(
        "brawl-land", "Brawl Plains", type_line="Basic Land — Plains",
        mana_cost="", identity="W")
    brawl_basic["legalities"].update({
        "brawl": "legal", "competitivebrawl": "legal"})
    brawl_spell = _card("brawl-spell", "Brawl Spell")
    brawl_spell["legalities"].update({
        "brawl": "legal", "competitivebrawl": "legal"})
    brawl = Deck("Brawl", "brawl")
    brawl.add(brawl_basic, "main", 99)
    brawl.add(brawl_spell, "main", 1)
    brawl_problems = legality_problems(brawl)
    short_brawl = Deck("Short Brawl", "brawl")
    short_brawl.add(brawl_basic, "main", 59)
    short_brawl.add(brawl_spell, "main", 1)
    short_brawl_problems = legality_problems(short_brawl)
    duplicate_brawl = Deck("Duplicate Brawl", "brawl")
    duplicate_brawl.add(brawl_basic, "main", 98)
    duplicate_brawl.add(brawl_spell, "main", 2)
    duplicate_brawl_problems = legality_problems(duplicate_brawl)

    competitive_brawl = Deck("Competitive Brawl", "competitivebrawl")
    competitive_brawl.add(brawl_basic, "main", 99)
    competitive_brawl.add(brawl_spell, "main", 1)
    competitive_brawl_problems = legality_problems(competitive_brawl)

    unknown_format_card = _card("future-format", "Future Format Card")
    unknown_format_card["legalities"] = {"neoformat": "legal"}
    unknown_format = Deck("Future Format", "neoformat")
    unknown_format.add(unknown_format_card, "main", 4)
    unknown_format_problems = legality_problems(unknown_format)

    missing_status_land = _card(
        "missing-land", "Missing Plains", type_line="Basic Land — Plains",
        mana_cost="", identity="W")
    missing_status_land["legalities"] = {}
    missing_status = Deck("Missing Status", "modern")
    missing_status.add(missing_status_land, "main", 60)
    missing_status_problems = legality_problems(missing_status)

    any_number = _card("rats", "Test Rats")
    any_number["oracle_text"] = (
        "A deck can have any number of cards named Test Rats.")
    any_number_deck = Deck("Any Number", "modern")
    any_number_deck.add(any_number, "main", 60)
    any_number_problems = legality_problems(any_number_deck)

    capped = _card("seven", "Test Seven")
    capped["oracle_text"] = (
        "A deck can have up to seven cards named Test Seven.")
    capped_land = _card(
        "cap-land", "Cap Plains", type_line="Basic Land — Plains",
        mana_cost="", identity="W")
    capped_ok = Deck("Capped OK", "modern")
    capped_ok.add(capped, "main", 7)
    capped_ok.add(capped_land, "main", 53)
    capped_ok_problems = legality_problems(capped_ok)
    capped_bad = Deck("Capped Bad", "modern")
    capped_bad.add(capped, "main", 8)
    capped_bad.add(capped_land, "main", 52)
    capped_bad_problems = legality_problems(capped_bad)

    sources_by_name = {
        name: (ROOT / name).read_text(encoding="utf-8")
        for name in (
            "mtgdb/deck/model.py", "mtgdb/deck/io.py", "mtgdb/deck/analysis.py",
            "mtgdb/deck/legality.py", "mtgdb/deck/arena_support.py",
            "mtgdb/deck/arena_limits.py", "mtgdb/deck/arena_tokens.py",
            "mtgdb/deck/arena_printings.py", "mtgdb/deck/arena_check.py",
            "mtgdb/ui/app.py", "mtgdb/ui/deck.py",
            "mtgdb/ui/deck_files.py", "mtgdb/ui/deck_stats.py")
    }
    imports = {
        name: _import_roots(source) for name, source in sources_by_name.items()
    }

    checks = {
        "Deck Format picker helper explains its purpose without provenance jargon": (
            "Set the format your deck is built for" in sources_by_name["mtgdb/ui/deck.py"]
            and "Scryfall legality catalog" not in sources_by_name["mtgdb/ui/deck.py"]),
        "a decklist opens whatever encoding it arrived in":
            _decklist_encoding_check(),
        "a (SET) 123 line keeps the printing it names":
            _paren_printing_check(),
        "one format is named the same way on every surface":
            _format_naming_check(),
        "statistics classify a card by the face it is played from":
            _double_faced_statistics_check(),
        "probability calculations never index a negative combination":
            _probability_guard_check(),
        "every deck mutation rejects an unknown board": (
            board_guard and card_never_swallowed),
        "face rules text reaches the copy-limit engine through JSON columns": (
            faced_limit is None and "Trample." in faced_text),
        "zero-quantity line skips one entry without aborting the import": (
            tolerant.total("main") == 6 and not tolerant_missing),
        "bare unquantified lines import as one copy": (
            bare.total("main") == 5),
        "unresolved bare lines stay quiet while quantified typos report": (
            bare_missing == ["Totally Absent Card"]),
        "Deck owns exact-printing state and board mutations": (
            BOARDS == ("main", "side")
            and model.total("main") == 18 and model.total("side") == 0
            and model.unique("main") == 4
            and next(
                entry for entry in model.entries()
                if entry["card"]["id"] == "spell-a")["qty"] == 3),
        "Deck entries retain deterministic name order": (
            [entry["card"]["name"] for entry in model.entries()]
            == sorted(
                [entry["card"]["name"] for entry in model.entries()])),
        "Deck.stats delegates without changing results": (
            method_stats == stats
            and stats["main_total"] == 18
            and stats["side_total"] == 0
            and stats["curve"][2] == 6
            and stats["curve"][3] == 2),
        "mana pips and sources retain quantity semantics": (
            pips == {"W": 6, "U": 6, "B": 0, "R": 0, "G": 6}
            and sources == {"W": 2, "U": 2, "B": 0, "R": 0, "G": 10}
            and source_total == 12),
        "format copy limits come from the verified rule table": (
            standard_four_problems == []
            and len(standard_five_problems) == 1
            and "limit 4" in standard_five_problems[0]
            and len(singleton_problems) == 1
            and "limit 1" in singleton_problems[0]),
        "Deck.add refuses non-positive quantities": quantity_guard,
        "probability calculations retain exact formulas": (
            math.isclose(
                hyper_at_least(60, 4, 7),
                1 - math.comb(56, 7) / math.comb(60, 7))
            # Independent closed form, NOT a second call to the function
            # under test: comparing hyper_between against itself is a tautology
            # that cannot fail, which previously let an off-by-one in its
            # summation bounds through undetected.
            and math.isclose(
                hyper_between(18, 10, 7, 2, 4),
                sum(math.comb(10, k) * math.comb(8, 7 - k)
                    for k in range(2, 5)) / math.comb(18, 7))
            # The deck-level helper must still route through that formula.
            and math.isclose(lands[3], hyper_between(18, 10, 7, 2, 4))
            # Inclusive bounds: a single-value window is one exact term.
            and math.isclose(
                hyper_between(18, 10, 7, 3, 3),
                math.comb(10, 3) * math.comb(8, 4) / math.comb(18, 7))
            and hyper_at_least(0, 4, 7) == 0.0),
        "curve and opening-hand calculations remain complete": (
            type_labels[0] == "Creatures"
            and type_curve[2]["Creatures"] == 6
            and type_curve[3]["Artifacts"] == 2
            and color_labels[-1] == "Colorless"
            and color_curve[3]["Colorless"] == 2
            and lands[:3] == (18, 10, 70 / 18)
            and len(hand) == 7),
        "UI summary calculations remain in the analysis domain": (
            average_mv == 18 / 8
            and draw_odds["deck_size"] == 18
            and draw_odds["copies"] == 6
            and math.isclose(
                draw_odds["opening"], hyper_at_least(18, 6, 7))
            and math.isclose(
                draw_odds["turn_3"], hyper_at_least(18, 6, 9))
            and math.isclose(
                draw_odds["turn_6"], hyper_at_least(18, 6, 12))),
        "TXT serialization preserves exact printings and metadata": (
            method_serialized == serialized
            and serialized.startswith("// Domain Test (modern)\n")
            and "[TST:10]" in serialized and "[ALT:44]" in serialized),
        "TXT function and compatibility method round-trip identically": (
            not missing and not method_missing
            and imported.name == method_imported.name == "Domain Test"
            and imported.fmt == method_imported.fmt == "modern"
            and [(entry["card"]["id"], entry["qty"], entry["board"])
                 for entry in imported.entries()]
            == [(entry["card"]["id"], entry["qty"], entry["board"])
                for entry in method_imported.entries()]),
        "numeric TXT header value is a card count, not the deck format": (
            count_header.name == "Lands" and count_header.fmt == "commander"
            and real_header.name == "Duels" and real_header.fmt == "pauper"),
        "section and authoritative set-tag behavior is preserved": (
            sectioned.total("main") == 1 and sectioned.total("side") == 2
            and section_missing == ["Missing Card [TST:999]"]
            and all(
                call[1].get("allowed_set_types") is None
                for call in resolver.calls if call[0] in {
                    "Hybrid Spell", "Mana Rock", "Missing Card"}
                and call[1].get("allowed_set_codes") == {"tst"})),
        "legality aggregates printings and checks sideboard-only cards": (
            any("5 copies" in problem for problem in illegal_problems)
            and any("BANNED" in problem for problem in illegal_problems)
            and any("requires at least 60" in problem
                    for problem in illegal_problems)),
        "singleton size, copies, and sideboard rules are preserved": (
            any("exactly 100" in problem for problem in commander_problems)
            and any("limit 1" in problem for problem in commander_problems)
            and any("no sideboard" in problem for problem in commander_problems)),
        "current Brawl is exactly 100 cards and singleton": (
            not brawl_problems
            and any("exactly 100" in problem for problem in short_brawl_problems)
            and any("limit 1" in problem for problem in duplicate_brawl_problems)),
        "Competitive Brawl uses the verified 100-card singleton profile": (
            not competitive_brawl_problems),
        "unknown formats fail closed instead of guessing Constructed rules": (
            len(unknown_format_problems) == 1
            and "construction rules are not verified" in unknown_format_problems[0]
            and not any("requires at least" in problem
                        for problem in unknown_format_problems)
            and not any("copies" in problem for problem in unknown_format_problems)),
        "missing Scryfall statuses are reported as unverified": (
            any("Scryfall legality status is unavailable" in problem
                for problem in missing_status_problems)),
        "Oracle copy-limit text overrides generic and singleton defaults": (
            not any("copies" in problem for problem in any_number_problems)
            and not any("copies" in problem for problem in capped_ok_problems)
            and any("limit 7" in problem for problem in capped_bad_problems)),
        "type classification priority is preserved": (
            classify_type("Artifact Creature — Golem") == "Creatures"
            and classify_type("Land Creature — Forest Dryad") == "Lands"),
        "all deck-domain modules are Tk and SQLite free": all(
            "tkinter" not in imports[name] and "sqlite3" not in imports[name]
            for name in (
                "mtgdb/deck/model.py", "mtgdb/deck/io.py", "mtgdb/deck/analysis.py",
                "mtgdb/deck/legality.py", "mtgdb/deck/arena_support.py",
                "mtgdb/deck/arena_limits.py", "mtgdb/deck/arena_tokens.py",
                "mtgdb/deck/arena_printings.py", "mtgdb/deck/arena_check.py")),
        "model owns no parser, probability, randomness, or legality imports": (
            not ({"re", "json", "math", "random"} & imports["mtgdb/deck/model.py"])),
        "Arena lookup keys mirror the Proxic Arena name rule":
            _arena_name_key_check(),
        "an unreadable Arena rules document reports nothing rather than raising":
            _arena_unreadable_rules_check(),
        "a saved deck reports only the names Proxic Arena cannot play":
            _arena_deck_report_check(),
        "Arena export limits follow Arena's format: size, copies, side decks":
            _arena_limits_check(),
        "the built-in Arena rules are Arena's own document, read in full":
            _arena_shipped_rules_check(),
        "the program knows nothing of the engine Arena runs":
            _no_engine_knowledge_check(),
        "token cards travel as pictures, counted apart from the cards":
            _arena_token_parts_check(),
        "the tokens a deck's cards can create are found once each, main deck only":
            _arena_tokens_check(),
        "the Arena rules location belongs to the shared asset owner": (
            'ARENA_DECK_RULES_FILE = "arena/deck_rules.json"'
            in (ROOT / "mtgdb/ui/assets.py").read_text(encoding="utf-8")
            # Neither the consumer nor the reader spells the path itself.
            and "arena/deck_rules.json" not in
            (ROOT / "mtgdb/ui/arena_export.py").read_text(encoding="utf-8")
            and "arena/deck_rules.json" not in
            sources_by_name["mtgdb/deck/arena_support.py"]),
        "production UI imports each deck responsibility from its owner": (
            "from mtgdb.deck.model import Deck" in sources_by_name["mtgdb/ui/app.py"]
            and ("from mtgdb.deck.io import deck_from_text, "
                 "read_deck_text, save_deck_text")
            in sources_by_name["mtgdb/ui/deck_files.py"]
            and "from mtgdb.deck.model import Deck" in sources_by_name["mtgdb/ui/deck.py"]
            and "from mtgdb.deck.analysis import" in sources_by_name["mtgdb/ui/deck_stats.py"]
            and "from mtgdb.deck.legality import legality_problems"
            in sources_by_name["mtgdb/ui/deck_stats.py"]
            and "from mtgdb.deck.io import" not in sources_by_name["mtgdb/ui/app.py"]
            and "from mtgdb.deck.analysis import" not in sources_by_name["mtgdb/ui/app.py"]
            and "from mtgdb.deck.legality import" not in sources_by_name["mtgdb/ui/app.py"]),
    }

    ok = True
    for label, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok &= bool(passed)
    print("\nDECK ARCHITECTURE:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
