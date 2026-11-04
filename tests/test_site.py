# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""The one-page site in docs/: structure, SEO metadata, local-only assets."""

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image

from rubato_waveform import __version__

DOCS = Path(__file__).resolve().parent.parent / "docs"
CANONICAL = "https://vukop.github.io/rubato-waveform-generator/"


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []  # (tag, attrs dict)
        self.jsonld = []
        self.title = ""
        self._in = None

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self._in = "jsonld"
            self.jsonld.append("")
        elif tag == "title":
            self._in = "title"

    def handle_endtag(self, tag):
        self._in = None

    def handle_data(self, data):
        if self._in == "jsonld":
            self.jsonld[-1] += data
        elif self._in == "title":
            self.title += data

    def find(self, tag):
        return [a for t, a in self.tags if t == tag]

    def meta(self, key):
        for a in self.find("meta"):
            if key in (a.get("name"), a.get("property")):
                return a.get("content", "")
        return None


def _page() -> Page:
    page = Page()
    page.feed((DOCS / "index.html").read_text(encoding="utf-8"))
    return page


def _local(ref: str) -> Path | None:
    ref = ref.split("#")[0].split("?")[0]
    if not ref or re.match(r"^[a-z][a-z0-9+.-]*:", ref, re.IGNORECASE) or ref.startswith("//"):
        return None
    return DOCS / ref


def test_single_h1_and_html_lang():
    page = _page()
    assert len(page.find("h1")) == 1
    assert page.find("html")[0].get("lang") == "en"


def test_json_ld_matches_version():
    page = _page()
    assert len(page.jsonld) == 1
    data = json.loads(page.jsonld[0])
    assert data["@type"] == "SoftwareApplication"
    assert data["softwareVersion"] == __version__
    assert data["offers"]["price"] == "0"
    assert data["applicationCategory"] == "MultimediaApplication"


def test_local_references_exist():
    page = _page()
    refs = []
    for tag, attrs in page.tags:
        for key in ("src", "href"):
            if key in attrs:
                refs.append(attrs[key])
        for part in (attrs.get("srcset") or "").split(","):
            if part.strip():
                refs.append(part.split()[0])
    css = (DOCS / "assets" / "site.css").read_text(encoding="utf-8")
    css_refs = [(DOCS / "assets", m) for m in re.findall(r"url\(\s*[\"']?([^\"')]+)", css)]
    missing = [r for r in refs if (p := _local(r)) is not None and not p.exists()]
    missing += [m for base, m in css_refs if not (base / m).exists()]
    assert not missing
    assert (DOCS / "assets" / "site.css").exists()


def test_no_external_resources():
    page = _page()
    external = []
    for tag, attrs in page.tags:
        for key in ("src", "srcset", "data", "poster"):
            if _is_remote(attrs.get(key)):
                external.append((tag, key))
        if tag == "link" and attrs.get("rel") not in ("canonical",) and _is_remote(attrs.get("href")):
            external.append((tag, "href"))
    assert not external
    css = (DOCS / "assets" / "site.css").read_text(encoding="utf-8")
    assert not re.search(r"url\(\s*[\"']?(https?:)?//|@import", css)
    assert not page.find("script") or all(
        a.get("type") == "application/ld+json" for a in page.find("script")
    )


def _is_remote(value) -> bool:
    return bool(value) and bool(re.match(r"^(https?:)?//", value.strip()))


def test_images_have_alt_and_size():
    for attrs in _page().find("img"):
        assert attrs.get("alt", "").strip() or attrs.get("src", "").endswith("favicon.svg"), attrs
        assert attrs.get("width") and attrs.get("height"), attrs


def test_gallery_images_have_non_empty_alt():
    layouts = [a for a in _page().find("img") if "media/layouts/" in a.get("src", "")]
    assert len(layouts) == 10
    assert all(a.get("alt", "").strip() and a.get("loading") == "lazy" for a in layouts)


def test_sitemap_lists_canonical():
    import xml.etree.ElementTree as ET

    root = ET.parse(DOCS / "sitemap.xml").getroot()
    locs = [el.text for el in root.iter() if el.tag.endswith("loc")]
    assert locs == [CANONICAL]
    assert "Sitemap: " + CANONICAL + "sitemap.xml" in (DOCS / "robots.txt").read_text()


def test_og_image_is_1280_by_640():
    page = _page()
    image = page.meta("og:image")
    assert image.startswith(CANONICAL)
    path = DOCS / image.removeprefix(CANONICAL)
    with Image.open(path) as im:
        assert im.size == (1280, 640)
    assert page.meta("og:image:width") == "1280"
    assert page.meta("og:image:height") == "640"
    assert page.meta("og:image:alt")
    assert page.meta("twitter:image") == image


def test_canonical_title_and_description_lengths():
    page = _page()
    canonical = [a for a in page.find("link") if a.get("rel") == "canonical"]
    assert [a["href"] for a in canonical] == [CANONICAL]
    assert 0 < len(page.title) < 60
    assert 140 <= len(page.meta("description")) <= 160
