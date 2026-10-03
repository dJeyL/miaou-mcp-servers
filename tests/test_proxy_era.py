"""Ère négociée par le proxy avec ses upstreams stdio et http.

Vrais handshakes, sans réseau : un subprocess Python pour stdio
(`era_fixture_server.py`, SDK 2.x ; `legacy_stdio_fixture.py`, faux serveur qui
ne parle que `initialize`), une app streamable-http servie par
`httpx2.ASGITransport` pour http. L'upstream legacy http est le même serveur 2.x,
derrière un middleware qui répond à toute requête de l'ère 2026-07-28 ce que
répond un serveur SDK 1.x.
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest

for p in (Path(__file__).parent.parent, Path(__file__).parent.parent / "servers"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import mcp_proxy  # noqa: E402,F401  (ajoute servers/ à sys.path)
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

from mcp_proxy.contract import AuthorizationRequired  # noqa: E402
from mcp_proxy.upstream import HttpUpstream, StdioUpstream  # noqa: E402
from tests import era_fixture_server  # noqa: E402

TESTS = Path(__file__).parent
MODERN = "2026-07-28"
LEGACY = "2025-11-25"


# ---------------------------------------------------------------------------
# Outillage
# ---------------------------------------------------------------------------


def _stdio_modern() -> StdioUpstream:
    return StdioUpstream(command=sys.executable, args=[str(TESTS / "era_fixture_server.py")])


def _stdio_legacy(log: Path, protocol_era: str = "auto") -> StdioUpstream:
    return StdioUpstream(
        command=sys.executable,
        args=[str(TESTS / "legacy_stdio_fixture.py"), str(log)],
        protocol_era=protocol_era,
    )


def _received(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


class _Recorder:
    """Ce que le client httpx2 de l'upstream a émis : verbe, méthode JSON-RPC,
    en-têtes `mcp-*`, corps."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def hook(self, request: httpx2.Request) -> None:
        body = json.loads(request.content) if request.method == "POST" else None
        self.requests.append(
            {
                "verb": request.method,
                "method": body.get("method") if isinstance(body, dict) else None,
                "headers": {
                    k.lower(): v for k, v in request.headers.items() if k.lower().startswith("mcp-")
                },
                "body": body,
            }
        )

    def methods(self) -> list[str | None]:
        return [r["method"] for r in self.requests if r["verb"] == "POST"]


def _app(server: Any) -> Any:
    return server.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )


async def _drain(receive: Any) -> None:
    while True:
        message = await receive()
        if not message.get("more_body"):
            return


def _legacy_only(app: Any) -> Any:
    """Répond à toute requête de l'ère 2026-07-28 ce que répond un serveur SDK
    1.28.1 (mesuré) : 400, erreur `-32600`, `id: "server-error"`."""

    async def wrapped(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and dict(scope["headers"]).get(b"mcp-protocol-version") == MODERN.encode():
            await _drain(receive)
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "server-error",
                    "error": {"code": -32600, "message": f"Bad Request: Unsupported protocol version: {MODERN}"},
                }
            ).encode()
            await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        await app(scope, receive, send)

    return wrapped


def _status_app(status: int) -> Any:
    """Refuse tout, sans corps JSON-RPC (401/403 d'une passerelle)."""

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        await _drain(receive)
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app


def _http_upstream(
    app: Any, recorder: _Recorder, auth: Any = None, protocol_era: str = "auto"
) -> HttpUpstream:
    upstream = HttpUpstream("http://upstream.test/mcp", auth=auth, timeout=5, protocol_era=protocol_era)

    def build_http_client() -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            timeout=httpx2.Timeout(5, read=300),
            auth=auth,
            event_hooks={"request": [recorder.hook]},
        )

    upstream._build_http_client = build_http_client
    return upstream


@asynccontextmanager
async def _hosted(upstream: HttpUpstream):
    """Task group hôte de la tâche de service, comme le lifespan du proxy."""
    async with anyio.create_task_group() as tg:
        upstream.host_tasks_in(tg)
        try:
            with anyio.fail_after(15):
                yield upstream
        finally:
            await upstream.stop()
            tg.cancel_scope.cancel()


# ---------------------------------------------------------------------------
# stdio
# ---------------------------------------------------------------------------


async def test_stdio_upstream_negotiates_modern_era():
    """Un upstream 2.x est abordé en ère moderne : ses instructions viennent du
    DiscoverResult, et ses extensions, invisibles en `initialize`, sont lues."""
    upstream = _stdio_modern()
    try:
        await upstream.start()
        assert upstream.protocol_version == MODERN
        assert upstream.instructions == era_fixture_server.INSTRUCTIONS
        assert era_fixture_server.EXTENSION_ID in (upstream.capabilities.extensions or {})
        result = await upstream.call_tool("ping", {"text": "x"})
        assert result.content[0].text == "pong x"
    finally:
        await upstream.stop()


async def test_stdio_upstream_falls_back_to_legacy_identically(tmp_path):
    """Un serveur qui ne parle que `initialize` est abordé comme avant : la sonde
    échoue, puis la session est celle qu'ouvre le mode legacy, message pour
    message, à l'`id` JSON-RPC près (la sonde a pris le premier)."""
    auto_log, legacy_log = tmp_path / "auto.jsonl", tmp_path / "legacy.jsonl"
    results = {}
    for log, mode in ((auto_log, "auto"), (legacy_log, "legacy")):
        upstream = _stdio_legacy(log, protocol_era=mode)
        try:
            await upstream.start()
            assert upstream.protocol_version == LEGACY
            assert upstream.instructions == "consigne de l'upstream legacy"
            assert [t.name for t in await upstream.list_tools()] == ["ping"]
            results[mode] = (await upstream.call_tool("ping", {"text": "x"})).model_dump()
        finally:
            await upstream.stop()

    auto, legacy = _received(auto_log), _received(legacy_log)
    assert auto[0]["method"] == "server/discover"
    strip = lambda messages: [{k: v for k, v in m.items() if k != "id"} for m in messages]
    assert strip(auto[1:]) == strip(legacy)
    assert results["auto"] == results["legacy"]


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------


async def test_http_upstream_negotiates_modern_era():
    server = era_fixture_server.build()
    app = _app(server)
    recorder = _Recorder()
    async with server.session_manager.run():
        async with _hosted(_http_upstream(app, recorder)) as upstream:
            await upstream.start()
            assert upstream.protocol_version == MODERN
            assert upstream.instructions == era_fixture_server.INSTRUCTIONS
            assert era_fixture_server.EXTENSION_ID in (upstream.capabilities.extensions or {})
            result = await upstream.call_tool("ping", {"text": "x"})
            assert result.content[0].text == "pong x"
    assert recorder.methods()[0] == "server/discover"
    assert "initialize" not in recorder.methods()
    # Ni session ni flux GET en ère moderne.
    assert all(r["verb"] == "POST" for r in recorder.requests)
    assert all("mcp-session-id" not in r["headers"] for r in recorder.requests)


def _session_shape(recorder: _Recorder) -> list[tuple]:
    """Séquence d'une session legacy, comparable d'une connexion à l'autre :
    l'`id` JSON-RPC et la valeur de `Mcp-Session-Id` sont retirés (la présence
    de l'en-tête reste comparée). Triée : le flux GET part en tâche de fond."""
    shape = []
    for r in recorder.requests:
        headers = dict(r["headers"])
        if "mcp-session-id" in headers:
            headers["mcp-session-id"] = "<session>"
        body = {k: v for k, v in r["body"].items() if k != "id"} if isinstance(r["body"], dict) else None
        shape.append((r["verb"], json.dumps(headers, sort_keys=True), json.dumps(body, sort_keys=True)))
    return sorted(shape)


async def test_http_upstream_falls_back_to_legacy_identically():
    """Un serveur 1.x refuse la sonde (400) : repli, puis exactement la session
    qu'ouvre le mode legacy — `initialize` sans en-tête de version, session,
    flux GET, DELETE à l'arrêt."""
    recorders = {}
    for mode in ("auto", "legacy"):
        server = era_fixture_server.build()
        app = _legacy_only(_app(server))
        recorder = recorders[mode] = _Recorder()
        async with server.session_manager.run():
            async with _hosted(_http_upstream(app, recorder, protocol_era=mode)) as upstream:
                await upstream.start()
                assert upstream.protocol_version == LEGACY
                assert upstream.instructions == era_fixture_server.INSTRUCTIONS
                assert (await upstream.call_tool("ping", {"text": "x"})).content[0].text == "pong x"

    auto, legacy = recorders["auto"], recorders["legacy"]
    assert auto.requests[0]["method"] == "server/discover"
    assert auto.requests[0]["headers"]["mcp-protocol-version"] == MODERN
    first_initialize = next(r for r in auto.requests if r["method"] == "initialize")
    assert "mcp-protocol-version" not in first_initialize["headers"]
    probe_free = _Recorder()
    probe_free.requests = auto.requests[1:]
    assert _session_shape(probe_free) == _session_shape(legacy)
    assert {"GET", "DELETE"} <= {r["verb"] for r in legacy.requests}


@pytest.mark.parametrize("status", [401, 403])
async def test_http_upstream_bare_refusal_fails_as_before(status):
    """Un 401/403 sans corps JSON-RPC : le SDK se replie sur `initialize`, qui
    reçoit le même refus. Une requête de plus, et l'erreur d'avant."""
    failures = {}
    for mode in ("auto", "legacy"):
        recorder = _Recorder()
        async with _hosted(_http_upstream(_status_app(status), recorder, protocol_era=mode)) as upstream:
            with pytest.raises(Exception) as excinfo:
                await upstream.start()
            failures[mode] = (type(excinfo.value), str(excinfo.value))
        if mode == "auto":
            assert recorder.methods() == ["server/discover", "initialize"]
        else:
            assert recorder.methods() == ["initialize"]
    assert failures["auto"] == failures["legacy"]


async def test_http_upstream_authorization_refusal_crosses_the_probe():
    """Avec l'OAuth sortant, le 401 est absorbé par l'`Auth` httpx2 : un parcours
    inhibé lève `AuthorizationRequired`, qui traverse la sonde sans repli —
    `start()` la relève telle quelle, et seule la sonde est partie."""

    class _RefusingAuth(httpx2.Auth):
        def auth_flow(self, request):
            response = yield request
            if response.status_code == 401:
                raise AuthorizationRequired("fixture")

    recorder = _Recorder()
    async with _hosted(_http_upstream(_status_app(401), recorder, auth=_RefusingAuth())) as upstream:
        with pytest.raises(AuthorizationRequired):
            await upstream.start()
    assert recorder.methods() == ["server/discover"]


# ---------------------------------------------------------------------------
# Relais depuis un upstream moderne
# ---------------------------------------------------------------------------


async def test_modern_upstream_sends_param_headers_without_a_client_listing():
    """Le SDK n'émet `Mcp-Param-*` que pour un outil du dernier `tools/list` de
    la session. Le proxy liste au démarrage d'un upstream moderne : un appel
    qui arrive avant tout `tools/list` client (catalogue en cache, redémarrage
    d'`authorize()`) porte l'en-tête, et l'upstream ne le refuse pas (`-32020`)."""
    server = era_fixture_server.build()
    app = _app(server)
    recorder = _Recorder()
    async with server.session_manager.run():
        async with _hosted(_http_upstream(app, recorder)) as upstream:
            await upstream.start()
            result = await upstream.call_tool("regional", {"region": "eu-west"})
            assert result.content[0].text == "region eu-west"
    call = next(r for r in recorder.requests if r["method"] == "tools/call")
    assert call["headers"]["mcp-param-region"] == "eu-west"


async def test_legacy_upstream_is_not_primed(tmp_path):
    """L'amorçage ne vaut que pour l'ère moderne : en legacy, `Mcp-Param-*`
    n'existe pas, et la session reste celle d'avant (aucun `tools/list` émis
    par `start()`)."""
    log = tmp_path / "legacy.jsonl"
    upstream = _stdio_legacy(log)
    try:
        await upstream.start()
    finally:
        await upstream.stop()
    assert "tools/list" not in [m.get("method") for m in _received(log)]


async def test_modern_upstream_identity_is_not_relayed():
    """Un upstream moderne signe ses résultats de son `serverInfo`, et le serveur
    du proxy ne pose le sien que si la clé est absente : relayée, l'identité de
    l'upstream passerait pour celle du proxy."""
    upstream = _stdio_modern()
    try:
        await upstream.start()
        result = await upstream.call_tool("ping", {"text": "x"})
    finally:
        await upstream.stop()
    assert "io.modelcontextprotocol/serverInfo" not in (result.meta or {})


def test_relay_call_result_drops_only_the_upstream_identity():
    import mcp.types as types
    from mcp_proxy.upstream import relay_call_result

    relayed = relay_call_result(
        types.CallToolResult(
            content=[],
            **{"_meta": {"miaou/web": {"title": "T"}, types.SERVER_INFO_META_KEY: {"name": "up"}}},
        )
    )
    assert relayed.meta == {"miaou/web": {"title": "T"}}
    alone = relay_call_result(
        types.CallToolResult(content=[], **{"_meta": {types.SERVER_INFO_META_KEY: {"name": "up"}}})
    )
    assert alone.meta is None


# ---------------------------------------------------------------------------
# Config et journal
# ---------------------------------------------------------------------------


async def test_startup_log_shows_the_negotiated_era(tmp_path, capsys):
    """Le journal de démarrage donne l'ère de chaque upstream stdio/http, celle
    qui a été négociée ; rien pour l'inprocess, sans fil. Un upstream moderne
    forcé en legacy par la config est abordé en legacy, et c'est le seul dont
    la ligne « skills non relayées » sort (l'upstream moderne de la fixture ne
    déclare pas l'extension Skills)."""
    from mcp_proxy import InProcessUpstream, build_app, build_proxy_server
    from tests.test_proxy_auth import LifespanManager

    upstreams = {
        "modern": _stdio_modern(),
        "forced": StdioUpstream(
            command=sys.executable, args=[str(TESTS / "era_fixture_server.py")], protocol_era="legacy"
        ),
        "probed": _stdio_legacy(tmp_path / "legacy.jsonl"),
        "bench": InProcessUpstream("mcp_bench"),
    }
    app = build_app(build_proxy_server(upstreams, {}), upstreams)
    async with LifespanManager(app):
        pass
    lines = capsys.readouterr().err.splitlines()

    def line(name: str) -> str:
        return next(l for l in lines if f" {name} " in l and "tool" in l)

    assert line("modern").endswith("2 tools (2026-07-28)")
    assert line("forced").endswith("2 tools (2025-11-25)")
    assert line("probed").endswith("1 tool (2025-11-25)")
    assert line("bench").endswith("7 tools, 1 skill")
    skills = [l for l in lines if "skills non relayées" in l or "sert des skills" in l]
    assert len(skills) == 1 and " forced " in skills[0]


# ---------------------------------------------------------------------------
# Erreur JSON-RPC d'un upstream http
# ---------------------------------------------------------------------------

_REFUSAL = "refus de l'upstream, motif lisible"


def _refuse_tool_calls(app: Any) -> Any:
    """Répond à tout `tools/call` une erreur JSON-RPC, en 400 avec corps comme
    un serveur moderne (l'AUTHORIZATION_REQUIRED d'un proxy pris comme upstream
    arrive ainsi) ; le reste passe à l'app."""

    async def wrapped(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await app(scope, receive, send)
            return
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        request = json.loads(body)
        if isinstance(request, dict) and request.get("method") == "tools/call":
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32600, "message": _REFUSAL}}
            ).encode()
            await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": payload})
            return
        replayed = False

        async def replay() -> Any:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await app(scope, replay, send)

    return wrapped


async def test_http_upstream_jsonrpc_error_reaches_the_client_with_its_message():
    """La garde contre la mort de la tâche de service fait courir l'appel dans un
    task group anyio, qui enveloppe la `MCPError` de l'upstream dans un
    `ExceptionGroup`. Non déballée, elle échappait à `handle_call_tool`, et le
    client lisait « unhandled errors in a TaskGroup » au lieu du motif."""
    from mcp.shared.exceptions import MCPError

    from mcp_proxy import build_proxy_server
    from tests.proxy_client import call_tool

    server = era_fixture_server.build()
    app = _refuse_tool_calls(_app(server))
    async with server.session_manager.run():
        async with _hosted(_http_upstream(app, _Recorder())) as upstream:
            await upstream.start()
            with pytest.raises(MCPError, match=_REFUSAL):
                await upstream.call_tool("ping", {"text": "x"})
            result = await call_tool(build_proxy_server({"up": upstream}, {}), "up__ping", {"text": "x"})
    assert result.is_error is True
    assert _REFUSAL in result.content[0].text


# ---------------------------------------------------------------------------
# Panne d'un upstream en cours de vie
# ---------------------------------------------------------------------------


def _kill_children(script: str) -> None:
    """Tue les subprocess de ce processus qui exécutent `script` : la mort d'un
    upstream stdio en cours de vie, sans passer par `stop()`."""
    import os
    import signal
    import subprocess

    pids = subprocess.run(
        ["pgrep", "-P", str(os.getpid()), "-f", script], capture_output=True, text=True
    ).stdout.split()
    assert pids, f"aucun subprocess {script}"
    for pid in pids:
        os.kill(int(pid), signal.SIGKILL)


async def test_dead_stdio_upstream_does_not_take_down_tools_list():
    """Un subprocess stdio tué reste dans la table (`upstream_is_live` ne voit
    pas sa mort) : son listage échoue, et `tools/list` du proxy doit répondre
    quand même pour les autres, à chaque appel — pas une erreur pour tous."""
    from mcp_proxy import InProcessUpstream, build_proxy_server
    from tests.proxy_client import call_tool, list_tools

    dead = _stdio_modern()
    upstreams = {"dead": dead, "fx": InProcessUpstream("tests.skills_fixture_server", config={})}
    server = build_proxy_server(upstreams, {})
    try:
        for upstream in upstreams.values():
            await upstream.start()
        assert "dead__ping" in [t.name for t in (await list_tools(server)).tools]
        _kill_children("era_fixture_server.py")
        await anyio.sleep(0.2)
        for _ in range(2):
            names = [t.name for t in (await list_tools(server)).tools]
            assert names == ["fx__ping", "fx__pong"]
        result = await call_tool(server, "dead__ping", {"text": "x"})
        assert result.is_error is True
    finally:
        await dead.stop()


async def test_dead_http_upstream_without_oauth_is_unreachable_not_unauthorized(tmp_path):
    """Un upstream http sans authorizer dont la session est fermée est
    injoignable : son appel rend un `isError` qui le dit, jamais
    AUTHORIZATION_REQUIRED (il n'a aucun parcours d'autorisation), et ses outils
    ne sont pas resservis depuis le cache avec une mention « non autorisé »."""
    from mcp_proxy import ToolCatalogCache, build_proxy_server
    from tests.proxy_client import call_tool, list_tools

    server_up = era_fixture_server.build()
    app = _app(server_up)
    catalog = ToolCatalogCache(tmp_path / "tools.json")
    async with server_up.session_manager.run():
        async with _hosted(_http_upstream(app, _Recorder())) as upstream:
            await upstream.start()
            proxy = build_proxy_server({"up": upstream}, {}, catalog=catalog)
            assert "up__ping" in [t.name for t in (await list_tools(proxy)).tools]
            await upstream.stop()
            assert (await list_tools(proxy)).tools == []
            result = await call_tool(proxy, "up__ping", {"text": "x"})
    assert result.is_error is True
    assert "injoignable" in result.content[0].text
    assert "autorisation" not in result.content[0].text.lower()
