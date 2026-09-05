"""Tests unitaires pour servers/mcp_weather.py."""
import io
import json
import sys
import urllib.error
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


@pytest.mark.asyncio
async def test_get_weather_http_error_returns_clear_string():
    """B6 : une erreur réseau doit renvoyer un message clair, pas fuiter
    l'exception urllib brute."""
    err = urllib.error.HTTPError("http://wttr.in/x", 503, "Service Unavailable", {}, None)
    with patch("urllib.request.OpenerDirector.open", side_effect=err):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool("get_weather", {"city": "Paris"})
    assert isinstance(result, str)
    assert "503" in result


@pytest.mark.asyncio
async def test_get_weather_url_error_returns_clear_string():
    err = urllib.error.URLError("Connection refused")
    with patch("urllib.request.OpenerDirector.open", side_effect=err):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool("get_weather", {"city": "Paris"})
    assert isinstance(result, str)
    assert "réseau" in result.lower() or "Connection refused" in result


@pytest.mark.asyncio
async def test_get_weather_invalid_json_returns_clear_string():
    """wttr.in peut renvoyer du HTML d'erreur au lieu de JSON."""
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read.return_value = b"<html>error</html>"
    with patch("urllib.request.OpenerDirector.open", return_value=mock):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool("get_weather", {"city": "Paris"})
    assert isinstance(result, str)
    assert "invalide" in result.lower()


@pytest.mark.asyncio
async def test_get_weather_truncated_utf8_does_not_raise():
    """W1 : une réponse tronquée à la borne des 2 Mo peut couper un caractère
    multi-octets — errors='replace' évite qu'UnicodeDecodeError fuite au client."""
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    body = json.dumps({"weather": [], "current_condition": []}).encode()
    mock.read.return_value = body[:-1] + "é".encode("utf-8")[:1]  # coupe un multi-octet
    with patch("urllib.request.OpenerDirector.open", return_value=mock):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool("get_weather", {"city": "Paris"})
    assert isinstance(result, str)
    assert "invalide" in result.lower()


@pytest.mark.asyncio
async def test_get_weather_degenerate_weather_field_returns_clear_string():
    """W2 : 'weather' n'est pas une liste — pas d'AttributeError/TypeError fuité."""
    mock_resp = _make_mock_resp({"weather": "not-a-list", "current_condition": []})
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool("get_weather", {"city": "Paris"})
    assert isinstance(result, str)
    assert "invalide" in result.lower()


@pytest.mark.asyncio
async def test_get_weather_weather_entry_not_dict_skipped():
    """W2 : une entrée de 'weather' qui n'est pas un dict est ignorée sans planter."""
    mock_resp = _make_mock_resp({"weather": ["not-a-dict", {"date": "x"}], "current_condition": []})
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool("get_weather", {"city": "Paris"})
    assert isinstance(result, types.EmbeddedResource)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args, expect_astronomy, expect_hourly",
    [
        ({}, False, False),
        ({"astronomy": True}, True, False),
        ({"hourly": True}, False, True),
        ({"astronomy": True, "hourly": True}, True, True),
    ],
)
async def test_get_weather_astronomy_and_hourly_are_independent(
    args, expect_astronomy, expect_hourly
):
    """Les deux blocs se demandent séparément : demander l'un ne ramène pas l'autre."""
    mock_resp = _make_mock_resp(_WTTR_SAMPLE)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool("get_weather", {"city": "Paris", **args})
    day = json.loads(result.resource.text)["weather"][0]
    assert ("astronomy" in day) is expect_astronomy
    assert ("hourly" in day) is expect_hourly
    if expect_astronomy:
        assert day["astronomy"] == [{"sunrise": "06:00 AM"}]
    if expect_hourly:
        assert day["hourly"] == [{"time": "0", "tempC": "10"}]


@pytest.mark.asyncio
async def test_get_weather_extract_returns_blob_and_descriptor():
    """extract=true : descripteur au modèle + blob binaire hors contexte (store_binary)."""
    import base64

    mock_resp = _make_mock_resp(_WTTR_SAMPLE)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool("get_weather", {"city": "Paris", "extract": True})

    assert isinstance(result, list) and len(result) == 2
    descripteur, resource = result
    assert isinstance(descripteur, types.TextContent)
    assert "weather-paris-20260628.json" in descripteur.text
    assert isinstance(resource, types.EmbeddedResource)
    assert isinstance(resource.resource, types.BlobResourceContents)
    assert resource.resource.mimeType == "application/json"
    data = json.loads(base64.b64decode(resource.resource.blob).decode())
    assert "astronomy" not in data["weather"][0]
    assert str(resource.resource.uri).endswith("weather-paris-20260628.json")


@pytest.mark.asyncio
async def test_get_weather_extract_name_slugifies_location():
    """Accents, espaces et virgule du lieu ne doivent pas fuiter dans le nom de ressource."""
    mock_resp = _make_mock_resp(_WTTR_SAMPLE)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool(
            "get_weather",
            {"city": "Saint-Étienne", "country": "France", "extract": True},
        )
    uri = str(result[1].resource.uri)
    assert "weather-saint-etienne-france-20260628.json" in uri


@pytest.mark.asyncio
async def test_get_weather_extract_falls_back_to_today_without_date():
    """Sans date exploitable dans la réponse, le nom retombe sur la date locale."""
    import datetime

    mock_resp = _make_mock_resp({"weather": [{"maxtempC": "15"}], "current_condition": []})
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool("get_weather", {"city": "Paris", "extract": True})
    today = datetime.date.today().strftime("%Y%m%d")
    assert f"weather-paris-{today}.json" in str(result[1].resource.uri)


@pytest.mark.asyncio
async def test_get_weather_blocks_and_extract_combine():
    """Les paramètres de contenu sont orthogonaux à extract : blob binaire complet."""
    import base64

    mock_resp = _make_mock_resp(_WTTR_SAMPLE)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool(
            "get_weather",
            {"city": "Paris", "astronomy": True, "hourly": True, "extract": True},
        )
    data = json.loads(base64.b64decode(result[1].resource.blob).decode())
    assert "hourly" in data["weather"][0]
    assert "astronomy" in data["weather"][0]
    assert "avec astronomy et hourly" in result[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args, expected",
    [
        ({}, "allégée"),
        ({"astronomy": True}, "avec astronomy"),
        ({"hourly": True}, "avec hourly"),
    ],
)
async def test_get_weather_extract_descriptor_states_content(args, expected):
    """Le descripteur dit littéralement ce que la ressource contient — le modèle
    n'a que lui pour savoir s'il a demandé le bon niveau de détail."""
    mock_resp = _make_mock_resp(_WTTR_SAMPLE)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool(
            "get_weather", {"city": "Paris", "extract": True, **args}
        )
    assert expected in result[0].text


@pytest.mark.asyncio
async def test_get_weather_defaults_unchanged():
    """Sans les nouveaux paramètres, le comportement d'avant à l'octet près :
    TextResourceContents, uri sur le lieu brut, astronomy/hourly retirés."""
    mock_resp = _make_mock_resp(_WTTR_SAMPLE)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool("get_weather", {"city": "Paris"})
    assert isinstance(result, types.EmbeddedResource)
    assert isinstance(result.resource, types.TextResourceContents)
    assert str(result.resource.uri) == "miaou://weather/Paris"


@pytest.mark.asyncio
async def test_get_weather_non_dict_json_returns_clear_string():
    """wttr.in renvoyant un JSON de type liste ne doit pas fuiter d'AttributeError."""
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read.return_value = b"[1, 2, 3]"
    with patch("urllib.request.OpenerDirector.open", return_value=mock):
        tm = weather_server.mcp._tool_manager
        result = await tm.call_tool("get_weather", {"city": "Paris"})
    assert isinstance(result, str)
    assert "invalide" in result.lower()
