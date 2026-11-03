"""Audit an entire Scryfall Default Cards bulk file.

This is deliberately separate from fast regressions. It streams the source,
builds a real temporary CardDB, compares discovery output with source truth, and
optionally writes a Markdown evidence report.

Usage:
  python tests/taxonomy_audit.py PATH_TO_DEFAULT_CARDS [--online-catalogs]
  python tests/taxonomy_audit.py --current [--report REPORT.md]
  python tests/taxonomy_audit.py PATH --online-catalogs --report REPORT.md

``--current`` discovers and downloads Scryfall's current Default Cards export and
refreshes all trusted catalogs before auditing it.

The weekly workflow also passes ``--snapshot-out`` (this run's values, kept as
an artifact), ``--previous`` (last run's, to list what is new), and
``--summary-out``/``--notice-out`` (the run page's summary and one-line
notice).
"""
import argparse
import collections
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mtgdb.database.bulk_import import iter_card_objects
from mtgdb.database.authorities import (
    SUBTYPE_AUTHORITIES, UPSTREAM_FINGERPRINT_META_KEY,
)
from mtgdb.database.constants import ART_LAYOUTS, NON_CARD_LAYOUTS
from mtgdb.database.db import CardDB
from mtgdb.database.semantics import _BFM_COMPLETE_TYPE_LINE
from mtgdb.database import semantics as S
from mtgdb.database.schema import RULES_SUPERTYPES_META_KEY
from mtgdb.database.sync import API_BASE, DatabaseSyncService
import mtgdb.core.net as net
from mtgdb.ui.mana import bundled_symbol_keys, resolve_symbol_key


def download_current_default_cards(directory):
    """Discover and download Scryfall's current Default Cards bulk export."""
    metadata = net.get_json(f"{API_BASE}/bulk-data")
    items = metadata.get("data", []) if isinstance(metadata, dict) else []
    item = next(
        (value for value in items
         if isinstance(value, dict) and value.get("type") == "default_cards"),
        None,
    )
    if not item:
        raise RuntimeError("Scryfall bulk-data response has no default_cards item")
    url = item.get("jsonl_download_uri") or item.get("download_uri")
    if not url:
        raise RuntimeError("Scryfall default_cards metadata has no download URI")
    basename = os.path.basename(str(url).split("?", 1)[0]) or "default-cards.json"
    path = os.path.join(directory, basename)
    net.download(str(url), path)
    return path, item


def source_inventory(path):
    counts = collections.Counter()
    ids = set()
    sets = {}
    discoverable_sets = set()
    keywords = set()
    printing_keys = collections.Counter()
    duplicate_ids = 0
    started = time.monotonic()
    for card in iter_card_objects(path):
        counts["objects"] += 1
        card_id = str(card.get("id") or "")
        name = str(card.get("name") or "")
        set_code = str(card.get("set") or "")
        set_name = str(card.get("set_name") or "")
        set_type = str(card.get("set_type") or "")
        collector = str(card.get("collector_number") or "")
        lang = str(card.get("lang") or "")
        if not card_id:
            counts["missing_id"] += 1
        elif card_id in ids:
            duplicate_ids += 1
        else:
            ids.add(card_id)
        for field, value in (("name", name), ("set_code", set_code),
                             ("set_name", set_name), ("set_type", set_type),
                             ("collector", collector)):
            if not value:
                counts[f"missing_{field}"] += 1
        if set_code:
            sets.setdefault(set_code, (set_name, set_type))
            if card.get("layout") not in ART_LAYOUTS:
                discoverable_sets.add(set_code)
        keywords.update(str(value) for value in (card.get("keywords") or [])
                        if str(value))
        if name and set_code and collector:
            printing_keys[(name.casefold(), set_code.casefold(), collector,
                           lang.casefold())] += 1
        if card.get("card_faces"):
            counts["multi_face_objects"] += 1
        if card.get("all_parts"):
            counts["related_objects"] += 1
    exact_collisions = sum(1 for value in printing_keys.values() if value > 1)
    return {
        "counts": counts, "ids": ids, "sets": sets,
        "discoverable_sets": discoverable_sets, "keywords": keywords,
        "duplicate_ids": duplicate_ids, "exact_printing_collisions": exact_collisions,
        "seconds": time.monotonic() - started,
    }


# Below this share of cards with a picture, Scryfall's image data changed shape.
PICTURE_FLOOR = 0.99


def picture_coverage(db):
    """Count playable cards and how many carry a PNG and an art-crop link."""
    marks = ",".join("?" for _ in NON_CARD_LAYOUTS)
    row = db.conn.execute(
        "SELECT COUNT(*), "
        "SUM(COALESCE(image_png, '') <> ''), "
        "SUM(COALESCE(image_art_crop, '') <> '') "
        f"FROM cards WHERE COALESCE(layout, '') NOT IN ({marks})",
        tuple(NON_CARD_LAYOUTS)).fetchone()
    return {"cards": row[0] or 0, "png": row[1] or 0, "art_crop": row[2] or 0}


# Card types whose cards already carry subtypes that Scryfall keeps no list
# for (Plane -- Dominaria, Emblem -- Ajani, Dungeon -- Undercity). The app files
# subtypes only under the groups in SUBTYPE_AUTHORITIES; a card type outside
# both is a new subtype group the Subtype filter cannot offer.
KNOWN_SUBTYPE_GROUPS_WITHOUT_LIST = frozenset(
    {"Conspiracy", "Dungeon", "Emblem", "Plane"})

COST_SYMBOL = re.compile(r"\{([^}]+)\}")


def subtype_groups(db):
    """Find subtype groups the app has no list for (VER-009).

    Returns the card types seen with subtypes that no app subtype list covers,
    the new ones among them, and any Scryfall ``<type>-types`` catalog that
    exists for a card type the app keeps no list for.
    """
    card_types = db.catalog("card-types")
    covered = {value for authority in SUBTYPE_AUTHORITIES
               for value in authority.applies_to}
    without_list = set()
    for (type_line,) in db.conn.execute("SELECT type_line FROM cards"):
        for face in str(type_line or "").split(" // "):
            left, dash, right = face.partition(" — ")
            if not dash or not right.strip():
                continue
            words = left.split()
            mine = [value for value in card_types if value in words]
            if mine and not any(value in covered for value in mine):
                without_list.update(mine)
    unused_lists, unchecked = [], []
    for value in card_types:
        if value in covered:
            continue
        name = f"{value.lower()}-types"
        try:
            net.get_json(f"{API_BASE}/catalog/{name}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                unchecked.append(name)
            continue
        except (OSError, ValueError):
            unchecked.append(name)
            continue
        unused_lists.append(name)
    return {
        "without_list": sorted(without_list),
        "new_without_list": sorted(without_list - KNOWN_SUBTYPE_GROUPS_WITHOUT_LIST),
        "unused_lists": unused_lists,
        "unchecked": unchecked,
    }


def cost_symbols(db):
    """Mana symbols in playable, non-joke cards' costs: {symbol: (cards, example)}."""
    marks = ",".join("?" for _ in ART_LAYOUTS)
    found = {}
    rows = db.conn.execute(
        "SELECT name, mana_cost, back_mana_cost FROM cards "
        "WHERE COALESCE(set_type, '') <> 'funny' "
        f"AND COALESCE(layout, '') NOT IN ({marks})", tuple(ART_LAYOUTS))
    for name, cost, back in rows:
        for token in set(COST_SYMBOL.findall(f"{cost or ''}{back or ''}")):
            cards, example = found.get(token, (0, name))
            found[token] = (cards + 1, example)
    return found


def joke_symbols():
    """Symbols Scryfall marks as funny ({HW}, {L} on playtest cards); empty if unknown."""
    try:
        data = net.get_json(f"{API_BASE}/symbology")
    except (OSError, ValueError):
        return set()
    return {str(item.get("symbol", "")).strip("{}")
            for item in (data.get("data", []) if isinstance(data, dict) else [])
            if isinstance(item, dict) and item.get("funny")}


def missing_symbol_pictures(symbols, jokes=frozenset()):
    """The real cost symbols the app draws as a grey circle, having no picture."""
    keys = bundled_symbol_keys()
    return {f"{{{token}}}": value for token, value in sorted(symbols.items())
            if token not in jokes and resolve_symbol_key(token, keys) is None}


def snapshot(db, sets, card_types, supertypes, subtypes, abilities, symbols):
    """This run's values, by the names the run page shows them under."""
    try:
        upstream = json.loads(db.get_meta(UPSTREAM_FINGERPRINT_META_KEY, "{}") or "{}")
    except (TypeError, ValueError):
        upstream = {}
    if not isinstance(upstream, dict):
        upstream = {}
    return {
        "Card types": list(card_types),
        "Supertypes": list(supertypes),
        "Subtypes": [f"{value} ({category})" for value, category in subtypes],
        "Keywords and ability words": [value for value, _category in abilities],
        "Sets": [f"{name} ({str(code).upper()})" for code, name in sets],
        "Formats": list(upstream.get("format_keys", [])),
        "Rarities": list(upstream.get("rarities", [])),
        "Set types": list(upstream.get("set_types", [])),
        "Card shapes": list(upstream.get("layouts", [])),
        "Legality statuses": list(upstream.get("legality_statuses", [])),
        "Mana symbols in costs": [f"{{{token}}}" for token in sorted(symbols)],
    }


def news(current, previous):
    """What each list gained since ``previous``; None when there is none.

    Only lists the previous run also kept are compared, so adding a list
    later does not report its whole contents as new.
    """
    if not isinstance(previous, dict):
        return None
    gained = {}
    for key, values in current.items():
        if not isinstance(previous.get(key), list):
            continue
        new = sorted(set(values) - set(previous[key]), key=str.casefold)
        if new:
            gained[key] = new
    return gained


def _listed(values, limit=20):
    shown = ", ".join(values[:limit])
    return shown + (f", and {len(values) - limit} more" if len(values) > limit else "")


def news_lines(result):
    gained = result["news"]
    if gained is None:
        return ["Nothing to compare with yet: the next run lists what is new "
                "since this one."]
    if not gained:
        return ["Nothing new."]
    return [f"- {key} ({len(values)}): {_listed(values)}"
            for key, values in gained.items()]


def symbol_lines(result):
    missing = result["missing_symbols"]
    if not missing:
        return ["Every mana symbol in a card's cost has a picture."]
    return [f"- {symbol} on {cards:,} card{'s' if cards != 1 else ''}, e.g. {example}"
            for symbol, (cards, example) in missing.items()]


def run_summary(result):
    """The run page: a short Markdown summary and one plain notice line."""
    checks = result["checks"]
    passed = all(checks.values())
    count = (f"{result['source']['objects']:,} printings in "
             f"{result['source_sets']:,} sets")
    data = (result.get("bulk_updated_at") or "")[:10] or "unknown"
    missing = result["missing_symbols"]
    gained = result["news"] or {}
    if not passed:
        heading = "## ⚠️ Needs attention"
        intro = "Scryfall's card data has something the Deck Builder can't handle yet."
    elif missing:
        heading = "## 👀 Something to look at"
        intro = ("Nothing urgent: the Deck Builder handles Scryfall's card data, "
                 "but some mana symbols have no picture yet.")
    else:
        heading = "## ✅ Nothing to do"
        intro = ("Scryfall's card data has nothing new the Deck Builder can't "
                 "handle, so no update is needed.")
    lines = [heading, "", intro, "", f"- Checked: {count}",
             f"- Scryfall data from: {data}", ""]
    if not passed:
        lines += ["### What failed", ""]
        lines += [f"- {label}" for label, ok in checks.items() if not ok]
        lines.append("")
    lines += ["### New since the last check", "", *news_lines(result), "",
              "### Mana symbols without a picture", "", *symbol_lines(result), "",
              "The full report is the **scryfall-taxonomy-audit** file at the "
              "bottom of this page."]
    summary = "\n".join(lines) + "\n"
    title = ("Needs attention" if not passed
             else "Something to look at" if missing else "Nothing to do")
    notice = intro + f" Checked {count} (Scryfall data of {data})."
    if gained:
        notice += " New since the last check: " + "; ".join(
            f"{key} {len(values)}" for key, values in gained.items()) + "."
    if missing:
        notice += " Mana symbols without a picture: " + ", ".join(missing) + "."
    return summary, f"{title}::{notice}"


def markdown(result):
    source = result["source"]
    pictures = result["pictures"]
    checks = result["checks"]
    lines = [
        "# Full Scryfall Taxonomy & Printing Audit",
        "",
        f"Bulk source: `{result['bulk_path']}`  ",
        f"Scryfall bulk updated: `{result.get('bulk_updated_at') or 'local file / unknown'}`  ",
        f"Audited: {result['audited_at']}  ",
        f"Result: **{'PASS' if all(checks.values()) else 'REVIEW REQUIRED'}**",
        "",
        "## Coverage",
        "",
        "| Area | Count |",
        "|---|---:|",
        f"| Card printing objects | {source['objects']:,} |",
        f"| Unique printing IDs | {result['db_cards']:,} |",
        f"| Source set codes | {result['source_sets']:,} |",
        f"| Searchable non-art set codes shown | {result['sets']:,} |",
        f"| Art-series-only set codes intentionally hidden | {result['art_only_sets']:,} |",
        f"| Set types | {result['set_types']:,} |",
        f"| Trusted Card Type names | {result['card_types']:,} |",
        f"| Rules-defined observed Supertype names | {result['supertypes']:,} |",
        f"| Trusted catalog-backed subtype names | {result['subtypes']:,} |",
        f"| Trusted Mechanics labels | {result['keywords']:,} |",
        f"| Multi-face objects | {source['multi_face_objects']:,} |",
        f"| Objects with related parts | {source['related_objects']:,} |",
        f"| Cards with a print picture (PNG) | {pictures['png']:,} of {pictures['cards']:,} |",
        f"| Cards with an art crop | {pictures['art_crop']:,} of {pictures['cards']:,} |",
        "",
        "## Category breakdown",
        "",
        "### Subtypes",
        "",
    ]
    for name, count in sorted(result["subtype_categories"].items()):
        lines.append(f"- {name}: {count:,}")
    lines += ["", "### Mechanics / keyword catalogs", ""]
    for name, count in sorted(result["keyword_categories"].items()):
        lines.append(f"- {name}: {count:,}")
    compatibility = result.get("compatibility") or {}
    lines += [
        "", "## Upstream compatibility", "",
        f"- Fingerprint: `{compatibility.get('fingerprint') or 'unavailable'}`",
        "- Unknown layouts: " + (", ".join(compatibility.get("unknown_layouts", [])) or "none"),
        "- Unknown legality statuses: " + (
            ", ".join(compatibility.get("unknown_legality_statuses", [])) or "none"),
        "- Subtype authority gaps (diagnostic only): " + (
            ", ".join(compatibility.get("subtype_authority_gaps", [])) or "none"),
        "- Mechanic authority gaps (diagnostic only): " + (
            ", ".join(compatibility.get("mechanic_authority_gaps", [])) or "none"),
        "- Card types with subtypes Scryfall keeps no list for: " + (
            ", ".join(result["subtype_groups"]["without_list"]) or "none"),
        "- New ones among them: " + (
            ", ".join(result["subtype_groups"]["new_without_list"]) or "none"),
        "- Scryfall subtype lists the app does not use: " + (
            ", ".join(result["subtype_groups"]["unused_lists"]) or "none"),
        "- Subtype lists that could not be looked up: " + (
            ", ".join(result["subtype_groups"]["unchecked"]) or "none"),
        "", "## New since the last check", "", *news_lines(result),
        "", "## Mana symbols without a picture", "", *symbol_lines(result),
        "", "## Integrity checks", "",
    ]
    for label, passed in checks.items():
        lines.append(f"- {'PASS' if passed else 'FAIL'} — {label}")
    lines += [
        "", "## Notes", "",
        "- B.F.M. collector numbers 28 and 29 remain separate printing IDs, "
        "but both receive the reconstructed official hyphenated creature subtype.",
        "- `raw_type_line`, `related_parts`, and `card_faces` preserve Scryfall's "
        "source representation for future reparsing.",
        "- Card Type, Subtype, and Mechanics picker vocabulary is the intersection "
        "of cached Scryfall catalogs and values actually observed in the local card "
        "snapshot; Supertypes instead use the strictly parsed current Wizards "
        "Comprehensive Rules taxonomy plus observed Scryfall type lines.",
        "- Uncataloged words remain available to explicit query callers but are not "
        "invented as interactive picker vocabulary.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("bulk_path", nargs="?")
    parser.add_argument("--current", action="store_true",
                        help="download and audit Scryfall's current Default Cards export")
    parser.add_argument("--online-catalogs", action="store_true")
    parser.add_argument("--report")
    parser.add_argument("--snapshot-out",
                        help="write this run's values as JSON, for the next run")
    parser.add_argument("--previous",
                        help="last run's --snapshot-out, to list what is new")
    parser.add_argument("--summary-out", help="write the run page's Markdown summary")
    parser.add_argument("--notice-out", help="write the run page's notice line")
    args = parser.parse_args(argv)
    if args.current and args.bulk_path:
        parser.error("provide either bulk_path or --current, not both")
    if not args.current and not args.bulk_path:
        parser.error("bulk_path is required unless --current is used")

    tmpdir = tempfile.mkdtemp(prefix="mtg-taxonomy-audit-")
    downloaded = False
    bulk_meta = {}
    if args.current:
        bulk_path, bulk_meta = download_current_default_cards(tmpdir)
        downloaded = True
    else:
        bulk_path = args.bulk_path

    inventory = source_inventory(bulk_path)
    db = CardDB(os.path.join(tmpdir, "cards.db"))
    if args.online_catalogs or args.current:
        refreshed = DatabaseSyncService(db).refresh_catalogs()
    else:
        refreshed = 0
    db_cards = db.load_cards(iter_card_objects(bulk_path), minimum_count=1000)
    pictures = picture_coverage(db)
    compatibility = db.refresh_compatibility_diagnostics()

    discovered_sets = db.sets()
    set_types = db.set_types()
    card_types = db.card_types()
    supertypes = db.supertypes()
    subtypes = db.subtype_catalog()
    abilities = db.keyword_catalog()
    groups = subtype_groups(db)
    symbols = cost_symbols(db)
    missing_symbols = missing_symbol_pictures(symbols, joke_symbols())
    values = snapshot(db, discovered_sets, card_types, supertypes, subtypes,
                      abilities, symbols)
    previous = None
    if args.previous and os.path.isfile(args.previous):
        try:
            with open(args.previous, encoding="utf-8") as handle:
                previous = json.load(handle)
        except (OSError, ValueError):
            previous = None
    try:
        rules_supertypes = json.loads(
            db.get_meta(RULES_SUPERTYPES_META_KEY, "[]"))
    except (TypeError, ValueError):
        rules_supertypes = []
    if not isinstance(rules_supertypes, list):
        rules_supertypes = []
    rules_source_url = db.get_meta("rules:supertypes_source_url", "")
    rules_document_hash = db.get_meta("rules:supertypes_document_sha256", "")
    subtype_categories = collections.Counter(category for _value, category in subtypes)
    keyword_categories = collections.Counter(category for _value, category in abilities)
    bfm = db.search(name="B.F.M. (Big Furry Monster)", set_codes=["ugl"])
    bfm_subtype = (
        "The-Biggest-Baddest-Nastiest-Scariest-Creature-You'll-Ever-See")
    source = inventory["counts"]
    checks = {
        "every source object has a unique stored Scryfall ID": (
            inventory["duplicate_ids"] == 0 and db_cards == len(inventory["ids"])),
        "no source card is missing its required name": source["missing_name"] == 0,
        "every non-art set code is represented in discovery": (
            {code for code, _name in discovered_sets} ==
            inventory["discoverable_sets"]),
        "art-series-only set exclusions exactly explain hidden set codes": (
            set(inventory["sets"]) - inventory["discoverable_sets"] ==
            set(inventory["sets"]) - {code for code, _name in discovered_sets}),
        "set names and set types are populated": (
            source["missing_set_code"] == 0 and source["missing_set_name"] == 0
            and source["missing_set_type"] == 0),
        "collector numbers are populated": source["missing_collector"] == 0,
        "no name/set/collector/language printing identity collides": (
            inventory["exact_printing_collisions"] == 0),
        "every trusted Mechanic is an observed Scryfall card keyword": (
            {S._type_key(value) for value, _category in abilities}
            <= {S._type_key(value) for value in inventory["keywords"]}),
        "trusted Card Types are authorized by Scryfall card-types catalog": (
            bool(db.catalog("card-types"))
            and {S._type_key(value) for value in card_types}
                <= {S._type_key(value) for value in db.catalog("card-types")}),
        "trusted Supertypes are Wizards-rules-defined and observed": (
            bool(rules_supertypes)
            and {S._type_key(value) for value in supertypes}
                <= {S._type_key(value) for value in rules_supertypes}
            and all(db.search(supertypes=[value]) for value in supertypes)),
        "Supertype rules provenance is recorded from Wizards": (
            rules_source_url.startswith("https://")
            and len(rules_document_hash) == 64),
        "trusted Mechanics are authorized by Scryfall catalogs": (
            {S._type_key(value) for value, _category in abilities}
            <= {S._type_key(value) for name in (
                "keyword-abilities", "keyword-actions", "ability-words")
                for value in db.catalog(name)}),
        "trusted Subtypes are authorized by Scryfall subtype catalogs": (
            {S._type_key(value) for value, _category in subtypes}
            <= {S._type_key(value) for name in (
                "artifact-types", "battle-types", "creature-types",
                "enchantment-types", "land-types", "planeswalker-types",
                "spell-types") for value in db.catalog(name)}),
        "case-only source variants do not duplicate picker names": (
            len(card_types) == len({S._type_key(value) for value in card_types}) and
            len(subtypes) == len({S._type_key(value) for value, _ in subtypes}) and
            len(abilities) == len({S._type_key(value) for value, _ in abilities})),
        "B.F.M. remains two distinct collector-number printings": (
            {card["collector_number"] for card in bfm} == {"28", "29"}),
        "B.F.M. subtype follows catalog trust without fragment guessing": (
            ((bfm_subtype in db.catalog("creature-types")
              and dict(subtypes).get(bfm_subtype) == "Creature")
             or (bfm_subtype not in db.catalog("creature-types")
                 and bfm_subtype not in dict(subtypes)))
            and all(card["type_line"] == _BFM_COMPLETE_TYPE_LINE for card in bfm)),
        "B.F.M. fragment words do not leak into card-type names": (
            not ({"Scariest", "You'll", "Ever", "See"} & set(card_types))),
        "all trusted Mechanics receive a category": (
            len(abilities) == sum(keyword_categories.values())),
        "all parsed subtype labels receive a category": (
            len(subtypes) == sum(subtype_categories.values())),
        "current Scryfall layouts are understood or deliberately classified": (
            not compatibility.get("unknown_layouts")),
        "current Scryfall legality statuses are understood": (
            not compatibility.get("unknown_legality_statuses")),
        # A new card type with its own subtypes, or a Scryfall subtype list the
        # app does not read, needs a new entry in SUBTYPE_AUTHORITIES.
        "no card type has subtypes the app keeps no list for": (
            not groups["new_without_list"] and not groups["unused_lists"]
            and not groups["unchecked"]),
        # Printing needs each card's PNG and the Proxic Arena send needs its
        # art crop. A few new cards may still lack pictures; a renamed or
        # dropped Scryfall image field empties them all.
        "nearly every card still has a print picture and an art crop": (
            pictures["cards"] > 0
            and pictures["png"] >= PICTURE_FLOOR * pictures["cards"]
            and pictures["art_crop"] >= PICTURE_FLOOR * pictures["cards"]),
    }

    result = {
        "bulk_path": os.path.basename(bulk_path),
        "bulk_updated_at": bulk_meta.get("updated_at", "") if bulk_meta else "",
        "bulk_download_uri": (bulk_meta.get("jsonl_download_uri")
                              or bulk_meta.get("download_uri", "")) if bulk_meta else "",
        "audited_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "source": dict(source), "db_cards": db_cards,
        "source_sets": len(inventory["sets"]), "sets": len(discovered_sets),
        "art_only_sets": len(set(inventory["sets"]) -
                             inventory["discoverable_sets"]),
        "set_types": len(set_types),
        "card_types": len(card_types), "supertypes": len(supertypes),
        "rules_supertypes": list(rules_supertypes),
        "rules_source_url": rules_source_url,
        "subtypes": len(subtypes),
        "keywords": len(abilities), "catalogs_refreshed": refreshed,
        "subtype_categories": dict(subtype_categories),
        "keyword_categories": dict(keyword_categories), "checks": checks,
        "inventory_seconds": round(inventory["seconds"], 2),
        # markdown() renders the Upstream compatibility section from this; without
        # it the report always showed "unavailable"/"none" even when a check for
        # an unknown layout or legality status failed.
        "compatibility": compatibility,
        "pictures": pictures,
        "subtype_groups": groups,
        "missing_symbols": missing_symbols,
        "news": news(values, previous),
    }
    report = markdown(result)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(report)
    if args.snapshot_out:
        with open(args.snapshot_out, "w", encoding="utf-8") as handle:
            json.dump(values, handle, ensure_ascii=False, indent=1, sort_keys=True)
    summary, notice = run_summary(result)
    if args.summary_out:
        with open(args.summary_out, "w", encoding="utf-8") as handle:
            handle.write(summary)
    if args.notice_out:
        with open(args.notice_out, "w", encoding="utf-8") as handle:
            handle.write(notice)
    print(report)
    db.close()
    if downloaded:
        shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        # Local-file audits still use a temporary database only.
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
