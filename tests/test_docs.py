# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Documentation checks: CLI flags, layout images, links, versions and honest claims."""

import re
import sys
from pathlib import Path

import pytest

from rubato_waveform import __version__
from rubato_waveform.cli import build_parser
from rubato_waveform.engine import LAYOUTS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from changelog_section import changelog_section

README = ROOT / "README.md"
INDEX = ROOT / "docs" / "index.html"
LINKED_DOCS = ["README.md", "CONTRIBUTING.md", "SECURITY.md", "RELEASING.md", "CHANGELOG.md"]
LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def anchors(md_text: str) -> set[str]:
    """GitHub-style heading anchors of a Markdown text."""
    found = set()
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", md_text, flags=re.MULTILINE):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        found.add(slug)
    return found


def broken_links(md_path: Path) -> list[str]:
    """Relative links and images in ``md_path`` whose target file or anchor is missing."""
    text = md_path.read_text(encoding="utf-8")
    bad = []
    for _alt, target in LINK_RE.findall(text):
        if re.match(r"[a-z][a-z0-9+.-]*:", target, flags=re.IGNORECASE):
            continue
        name, _, fragment = target.partition("#")
        dest = (md_path.parent / name) if name else md_path
        if not dest.exists() or fragment and dest.suffix == ".md" and fragment not in anchors(dest.read_text(encoding="utf-8")):
            bad.append(target)
    return bad


def newest_changelog_version(text: str) -> str:
    return re.search(r"^## \[([^\]]+)\]", text.replace("## [Unreleased]", ""), flags=re.MULTILINE).group(1)


def test_every_cli_flag_is_in_readme() -> None:
    readme = README.read_text(encoding="utf-8")
    flags = {opt for action in build_parser()._actions for opt in action.option_strings if opt.startswith("--")}
    assert flags
    missing = sorted(f for f in flags if f not in readme)
    assert not missing, f"flags missing from README.md: {missing}"


@pytest.mark.parametrize("key", LAYOUTS)
def test_layout_image_in_readme(key: str) -> None:
    image = ROOT / "docs" / "media" / "layouts" / f"{key}.webp"
    assert image.is_file() and image.stat().st_size > 0, f"{image} is missing or empty"
    match = re.search(rf"!\[([^\]]*)\]\(docs/media/layouts/{re.escape(key)}\.webp\)", README.read_text(encoding="utf-8"))
    assert match, f"README.md has no image for layout {key}"
    assert match.group(1).strip(), f"README.md image for layout {key} has empty alt text"


def test_catalog_has_ten_layouts() -> None:
    assert len(LAYOUTS) == 10


@pytest.mark.parametrize("name", LINKED_DOCS)
def test_relative_links_resolve(name: str) -> None:
    assert broken_links(ROOT / name) == []


def test_broken_link_checker_can_fail(tmp_path: Path) -> None:
    (tmp_path / "real.md").write_text("# Real heading\n", encoding="utf-8")
    doc = tmp_path / "doc.md"
    doc.write_text(
        "[ok](real.md) [ok2](real.md#real-heading) [web](https://example.com/x)\n"
        "[gone](missing.md) ![pic](media/missing.webp) [anchor](real.md#nope) [self](#nowhere)\n",
        encoding="utf-8",
    )
    assert broken_links(doc) == ["missing.md", "media/missing.webp", "real.md#nope", "#nowhere"]


def test_versions_are_consistent() -> None:
    cff = re.search(r"^version:\s*\"?([^\s\"]+)", (ROOT / "CITATION.cff").read_text(encoding="utf-8"), flags=re.MULTILINE)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    ld = re.search(r'"softwareVersion":\s*"([^"]+)"', INDEX.read_text(encoding="utf-8"))
    assert cff and ld
    assert cff.group(1) == __version__
    assert newest_changelog_version(changelog) == __version__
    assert ld.group(1) == __version__
    assert changelog_section(changelog, __version__) is not None


@pytest.mark.parametrize("path", [README, INDEX])
def test_untested_editors_are_not_called_verified(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    editors = ("premiere pro", "final cut pro", "after effects")
    near = [i for i, line in enumerate(lines) if any(e in line.lower() for e in editors)]
    assert near, f"{path.name} does not mention the unverified editors"
    for i in near:
        assert "verified" not in lines[i].lower(), f"{path.name}:{i + 1} calls an untested editor verified"
    assert any("not tested by the author" in " ".join(lines[max(0, i - 2):i + 3]).lower() for i in near), (
        f"{path.name} must say these editors were not tested by the author"
    )
