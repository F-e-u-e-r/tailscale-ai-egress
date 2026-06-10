#!/usr/bin/env python3
"""Offline Markdown link checker.

Verifies that local (relative) links and image paths in the repo's Markdown
files point at files that exist. External links (http/https/mailto) and pure
in-page anchors (#section) are not fetched; this check is deliberately network
free so it is fast and reliable in CI.

Usage:
    python3 scripts/check_docs_links.py [root]

Exits non-zero if any local link target is missing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# [text](target) and ![alt](target). Captures the target up to the first
# closing paren; titles like (path "title") are handled by splitting on space.
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
SKIP_DIRS = {".git", "generated", "__pycache__", "node_modules", "dist"}
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "//")


def iter_markdown_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.md"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        files.append(path)
    return sorted(files)


def strip_code(text: str) -> str:
    """Blank out fenced code blocks and inline code so links inside code are
    not treated as real links."""
    out_lines: list[str] = []
    in_fence = False
    fence_marker = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        if in_fence:
            if stripped.startswith(fence_marker):
                in_fence = False
            out_lines.append("")
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = True
            fence_marker = "```" if stripped.startswith("```") else "~~~"
            out_lines.append("")
            continue
        # Remove inline code spans.
        out_lines.append(re.sub(r"`[^`]*`", "", line))
    return "\n".join(out_lines)


def normalize_target(raw: str) -> str | None:
    target = raw.strip()
    if not target:
        return None
    # Drop optional <...> wrapping and any link title after whitespace.
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    else:
        target = target.split()[0]
    # Strip anchors and query strings.
    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target:
        return None
    if target.startswith(EXTERNAL_PREFIXES):
        return None
    return target


def check_file(path: Path, root: Path) -> list[str]:
    problems: list[str] = []
    text = strip_code(path.read_text(encoding="utf-8"))
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in LINK_RE.finditer(line):
            target = normalize_target(match.group(1))
            if target is None:
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                rel = path.relative_to(root)
                problems.append(f"{rel}:{lineno}: missing link target -> {target}")
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parents[1]
    files = iter_markdown_files(root)
    problems: list[str] = []
    for path in files:
        problems.extend(check_file(path, root))

    if problems:
        print("Broken local Markdown links:")
        for problem in problems:
            print(f"  {problem}")
        print(f"\n{len(problems)} broken link(s) across {len(files)} file(s).")
        return 1

    print(f"OK: checked {len(files)} Markdown file(s); all local links resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
