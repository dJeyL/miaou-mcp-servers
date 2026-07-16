"""Tests pour mcp_proxy.py : config, upstreams, routing."""
import os
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
    apply_proxy_env_overrides_to_process,
    build_app,
    build_proxy_server,
    build_upstreams,
    compute_proxy_env_overrides,
    load_config,
    merge_proxy_env_overrides,
    resolve_proxy_url,
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


def test_load_config_invalid_json_raises_clear_error(tmp_path):
    """PRX3 : un config.json malformé doit lever un message clair, pas un
    json.JSONDecodeError brut face à l'opérateur."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{not valid json")
    with pytest.raises(ValueError, match="JSON"):
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
# StdioUpstream — timeout de handshake (PRX4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stdio_upstream_start_times_out_on_hanging_handshake(monkeypatch):
    """PRX4 : un subprocess qui ne répond jamais à initialize ne doit pas
    bloquer le démarrage du proxy indéfiniment — timeout avec message clair."""
    import asyncio

    monkeypatch.setattr(mcp_proxy, "_STDIO_HANDSHAKE_TIMEOUT_S", 0.05)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _hanging_stdio_client(params):
        await asyncio.sleep(10)
        yield (None, None)

    with patch("mcp.client.stdio.stdio_client", _hanging_stdio_client):
        upstream = StdioUpstream(command="does-not-matter", args=[])
        with pytest.raises(RuntimeError, match="handshake"):
            await upstream.start()


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


# ---------------------------------------------------------------------------
# InProcessUpstream — support env
# ---------------------------------------------------------------------------

def test_build_upstreams_inprocess_env_stored():
    """L'entrée env de config.json est transmise à InProcessUpstream."""
    cfg = {
        "port": 8767,
        "mcpServers": {
            "brave": {
                "type": "inprocess",
                "module": "mcp_bench",
                "env": {"MY_SECRET": "value"},
            }
        },
    }
    upstreams = build_upstreams(cfg)
    assert upstreams["brave"]._env == {"MY_SECRET": "value"}


@pytest.mark.asyncio
async def test_inprocess_upstream_env_applied_before_import():
    """os.environ.setdefault est appelé avant importlib.import_module."""
    sentinel_key = "MIAOU_TEST_SENTINEL_KEY_XYZ"
    import os
    os.environ.pop(sentinel_key, None)

    upstream = InProcessUpstream("mcp_bench", env={sentinel_key: "sentinel-value"})
    await upstream.start()

    assert os.environ.get(sentinel_key) == "sentinel-value"
    del os.environ[sentinel_key]


@pytest.mark.asyncio
async def test_inprocess_upstream_env_does_not_override_existing():
    """setdefault ne doit pas écraser une variable déjà présente."""
    import os
    key = "MIAOU_TEST_EXISTING_KEY_XYZ"
    os.environ[key] = "original"

    upstream = InProcessUpstream("mcp_bench", env={key: "should-not-override"})
    await upstream.start()

    assert os.environ[key] == "original"
    del os.environ[key]


# ---------------------------------------------------------------------------
# Override CLI --proxy / --noproxy
# ---------------------------------------------------------------------------

def test_resolve_proxy_url_adds_scheme_if_missing():
    assert resolve_proxy_url("127.0.0.1:3128") == "http://127.0.0.1:3128"


def test_resolve_proxy_url_keeps_existing_scheme():
    assert resolve_proxy_url("https://proxy.example:3128") == "https://proxy.example:3128"


def test_compute_proxy_env_overrides_none_by_default():
    assert compute_proxy_env_overrides(None, False) is None


def test_compute_proxy_env_overrides_proxy_sets_all_case_variants():
    overrides = compute_proxy_env_overrides("127.0.0.1:3128", False)
    assert overrides == {
        "http_proxy": "http://127.0.0.1:3128",
        "https_proxy": "http://127.0.0.1:3128",
        "HTTP_PROXY": "http://127.0.0.1:3128",
        "HTTPS_PROXY": "http://127.0.0.1:3128",
    }


def test_compute_proxy_env_overrides_noproxy_clears_all_case_variants():
    overrides = compute_proxy_env_overrides(None, True)
    assert overrides == {
        "http_proxy": None,
        "https_proxy": None,
        "HTTP_PROXY": None,
        "HTTPS_PROXY": None,
    }


def test_apply_proxy_env_overrides_to_process_sets_and_clears(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://old:1")
    monkeypatch.delenv("https_proxy", raising=False)

    apply_proxy_env_overrides_to_process(
        {"http_proxy": "http://new:2", "https_proxy": "http://new:2"}
    )
    assert os.environ["http_proxy"] == "http://new:2"
    assert os.environ["https_proxy"] == "http://new:2"

    apply_proxy_env_overrides_to_process({"http_proxy": None, "https_proxy": None})
    assert "http_proxy" not in os.environ
    assert "https_proxy" not in os.environ


def test_merge_proxy_env_overrides_none_leaves_env_untouched():
    assert merge_proxy_env_overrides(None, None) is None
    assert merge_proxy_env_overrides({"A": "1"}, None) == {"A": "1"}


def test_merge_proxy_env_overrides_cli_wins_over_config_env():
    """--proxy écrase un http_proxy déjà défini explicitement dans config.json."""
    env = {"http_proxy": "http://from-config:1", "OTHER": "kept"}
    overrides = {"http_proxy": "http://from-cli:2", "https_proxy": "http://from-cli:2"}
    merged = merge_proxy_env_overrides(env, overrides)
    assert merged == {
        "http_proxy": "http://from-cli:2",
        "https_proxy": "http://from-cli:2",
        "OTHER": "kept",
    }


def test_merge_proxy_env_overrides_noproxy_removes_config_env():
    """--noproxy retire même un http_proxy explicitement défini dans config.json."""
    env = {"http_proxy": "http://from-config:1", "OTHER": "kept"}
    overrides = {"http_proxy": None, "https_proxy": None}
    merged = merge_proxy_env_overrides(env, overrides)
    assert merged == {"OTHER": "kept"}


def test_build_upstreams_stdio_applies_proxy_overrides():
    cfg = {
        "port": 8767,
        "mcpServers": {
            "external": {
                "type": "stdio",
                "command": "uv",
                "args": ["run", "servers/mcp_bench.py", "--transport", "stdio"],
                "env": {"http_proxy": "http://from-config:1"},
            }
        },
    }
    overrides = {"http_proxy": "http://from-cli:2", "https_proxy": "http://from-cli:2"}
    upstreams = build_upstreams(cfg, proxy_env_overrides=overrides)
    assert upstreams["external"]._env == {
        "http_proxy": "http://from-cli:2",
        "https_proxy": "http://from-cli:2",
    }


# ---------------------------------------------------------------------------
# InProcessUpstream — support config (multi-instance du même module)
# ---------------------------------------------------------------------------

def test_build_upstreams_inprocess_config_stored():
    """L'entrée config de config.json est transmise à InProcessUpstream."""
    cfg = {
        "port": 8767,
        "mcpServers": {
            "ibm_prod": {
                "type": "inprocess",
                "module": "mcp_bench",
                "config": {"base_url": "https://prod.example.com"},
            }
        },
    }
    upstreams = build_upstreams(cfg)
    assert upstreams["ibm_prod"]._config == {"base_url": "https://prod.example.com"}


@pytest.mark.asyncio
async def test_inprocess_upstream_uses_build_when_available():
    """Un module avec build(config) est instancié via build(), pas via l'attribut mcp."""
    import types as _types

    from mcp.server.fastmcp import FastMCP

    fake_module = _types.ModuleType("mcp_fake_buildable")
    captured: dict = {}

    def build(config=None):
        captured["config"] = config
        fastmcp = FastMCP("fake")

        @fastmcp.tool()
        def get_base_url() -> str:
            return (config or {}).get("base_url", "unset")

        return fastmcp

    fake_module.build = build
    sys.modules["mcp_fake_buildable"] = fake_module

    try:
        upstream = InProcessUpstream("mcp_fake_buildable", config={"base_url": "https://prod.example.com"})
        await upstream.start()
        assert captured["config"] == {"base_url": "https://prod.example.com"}
        result = await upstream.call_tool("get_base_url", {})
        assert any("prod.example.com" in str(r) for r in result)
    finally:
        del sys.modules["mcp_fake_buildable"]


@pytest.mark.asyncio
async def test_inprocess_upstream_falls_back_to_mcp_singleton_without_build():
    """Non-régression : un module sans build() (tous les serveurs existants
    aujourd'hui) continue de résoudre via l'attribut module.mcp, comme avant."""
    upstream = InProcessUpstream("mcp_bench")
    await upstream.start()
    tools = await upstream.list_tools()
    assert {t.name for t in tools} >= {"echo", "add"}


@pytest.mark.asyncio
async def test_inprocess_upstream_warns_on_shared_module_without_build(capsys):
    """PRX5 : deux entrées inprocess pointant le même module sans build()
    partagent le même singleton FastMCP (env figé au premier import) — un
    warning stderr doit signaler la situation, pas silencieusement."""
    first = InProcessUpstream("mcp_bench")
    second = InProcessUpstream("mcp_bench")
    await first.start()
    capsys.readouterr()  # ignore un éventuel warning du 1er import (déjà chargé par d'autres tests)
    await second.start()
    captured = capsys.readouterr()
    assert "mcp_bench" in captured.err
    assert "build" in captured.err


@pytest.mark.asyncio
async def test_inprocess_upstream_two_instances_same_module_different_config():
    """Le cas d'usage cible : deux instances du même module (un seul fichier),
    chacune avec sa propre config, produisent des FastMCP indépendants qui
    renvoient des valeurs différentes."""
    import types as _types

    from mcp.server.fastmcp import FastMCP

    fake_module = _types.ModuleType("mcp_fake_multi")

    def build(config=None):
        fastmcp = FastMCP("fake-multi")

        @fastmcp.tool()
        def get_base_url() -> str:
            return (config or {}).get("base_url", "unset")

        return fastmcp

    fake_module.build = build
    sys.modules["mcp_fake_multi"] = fake_module

    try:
        upstream_prod = InProcessUpstream("mcp_fake_multi", config={"base_url": "prod"})
        upstream_uat = InProcessUpstream("mcp_fake_multi", config={"base_url": "uat"})
        await upstream_prod.start()
        await upstream_uat.start()

        result_prod = await upstream_prod.call_tool("get_base_url", {})
        result_uat = await upstream_uat.call_tool("get_base_url", {})

        assert any("prod" in str(r) for r in result_prod)
        assert any("uat" in str(r) for r in result_uat)
    finally:
        del sys.modules["mcp_fake_multi"]


@pytest.mark.asyncio
async def test_proxy_call_tool_falls_back_to_prefix_when_not_in_tool_map():
    """P2 : si `name` n'est pas dans tool_map au moment de l'appel (ex. le
    rafraîchissement automatique du cache SDK n'a pas (encore) exposé cet
    outil précis), le fallback par préfixe doit router quand même vers le bon
    upstream plutôt que de renvoyer "Outil inconnu" à tort."""
    import mcp.types as types

    mock_upstream = MagicMock()
    mock_upstream.list_tools = AsyncMock(return_value=[])  # cache SDK : rien à lister
    mock_upstream.call_tool = AsyncMock(
        return_value=[types.TextContent(type="text", text="hello")]
    )
    upstreams = {"bench": mock_upstream}
    tool_map: dict = {}  # "bench__echo" absent malgré le upstream connu
    server = build_proxy_server(upstreams, tool_map)

    call_handler = server.request_handlers.get(types.CallToolRequest)
    await call_handler(
        types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name="bench__echo", arguments={"text": "hi"}),
        )
    )
    mock_upstream.call_tool.assert_awaited_once_with("echo", {"text": "hi"})


@pytest.mark.asyncio
async def test_proxy_call_tool_unknown_prefix_still_fails():
    """Le fallback ne doit pas masquer un vrai outil inconnu : un préfixe qui
    ne correspond à aucun upstream reste une erreur (T7 : fusion de deux tests
    quasi-identiques, même chemin de code — outil inconnu = préfixe inconnu
    ici, aucun upstream enregistré). Le pattern try/except est tolérant par
    design : le SDK peut soit capturer McpError en isError, soit la propager."""
    import mcp.types as types
    from mcp.shared.exceptions import McpError

    upstreams: dict = {}
    tool_map: dict = {}
    server = build_proxy_server(upstreams, tool_map)

    call_handler = server.request_handlers.get(types.CallToolRequest)
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="ghost__tool", arguments={}),
    )
    try:
        result = await call_handler(request)
        assert result.root.isError, "Attendu isError=True pour un outil inconnu"
    except McpError:
        pass  # comportement attendu si le SDK propage l'exception


# ---------------------------------------------------------------------------
# build_app — /mcp sans slash final ne doit pas rediriger (307)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_app_mcp_without_trailing_slash_no_redirect():
    """POST /mcp doit être servi directement, sans 307 vers /mcp/.

    Mount("/mcp", ...) redirige par défaut en 307 ; certains clients MCP ne
    suivent pas les redirections sur POST/DELETE, d'où l'intérêt du test.
    On appelle directement l'app ASGI (sans lifespan ni vrai handshake MCP,
    déjà couverts par d'autres tests) et on inspecte le scope reçu en aval.
    """
    server = build_proxy_server({}, {})
    app = build_app(server, {})

    received_paths = []

    async def fake_session_manager_handle_request(self, scope, receive, send):
        received_paths.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    with patch.object(
        mcp_proxy.StreamableHTTPSessionManager,
        "handle_request",
        new=fake_session_manager_handle_request,
    ):
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "headers": [],
            "query_string": b"",
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)

    assert received_paths == ["/mcp/"]
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 200


# ---------------------------------------------------------------------------
# Portée du sentinel REF_UNKNOWN (PRX1/PRX2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ref_unknown_sentinel_not_converted_for_upstream_without_contract():
    """PRX1 : un upstream qui ne déclare pas le contrat REF_UNKNOWN garde son
    isError textuel, même si le texte contient « REF_UNKNOWN » — avant le
    scoping, ce faux positif était converti en erreur JSON-RPC et déclenchait un
    rejeu client inutile."""
    import mcp.types as types

    upstream = MagicMock()
    upstream.ref_unknown_contract = None
    upstream.call_tool = AsyncMock(
        side_effect=RuntimeError("boom REF_UNKNOWN mentionné dans le message")
    )
    upstream.list_tools = AsyncMock(return_value=[])

    server = build_proxy_server({"other": upstream}, {"other__t": ("other", "t")})
    handler = server.request_handlers[types.CallToolRequest]
    result = await handler(
        types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name="other__t", arguments={}),
        )
    )

    # Pas de McpError levée : le résultat reste un CallToolResult isError.
    assert result.root.isError is True
    assert "REF_UNKNOWN" in result.root.content[0].text


@pytest.mark.asyncio
async def test_ref_unknown_contract_read_from_upstream_module():
    """PRX2 : le contrat est lu sur le module upstream réellement importé
    (getattr), pas via un import mcp_docs codé en dur dans le proxy."""
    upstream = InProcessUpstream("mcp_docs")
    await upstream.start()

    import mcp_docs

    assert upstream.ref_unknown_contract == (
        mcp_docs.REF_UNKNOWN_SENTINEL,
        mcp_docs.REF_UNKNOWN_ERROR_CODE,
    )

    # Un module sans les constantes ne déclare aucun contrat.
    bench = InProcessUpstream("mcp_bench")
    await bench.start()
    assert bench.ref_unknown_contract is None


def test_proxy_without_docs_does_not_import_mcp_docs(tmp_path):
    """PRX2 : un proxy configuré sans l'entrée docs ne doit pas importer
    mcp_docs (ni pymupdf/python-docx/openpyxl/python-pptx, ni instancier
    DocsServer()). Vérifié dans un subprocess neuf : mcp_docs est déjà chargé
    dans le process de test par les autres cas."""
    import subprocess
    import textwrap

    script = textwrap.dedent(
        f"""
        import asyncio, sys
        sys.path.insert(0, {str(_ROOT)!r})
        sys.path.insert(0, {str(_SERVERS)!r})
        from mcp_proxy import build_upstreams, build_proxy_server

        cfg = {{"port": 8767, "mcpServers": {{"bench": {{"type": "inprocess", "module": "mcp_bench"}}}}}}
        upstreams = build_upstreams(cfg)

        async def go():
            for u in upstreams.values():
                await u.start()
            build_proxy_server(upstreams, {{}})

        asyncio.run(go())
        assert "mcp_docs" not in sys.modules, "mcp_docs importé alors que docs est absent de la config"
        print("OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
