"""Extraction de structure (headings + liens) d'une page HTML pour mcp_web.

Parsing en stdlib pure (html.parser), pas de dépendance tierce — même esprit
que mcp_ddg qui parse le HTML de DuckDuckGo sans lib externe. Le résultat est
un flux plat ordonné d'entrées, dans l'ordre d'apparition sur la page :
    {"type": "heading", "level": 1..6, "text": "..."}
    {"type": "link", "text": "...", "url": "..."}

Un lien sans texte (icône, image seule) est ignoré. Un heading sans texte
(balise vide) est ignoré. Le texte est aplati sur une seule ligne (espaces
normalisés) — la mise en forme interne d'un heading ou d'un lien n'a pas
d'intérêt pour un sommaire de navigation."""

from __future__ import annotations

import re
from html.parser import HTMLParser

_HEADING_TAGS = {f"h{i}": i for i in range(1, 7)}
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


class _StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[dict] = []
        self._heading_level: int | None = None
        self._heading_chunks: list[str] = []
        self._link_url: str | None = None
        self._link_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HEADING_TAGS and self._heading_level is None:
            self._heading_level = _HEADING_TAGS[tag]
            self._heading_chunks = []
        elif tag == "a" and self._link_url is None:
            href = next((v for k, v in attrs if k == "href"), None)
            # Bruit de navigation sans intérêt dans un sommaire (W11/WEB8) : ancre
            # de page (#section), pseudo-URL non naviguable (javascript:) et URI
            # data: (potentiellement très longue, jamais un lien de navigation).
            href_lower = href.lower() if href else ""
            if (
                href
                and not href.startswith("#")
                and not href_lower.startswith("javascript:")
                and not href_lower.startswith("data:")
            ):
                self._link_url = href
                self._link_chunks = []

    def handle_data(self, data: str) -> None:
        if self._heading_level is not None:
            self._heading_chunks.append(data)
        if self._link_url is not None:
            self._link_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _HEADING_TAGS and self._heading_level is not None:
            text = _normalize("".join(self._heading_chunks))
            if text:
                self.entries.append(
                    {"type": "heading", "level": self._heading_level, "text": text}
                )
            self._heading_level = None
            self._heading_chunks = []
        elif tag == "a" and self._link_url is not None:
            text = _normalize("".join(self._link_chunks))
            if text:
                self.entries.append({"type": "link", "text": text, "url": self._link_url})
            self._link_url = None
            self._link_chunks = []


def extract_structure(html_text: str) -> list[dict]:
    parser = _StructureParser()
    parser.feed(html_text)
    return parser.entries
