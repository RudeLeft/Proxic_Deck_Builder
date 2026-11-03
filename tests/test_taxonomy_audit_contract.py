"""Static contract for the networked full-Scryfall taxonomy audit."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mtgdb.database.authorities import (
    CATALOG_AUTHORITIES, MECHANIC_AUTHORITIES, SCRYFALL_CATALOGS, SUBTYPE_AUTHORITIES,
)
from mtgdb.database.schema import RULES_SUPERTYPES_META_KEY


def _python_version(workflow):
    """The python-version a workflow's setup-python step asks for."""
    for line in workflow.splitlines():
        if line.strip().startswith("python-version:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return None


def _behaviour():
    """Run the audit's pure helpers on made-up values (no network)."""
    import importlib.util
    from mtgdb.ui.mana import bundled_symbol_keys, resolve_symbol_key
    spec = importlib.util.spec_from_file_location(
        "taxonomy_audit", ROOT / "tests/taxonomy_audit.py")
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    keys = bundled_symbol_keys()
    previous = {"Sets": ["Alpha (LEA)"], "Card types": ["Creature"]}
    current = {"Sets": ["Alpha (LEA)", "Beta (LEB)"], "Card types": ["Creature"],
               "Keywords and ability words": ["Flying"]}
    gained = audit.news(current, previous)
    missing = audit.missing_symbol_pictures(
        {"C/W": (6, "Ulalek"), "G/W/P": (8, "Ajani"), "Q/Z": (2, "Odd Card"),
         "HW": (1, "Joke Card")}, jokes={"HW"})
    base = {"checks": {"ok": True}, "source": {"objects": 10},
            "source_sets": 2, "bulk_updated_at": "2026-11-02T10:00"}
    quiet = audit.run_summary({**base, "missing_symbols": {}, "news": gained})
    look = audit.run_summary({**base, "missing_symbols": missing, "news": None})
    failed = audit.run_summary({**base, "checks": {"ok": False},
                                "missing_symbols": {}, "news": {}})
    return {
        "each real colorless and Phyrexian hybrid symbol has its own picture": all(
            resolve_symbol_key(token, keys) == token.replace("/", "")
            for token in ("C/W", "C/U", "C/B", "C/R", "C/G", "C/P",
                          "W/U/P", "W/B/P", "U/B/P", "U/R/P", "B/R/P",
                          "B/G/P", "R/W/P", "R/G/P", "G/W/P", "G/U/P")),
        "only lists the previous run kept are compared, and only gains count": (
            gained == {"Sets": ["Beta (LEB)"]}
            and audit.news(current, None) is None),
        "a symbol without a picture is listed; joke symbols are not": (
            missing == {"{Q/Z}": (2, "Odd Card")}),
        "the run page says Nothing to do, Something to look at, or Needs attention": (
            quiet[1].startswith("Nothing to do::")
            and "New since the last check: Sets 1." in quiet[1]
            and "- Sets (1): Beta (LEB)" in quiet[0]
            and look[1].startswith("Something to look at::")
            and "{Q/Z} on 2 cards, e.g. Odd Card" in look[0]
            and "Nothing to compare with yet" in look[0]
            and failed[1].startswith("Needs attention::")
            and "### What failed" in failed[0]),
    }


def main():
    audit = (ROOT / "tests/taxonomy_audit.py").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/taxonomy-audit.yml").read_text(
        encoding="utf-8")
    constants = (ROOT / "mtgdb/database/constants.py").read_text(encoding="utf-8")
    taxonomy = (ROOT / "mtgdb/database/taxonomy.py").read_text(encoding="utf-8")
    semantics = (ROOT / "mtgdb/database/semantics.py").read_text(encoding="utf-8")
    checks = {
        "Scryfall card-types is a first-class cached catalog": (
            SCRYFALL_CATALOGS.get("card-types") == "Card type"),
        "taxonomy authority sources have one declarative registry": (
            len(CATALOG_AUTHORITIES) == len(SCRYFALL_CATALOGS)
            and {item.name for item in SUBTYPE_AUTHORITIES}
                == {name for name in SCRYFALL_CATALOGS if name.endswith("-types") and name != "card-types"}
            and {item.name for item in MECHANIC_AUTHORITIES}
                == {"keyword-abilities", "keyword-actions", "ability-words"}
            and "catalog_categories = (" not in taxonomy),
        "no hardcoded Card Type tuple remains": (
            "CARD_TYPES =" not in constants and "CARD_TYPES =" not in taxonomy
            and "CARD_TYPES =" not in semantics),
        "Card Type picker is catalog intersect local occurrence": (
            'catalog = self.catalog("card-types")' in taxonomy
            and "if not catalog:" in taxonomy
            and "_card_has_type(type_line, value)" in taxonomy),
        "Supertype picker uses verified Wizards metadata plus local occurrence": (
            RULES_SUPERTYPES_META_KEY == "rules:supertypes"
            and "_verified_rules_supertypes" in taxonomy
            and 'self.catalog("supertypes")' not in taxonomy
            and "_observed_left_phrase" in taxonomy
            and "SUPERTYPES =" not in constants),

        "current audit discovers Default Cards instead of hardcoding a dated file": (
            'net.get_json(f"{API_BASE}/bulk-data")' in audit
            and 'value.get("type") == "default_cards"' in audit
            and "jsonl_download_uri" in audit),
        "current audit refreshes Scryfall and Wizards taxonomy sources": (
            "if args.online_catalogs or args.current:" in audit
            and "refresh_catalogs()" in audit
            and "RULES_SUPERTYPES_META_KEY" in audit),
        "full audit proves Card Types are authorized by Scryfall": (
            "trusted Card Types are authorized by Scryfall card-types catalog" in audit),
        "full audit proves Supertypes stay inside parsed Wizards vocabulary": (
            "trusted Supertypes are Wizards-rules-defined and observed" in audit
            and "Supertype rules provenance is recorded from Wizards" in audit),
        "full audit records upstream compatibility fingerprints": (
            "refresh_compatibility_diagnostics()" in audit
            and "## Upstream compatibility" in audit
            and "current Scryfall layouts are understood or deliberately classified" in audit
            and "current Scryfall legality statuses are understood" in audit),
        "weekly/manual CI runs the current full audit": (
            "workflow_dispatch:" in workflow and "schedule:" in workflow
            and "tests/taxonomy_audit.py --current" in workflow),
        "CI retains Markdown audit evidence": (
            "taxonomy-audit.md" in workflow and "actions/upload-artifact@" in workflow),
        "a passing run says so in one plain line on its run page": (
            "--summary-out run-summary.md" in workflow
            and "--notice-out run-notice.txt" in workflow
            and "GITHUB_STEP_SUMMARY" in workflow
            and "::notice title=" in workflow
            and "def run_summary(result):" in audit
            and '"Nothing to do"' in audit),
        "CI emails the owner by issue when the job fails, drift or not": (
            "issues: write" in workflow
            # Any failure of the job, not only the audit step's own.
            and "if: failure()" in workflow
            and "steps.audit.conclusion" in workflow
            and "gh issue create" in workflow
            # Assigned and mentioned, which is what GitHub always emails.
            and "--assignee" in workflow
            and "@%s" in workflow),
        "the run lists what is new since the last passing check": (
            "actions: read" in workflow
            and "gh run download" in workflow
            and "scryfall-taxonomy-snapshot" in workflow
            and "--previous previous/taxonomy-snapshot.json" in workflow
            and "--snapshot-out taxonomy-snapshot.json" in workflow
            and "retention-days: 90" in workflow),
        "a new subtype group fails the audit": (
            "no card type has subtypes the app keeps no list for" in audit
            and "KNOWN_SUBTYPE_GROUPS_WITHOUT_LIST" in audit
            and '/catalog/{name}' in audit),
        "a passing run closes an earlier alert": (
            "gh issue close" in workflow
            and "Close an earlier alert now that the check passes" in workflow),
        "the audit checks Scryfall still gives print pictures and art crops": (
            "nearly every card still has a print picture and an art crop" in audit
            and "PICTURE_FLOOR = 0.99" in audit
            and "image_png" in audit and "image_art_crop" in audit),
        "the audit runs on the Python the Windows app is built with": (
            _python_version(workflow) is not None
            and _python_version(workflow) == _python_version(
                (ROOT / ".github/workflows/build-windows.yml").read_text(
                    encoding="utf-8"))),
    }
    checks.update(_behaviour())
    ok = True
    for label, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok &= bool(passed)
    print("\nTAXONOMY AUDIT CONTRACT:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
