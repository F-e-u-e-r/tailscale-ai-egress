#!/usr/bin/env python3
"""Print the CHANGELOG.md section for a given version.

Used by the release workflow to turn the changelog entry for `X.Y.Z` into the
body of the GitHub release. Exits non-zero if the version section is absent.

Usage:
    python3 scripts/changelog_release_notes.py 1.0.0 [path/to/CHANGELOG.md]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def extract_section(text: str, version: str) -> str | None:
    """Return the body under `## [<version>] ...`, up to the next H2 heading."""
    heading_re = re.compile(r"^##\s+\[" + re.escape(version) + r"\]")
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if heading_re.match(line)), None)
    if start is None:
        return None

    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):  # next H2 section (### subsections are kept)
            break
        body.append(line)

    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(body)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: changelog_release_notes.py <version> [changelog]", file=sys.stderr)
        return 2
    version = argv[1].lstrip("v")
    changelog = Path(argv[2]) if len(argv) > 2 else Path(__file__).resolve().parents[1] / "CHANGELOG.md"
    try:
        text = changelog.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: could not read {changelog}: {exc}", file=sys.stderr)
        return 1

    section = extract_section(text, version)
    if section is None:
        print(f"error: no CHANGELOG section for version {version}", file=sys.stderr)
        return 1
    print(section)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
