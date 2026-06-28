"""Tests unitaires pour servers/mcp_weather.py."""
import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from mcp import types
from mcp_weather import server as weather_server


_WTTR_SAMPLE = {
    "current_condition": [{"temp_C": "12", "weatherDesc": [{"value": "Cloudy"}]}],
    "weather": [
        {
            "date": "2026-06-28",
            "astronomy": [{"sunrise": "06:00 AM"}],
            "hourly": [{"time": "0", "tempC": "10"}],
            "maxtempC": "15",
            "mintempC": "9",
        }
    ],
    "nearest_area": [{"areaName": [{"value": "Paris"}]}],
}


def _make_mock_resp(data: dict):
    body = json.dumps(data).encode()
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read.return_value = body
    return mock


@pytest.mark.asyncio
async def test_get_weather_returns_embedded_resource():
    mock_resp = _make_mock_resp(_WTTR_SAMPLE)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool("get_weather", {"city": "Paris"})
    assert isinstance(result, types.EmbeddedResource)
    assert result.resource.mimeType == "application/json"


@pytest.mark.asyncio
async def test_get_weather_strips_astronomy_and_hourly():
    mock_resp = _make_mock_resp(_WTTR_SAMPLE)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool("get_weather", {"city": "Paris"})
    data = json.loads(result.resource.text)
    for day in data.get("weather", []):
        assert "astronomy" not in day
        assert "hourly" not in day


@pytest.mark.asyncio
async def test_get_weather_uri_contains_location():
    mock_resp = _make_mock_resp(_WTTR_SAMPLE)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool(
            "get_weather", {"city": "Lyon", "country": "France"}
        )
    assert "Lyon" in str(result.resource.uri)
    assert "France" in str(result.resource.uri)


@pytest.mark.asyncio
async def test_get_weather_optional_params_omitted():
    """state et country sont optionnels — ne doivent pas apparaître dans l'URL si absents."""
    captured_url = []

    original_open = __import__("urllib.request", fromlist=["OpenerDirector"]).OpenerDirector.open

    def mock_open(self, url, *args, **kwargs):
        captured_url.append(url)
        return _make_mock_resp(_WTTR_SAMPLE)

    with patch("urllib.request.OpenerDirector.open", mock_open):
        tm = weather_server.mcp._tool_manager
        await tm.call_tool("get_weather", {"city": "Bordeaux"})

    assert len(captured_url) == 1
    assert "Bordeaux" in captured_url[0]
    # Pas de virgule parasite dans l'URL
    assert ",," not in captured_url[0]
