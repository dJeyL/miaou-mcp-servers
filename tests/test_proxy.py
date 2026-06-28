"""Tests pour mcp_proxy.py : config, upstreams, routing."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Assure que servers/ et la racine sont dans sys.path
_ROOT = Path(__file__).parent.parent
_SERVERS = _ROOT / "servers"
for p in (_ROOT, _SERVERS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import mcp_proxy
from mcp_proxy import (
    InProcessUpstream,
    StdioUpstream,
    build_proxy_server,
    build_upstreams,
    load_config,
)


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def test_load_config_nominal(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text('{"port": 8767, "mcpServers": {}}')
    cfg = load_config(cfg_file)
    assert cfg["port"] == 8767


def test_load_config_missing_port(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text('{"mcpServers": {}}')
    with pytest.raises(ValueError, match="port"):
        load_config(cfg_file)


# ---------------------------------------------------------------------------
# build_upstreams
# ---------------------------------------------------------------------------

def test_build_upstreams_inprocess():
    cfg = {
        "port": 8767,
        "mcpServers": {"bench": {"type": "inprocess", "module": "mcp_bench"}},
    }
    upstreams = build_upstreams(cfg)
    assert "bench" in upstreams
    assert isinstance(upstreams["bench"], InProcessUpstream)


def test_build_upstreams_stdio():
    cfg = {
        "port": 8767,
        "mcpServers": {
            "ext": {"command": "uv", "args": ["run", "something.py"]}
        },
    }
    upstreams = build_upstreams(cfg)
    assert isinstance(upstreams["ext"], StdioUpstream)


def test_build_upstreams_unknown_type():
    cfg = {
        "port": 8767,
        "mcpServers": {"bad": {"type": "grpc"}},
    }
    with pytest.raises(ValueError, match="grpc"):
        build_upstreams(cfg)


def test_build_upstreams_inprocess_missing_module():
    cfg = {
        "port": 8767,
        "mcpServers": {"oops": {"type": "inprocess"}},
    }
    with pytest.raises(ValueError, match="module"):
        build_upstreams(cfg)


def test_build_upstreams_disabled_skipped():
    cfg = {
        "port": 8767,
        "mcpServers": {
            "bench": {"type": "inprocess", "module": "mcp_bench"},
            "off": {"_disabled": True, "command": "uv", "args": []},
        },
    }
    upstreams = build_upstreams(cfg)
    assert "bench" in upstreams
    assert "off" not in upstreams


def test_build_upstreams_stdio_missing_command():
    cfg = {
        "port": 8767,
        "mcpServers": {"oops": {"args": ["run"]}},
    }
    with pytest.raises(ValueError, match="command"):
        build_upstreams(cfg)


# ---------------------------------------------------------------------------
# InProcessUpstream — liste et appel d'outils
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inprocess_upstream_list_tools():
    upstream = InProcessUpstream("mcp_bench")
    await upstream.start()
    tools = await upstream.list_tools()
    names = {t.name for t in tools}
    assert {"echo", "add", "get_image", "get_json_resource", "dns_lookup", "reverse_dns"} == names


@pytest.mark.asyncio
async def test_inprocess_upstream_call_echo():
    from unittest.mock import AsyncMock, patch

    upstream = InProcessUpstream("mcp_bench")
    await upstream.start()
    with patch("asyncio.sleep", new=AsyncMock()):
        result = await upstream.call_tool("echo", {"text": "test"})
    # convert_result=True wraps the string in a TextContent block
    assert any("test" in str(r) for r in result)


# ---------------------------------------------------------------------------
# Proxy server — préfixage et routing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proxy_list_tools_prefixes():
    """Les outils doivent apparaître préfixés du nom du serveur."""
    mock_upstream = MagicMock()
    import mcp.types as types

    mock_upstream.list_tools = AsyncMock(
        return_value=[
            types.Tool(name="echo", description="", inputSchema={}),
            types.Tool(name="add", description="", inputSchema={}),
        ]
    )

    upstreams = {"bench": mock_upstream}
    tool_map: dict = {}
    server = build_proxy_server(upstreams, tool_map)

    # Appelle le handler list_tools directement via le request_handlers dict
    handler = server.request_handlers.get(types.ListToolsRequest)
    assert handler is not None

    result = await handler(types.ListToolsRequest(method="tools/list", params=None))
    names = {t.name for t in result.root.tools}
    assert "bench__echo" in names
    assert "bench__add" in names
    assert "echo" not in names


@pytest.mark.asyncio
async def test_proxy_call_tool_routes_correctly():
    """call_tool doit dépréfixer le nom et router vers le bon upstream."""
    mock_upstream = MagicMock()
    import mcp.types as types

    mock_upstream.list_tools = AsyncMock(
        return_value=[types.Tool(name="echo", description="", inputSchema={})]
    )
    mock_upstream.call_tool = AsyncMock(
        return_value=[types.TextContent(type="text", text="hello")]
    )

    upstreams = {"bench": mock_upstream}
    tool_map: dict = {}
    server = build_proxy_server(upstreams, tool_map)

    # Peuple le tool_map via list_tools
    list_handler = server.request_handlers.get(types.ListToolsRequest)
    await list_handler(types.ListToolsRequest(method="tools/list", params=None))

    call_handler = server.request_handlers.get(types.CallToolRequest)
    await call_handler(
        types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name="bench__echo", arguments={"text": "hello"}),
        )
    )
    mock_upstream.call_tool.assert_awaited_once_with("echo", {"text": "hello"})


@pytest.mark.asyncio
async def test_proxy_call_unknown_tool_returns_error():
    """Un outil inconnu doit lever McpError (propagé ou capturé par le SDK en isError)."""
    import mcp.types as types
    from mcp.shared.exceptions import McpError

    upstreams: dict = {}
    tool_map: dict = {}

    server = build_proxy_server(upstreams, tool_map)
    call_handler = server.request_handlers.get(types.CallToolRequest)
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="nonexistent__tool", arguments={}),
    )
    try:
        result = await call_handler(request)
        # Le SDK a capturé McpError et renvoyé une réponse isError
        assert result.root.isError, "Attendu isError=True pour un outil inconnu"
    except McpError:
        pass  # comportement attendu si le SDK propage l'exception
