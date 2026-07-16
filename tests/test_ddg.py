"""Tests unitaires pour servers/mcp_ddg.py."""
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from mcp import types
from mcp_ddg import server as ddg_server

_TM = ddg_server.mcp._tool_manager

# HTML minimaliste reproduisant le markup DDG (classes result__a / result__snippet).
_DDG_HTML = b"""
<html><body>
<a class="result__a" href="https://example.com">Premier r\xc3\xa9sultat</a>
<a class="result__snippet">Extrait du premier r\xc3\xa9sultat.</a>
<a class="result__a" href="https://python.org">Python</a>
<a class="result__snippet">Langage de programmation.</a>
<a class="result__a" href="https://third.example">Troisi\xc3\xa8me</a>
<a class="result__snippet">Troisi\xc3\xa8me extrait.</a>
</body></html>
"""


def _make_mock_resp(body: bytes):
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read.return_value = body
    return mock


@pytest.mark.asyncio
async def test_ddg_search_returns_embedded_resource():
    mock_resp = _make_mock_resp(_DDG_HTML)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("ddg_search", {"query": "python"})
    assert isinstance(result, types.EmbeddedResource)
    assert result.resource.mimeType == "application/json"


@pytest.mark.asyncio
async def test_ddg_search_extracts_fields():
    mock_resp = _make_mock_resp(_DDG_HTML)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("ddg_search", {"query": "python"})
    items = json.loads(result.resource.text)
    assert len(items) > 0
    first = items[0]
    assert first["url"] == "https://example.com"
    assert "résultat" in first["title"].lower()
    assert first["snippet"]


@pytest.mark.asyncio
async def test_ddg_search_max_results():
    mock_resp = _make_mock_resp(_DDG_HTML)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("ddg_search", {"query": "python", "max_results": 2})
    items = json.loads(result.resource.text)
    assert len(items) <= 2


@pytest.mark.asyncio
async def test_ddg_search_max_results_clamped_to_30():
    """B9 : max_results ne doit pas dépasser 30, ni être négatif/nul."""
    mock_resp = _make_mock_resp(_DDG_HTML)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("ddg_search", {"query": "python", "max_results": 500})
    items = json.loads(result.resource.text)
    assert len(items) <= 30


@pytest.mark.asyncio
async def test_ddg_search_max_results_clamped_to_1():
    mock_resp = _make_mock_resp(_DDG_HTML)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("ddg_search", {"query": "python", "max_results": -5})
    items = json.loads(result.resource.text)
    assert len(items) <= 1


@pytest.mark.asyncio
async def test_ddg_search_uri_contains_query():
    mock_resp = _make_mock_resp(_DDG_HTML)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("ddg_search", {"query": "asyncio"})
    assert "asyncio" in str(result.resource.uri)


@pytest.mark.asyncio
async def test_ddg_search_empty_results():
    mock_resp = _make_mock_resp(b"<html><body><p>No results</p></body></html>")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("ddg_search", {"query": "xyzzy"})
    assert isinstance(result, types.EmbeddedResource)
    assert json.loads(result.resource.text) == []


@pytest.mark.asyncio
async def test_ddg_search_http_error_returns_string():
    err = urllib.error.HTTPError("https://html.duckduckgo.com/html/", 503, "Service Unavailable", {}, None)
    with patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool("ddg_search", {"query": "python"})
    assert isinstance(result, str)
    assert "503" in result


@pytest.mark.asyncio
async def test_ddg_search_url_error_returns_string():
    err = urllib.error.URLError("Network unreachable")
    with patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool("ddg_search", {"query": "python"})
    assert isinstance(result, str)
    assert "réseau" in result.lower() or "Network unreachable" in result


def test_tool_list_contains_ddg_search():
    names = {t.name for t in _TM.list_tools()}
    assert "ddg_search" in names
