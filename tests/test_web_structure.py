"""Tests unitaires pour servers/mcp_web/structure.py (extraction stdlib pure)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from mcp_web.structure import extract_structure


def test_extracts_headings_with_level():
    html = "<h1>Titre</h1><h3>Sous-titre</h3>"
    entries = extract_structure(html)
    assert entries == [
        {"type": "heading", "level": 1, "text": "Titre"},
        {"type": "heading", "level": 3, "text": "Sous-titre"},
    ]


def test_extracts_links_with_url():
    html = '<a href="/foo">Foo</a><a href="https://x.example.com">Bar</a>'
    entries = extract_structure(html)
    assert entries == [
        {"type": "link", "text": "Foo", "url": "/foo"},
        {"type": "link", "text": "Bar", "url": "https://x.example.com"},
    ]


def test_ignores_links_without_href_or_text():
    html = '<a>No href</a><a href="/empty"></a><a href="/ok">Ok</a>'
    entries = extract_structure(html)
    assert entries == [{"type": "link", "text": "Ok", "url": "/ok"}]


def test_ignores_empty_headings():
    html = "<h2></h2><h2>   </h2><h2>Réel</h2>"
    entries = extract_structure(html)
    assert entries == [{"type": "heading", "level": 2, "text": "Réel"}]


def test_normalizes_whitespace_and_nested_tags():
    html = "<h1>  Titre   avec  <b>gras</b>  \n et retour</h1>"
    entries = extract_structure(html)
    assert entries == [{"type": "heading", "level": 1, "text": "Titre avec gras et retour"}]


def test_preserves_document_order():
    html = "<a href='/1'>Un</a><h2>Section</h2><a href='/2'>Deux</a>"
    entries = extract_structure(html)
    assert [e["type"] for e in entries] == ["link", "heading", "link"]


def test_empty_document_returns_empty_list():
    assert extract_structure("<html><body><p>Rien ici</p></body></html>") == []


def test_ignores_fragment_and_javascript_links():
    """W11 : #fragment et javascript: sont du bruit de navigation, pas des
    liens exploitables dans un sommaire."""
    html = (
        '<a href="#section-2">Aller à la section</a>'
        '<a href="javascript:void(0)">Cliquer ici</a>'
        '<a href="/reel">Lien réel</a>'
    )
    entries = extract_structure(html)
    assert entries == [{"type": "link", "text": "Lien réel", "url": "/reel"}]


def test_ignores_data_uri_links():
    """WEB8 : un href data: (potentiellement très long) est du même bruit de
    navigation que #fragment/javascript:, jamais un lien de navigation réel."""
    html = (
        '<a href="data:text/plain;base64,SGVsbG8=">Blob</a>'
        '<a href="DATA:image/png;base64,iVBORw0KGgo=">Blob maj</a>'
        '<a href="/reel">Lien réel</a>'
    )
    entries = extract_structure(html)
    assert entries == [{"type": "link", "text": "Lien réel", "url": "/reel"}]
