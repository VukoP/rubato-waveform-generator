# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Prints one version's section of a Keep a Changelog file: ``changelog_section.py v1.0.0``."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def changelog_section(text: str, version: str) -> str | None:
    """The body of ``## [version] - date`` up to the next ``## `` heading, or None if absent."""
    version = version.removeprefix("v")
    heading = re.compile(rf"^## \[{re.escape(version)}\](?:\s|$)")
    lines = text.splitlines()
    for start, line in enumerate(lines):
        if heading.match(line):
            break
    else:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="for example v1.0.0 or 1.0.0")
    parser.add_argument("--file", default="CHANGELOG.md", type=Path)
    args = parser.parse_args(argv)
    try:
        text = args.file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 1
    section = changelog_section(text, args.version)
    if section is None:
        print(f"no section for {args.version} in {args.file}", file=sys.stderr)
        return 1
    sys.stdout.write(section)
    return 0


if __name__ == "__main__":
    sys.exit(main())
