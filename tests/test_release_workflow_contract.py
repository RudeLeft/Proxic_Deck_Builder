"""Static contract for the tag-triggered Windows release (REL-008, REL-009)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    workflow = (ROOT / ".github/workflows/build-windows.yml").read_text(
        encoding="utf-8")
    version_check = workflow.find("Check the tag matches the app's version")
    build = workflow.find("pyinstaller ProxicDeckBuilder.spec")
    publish = workflow.find("Publish the GitHub Release")
    checks = {
        # REL-008: a tag ahead of pyproject publishes an app the updater keeps
        # calling out of date.
        "a release tag must equal v + pyproject's version": (
            "tomllib" in workflow
            and "['project']['version']" in workflow
            and '$tag -ne "v$version"' in workflow),
        "the version check runs on tags only, before anything is built": (
            -1 not in {version_check, build}
            and version_check < build
            and "if: startsWith(github.ref, 'refs/tags/')"
                in workflow[version_check:build]),
        # REL-009: hand-written notes travel in the annotated tag.
        "release notes come from the annotated tag's message": (
            publish != -1
            and "git/ref/tags/$tag" in workflow[publish:]
            and '$ref.object.type -eq "tag"' in workflow[publish:]
            and "--jq \".message\"" in workflow[publish:]),
        "only a message of bullets counts; otherwise commit subjects": (
            "-match '(?m)^- '" in workflow[publish:]
            and "git log \"${prev}..${tag}\"" in workflow[publish:]),
    }
    ok = True
    for label, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok &= bool(passed)
    print("\nRELEASE WORKFLOW CONTRACT:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
