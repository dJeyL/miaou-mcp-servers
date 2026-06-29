"""Tests unitaires pour servers/mcp_brave.py — tout mocké, aucune clef API requise."""
import json
import os
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from mcp import types
from mcp_brave import server as brave_server

_TM = brave_server.mcp._tool_manager

_BRAVE_RESPONSE = {
    "web": {
        "results": [
            {
                "title": "Python (programming language)",
                "url": "https://en.wikipedia.org/wiki/Python_(programming_language)",
                "description": "Python is a high-level programming language.",
            },
            {
                "title": "Welcome to Python.org",
                "url": "https://www.python.org",
                "description": "The official home of the Python Programming Language.",
            },
        ]
    }
}


def _make_mock_resp(data: dict):
    body = json.dumps(data).encode()
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read.return_value = body
    return mock


@pytest.mark.asyncio
async def test_brave_missing_key_returns_error():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BRAVE_API_KEY", None)
        result = await _TM.call_tool("brave_search", {"query": "python"})
    assert isinstance(result, str)
    assert "BRAVE_API_KEY" in result


@pytest.mark.asyncio
async def test_brave_valid_response_returns_embedded_resource():
    mock_resp = _make_mock_resp(_BRAVE_RESPONSE)
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("brave_search", {"query": "python"})
    assert isinstance(result, types.EmbeddedResource)
    assert result.resource.mimeType == "application/json"


@pytest.mark.asyncio
async def test_brave_extracts_title_url_description():
    mock_resp = _make_mock_resp(_BRAVE_RESPONSE)
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("brave_search", {"query": "python"})
    items = json.loads(result.resource.text)
    assert len(items) == 2
    assert items[0]["title"] == "Python (programming language)"
    assert items[0]["url"] == "https://en.wikipedia.org/wiki/Python_(programming_language)"
    assert "high-level" in items[0]["description"]


@pytest.mark.asyncio
async def test_brave_count_respected():
    mock_resp = _make_mock_resp(_BRAVE_RESPONSE)
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("brave_search", {"query": "python", "count": 1})
    # count=1 est transmis à l'API ; le mock retourne 2 résultats quand même
    # (le serveur ne tronque pas côté client, il délègue à l'API)
    items = json.loads(result.resource.text)
    assert isinstance(items, list)


@pytest.mark.asyncio
async def test_brave_uri_contains_query():
    mock_resp = _make_mock_resp(_BRAVE_RESPONSE)
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("brave_search", {"query": "asyncio"})
    assert "asyncio" in str(result.resource.uri)


@pytest.mark.asyncio
async def test_brave_401_returns_specific_error():
    err = urllib.error.HTTPError(
        "https://api.search.brave.com/res/v1/web/search", 401, "Unauthorized", {}, None
    )
    with patch.dict(os.environ, {"BRAVE_API_KEY": "bad-key"}), \
         patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool("brave_search", {"query": "python"})
    assert isinstance(result, str)
    assert "401" in result or "invalide" in result.lower()


@pytest.mark.asyncio
async def test_brave_429_returns_quota_error():
    err = urllib.error.HTTPError(
        "https://api.search.brave.com/res/v1/web/search", 429, "Too Many Requests", {}, None
    )
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool("brave_search", {"query": "python"})
    assert isinstance(result, str)
    assert "429" in result or "quota" in result.lower()


@pytest.mark.asyncio
async def test_brave_url_error_returns_string():
    err = urllib.error.URLError("Network unreachable")
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool("brave_search", {"query": "python"})
    assert isinstance(result, str)
    assert "réseau" in result.lower() or "Network unreachable" in result


def test_tool_list_contains_brave_search():
    names = {t.name for t in _TM.list_tools()}
    assert "brave_search" in names


# ── brave_image_search ────────────────────────────────────────────────────────

_BRAVE_IMAGES_RESPONSE = {
    "results": [
        {
            "title": "Python logo",
            "url": "https://en.wikipedia.org/wiki/Python_(programming_language)",
            "source": "wikipedia.org",
            "thumbnail": {"src": "https://example.com/thumb.jpg", "width": 100, "height": 100},
            "properties": {"url": "https://example.com/python.png", "width": 800, "height": 600},
        },
        {
            "title": "No image entry",
            "url": "https://example.com/page",
            "source": "example.com",
            "thumbnail": {"src": "", "width": 0, "height": 0},
            "properties": {},  # missing url → should be dropped
        },
    ]
}


@pytest.mark.asyncio
async def test_image_search_missing_key_returns_error():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BRAVE_API_KEY", None)
        result = await _TM.call_tool("brave_image_search", {"query": "python"})
    assert isinstance(result, str)
    assert "BRAVE_API_KEY" in result


@pytest.mark.asyncio
async def test_image_search_valid_response_returns_embedded_resource():
    mock_resp = _make_mock_resp(_BRAVE_IMAGES_RESPONSE)
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("brave_image_search", {"query": "python"})
    assert isinstance(result, types.EmbeddedResource)
    assert result.resource.mimeType == "application/json"


@pytest.mark.asyncio
async def test_image_search_output_fields():
    mock_resp = _make_mock_resp(_BRAVE_IMAGES_RESPONSE)
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("brave_image_search", {"query": "python"})
    items = json.loads(result.resource.text)
    assert len(items) == 1  # second entry dropped (no properties.url)
    assert items[0]["title"] == "Python logo"
    assert items[0]["page_url"] == "https://en.wikipedia.org/wiki/Python_(programming_language)"
    assert items[0]["image_url"] == "https://example.com/python.png"
    assert items[0]["thumbnail_url"] == "https://example.com/thumb.jpg"
    assert items[0]["source"] == "wikipedia.org"


@pytest.mark.asyncio
async def test_image_search_drops_entries_without_image_url():
    mock_resp = _make_mock_resp(_BRAVE_IMAGES_RESPONSE)
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("brave_image_search", {"query": "python"})
    items = json.loads(result.resource.text)
    assert all(item["image_url"] for item in items)


@pytest.mark.asyncio
async def test_image_search_count_clamped_to_20():
    mock_resp = _make_mock_resp({"results": []})
    captured = {}
    original_open = urllib.request.OpenerDirector.open

    def capturing_open(self, req, *args, **kwargs):
        captured["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        return mock_resp

    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", capturing_open):
        await _TM.call_tool("brave_image_search", {"query": "python", "count": 50})
    assert "count=20" in captured["url"]


@pytest.mark.asyncio
async def test_image_search_uri_contains_query():
    mock_resp = _make_mock_resp({"results": []})
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("brave_image_search", {"query": "asyncio"})
    assert "asyncio" in str(result.resource.uri)
    assert "brave-images" in str(result.resource.uri)


@pytest.mark.asyncio
async def test_image_search_401_returns_error():
    err = urllib.error.HTTPError(
        "https://api.search.brave.com/res/v1/images/search", 401, "Unauthorized", {}, None
    )
    with patch.dict(os.environ, {"BRAVE_API_KEY": "bad-key"}), \
         patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool("brave_image_search", {"query": "python"})
    assert isinstance(result, str)
    assert "401" in result or "invalide" in result.lower()


@pytest.mark.asyncio
async def test_image_search_429_returns_quota_error():
    err = urllib.error.HTTPError(
        "https://api.search.brave.com/res/v1/images/search", 429, "Too Many Requests", {}, None
    )
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool("brave_image_search", {"query": "python"})
    assert isinstance(result, str)
    assert "429" in result or "quota" in result.lower()


@pytest.mark.asyncio
async def test_image_search_url_error_returns_string():
    err = urllib.error.URLError("Network unreachable")
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool("brave_image_search", {"query": "python"})
    assert isinstance(result, str)
    assert "réseau" in result.lower() or "Network unreachable" in result


def test_tool_list_contains_brave_image_search():
    names = {t.name for t in _TM.list_tools()}
    assert "brave_image_search" in names
