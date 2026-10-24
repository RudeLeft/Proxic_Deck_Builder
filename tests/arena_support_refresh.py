"""Regenerate assets/arena/supported_cards.json from Forge's card scripts and editions.

Proxic Arena resolves a card by name and then checks its printing, so the Deck
Builder can only warn about a card Arena will not find, and pick a printing
Arena will accept, if it holds both lists: the card names Forge has scripts
for, and the printings Forge has of each. The two applications are separate
projects and neither reads the other's tree at runtime, so those lists ship as
a generated asset and are refreshed by this script whenever the Forge Arena
plays with is updated (DECK-012). Give it the card scripts and editions of the
SAME Forge release, the one Arena runs: Arena refuses a card or printing that
only Forge's unreleased work has, so a list built from newer Forge would call
such a card playable and keep such a printing.

This is a maintenance tool, not a gate: it reads a tree outside this
repository and rewrites a source asset, so it is deliberately named outside the
`test_*.py` pattern the release suite runs. `tests/test_deck_architecture.py`
checks its contract without needing the Forge tree, the way
`test_taxonomy_audit_contract.py` does for the taxonomy audit.

Usage, from the project root. Both locations are always given, since nothing
in this repository may depend on where another project keeps its files:

    python tests/arena_support_refresh.py --cardsfolder <path to cardsfolder> --editions <path to editions>
    python tests/arena_support_refresh.py --cardsfolder <path> --check
    python tests/arena_support_refresh.py --cardsfolder <path> --editions <path> --check

`--check` writes nothing and exits nonzero when the shipped asset no longer
matches, which is the question to ask after bumping Forge. Without
`--editions` it compares the names alone, which is what the weekly upstream
check asks; a write always needs both, because the asset holds both.

The printing rule is Forge's own, read from its source (`CardEdition`,
`CardDb`) and measured against Proxic Arena's worker: a requested set code is
looked up as an edition's `Code`, then as any edition's `Alias` or `Code2`
(case-insensitive), and the printing must then exist in that edition with
exactly that collector number. An edition's `ScryfallCode` is NOT one of the
codes Forge accepts -- "UGIN" for Ugin's Fate is refused where "UGF" resolves
-- so it is recorded only to find the Scryfall printing that carries a Forge
printing's picture.
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mtgdb.deck.arena_support import (
    ASSET_VERSION, PRINTING_SEPARATOR, front_face_name, normalize_card_name,
)
from mtgdb.ui.assets import ARENA_SUPPORTED_CARDS_FILE

NAME_PREFIX = "Name:"
# Forge's alternate printed names, `Variant:<variant>:FlavorName:<name>`.
VARIANT_PREFIX = "Variant:"
FLAVOR_NAME_FIELD = "FlavorName"
# A script Arena ships but marks as a variant it cannot play. The file exists,
# so a name test alone would call the card playable.
UNSUPPORTED_MARKER = "<Unsupported Variant>"
# A refresh that collapses to a handful of names or printings means the tree
# was wrong or unreadable, and MUST NOT be allowed to replace a complete asset.
MINIMUM_CREDIBLE_NAMES = 10000
MINIMUM_CREDIBLE_PRINTINGS = 50000
# How many added names a comparison prints before counting the remainder.
LISTED_NAME_LIMIT = 40

# The edition-file sections Forge reads as printings with collector numbers
# (`CardEdition.EditionSectionWithCollectorNumbers`).
PRINTING_SECTIONS = frozenset({
    "cards", "special slot", "precon product", "borderless", "etched",
    "showcase", "full art", "extended art", "alternate art", "retro frame",
    "buy a box", "promo", "prerelease promo", "bundle", "box topper",
    "jumpstart", "rebalanced", "eternal", "conjured", "scheme", "printsheets",
})
# Forge's `CardEdition.Reader.CARD_PATTERN`, verbatim: collector number,
# rarity, name, artist, and `${...}` parameters.
CARD_LINE = re.compile(
    r"(^(.?[0-9A-Z-]+\S*[A-Z]*)\s)?(([SCURML])\s)?([^@$]+)( @([^$]*))?"
    r"( \$\{(.+)\})?$")
VARIANT_PARAMETER = re.compile(r'"variant"\s*:\s*"([^"]+)"', re.IGNORECASE)


def read_scripts(cardsfolder):
    """Return ``(keys, alternate names, variants)`` from Forge's card scripts.

    ``keys`` holds the lookup key of every card name Forge ships a script for.
    Each script holds one `Name:` line per face, and a card is filed under its
    front face, so both faces' names are read and reduced to the same key the
    runtime uses. Reading the names rather than the filenames is deliberate:
    the filename is a lossy spelling of the name and cannot be reversed.

    An edition may list a card by a face or flavor name rather than its main
    name, and Forge files that printing under the card; ``alternate`` maps
    such a name's key to the card's. ``variants`` holds, per card key, the
    functional variants its script declares, because Forge skips an edition
    entry for a variant the script does not support.
    """
    folder = Path(cardsfolder)
    if not folder.is_dir():
        raise SystemExit(f"Not a directory: {folder}")

    keys = set()
    alternate = {}
    variants = {}
    scripts = 0
    skipped = 0
    for script in sorted(folder.rglob("*.txt")):
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SystemExit(f"Cannot read {script}: {exc}") from exc
        scripts += 1
        if UNSUPPORTED_MARKER in text:
            skipped += 1
            continue
        primary = None
        for line in text.splitlines():
            if line.startswith(NAME_PREFIX):
                key = _key(line[len(NAME_PREFIX):])
                if not key:
                    continue
                keys.add(key)
                if primary is None:
                    primary = key
                else:
                    alternate.setdefault(key, primary)
            elif line.startswith(VARIANT_PREFIX) and primary is not None:
                parts = line[len(VARIANT_PREFIX):].split(":", 2)
                variants.setdefault(primary, set()).add(parts[0].strip())
                if len(parts) == 3 and parts[1] == FLAVOR_NAME_FIELD:
                    flavor = _key(parts[2])
                    if flavor and flavor != primary:
                        alternate.setdefault(flavor, primary)
    print(f"  scripts read:            {scripts}")
    print(f"  skipped as unsupported:  {skipped}")
    print(f"  distinct lookup keys:    {len(keys)}")
    if len(keys) < MINIMUM_CREDIBLE_NAMES:
        raise SystemExit(
            f"Found only {len(keys)} card names, below the credible minimum "
            f"of {MINIMUM_CREDIBLE_NAMES}; refusing to replace the shipped "
            "asset. Check --cardsfolder.")
    return keys, alternate, variants


def collect_supported_keys(cardsfolder):
    """Return the lookup keys for every card name Forge ships a script for."""
    return read_scripts(cardsfolder)[0]


def _key(name):
    return normalize_card_name(front_face_name(str(name).strip()))


def _sections(text):
    """Forge's `FileSection.parseSections`: ``[name]`` headers, `#` comments."""
    sections = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
        elif current is not None and line:
            sections[current].append(line)
    return sections


def read_editions(editions_folder, keys, alternate, variants):
    """Return ``(printings, set_codes, editions)`` from Forge's edition files.

    ``printings`` maps each card key to the ``"<EDITION> <NUMBER>"`` printings
    Forge registers for it; an entry naming no card Forge has a script for is
    dropped, as Forge drops it. ``set_codes`` maps every code Forge's lookup
    accepts to its edition, with Forge's precedence: an edition's own code
    first, then aliases and second codes in Forge's iteration order, the last
    one winning, as Forge's alias map does. ``editions`` gives each edition's
    Scryfall code, type and date, which the export uses to fetch a picture and
    to prefer an ordinary printing.
    """
    folder = Path(editions_folder)
    if not folder.is_dir():
        raise SystemExit(f"Not a directory: {folder}")

    read = []
    for path in sorted(folder.glob("*.txt")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SystemExit(f"Cannot read {path}: {exc}") from exc
        sections = _sections(text)
        # Forge's metadata keys are case-insensitive.
        metadata = {}
        for line in sections.get("metadata", ()):
            name, _separator, value = line.partition("=")
            metadata[name.strip().casefold()] = value.strip()
        code = metadata.get("code")
        if not code:
            continue
        read.append((code, metadata, sections))

    # Forge stores editions by code in a case-insensitive sorted map and builds
    # its alias map by walking it in that order.
    read.sort(key=lambda item: item[0].casefold())
    set_codes = {}
    aliases = {}
    editions = {}
    printings = {}
    entries = 0
    for code, metadata, sections in read:
        set_codes[code.upper()] = code
        second = metadata.get("code2") or code
        alias = metadata.get("alias")
        if alias:
            aliases[alias.upper()] = code
        aliases[second.upper()] = code
        editions[code] = [
            (metadata.get("scryfallcode") or code).casefold(),
            metadata.get("type", ""), metadata.get("date", "")]
        for section, lines in sections.items():
            if section not in PRINTING_SECTIONS:
                continue
            for line in lines:
                match = CARD_LINE.match(line)
                if not match or not match.group(2):
                    continue
                key = _key(match.group(5))
                key = key if key in keys else alternate.get(key)
                if not key:
                    continue
                variant = VARIANT_PARAMETER.search(match.group(9) or "")
                declared = variants.get(key)
                if variant and declared and variant.group(1) not in declared:
                    continue
                printings.setdefault(key, set()).add(
                    f"{code}{PRINTING_SEPARATOR}{match.group(2)}")
                entries += 1
    for accepted, code in aliases.items():
        set_codes.setdefault(accepted, code)

    print(f"  editions read:           {len(editions)}")
    print(f"  printings recorded:      {entries}")
    print(f"  cards with a printing:   {len(printings)}")
    if entries < MINIMUM_CREDIBLE_PRINTINGS:
        raise SystemExit(
            f"Found only {entries} printings, below the credible minimum of "
            f"{MINIMUM_CREDIBLE_PRINTINGS}; refusing to replace the shipped "
            "asset. Check --editions.")
    return printings, set_codes, editions


def build_payload(keys, printings, set_codes, editions):
    """Return the asset payload, sorted so a diff stays readable."""
    return {
        "version": ASSET_VERSION,
        "source": "Forge card scripts (cardsfolder) and editions",
        "name_count": len(keys),
        "names": sorted(keys),
        "set_codes": dict(sorted(set_codes.items())),
        "editions": dict(sorted(editions.items())),
        "printings": {
            key: sorted(found) for key, found in sorted(printings.items())},
    }


def write_asset(path, payload):
    """Write the asset with LF endings, which .gitattributes pins for this tree."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
        handle.write("\n")


def _report_names(previous, names):
    was = set(previous.get("names") or ())
    added = sorted(names - was)
    print(f"\n  names added:   {len(added)}")
    print(f"  names removed: {len(was - names)}")
    # A short sample, so a report says which cards changed and not only how
    # many; the full list is whatever a refresh would write.
    for key in added[:LISTED_NAME_LIMIT]:
        print(f"    + {key}")
    if len(added) > LISTED_NAME_LIMIT:
        print(f"    ... and {len(added) - LISTED_NAME_LIMIT} more")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Regenerate the Proxic Arena playable-card asset.")
    parser.add_argument(
        "--cardsfolder", required=True,
        help="Path to a Forge cardsfolder tree, such as Forge's "
             "forge-gui/res/cardsfolder.")
    parser.add_argument(
        "--editions",
        help="Path to the Forge editions folder of the Forge Proxic Arena "
             "runs, such as forge-gui/res/editions. Required to write.")
    parser.add_argument(
        "--check", action="store_true",
        help="Compare only; write nothing and exit nonzero when stale.")
    args = parser.parse_args(argv)
    if not args.check and not args.editions:
        parser.error("--editions is required to write the asset")

    asset = ROOT / "assets" / ARENA_SUPPORTED_CARDS_FILE
    print(f"Forge card scripts: {args.cardsfolder}")
    if args.editions:
        print(f"Forge editions:     {args.editions}")
    print(f"Asset:              {asset.relative_to(ROOT).as_posix()}")
    keys, alternate, variants = read_scripts(args.cardsfolder)

    previous = None
    if asset.is_file():
        try:
            previous = json.loads(asset.read_text(encoding="utf-8"))
        except ValueError:
            previous = None

    if not args.editions:
        # Names alone: the printings need the editions, so they are left out
        # of the comparison rather than reported as missing.
        if previous is not None and set(previous.get("names") or ()) == keys:
            print("\nThe shipped asset's names already match the card scripts.")
            return 0
        if previous is not None:
            _report_names(previous, keys)
        print("\nSTALE: the asset's names do not match the card scripts. "
              "Re-run with --editions and without --check to refresh it.")
        return 1

    payload = build_payload(
        keys, *read_editions(args.editions, keys, alternate, variants))
    if previous == payload:
        print("\nThe shipped asset already matches the card scripts and editions.")
        return 0

    if previous is None:
        print("\nNo readable existing asset; this refresh creates it.")
    else:
        _report_names(previous, keys)
        if previous.get("printings") != payload["printings"]:
            print("  printings changed")

    if args.check:
        print("\nSTALE: the asset does not match the card scripts and "
              "editions. Re-run without --check to refresh it.")
        return 1

    write_asset(asset, payload)
    print(f"\nWrote {payload['name_count']} names and the printings of "
          f"{len(payload['printings'])} cards "
          f"({asset.stat().st_size / 1024:.0f} KB).")
    print("Run tests/test_deck_architecture.py to verify the shipped asset.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
