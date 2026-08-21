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
import mcp_brave
from mcp_brave import server as brave_server

_TM = brave_server.mcp._tool_manager

# Le singleton est construit à l'import avec require_api_key=False : sans clef
# dans l'environnement à ce moment-là, self.api_key serait vide et les tests qui
# patchent os.environ après coup ne la verraient pas (elle n'est plus relue à
# chaque appel). On la pose explicitement pour que les mocks d'opener soient
# exercés avec une clef présente.
brave_server.api_key = "test-key"

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


def test_build_without_key_raises():
    """Sans clef, le serveur refuse de s'initialiser au lieu d'exposer des outils
    qui échoueraient à chaque appel."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BRAVE_API_KEY", None)
        with pytest.raises(mcp_brave.MissingAPIKeyError):
            mcp_brave.build(None)


def test_build_with_key_from_config_succeeds():
    """La clef du bloc config d'une entrée config.json suffit, sans variable
    d'environnement (multi-instance : os.environ ne peut pas distinguer
    plusieurs entrées du même module)."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BRAVE_API_KEY", None)
        built = mcp_brave.build({"api_key": "cfg-key"})
    assert built is not None


def test_build_with_key_from_env_succeeds():
    with patch.dict(os.environ, {"BRAVE_API_KEY": "env-key"}):
        built = mcp_brave.build(None)
    assert built is not None


def test_config_key_takes_precedence_over_env():
    with patch.dict(os.environ, {"BRAVE_API_KEY": "env-key"}):
        assert mcp_brave.resolve_api_key({"api_key": "cfg-key"}) == "cfg-key"


def test_blank_key_is_treated_as_missing():
    """Une clef vide ou uniquement des espaces ne doit pas passer pour une clef."""
    with patch.dict(os.environ, {"BRAVE_API_KEY": "   "}):
        assert mcp_brave.resolve_api_key(None) == ""
        with pytest.raises(mcp_brave.MissingAPIKeyError):
            mcp_brave.build({"api_key": "  "})


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


@pytest.mark.asyncio
async def test_brave_search_degenerate_web_field_returns_empty_results():
    """W2-bis : data['web'] scalaire au lieu d'un dict — pas d'AttributeError
    fuité ; traité comme absence de résultats, pas une erreur (le JSON est
    valide, juste dégénéré sur ce champ)."""
    mock_resp = _make_mock_resp({"web": None})
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("brave_search", {"query": "python"})
    assert isinstance(result, types.EmbeddedResource)
    assert json.loads(result.resource.text) == []


@pytest.mark.asyncio
async def test_brave_search_degenerate_results_field_returns_clear_string():
    mock_resp = _make_mock_resp({"web": {"results": "not-a-list"}})
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("brave_search", {"query": "python"})
    assert isinstance(result, str)
    assert "invalide" in result.lower()


@pytest.mark.asyncio
async def test_brave_search_result_entry_not_dict_skipped():
    mock_resp = _make_mock_resp({"web": {"results": ["not-a-dict"]}})
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("brave_search", {"query": "python"})
    items = json.loads(result.resource.text)
    assert items == []


@pytest.mark.asyncio
async def test_brave_search_count_clamped_to_20():
    """B12 : count doit être clampé symétriquement à web et images (l'API
    Brave web plafonne aussi à 20, 422 au-delà)."""
    captured = {}

    def capturing_open(self, req, *args, **kwargs):
        captured["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        return _make_mock_resp({"web": {"results": []}})

    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", capturing_open):
        await _TM.call_tool("brave_search", {"query": "python", "count": 50})
    assert "count=20" in captured["url"]


@pytest.mark.asyncio
async def test_brave_search_count_clamped_to_1():
    captured = {}

    def capturing_open(self, req, *args, **kwargs):
        captured["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        return _make_mock_resp({"web": {"results": []}})

    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", capturing_open):
        await _TM.call_tool("brave_search", {"query": "python", "count": 0})
    assert "count=1" in captured["url"]


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


@pytest.mark.asyncio
async def test_image_search_handles_null_thumbnail():
    """B13 : "thumbnail": null (pas absent, explicitement null) ne doit pas
    lever AttributeError — .get() ne protège que l'absence de clé."""
    response = {
        "results": [
            {
                "title": "No thumbnail",
                "url": "https://example.com/page",
                "source": "example.com",
                "thumbnail": None,
                "properties": {"url": "https://example.com/img.png"},
            }
        ]
    }
    mock_resp = _make_mock_resp(response)
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("brave_image_search", {"query": "python"})
    items = json.loads(result.resource.text)
    assert len(items) == 1
    assert items[0]["thumbnail_url"] == ""


@pytest.mark.asyncio
async def test_image_search_degenerate_results_field_returns_clear_string():
    """W2-bis : data['results'] scalaire — pas d'AttributeError fuité."""
    mock_resp = _make_mock_resp({"results": "not-a-list"})
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("brave_image_search", {"query": "python"})
    assert isinstance(result, str)
    assert "invalide" in result.lower()


@pytest.mark.asyncio
async def test_image_search_result_entry_not_dict_skipped():
    mock_resp = _make_mock_resp({"results": ["not-a-dict"]})
    with patch.dict(os.environ, {"BRAVE_API_KEY": "test-key"}), \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("brave_image_search", {"query": "python"})
    items = json.loads(result.resource.text)
    assert items == []
