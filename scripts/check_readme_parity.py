#!/usr/bin/env python3
"""Bilingual README entrypoint-parity check.

Fails if `README.md` and `README.zh-HK.md` do not mention the SAME SET of
entrypoints -- catching the common drift where a script is added to one
language's README but not the other. This is a PARITY check, not a completeness
check: an entrypoint absent from BOTH READMEs is fine; only an asymmetry (present
in one, missing from the other) is an error.

Entrypoints are the repo's top-level `*.sh` scripts plus the two user-facing
Python CLIs (`policy_tool.py`, `health_check.py`). Matching is filename-bounded so
`install.sh` is not satisfied by `reinstall.sh`, `old.install.sh`, `install.sh-old`,
or `install.sh.backup`, while `./install.sh`, `scripts/policy_tool.py`, a backticked
`` `install.sh` ``, and a sentence-final `install.sh.` all count.

Usage:
    python3 scripts/check_readme_parity.py [root]

Exits non-zero if the two READMEs' entrypoint sets differ.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# User-facing Python CLIs (the other scripts/*.py are internal tooling).
CLI_BASENAMES = ("policy_tool.py", "health_check.py")
READMES = ("README.md", "README.zh-HK.md")


def entrypoints(root: Path) -> list[str]:
    """Top-level *.sh scripts (so a new one auto-joins) plus the fixed CLIs."""
    names = {path.name for path in root.glob("*.sh")}
    names.update(CLI_BASENAMES)
    return sorted(names)


def mentions(text: str, basename: str) -> bool:
    """True if `basename` appears as a whole filename token in `text`.

    A leading `/`, backtick, or space is allowed (so paths and code spans match),
    but a preceding word char / `.` / `-` / `~` is not; a trailing word char, `-`,
    `~`, or `.<char>` is rejected (so backup names like `install.sh~` and
    `install.sh.~1~` do NOT count) while a sentence-final `.` is fine.
    """
    pattern = r"(?<![A-Za-z0-9_.~-])" + re.escape(basename) + r"(?![A-Za-z0-9_~-]|\.[A-Za-z0-9_~-])"
    return re.search(pattern, text) is not None


def check_parity(root: Path) -> list[str]:
    """Return a list of parity problems (empty if the READMEs agree)."""
    names = entrypoints(root)
    texts = {}
    problems: list[str] = []
    for readme in READMES:
        path = root / readme
        if not path.is_file():
            problems.append(f"missing README: {readme}")
            continue
        texts[readme] = path.read_text(encoding="utf-8")
    if len(texts) != len(READMES):
        return problems

    en, zh = READMES
    en_set = {name for name in names if mentions(texts[en], name)}
    zh_set = {name for name in names if mentions(texts[zh], name)}
    for name in sorted(en_set - zh_set):
        problems.append(f"{name}: mentioned in {en} but not {zh}")
    for name in sorted(zh_set - en_set):
        problems.append(f"{name}: mentioned in {zh} but not {en}")
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parents[1]
    problems = check_parity(root)
    if problems:
        print("README entrypoint parity errors:")
        for problem in problems:
            print(f"  {problem}")
        print(f"\n{len(problems)} parity problem(s). Keep README.md and README.zh-HK.md in sync.")
        return 1
    print(f"OK: {' and '.join(READMES)} mention the same {len(entrypoints(root))} entrypoints.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
