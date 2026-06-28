"""Tests unitaires pour servers/mcp_bench.py."""
import base64
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from mcp_bench import server as bench_server


# Tous les outils ont asyncio.sleep(2) — on le neutralise dans chaque test.


@pytest.mark.asyncio
async def test_echo_returns_input():
    with patch("asyncio.sleep", new=AsyncMock()):
        tm = bench_server.mcp._tool_manager
        result = await tm.call_tool("echo", {"text": "bonjour"})
    assert result == "bonjour"


@pytest.mark.asyncio
async def test_add_returns_formatted_sum():
    with patch("asyncio.sleep", new=AsyncMock()):
        tm = bench_server.mcp._tool_manager
        result = await tm.call_tool("add", {"a": 3.0, "b": 4.5})
    assert "3.0 + 4.5 = 7.5" in result


@pytest.mark.asyncio
async def test_dns_lookup_success():
    mock_infos = [
        (None, None, None, None, ("93.184.216.34", 0)),
        (None, None, None, None, ("93.184.216.34", 0)),
    ]
    with patch("asyncio.sleep", new=AsyncMock()), patch(
        "socket.getaddrinfo", return_value=mock_infos
    ):
        tm = bench_server.mcp._tool_manager
        result = await tm.call_tool("dns_lookup", {"hostname": "example.com"})
    assert "example.com" in result
    assert "93.184.216.34" in result


@pytest.mark.asyncio
async def test_dns_lookup_failure():
    with patch("asyncio.sleep", new=AsyncMock()), patch(
        "socket.getaddrinfo", side_effect=OSError("NXDOMAIN")
    ):
        tm = bench_server.mcp._tool_manager
        result = await tm.call_tool("dns_lookup", {"hostname": "nope.invalid"})
    assert "Échec" in result


@pytest.mark.asyncio
async def test_reverse_dns_success():
    with patch("asyncio.sleep", new=AsyncMock()), patch(
        "socket.gethostbyaddr", return_value=("host.example.com", [], ["93.184.216.34"])
    ):
        tm = bench_server.mcp._tool_manager
        result = await tm.call_tool("reverse_dns", {"ip": "93.184.216.34"})
    assert "93.184.216.34" in result
    assert "host.example.com" in result


@pytest.mark.asyncio
async def test_reverse_dns_failure():
    with patch("asyncio.sleep", new=AsyncMock()), patch(
        "socket.gethostbyaddr", side_effect=OSError("no PTR")
    ):
        tm = bench_server.mcp._tool_manager
        result = await tm.call_tool("reverse_dns", {"ip": "10.0.0.1"})
    assert "Échec" in result


@pytest.mark.asyncio
async def test_get_image_returns_image_object():
    from mcp.server.fastmcp import Image

    with patch("asyncio.sleep", new=AsyncMock()):
        tm = bench_server.mcp._tool_manager
        result = await tm.call_tool("get_image", {})
    assert isinstance(result, Image)
    # Vérifie que le PNG commence par la signature PNG
    assert result.data is not None
    png_bytes = result.data
    assert png_bytes[:4] == b"\x89PNG"


@pytest.mark.asyncio
async def test_get_json_resource_returns_embedded_resource():
    import json as json_mod

    from mcp import types

    with patch("asyncio.sleep", new=AsyncMock()):
        tm = bench_server.mcp._tool_manager
        result = await tm.call_tool("get_json_resource", {})
    assert isinstance(result, types.EmbeddedResource)
    assert result.resource.mimeType == "application/json"
    data = json_mod.loads(result.resource.text)
    assert data["ok"] is True
    assert data["items"] == [1, 2, 3]


def test_tool_list_contains_all_expected():
    """Six outils doivent être enregistrés."""
    tools = bench_server.mcp._tool_manager.list_tools()
    names = {t.name for t in tools}
    assert names == {"echo", "add", "dns_lookup", "reverse_dns", "get_image", "get_json_resource"}
