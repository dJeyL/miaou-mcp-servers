"""Relais LLM sous `/llm/<slug>/…` (`mcp_proxy/llm_relay.py`).

Trois niveaux :

- les purs : validation de la clef `llm` (couches comprises), règles
  d'en-têtes, découpe et garde du chemin ;
- l'app de `build_app` sur `httpx2.ASGITransport`, vers un amont Starlette
  qui enregistre ce qu'il reçoit (transport injecté) : garde d'origine, en-têtes
  émis et rendus, erreurs, contournement de l'auth entrante ;
- deux VRAIS uvicorn (proxy et amont) : le flux SSE arrive morceau par morceau,
  et un client qui part arrête la génération amont. `ASGITransport` lit la
  réponse en entier, il ne prouve ni l'un ni l'autre.
"""
import asyncio
import http.client
import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

_ROOT = Path(__file__).parent.parent
_SERVERS = _ROOT / "servers"
for p in (_ROOT, _SERVERS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from mcp_proxy import build_app, build_proxy_server, resolve_auth_config
from mcp_proxy import configwatch as cw
from mcp_proxy.layers import load_layers, mask_secrets
from mcp_proxy.llm_relay import (
    DEFAULT_TIMEOUT_S,
    LlmRelay,
    apply_rules,
    resolve_llm_config,
    safe_suffix,
    split_path,
)

PORT = 8765
SAME = {"origin": f"http://127.0.0.1:{PORT}"}


def _cfg(backends, **llm):
    return {"llm": {**llm, "backends": backends}}


# ---------------------------------------------------------------------------
# resolve_llm_config
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cfg", [{}, {"llm": None}, {"llm": {}}, {"llm": {"backends": {}}}])
def test_absent_or_empty_key_relays_nothing(cfg):
    assert resolve_llm_config(cfg).backends == {}


def test_backend_merges_global_rules_and_can_withdraw_one():
    llm = resolve_llm_config(_cfg(
        {"a": {
            "base_url": "https://api.example.org/v1/",
            "request_headers": {"accept": False, "Authorization": "Bearer k"},
            "response_headers": {"X-Extra": True},
        }},
        request_headers={"Content-Type": True, "Accept": True},
        response_headers={"Content-Type": True},
    ))
    a = llm.backends["a"]
    assert a.base_url == "https://api.example.org/v1"
    assert a.request_headers == {"content-type": True, "accept": False, "authorization": "Bearer k"}
    assert a.response_headers == {"content-type": True, "x-extra": True}
    assert a.timeout == DEFAULT_TIMEOUT_S


def test_disabled_backend_is_not_served_and_not_validated():
    llm = resolve_llm_config(_cfg({
        "off": {"disabled": True, "base_url": "pas une url"},
        "on": {"base_url": "http://127.0.0.1:9/v1"},
    }))
    assert list(llm.backends) == ["on"]
    assert llm.disabled == ("off",)


@pytest.mark.parametrize("cfg, needle", [
    ({"llm": []}, "objet attendu"),
    ({"llm": {"backend": {}}}, "clef(s) inconnue(s) backend"),
    (_cfg([]), "objet attendu"),
    (_cfg({"a/b": {"base_url": "http://x"}}), "slug invalide"),
    (_cfg({"a": "http://x"}), "objet attendu"),
    (_cfg({"a": {"base_url": "http://x", "request_header": {}}}), "request_header"),
    (_cfg({"a": {}}), "base_url"),
    (_cfg({"a": {"base_url": "ftp://x"}}), "base_url"),
    (_cfg({"a": {"base_url": "https://u:p@x/v1"}}), "identifiants"),
    (_cfg({"a": {"base_url": "https://x/v1?key=1"}}), "query"),
    (_cfg({"a": {"base_url": "https://x", "timeout": 0}}), "timeout"),
    (_cfg({"a": {"base_url": "https://x", "timeout": True}}), "timeout"),
    (_cfg({"a": {"base_url": "https://x", "disabled": "yes"}}), "disabled"),
    (_cfg({"a": {"base_url": "https://x", "request_headers": {"X": 1}}}), "true, false ou une chaîne"),
    (_cfg({"a": {"base_url": "https://x", "request_headers": {"X": "a\r\nB: c"}}}), "retour à la ligne"),
    (_cfg({"a": {"base_url": "https://x", "request_headers": {"Bad Name": True}}}), "nom d'en-tête"),
    (_cfg({"a": {"base_url": "https://x", "request_headers": {"User-Agent": "a", "user-agent": "b"}}}),
     "même en-tête"),
])
def test_invalid_config_is_refused(cfg, needle):
    with pytest.raises(ValueError, match=None) as e:
        resolve_llm_config(cfg)
    assert needle in str(e.value)


@pytest.mark.parametrize("name", ["Host", "Content-Length", "Accept-Encoding", "Connection", "Transfer-Encoding"])
def test_headers_the_relay_owns_are_refused_in_requests(name):
    with pytest.raises(ValueError, match="géré par le relais"):
        resolve_llm_config(_cfg({}, request_headers={name: True}))


@pytest.mark.parametrize("name", ["Content-Encoding", "Content-Length", "Access-Control-Allow-Origin", "Connection"])
def test_headers_the_relay_owns_are_refused_in_responses(name):
    with pytest.raises(ValueError, match="géré par le relais"):
        resolve_llm_config(_cfg({"a": {"base_url": "https://x", "response_headers": {name: True}}}))


def test_two_layers_spelling_a_header_differently_are_refused(tmp_path):
    """Le Merge Patch garde les deux casses côte à côte : sans le refus, l'une
    gagnerait en silence."""
    base = tmp_path / "base.json"
    local = tmp_path / "config.json"
    base.write_text(json.dumps({"port": PORT, "llm": {"request_headers": {"User-Agent": "a"}}}))
    local.write_text(json.dumps({"llm": {"request_headers": {"user-agent": "b"}}}))
    loaded = load_layers([base, local], local, env={})
    with pytest.raises(ValueError, match="même en-tête"):
        resolve_llm_config(loaded.cfg)


def test_prepare_config_refuses_an_invalid_llm_block(tmp_path):
    """Même fonction pour `main()` et la validation avant redémarrage."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"port": PORT, "mcpServers": {}, "llm": {"backends": {"a": {}}}}))
    with pytest.raises(ValueError, match="base_url"):
        cw.prepare_config(load_layers([path], path, env={}), cw.LaunchOptions())


def test_shipped_defaults_are_valid_and_relay_nothing():
    path = _ROOT / "config.defaults.json"
    llm = resolve_llm_config(load_layers([path], None, env={}).cfg)
    assert llm.backends == {}
    assert llm.disabled == ("openrouter",)


def test_print_config_masks_fixed_secrets_but_not_slugs_or_true():
    masked = mask_secrets(_cfg({"token_vault": {
        "base_url": "https://x/v1",
        "request_headers": {"Authorization": "Bearer sk-1", "X-Api-Key": "k", "User-Agent": "ua"},
    }, "fwd": {"base_url": "https://y", "request_headers": {"Authorization": True}}}))
    vault = masked["llm"]["backends"]["token_vault"]
    assert vault["base_url"] == "https://x/v1"
    assert vault["request_headers"] == {"Authorization": "***", "X-Api-Key": "***", "User-Agent": "ua"}
    assert masked["llm"]["backends"]["fwd"]["request_headers"] == {"Authorization": True}


# ---------------------------------------------------------------------------
# Purs : règles et chemin
# ---------------------------------------------------------------------------

def test_apply_rules_emits_only_what_the_rules_name():
    received = [("Origin", "http://x"), ("Set-Cookie", "a=1"), ("set-cookie", "b=2"), ("Accept", "*/*")]
    rules = {"set-cookie": True, "accept": False, "user-agent": "fixed", "x-absent": True}
    assert apply_rules(rules, received) == [
        ("set-cookie", "a=1"), ("set-cookie", "b=2"), ("user-agent", "fixed"),
    ]


@pytest.mark.parametrize("raw, expected", [
    (b"/llm/openai/chat/completions", ("openai", "/chat/completions")),
    (b"/llm/openai/models/a%2Fb", ("openai", "/models/a%2Fb")),
    (b"/llm/openai/", ("openai", "/")),
    (b"/llm/openai", None),
    (b"/llm/", None),
    (b"/llm", None),
    (b"/mcp/", None),
])
def test_split_path(raw, expected):
    assert split_path(raw) == expected


@pytest.mark.parametrize("suffix, ok", [
    ("/chat/completions", True),
    ("/", False),
    ("", False),
    ("/../admin", False),
    ("/%2e%2e/admin", False),
    ("/a/./b", False),
    ("/a..b", True),
])
def test_safe_suffix(suffix, ok):
    assert safe_suffix(suffix) is ok


# ---------------------------------------------------------------------------
# App ASGI, amont enregistreur
# ---------------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.requests: list[dict] = []

    def app(self):
        async def any_route(request: Request):
            body = await request.body()
            self.requests.append({
                "method": request.method,
                "path": request.scope["raw_path"].decode(),
                "query": request.url.query,
                "headers": [(k.decode().lower(), v.decode()) for k, v in request.scope["headers"]],
                "body": body,
            })
            if request.url.path.endswith("/limited"):
                return JSONResponse(
                    {"error": {"message": "slow down"}}, status_code=429,
                    headers={"Retry-After": "7"},
                )
            response = JSONResponse({"ok": True}, headers={
                "X-Ratelimit-Remaining-Requests": "41",
                "X-Internal": "secret-ish",
                "Access-Control-Allow-Origin": "https://elsewhere",
            })
            response.raw_headers.append((b"set-cookie", b"a=1"))
            response.raw_headers.append((b"set-cookie", b"b=2"))
            return response

        return Starlette(routes=[Route("/{p:path}", any_route, methods=["GET", "POST", "DELETE"])])


def _relay(backends_cfg, transport, **llm):
    return LlmRelay(resolve_llm_config(_cfg(backends_cfg, **llm)).backends, PORT, transport=transport)


def _proxy(relay, auth=None, token_verifier=None):
    upstreams: dict = {}
    return build_app(
        build_proxy_server(upstreams, {}), upstreams, auth=auth,
        token_verifier=token_verifier, llm_relay=relay,
    )


def _client(app, host=f"127.0.0.1:{PORT}"):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=f"http://{host}")


@pytest.fixture
def upstream():
    rec = Recorder()
    return rec, httpx2.ASGITransport(app=rec.app())


_DEFAULTS = dict(
    request_headers={"Content-Type": True, "Accept": True},
    response_headers={"Content-Type": True, "Retry-After": True, "X-Ratelimit-Remaining-Requests": True},
)


async def test_request_carries_only_configured_headers(upstream):
    rec, transport = upstream
    relay = _relay({"a": {
        "base_url": "http://amont.test/v1",
        "request_headers": {"Authorization": "Bearer sk-fixe", "X-Tenant": "t1"},
    }}, transport, **_DEFAULTS)
    async with _client(_proxy(relay)) as c:
        r = await c.post(
            "/llm/a/chat/completions?x=1&y=%2F",
            content=b'{"model":"m"}',
            headers={
                **SAME, "Content-Type": "application/json", "Authorization": "Bearer du-client",
                "User-Agent": "Mozilla/5.0", "Referer": "http://127.0.0.1:8765/app/",
                "X-Other": "no",
            },
        )
    assert r.status_code == 200 and r.json() == {"ok": True}
    sent = rec.requests[0]
    assert sent["method"] == "POST"
    assert sent["path"] == "/v1/chat/completions"
    assert sent["query"] == "x=1&y=%2F"
    assert sent["body"] == b'{"model":"m"}'
    headers = dict(sent["headers"])
    assert headers["authorization"] == "Bearer sk-fixe"
    assert headers["x-tenant"] == "t1"
    assert headers["content-type"] == "application/json"
    for absent in ("origin", "referer", "user-agent", "x-other", "sec-fetch-site"):
        assert absent not in headers, absent


async def test_true_forwards_the_client_value_and_nothing_when_absent(upstream):
    rec, transport = upstream
    relay = _relay({"a": {"base_url": "http://amont.test/v1", "request_headers": {"Authorization": True}}}, transport)
    async with _client(_proxy(relay)) as c:
        await c.get("/llm/a/models", headers={**SAME, "Authorization": "Bearer du-client"})
        await c.get("/llm/a/models", headers=SAME)
    assert dict(rec.requests[0]["headers"])["authorization"] == "Bearer du-client"
    assert "authorization" not in dict(rec.requests[1]["headers"])


async def test_response_carries_only_configured_headers(upstream):
    _, transport = upstream
    relay = _relay(
        {"a": {"base_url": "http://amont.test/v1", "response_headers": {"Set-Cookie": True}}},
        transport, **_DEFAULTS,
    )
    async with _client(_proxy(relay)) as c:
        r = await c.get("/llm/a/models", headers=SAME)
    assert r.headers["x-ratelimit-remaining-requests"] == "41"
    assert r.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert "x-internal" not in r.headers
    # Ni l'Access-Control de l'amont, ni celui du proxy : hors de l'app Starlette.
    assert "access-control-allow-origin" not in r.headers


async def test_upstream_error_status_and_body_pass_through(upstream):
    _, transport = upstream
    relay = _relay({"a": {"base_url": "http://amont.test/v1"}}, transport, **_DEFAULTS)
    async with _client(_proxy(relay)) as c:
        r = await c.post("/llm/a/limited", headers=SAME)
    assert r.status_code == 429
    assert r.headers["retry-after"] == "7"
    assert r.json() == {"error": {"message": "slow down"}}


@pytest.mark.parametrize("host, headers", [
    (f"127.0.0.1:{PORT}", {"origin": "https://evil.example"}),
    (f"127.0.0.1:{PORT}", {"origin": "null"}),
    (f"127.0.0.1:{PORT}", {}),
    (f"127.0.0.1:{PORT}", {"sec-fetch-site": "cross-site"}),
    (f"127.0.0.1:{PORT}", {"origin": "https://evil.example", "sec-fetch-site": "same-origin"}),
    ("evil.example", {"origin": "http://evil.example"}),
    ("evil.example", {"sec-fetch-site": "same-origin"}),
    (f"127.0.0.1:{PORT + 1}", {"origin": f"http://127.0.0.1:{PORT + 1}"}),
])
async def test_guard_refuses_anything_but_miaou_served_here(upstream, host, headers):
    rec, transport = upstream
    relay = _relay({"a": {"base_url": "http://amont.test/v1"}}, transport)
    async with _client(_proxy(relay), host=host) as c:
        r = await c.post("/llm/a/chat/completions", headers=headers)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "LLM_ORIGIN_REFUSED"
    assert rec.requests == []


@pytest.mark.parametrize("host, headers", [
    (f"127.0.0.1:{PORT}", SAME),
    (f"localhost:{PORT}", {"origin": f"http://localhost:{PORT}"}),
    (f"127.0.0.1:{PORT}", {"sec-fetch-site": "same-origin"}),
])
async def test_guard_accepts_same_origin_including_a_get_without_origin(upstream, host, headers):
    rec, transport = upstream
    relay = _relay({"a": {"base_url": "http://amont.test/v1"}}, transport)
    async with _client(_proxy(relay), host=host) as c:
        r = await c.get("/llm/a/models", headers=headers)
    assert r.status_code == 200
    assert len(rec.requests) == 1


@pytest.mark.parametrize("path, status, code", [
    ("/llm/inconnu/models", 404, "LLM_UNKNOWN_BACKEND"),
    ("/llm/off/models", 404, "LLM_UNKNOWN_BACKEND"),
    ("/llm/a", 404, "LLM_UNKNOWN_BACKEND"),
    ("/llm", 404, "LLM_UNKNOWN_BACKEND"),
    ("/llm/a/", 400, "LLM_INVALID_PATH"),
    ("/llm/a/%2e%2e/admin", 400, "LLM_INVALID_PATH"),
])
async def test_unknown_backend_and_bad_paths(upstream, path, status, code):
    rec, transport = upstream
    relay = _relay(
        {"a": {"base_url": "http://amont.test/v1"}, "off": {"disabled": True, "base_url": "http://x"}},
        transport,
    )
    async with _client(_proxy(relay)) as c:
        r = await c.get(path, headers=SAME)
    assert r.status_code == status
    assert r.json()["error"]["code"] == code
    assert rec.requests == []


async def test_unreachable_and_timeout_are_502_and_504():
    def handler(request):
        if request.url.path.endswith("/slow"):
            raise httpx2.ReadTimeout("trop lent", request=request)
        raise httpx2.ConnectError("refusé", request=request)

    relay = _relay({"a": {"base_url": "http://amont.test/v1"}}, httpx2.MockTransport(handler))
    async with _client(_proxy(relay)) as c:
        down = await c.get("/llm/a/models", headers=SAME)
        slow = await c.get("/llm/a/slow", headers=SAME)
    assert down.status_code == 502 and down.json()["error"]["code"] == "LLM_UPSTREAM_UNREACHABLE"
    assert slow.status_code == 504 and slow.json()["error"]["code"] == "LLM_UPSTREAM_TIMEOUT"


async def test_without_relay_llm_paths_are_not_served():
    async with _client(_proxy(None)) as c:
        r = await c.get("/llm/a/models", headers=SAME)
    assert r.status_code == 404
    assert "error" not in r.text  # 404 de Starlette, pas celui du relais


async def test_inbound_auth_neither_guards_nor_inspects_the_relay(upstream):
    """La clef d'API du client n'est pas un jeton du proxy : elle passe au
    backend sans être vérifiée, alors que /mcp exige toujours son jeton."""
    rec, transport = upstream

    class Verifier:
        calls = 0

        async def verify_token(self, token):
            Verifier.calls += 1
            return None

    auth = resolve_auth_config(
        {"auth": {"issuer_url": "http://127.0.0.1:8787"}}, cli_auth=None, host="127.0.0.1", port=PORT
    )
    relay = _relay({"a": {"base_url": "http://amont.test/v1", "request_headers": {"Authorization": True}}}, transport)
    async with _client(_proxy(relay, auth=auth, token_verifier=Verifier())) as c:
        r = await c.get("/llm/a/models", headers={**SAME, "Authorization": "Bearer sk-client"})
        mcp = await c.post("/mcp", json={}, headers={"Authorization": "Bearer sk-client"})
    assert r.status_code == 200
    assert dict(rec.requests[0]["headers"])["authorization"] == "Bearer sk-client"
    assert mcp.status_code == 401
    assert Verifier.calls == 1  # celui de /mcp seulement


# ---------------------------------------------------------------------------
# Vrais uvicorn : streaming et déconnexion
# ---------------------------------------------------------------------------

def _serve(app):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn n'a pas démarré"
        time.sleep(0.02)
    return server, thread, server.servers[0].sockets[0].getsockname()[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SseUpstream:
    """Un flux SSE qui attend le feu vert du test entre deux événements :
    tant qu'il ne l'a pas, le deuxième n'existe pas — un relais qui tamponne
    ne rendrait donc jamais le premier."""

    def __init__(self):
        self.go = threading.Event()
        self.closed = threading.Event()

    def app(self):
        async def stream(request: Request):
            async def events():
                try:
                    yield b"data: un\n\n"
                    while not self.go.is_set():
                        await asyncio.sleep(0.01)
                    yield b"data: deux\n\n"
                    if request.url.path.endswith("/forever"):
                        while True:
                            await asyncio.sleep(0.05)
                            yield b": ping\n\n"
                    yield b"data: [DONE]\n\n"
                finally:
                    self.closed.set()

            return StreamingResponse(events(), media_type="text/event-stream")

        return Starlette(routes=[Route("/v1/{p:path}", stream, methods=["POST"])])


@pytest.fixture
def live_pair():
    sse = SseUpstream()
    up_server, up_thread, up_port = _serve(sse.app())
    port = _free_port()
    relay = LlmRelay(
        resolve_llm_config(_cfg(
            {"s": {"base_url": f"http://127.0.0.1:{up_port}/v1"}},
            response_headers={"Content-Type": True},
        )).backends,
        port,
    )
    import uvicorn

    proxy = uvicorn.Server(uvicorn.Config(_proxy(relay), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=proxy.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not proxy.started:
        assert time.monotonic() < deadline, "uvicorn n'a pas démarré"
        time.sleep(0.02)
    try:
        yield sse, port
    finally:
        sse.go.set()
        proxy.should_exit = True
        up_server.should_exit = True
        thread.join(timeout=10)
        up_thread.join(timeout=10)


def _open_stream(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=b"{}", headers={
        "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json",
    })
    return conn, conn.getresponse()


def test_wire_sse_arrives_chunk_by_chunk(live_pair):
    sse, port = live_pair
    conn, response = _open_stream(port, "/llm/s/chat/completions")
    try:
        assert response.status == 200
        assert response.getheader("content-type").startswith("text/event-stream")
        assert response.readline() == b"data: un\n"
        assert response.readline() == b"\n"
        sse.go.set()
        assert response.read() == b"data: deux\n\ndata: [DONE]\n\n"
    finally:
        conn.close()
    assert sse.closed.wait(5)


def test_wire_client_leaving_stops_the_upstream_generation(live_pair):
    sse, port = live_pair
    conn, response = _open_stream(port, "/llm/s/chat/forever")
    assert response.readline() == b"data: un\n"
    sse.go.set()
    # Fermeture brutale côté client, flux amont sans fin.
    conn.sock.shutdown(socket.SHUT_RDWR)
    conn.close()
    assert sse.closed.wait(5), "l'amont continue de générer après le départ du client"
