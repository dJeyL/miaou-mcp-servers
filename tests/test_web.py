"""Tests unitaires pour servers/mcp_web/ (package)."""
import base64
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from mcp import types
from mcp_web import server as fetch_server
from mcp_web import cache as mcp_web_cache
from mcp_web import _is_textual_mime

_TM = fetch_server.mcp._tool_manager


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_web_cache, "WORKDIR", tmp_path)


def _make_mock_resp(body: bytes, content_type: str = "text/html; charset=utf-8"):
    headers = MagicMock()
    headers.get.side_effect = (
        lambda key, default="": content_type if key == "Content-Type" else default
    )
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read.return_value = body
    mock.headers = headers
    return mock


@pytest.mark.asyncio
async def test_fetch_html_returns_text_resource():
    html = b"<html><body><p>Hello world</p></body></html>"
    mock_resp = _make_mock_resp(html, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com"})
    assert isinstance(result, types.EmbeddedResource)
    assert result.resource.mimeType == "text/plain"
    assert "Hello world" in result.resource.text


@pytest.mark.asyncio
async def test_fetch_html_strips_scripts():
    html = b"<html><body><script>alert(1)</script><p>Contenu</p></body></html>"
    mock_resp = _make_mock_resp(html)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com"})
    assert "alert" not in result.resource.text
    assert "Contenu" in result.resource.text


@pytest.mark.asyncio
async def test_fetch_text_plain_returns_raw():
    body = b"texte brut ici"
    mock_resp = _make_mock_resp(body, "text/plain; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com/data.txt"})
    assert isinstance(result, types.EmbeddedResource)
    assert result.resource.mimeType == "text/plain"
    assert "texte brut ici" in result.resource.text


@pytest.mark.asyncio
async def test_fetch_json_preserves_mime():
    body = b'{"ok": true}'
    mock_resp = _make_mock_resp(body, "application/json")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://api.example.com/data"})
    assert isinstance(result, types.EmbeddedResource)
    assert isinstance(result.resource, types.TextResourceContents)
    assert result.resource.mimeType == "application/json"
    assert '{"ok": true}' in result.resource.text


@pytest.mark.asyncio
async def test_fetch_json_suffix_mime_returns_text():
    body = b'{"data": []}'
    mock_resp = _make_mock_resp(body, "application/vnd.api+json")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://api.example.com/vnd"})
    assert isinstance(result.resource, types.TextResourceContents)
    assert result.resource.mimeType == "application/vnd.api+json"
    assert '{"data": []}' in result.resource.text


@pytest.mark.asyncio
async def test_fetch_xml_returns_text():
    body = b"<root><item>1</item></root>"
    mock_resp = _make_mock_resp(body, "application/xml")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com/data.xml"})
    assert isinstance(result.resource, types.TextResourceContents)
    assert result.resource.mimeType == "application/xml"
    assert "<item>1</item>" in result.resource.text


@pytest.mark.asyncio
async def test_fetch_svg_xml_suffix_returns_text_not_blob():
    body = b"<svg><circle r='5'/></svg>"
    mock_resp = _make_mock_resp(body, "image/svg+xml")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com/icon.svg"})
    assert isinstance(result.resource, types.TextResourceContents)
    assert result.resource.mimeType == "image/svg+xml"
    assert "<circle" in result.resource.text


@pytest.mark.parametrize(
    "mime,expected",
    [
        ("text/plain", True),
        ("application/json", True),
        ("application/ld+json", True),
        ("application/vnd.api+json", True),
        ("image/svg+xml", True),
        ("image/png", False),
        ("application/octet-stream", False),
        ("application/pdf", False),
    ],
)
def test_is_textual_mime(mime, expected):
    assert _is_textual_mime(mime) is expected


@pytest.mark.asyncio
async def test_fetch_binary_returns_blob():
    body = bytes(range(256))
    mock_resp = _make_mock_resp(body, "image/png")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com/image.png"})
    assert isinstance(result, types.EmbeddedResource)
    assert isinstance(result.resource, types.BlobResourceContents)
    assert result.resource.mimeType == "image/png"
    assert base64.b64decode(result.resource.blob) == body


@pytest.mark.asyncio
async def test_fetch_truncation_adds_note():
    body = b"A" * 20
    mock_resp = _make_mock_resp(body, "text/plain")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com", "max_bytes": 10})
    assert "Tronqué" in result.resource.text
    assert "10" in result.resource.text


@pytest.mark.asyncio
async def test_fetch_http_error_returns_string():
    err = urllib.error.HTTPError("http://example.com", 404, "Not Found", {}, None)
    with patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com/missing"})
    assert isinstance(result, str)
    assert "404" in result


@pytest.mark.asyncio
async def test_fetch_url_error_returns_string():
    err = urllib.error.URLError("Connection refused")
    with patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool("fetch_url", {"url": "http://unreachable.local"})
    assert isinstance(result, str)
    assert "réseau" in result.lower() or "Connection refused" in result


def test_tool_list_contains_fetch_url():
    names = {t.name for t in _TM.list_tools()}
    assert "fetch_url" in names
    assert "fetch_read" in names
    assert "fetch_list" in names


@pytest.mark.asyncio
async def test_fetch_url_caps_output_and_notes_pagination(monkeypatch):
    monkeypatch.setattr(mcp_web_cache, "READ_CAP", 10)
    body = b"A" * 30
    mock_resp = _make_mock_resp(body, "text/plain")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com/big"})
    assert len(result.resource.text) > 10
    assert result.resource.text.startswith("A" * 10)
    assert "fetch_read" in result.resource.text
    assert "char_start=10" in result.resource.text


@pytest.mark.asyncio
async def test_fetch_read_paginates_cached_content(monkeypatch):
    monkeypatch.setattr(mcp_web_cache, "READ_CAP", 10)
    body = b"0123456789ABCDEFGHIJ"
    mock_resp = _make_mock_resp(body, "text/plain")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/paged"})

    result = await _TM.call_tool(
        "fetch_read", {"url": "http://example.com/paged", "char_start": 10}
    )
    assert result.startswith("ABCDEFGHIJ")
    assert "fetch_read" not in result  # dernière page, pas de notice de suite


@pytest.mark.asyncio
async def test_fetch_read_caps_output_even_with_large_char_end(monkeypatch):
    monkeypatch.setattr(mcp_web_cache, "READ_CAP", 10)
    body = b"0123456789ABCDEFGHIJKLMNOPQRST"  # 30 caractères
    mock_resp = _make_mock_resp(body, "text/plain")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/verybig"})

    result = await _TM.call_tool(
        "fetch_read",
        {"url": "http://example.com/verybig", "char_start": 0, "char_end": 30},
    )
    assert result.startswith("0123456789")
    assert len(result.split("\n\n[")[0]) == 10  # extrait borné au cap, pas aux 30 demandés
    assert "char_start=10" in result


@pytest.mark.asyncio
async def test_fetch_read_unknown_url_returns_clear_message():
    result = await _TM.call_tool("fetch_read", {"url": "http://never-fetched.example.com"})
    assert isinstance(result, str)
    assert "fetch_url" in result


@pytest.mark.asyncio
async def test_fetch_read_rejects_invalid_range():
    body = b"hello"
    mock_resp = _make_mock_resp(body, "text/plain")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/small"})

    result = await _TM.call_tool(
        "fetch_read", {"url": "http://example.com/small", "char_start": -1}
    )
    assert "char_start" in result


_STRUCTURED_HTML = b"""
<html><body>
<h1>Titre principal</h1>
<p>Intro <a href="/a">Lien A</a></p>
<h2>Sous-section</h2>
<a href="https://ext.example.com/b">Lien B</a>
<a href="/empty"></a>
</body></html>
"""


@pytest.mark.asyncio
async def test_fetch_list_extracts_headings_and_links():
    mock_resp = _make_mock_resp(_STRUCTURED_HTML, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/page"})

    result = await _TM.call_tool("fetch_list", {"url": "http://example.com/page"})
    assert "# Titre principal" in result
    assert "## Sous-section" in result
    assert "[Lien A](/a)" in result
    assert "[Lien B](https://ext.example.com/b)" in result
    assert "/empty" not in result  # lien sans texte, ignoré


@pytest.mark.asyncio
async def test_fetch_list_paginates_and_caches_structure(monkeypatch):
    monkeypatch.setattr(mcp_web_cache, "LIST_CAP", 2)
    mock_resp = _make_mock_resp(_STRUCTURED_HTML, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/paged-list"})

    first = await _TM.call_tool("fetch_list", {"url": "http://example.com/paged-list"})
    assert "entry_start=2" in first

    # Reparse déjà mise en cache (structure.json) : on force le HTML en cache à une
    # valeur incohérente pour prouver que fetch_list ne reparse pas au second appel.
    mcp_web_cache.store_html("http://example.com/paged-list", "<html></html>")
    second = await _TM.call_tool(
        "fetch_list", {"url": "http://example.com/paged-list", "entry_start": 2}
    )
    assert "Sous-section" in second or "Lien" in second


@pytest.mark.asyncio
async def test_fetch_list_unknown_url_returns_clear_message():
    result = await _TM.call_tool("fetch_list", {"url": "http://never-fetched.example.com"})
    assert isinstance(result, str)
    assert "fetch_url" in result


@pytest.mark.asyncio
async def test_fetch_list_rejects_invalid_range():
    mock_resp = _make_mock_resp(_STRUCTURED_HTML, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/small-list"})

    result = await _TM.call_tool(
        "fetch_list", {"url": "http://example.com/small-list", "entry_start": -1}
    )
    assert "entry_start" in result
