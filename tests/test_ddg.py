"""Tests unitaires pour servers/mcp_ddg.py."""
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from mcp import types
import mcp_ddg
from mcp_ddg import server as ddg_server

_TM = ddg_server.mcp._tool_manager


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    """Espacement désactivé hors des tests qui le visent, créneau remis à zéro."""
    monkeypatch.setattr(mcp_ddg, "_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(ddg_server, "_next_slot", 0.0)

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
        result = await _TM.call_tool("ddg_search", {"query": "python"}, None)
    assert isinstance(result, types.EmbeddedResource)
    assert result.resource.mime_type == "application/json"


@pytest.mark.asyncio
async def test_ddg_search_extracts_fields():
    mock_resp = _make_mock_resp(_DDG_HTML)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("ddg_search", {"query": "python"}, None)
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
        result = await _TM.call_tool("ddg_search", {"query": "python", "max_results": 2}, None)
    items = json.loads(result.resource.text)
    assert len(items) <= 2


@pytest.mark.asyncio
async def test_ddg_search_max_results_clamped_to_30():
    """B9 : max_results ne doit pas dépasser 30, ni être négatif/nul."""
    mock_resp = _make_mock_resp(_DDG_HTML)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("ddg_search", {"query": "python", "max_results": 500}, None)
    items = json.loads(result.resource.text)
    assert len(items) <= 30


@pytest.mark.asyncio
async def test_ddg_search_max_results_clamped_to_1():
    mock_resp = _make_mock_resp(_DDG_HTML)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("ddg_search", {"query": "python", "max_results": -5}, None)
    items = json.loads(result.resource.text)
    assert len(items) <= 1


@pytest.mark.asyncio
async def test_ddg_search_uri_contains_query():
    mock_resp = _make_mock_resp(_DDG_HTML)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("ddg_search", {"query": "asyncio"}, None)
    assert "asyncio" in str(result.resource.uri)


@pytest.mark.asyncio
async def test_ddg_search_empty_results():
    mock_resp = _make_mock_resp(b"<html><body><p>No results</p></body></html>")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("ddg_search", {"query": "xyzzy"}, None)
    assert isinstance(result, types.EmbeddedResource)
    assert json.loads(result.resource.text) == []


@pytest.mark.asyncio
async def test_ddg_search_bot_challenge_returns_string():
    """Page de défi anti-bot (extrait du markup mesuré le 2026-10-05) : message, pas []."""
    body = b"""<html><body><div class="anomaly-modal__modal  is-ie"><div class="anomaly-modal__box">
<div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div>
<form id="challenge-form" action="//duckduckgo.com/anomaly.js?sv=html&cc=botnet" method="POST">
</form></div></div></body></html>"""
    with patch("urllib.request.OpenerDirector.open", return_value=_make_mock_resp(body)):
        result = await _TM.call_tool("ddg_search", {"query": "python"}, None)
    assert isinstance(result, str)
    assert "anti-bot" in result


@pytest.mark.asyncio
async def test_ddg_search_http_error_returns_string():
    err = urllib.error.HTTPError("https://html.duckduckgo.com/html/", 503, "Service Unavailable", {}, None)
    with patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool("ddg_search", {"query": "python"}, None)
    assert err.fp.closed   # fermée par le serveur, pas par le test
    assert isinstance(result, str)
    assert "503" in result


@pytest.mark.asyncio
async def test_ddg_search_url_error_returns_string():
    err = urllib.error.URLError("Network unreachable")
    with patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool("ddg_search", {"query": "python"}, None)
    assert isinstance(result, str)
    assert "réseau" in result.lower() or "Network unreachable" in result


@pytest.mark.asyncio
async def test_ddg_search_throttle_waits_then_refuses(monkeypatch):
    """Rafale de trois appels : le 1er part, le 2e attend 15 s, le 3e (30 s) est refusé sans requête."""
    monkeypatch.setattr(mcp_ddg, "_MIN_INTERVAL_S", 15.0)
    waits: list[float] = []

    async def fake_sleep(delay):
        waits.append(delay)

    opened = MagicMock(side_effect=lambda *a, **k: _make_mock_resp(_DDG_HTML))
    with patch("urllib.request.OpenerDirector.open", opened), \
            patch.object(mcp_ddg.asyncio, "sleep", fake_sleep):
        first = await _TM.call_tool("ddg_search", {"query": "a"}, None)
        second = await _TM.call_tool("ddg_search", {"query": "b"}, None)
        third = await _TM.call_tool("ddg_search", {"query": "c"}, None)
    assert isinstance(first, types.EmbeddedResource)
    assert isinstance(second, types.EmbeddedResource)
    assert len(waits) == 1 and 14 < waits[0] <= 15
    assert isinstance(third, str) and "réessayer dans" in third
    assert opened.call_count == 2


def test_ddg_search_throttle_fits_miaou_timeout():
    """Pire cas d'un appel accepté (attente max + timeout de la requête) sous les 30 s de MIAOU."""
    assert mcp_ddg._MAX_WAIT_S + mcp_ddg._FETCH_TIMEOUT_S < 30


def test_tool_list_contains_ddg_search():
    names = {t.name for t in _TM.list_tools()}
    assert "ddg_search" in names
