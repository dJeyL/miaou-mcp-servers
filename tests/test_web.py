"""Tests unitaires pour servers/mcp_web.py."""
import base64
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from mcp import types
from mcp_web import server as fetch_server

_TM = fetch_server.mcp._tool_manager


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
    assert result.resource.mimeType == "application/json"


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
