"""Secrets volatils (lot AU) : marqueurs de la config, couches, refus au
démarrage."""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_SERVERS = _ROOT / "servers"
for p in (_ROOT, _SERVERS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import mcp_base
from mcp_proxy import logging as proxy_logging
from mcp_proxy.config import build_upstreams
from mcp_proxy.layers import load_layers
from mcp_proxy.volatile import (
    VolatileMarkerError,
    format_path,
    iter_markers,
    keys_in,
    marker_key,
    mask,
    resolve,
)

KEY = "Mot de passe SSO"


def _write(path: Path, data) -> Path:
    path.write_text(json.dumps(data))
    return path


# --- Marqueurs ---------------------------------------------------------------


def test_marker_is_an_object_with_volatile_as_its_only_key():
    assert marker_key({"volatile": KEY}) == KEY
    assert marker_key({"volatile": KEY, "other": 1}) is None
    assert marker_key("chaîne") is None
    assert marker_key({"volatil": KEY}) is None


@pytest.mark.parametrize("bad", ["", "   ", 3, None, ["x"]])
def test_marker_without_a_usable_key_is_refused(bad):
    with pytest.raises(VolatileMarkerError):
        marker_key({"volatile": bad})


def test_markers_found_at_any_depth_lists_included_underscore_keys_skipped():
    config = {
        "login": "moi",
        "instances": [
            {"url": "a", "password": {"volatile": KEY}},
            {"url": "b", "password": {"volatile": KEY}},
            {"url": "c", "nested": {"password": {"volatile": KEY}}},
        ],
        "_example": {"password": {"volatile": "ignorée"}},
    }
    found = list(iter_markers(config))
    assert [format_path(p) for p, _ in found] == [
        "instances[0].password",
        "instances[1].password",
        "instances[2].nested.password",
    ]
    assert keys_in(config) == [KEY]


def test_malformed_marker_names_its_path():
    with pytest.raises(VolatileMarkerError, match=r"instances\[1\]\.password"):
        list(iter_markers({"instances": [{}, {"password": {"volatile": ""}}]}))
    assert list(iter_markers({"a": {"volatile": ""}}, strict=False)) == []


def test_resolve_returns_a_copy_and_leaves_underscore_keys_alone():
    config = {
        "password": {"volatile": KEY},
        "list": [{"volatile": KEY}, "x"],
        "_comment": {"volatile": KEY},
    }
    resolved = resolve(config, "s3cret")
    assert resolved == {"password": "s3cret", "list": ["s3cret", "x"], "_comment": {"volatile": KEY}}
    assert config["password"] == {"volatile": KEY}


def test_mask_removes_every_occurrence():
    assert mask("a s3cret b s3cret", "s3cret") == "a *** b ***"
    assert mask("rien", None) == "rien"


# --- Exceptions de mcp_base --------------------------------------------------


def test_mcp_base_exposes_the_two_contract_exceptions():
    assert issubclass(mcp_base.CredentialsRejected, Exception)
    assert issubclass(mcp_base.ConfigIncomplete, ValueError)


# --- build_upstreams ---------------------------------------------------------


def _cfg(**servers):
    return {"port": 1, "mcpServers": servers}


def test_inprocess_config_marker_gives_the_upstream_its_key():
    ups = build_upstreams(_cfg(api={
        "type": "inprocess", "module": "m",
        "config": {"instances": [{"password": {"volatile": KEY}}] * 3},
    }))
    assert ups["api"].volatile_key == KEY
    assert ups["api"].volatile_error is None


def test_upstream_without_marker_has_no_key():
    ups = build_upstreams(_cfg(bench={"type": "inprocess", "module": "mcp_bench"}))
    assert ups["bench"].volatile_key is None


def test_two_keys_in_one_upstream_set_it_aside_with_a_clear_reason():
    ups = build_upstreams(_cfg(api={
        "type": "inprocess", "module": "m",
        "config": {"prod": {"volatile": "A"}, "uat": {"volatile": "B"}},
    }))
    assert ups["api"].volatile_key is None
    assert "« A »" in ups["api"].volatile_error and "« B »" in ups["api"].volatile_error


def test_malformed_marker_in_inprocess_config_sets_the_upstream_aside():
    ups = build_upstreams(_cfg(api={
        "type": "inprocess", "module": "m", "config": {"password": {"volatile": ""}},
    }))
    assert "config.password" in ups["api"].volatile_error


async def test_set_aside_upstream_fails_to_start_with_its_reason():
    ups = build_upstreams(_cfg(api={
        "type": "inprocess", "module": "m",
        "config": {"prod": {"volatile": "A"}, "uat": {"volatile": "B"}},
    }))
    with pytest.raises(RuntimeError, match="plusieurs secrets volatils"):
        await ups["api"].start()


@pytest.mark.parametrize("entry, where", [
    ({"type": "inprocess", "module": "m", "env": {"PASS": {"volatile": KEY}}}, "mcpServers.api.env.PASS"),
    ({"type": "stdio", "command": "x", "env": {"PASS": {"volatile": KEY}}}, "mcpServers.api.env.PASS"),
    ({"type": "http", "url": "http://x", "headers": {"X-Key": {"volatile": KEY}}}, "mcpServers.api.headers.X-Key"),
    # `config` n'est résolu que pour un inprocess : sur un stdio, il ne vaut rien.
    ({"type": "stdio", "command": "x", "config": {"p": {"volatile": KEY}}}, "mcpServers.api.config.p"),
])
def test_marker_outside_an_inprocess_config_is_a_config_error(entry, where):
    with pytest.raises(ValueError, match=f"{where} : secret volatil « {KEY} »"):
        build_upstreams(_cfg(api=entry))


def test_marker_outside_mcpservers_is_a_config_error():
    with pytest.raises(ValueError, match=r"config\.auth\.client_secret"):
        build_upstreams({"port": 1, "auth": {"client_secret": {"volatile": KEY}}, "mcpServers": {}})


def test_disabled_entry_with_marker_is_ignored():
    assert build_upstreams(_cfg(api={
        "disabled": True, "type": "stdio", "command": "x", "env": {"P": {"volatile": KEY}},
    })) == {}


# --- Couches : mot de passe en clair par-dessus un marqueur -------------------


def _site_with_marker():
    return {"port": 1, "mcpServers": {"api": {"type": "inprocess", "module": "m", "config": {
        "login": "", "instances": [{"password": {"volatile": KEY}}, {"password": {"volatile": KEY}}],
    }}}}


def test_plain_string_over_a_lower_layer_marker_is_reported(tmp_path):
    site = _write(tmp_path / "site.json", _site_with_marker())
    local = _write(tmp_path / "local.json", {"mcpServers": {"api": {"config": {
        "login": "moi", "instances": [{"password": "en-clair"}, {"password": {"volatile": KEY}}],
    }}}})
    loaded = load_layers([site, local])
    assert loaded.plain_over_volatile == [
        ("mcpServers.api.config.instances[0].password", KEY, site),
    ]


def test_marker_kept_or_only_in_the_top_layer_reports_nothing(tmp_path):
    site = _write(tmp_path / "site.json", _site_with_marker())
    local = _write(tmp_path / "local.json", {"mcpServers": {"api": {"config": {"login": "moi"}}}})
    assert load_layers([site, local]).plain_over_volatile == []
    assert load_layers([site]).plain_over_volatile == []


def test_warn_uses_the_uvicorn_format(capsys, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
    proxy_logging._warn("attention")
    proxy_logging._log("info")
    assert capsys.readouterr().err == "WARNING:  attention\nINFO:     info\n"


def test_warn_is_yellow_on_a_tty(capsys, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    proxy_logging._warn("attention")
    assert capsys.readouterr().err == "\033[33mWARNING\033[0m:  attention\n"


# --- Upstream en attente de son secret (AU-1b) --------------------------------

from mcp.shared.exceptions import MCPError  # noqa: E402

from mcp_proxy.contract import SECRET_REQUIRED  # noqa: E402
from mcp_proxy.server import (  # noqa: E402
    ToolCatalogCache,
    aggregate_instructions,
    build_proxy_server,
    build_status_report,
)
from mcp_proxy.upstream import InProcessUpstream  # noqa: E402
from tests import volatile_fixture_server as fixture  # noqa: E402
from tests.proxy_client import call_tool, list_tools  # noqa: E402

FIXTURE = "tests.volatile_fixture_server"


def _pending(config=None, name="api"):
    config = config if config is not None else {"login": "moi", "password": {"volatile": KEY}}
    ups = build_upstreams(_cfg(**{name: {"type": "inprocess", "module": FIXTURE, "config": config}}))
    return ups[name]


@pytest.fixture(autouse=True)
def _reset_fixture():
    fixture.CHECKS.clear()
    fixture.BUILDS.clear()


async def test_pending_upstream_is_not_built_and_check_config_sees_the_marker():
    upstream = _pending()
    await upstream.start()
    assert upstream.secret_pending is True
    assert fixture.BUILDS == []
    assert fixture.CHECKS == [{"login": "moi", "password": {"volatile": KEY}}]


async def test_incomplete_config_sets_the_upstream_aside():
    upstream = _pending({"password": {"volatile": KEY}})
    with pytest.raises(mcp_base.ConfigIncomplete, match="login absent"):
        await upstream.start()


async def test_volatile_key_requires_a_build_function():
    ups = build_upstreams(_cfg(bench={
        "type": "inprocess", "module": "mcp_weather", "config": {"k": {"volatile": KEY}},
    }))
    with pytest.raises(RuntimeError, match="n'expose pas build"):
        await ups["bench"].start()


def _wire(tools) -> list[str]:
    return [t.model_dump_json(by_alias=True, exclude_none=True) for t in tools]


async def test_pending_tools_come_from_the_cache_byte_identical_and_without_notice(tmp_path):
    catalog = ToolCatalogCache(tmp_path / "tools-cache.json")
    # 1. Upstream construit (valeur en clair) : sa liste nourrit le cache.
    live = _pending({"login": "moi", "password": fixture.EXPECTED})
    await live.start()
    server = build_proxy_server({"api": live}, {}, catalog=catalog)
    live_listing = await list_tools(server)
    # 2. Même upstream, en attente : resservi depuis le cache.
    pending = _pending()
    await pending.start()
    server = build_proxy_server({"api": pending}, {}, catalog=catalog)
    cached_listing = await list_tools(server)
    assert [t.name for t in cached_listing.tools] == ["api__query"]
    assert _wire(cached_listing.tools) == _wire(live_listing.tools)
    assert list(cached_listing.tools[0].input_schema["properties"]) == ["url", "max_bytes", "count"]
    assert cached_listing.meta is None   # rien sous miaou/unauthorized_upstreams


async def test_pending_upstream_without_cache_lists_nothing(tmp_path):
    pending = _pending()
    await pending.start()
    server = build_proxy_server({"api": pending}, {}, catalog=ToolCatalogCache(tmp_path / "c.json"))
    assert (await list_tools(server)).tools == []


async def test_call_on_a_pending_upstream_is_refused_with_secret_required(tmp_path):
    pending = _pending()
    await pending.start()
    server = build_proxy_server({"api": pending}, {}, catalog=ToolCatalogCache(tmp_path / "c.json"))
    with pytest.raises(MCPError) as info:
        await call_tool(server, "api__query", {"url": "x"})
    assert info.value.error.data == {"code": SECRET_REQUIRED, "upstream": "api"}
    assert "seul l'utilisateur" in info.value.error.message
    assert KEY not in info.value.error.message
    assert fixture.BUILDS == []


async def test_status_report_says_the_upstream_waits():
    pending = _pending()
    await pending.start()
    report = build_status_report({"api": pending})
    assert "api (inprocess) : EN ATTENTE" in report and SECRET_REQUIRED in report


def test_cache_keeps_key_order_and_instructions(tmp_path):
    import mcp.types as types

    catalog = ToolCatalogCache(tmp_path / "c.json")
    schema = {"type": "object", "properties": {"z": {"type": "string"}, "a": {"type": "integer"}}}
    catalog.remember_instructions("api", "Consigne.")
    catalog.remember("api", [types.Tool(name="t", input_schema=schema)])
    tools, _ = catalog.recall("api")
    assert list(tools[0].input_schema["properties"]) == ["z", "a"]
    assert catalog.recall_instructions("api") == "Consigne."
    catalog.remember_instructions("api", None)
    assert catalog.recall_instructions("api") is None
    assert [t.name for t in catalog.recall("api")[0]] == ["t"]


async def test_lifespan_parks_the_upstream_and_publishes_its_cached_instructions(tmp_path, capsys):
    from mcp_proxy.app import build_app
    from tests.test_proxy import _run_lifespan

    catalog = ToolCatalogCache(tmp_path / "c.json")
    catalog.remember_instructions("api", "Consigne du serveur à secret.")
    upstreams = {"api": _pending()}
    server = build_proxy_server(upstreams, {}, catalog=catalog)
    app = build_app(server, upstreams, catalog=catalog)
    async with _run_lifespan(app):
        assert "api" in upstreams
        assert "## api\n\nConsigne du serveur à secret." in server.instructions
    err = capsys.readouterr().err
    assert f"pending — volatile secret « {KEY} » to enter in MIAOU (0 tools from cache)" in err
    assert fixture.BUILDS == []


# --- Extension miaou/volatile-secrets (AU-1c) ----------------------------------

from typing import Any  # noqa: E402

import httpx2  # noqa: E402
from mcp.client import Client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from pydantic import TypeAdapter  # noqa: E402
import mcp.types as types  # noqa: E402

from mcp_proxy import volatile_store as vs  # noqa: E402
from mcp_proxy.app import build_app  # noqa: E402
from tests.proxy_client import _single_cause  # noqa: E402
from tests.test_proxy import _run_lifespan  # noqa: E402

PORT = 18765
ORIGIN = f"http://127.0.0.1:{PORT}"


class _Proxy:
    """Proxy complet (lifespan, app Starlette) sur `httpx2.ASGITransport` :
    `Host` et `Origin` se posent comme un navigateur les poserait."""

    def __init__(self, tmp_path, servers: dict, port: int | None = PORT):
        self.catalog = ToolCatalogCache(tmp_path / "tools-cache.json")
        self.upstreams = build_upstreams(_cfg(**servers))
        self.server = build_proxy_server(self.upstreams, {}, catalog=self.catalog)
        self.secrets = vs.VolatileSecrets(self.upstreams, self.catalog, port=port)
        self.app = build_app(self.server, self.upstreams, catalog=self.catalog, secrets=self.secrets)

    async def rpc(self, method: str, params: dict | None = None, origin: str | None = ORIGIN,
                  base: str = ORIGIN) -> dict:
        headers = {"Origin": origin} if origin else {}
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), headers=headers)
        transport = streamable_http_client(f"{base}/mcp", http_client=http)
        try:
            async with http, Client(transport, mode="2026-07-28") as client:
                request = types.Request[Any, str](method=method, params=params or {})
                return await client.session.send_request(request, TypeAdapter(dict[str, Any]))
        except BaseExceptionGroup as group:
            cause = _single_cause(group)
            if cause is None:
                raise
            raise cause from None

    async def refused(self, method: str, params: dict | None = None, **kw) -> MCPError:
        with pytest.raises(MCPError) as info:
            await self.rpc(method, params, **kw)
        return info.value


def _api(password=None, **extra):
    config = {"login": "moi", "password": password or {"volatile": KEY}, **extra}
    return {"type": "inprocess", "module": FIXTURE, "config": config}


def _consumer(result: dict, name: str = "api") -> dict:
    return next(c for c in result["secret"]["consumers"] if c["upstream"] == name)


async def test_extension_is_discovered_and_status_lists_the_key(tmp_path):
    proxy = _Proxy(tmp_path, {"api": _api(), "bench": {"type": "inprocess", "module": "mcp_bench"}})
    async with _run_lifespan(proxy.app):
        assert vs.EXTENSION_ID in proxy.server.extensions
        status = (await proxy.rpc(vs.STATUS_METHOD))["status"]
    assert status["instance"] == proxy.secrets.instance and len(status["instance"]) == 16
    assert status["secrets"] == [{
        "key": KEY, "held": False,
        "consumers": [{"upstream": "api", "state": "pending", "verdict": None, "detail": None}],
    }]


async def test_no_consumer_left_means_no_extension(tmp_path):
    proxy = _Proxy(tmp_path, {"api": _api(login="")})
    async with _run_lifespan(proxy.app):
        assert vs.EXTENSION_ID not in proxy.server.extensions
        assert "api" not in proxy.upstreams


@pytest.mark.parametrize("origin, base", [
    (None, ORIGIN),                                        # pas d'Origin
    ("null", ORIGIN),                                      # MIAOU en file://, iframe sandboxée
    ("https://evil.example", ORIGIN),                      # page tierce
    ("http://localhost:18765", ORIGIN),                    # autre nom que la carte
    ("http://evil.example:18765", "http://evil.example:18765"),  # DNS rebinding
    ("http://127.0.0.1:9999", "http://127.0.0.1:9999"),    # autre port
])
async def test_guard_refuses_anything_but_the_page_served_by_this_proxy(tmp_path, origin, base):
    proxy = _Proxy(tmp_path, {"api": _api()})
    async with _run_lifespan(proxy.app):
        for method, params in ((vs.STATUS_METHOD, {}), (vs.SUBMIT_METHOD, {"key": KEY, "value": fixture.EXPECTED}),
                               (vs.FORGET_METHOD, {"key": KEY})):
            err = await proxy.refused(method, params, origin=origin, base=base)
            assert err.error.data == {"code": vs.ORIGIN_REFUSED}
            assert f"http://127.0.0.1:{PORT}/app/" in err.error.message
    assert fixture.BUILDS == []


async def test_guard_accepts_localhost_as_well(tmp_path):
    proxy = _Proxy(tmp_path, {"api": _api()})
    async with _run_lifespan(proxy.app):
        local = f"http://localhost:{PORT}"
        assert (await proxy.rpc(vs.STATUS_METHOD, origin=local, base=local))["status"]["secrets"]


async def test_guard_refuses_without_a_known_port(tmp_path):
    proxy = _Proxy(tmp_path, {"api": _api()}, port=None)
    async with _run_lifespan(proxy.app):
        assert (await proxy.refused(vs.STATUS_METHOD)).error.data["code"] == vs.ORIGIN_REFUSED


async def test_accepted_value_builds_the_upstream_and_publishes_it(tmp_path):
    proxy = _Proxy(tmp_path, {"api": _api()})
    async with _run_lifespan(proxy.app):
        result = await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": fixture.EXPECTED})
        assert result["secret"]["held"] is True
        assert _consumer(result) == {"upstream": "api", "state": "ready", "verdict": "accepted", "detail": None}
        assert fixture.BUILDS == [{"login": "moi", "password": fixture.EXPECTED}]
        called = await call_tool(proxy.server, "api__query", {"url": "u"})
        assert called.content[0].text == "moi u"
        assert "## api\n\nConsigne du serveur à secret." in proxy.server.instructions
        # Même valeur : rien n'est reconstruit, aucun appel sortant.
        again = await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": fixture.EXPECTED})
        assert _consumer(again)["verdict"] == "accepted"
        assert len(fixture.BUILDS) == 1
    # Le prochain démarrage en attente a de quoi servir.
    assert [t.name for t in proxy.catalog.recall("api")[0]] == ["query"]
    assert proxy.catalog.recall_instructions("api") == "Consigne du serveur à secret."


async def test_rejected_value_is_never_retested(tmp_path):
    proxy = _Proxy(tmp_path, {"api": _api()})
    async with _run_lifespan(proxy.app):
        first = await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": "faux"})
        assert _consumer(first)["verdict"] == "rejected" and _consumer(first)["state"] == "pending"
        second = await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": "faux"})
        assert _consumer(second)["verdict"] == "rejected"
        assert "non retestée" in _consumer(second)["detail"]
        assert len(fixture.BUILDS) == 1
        with pytest.raises(MCPError) as info:
            await call_tool(proxy.server, "api__query", {"url": "u"})
        assert info.value.error.data["code"] == SECRET_REQUIRED


async def test_sequential_validation_stops_at_the_first_refusal(tmp_path):
    proxy = _Proxy(tmp_path, {"one": _api(), "two": _api()})
    async with _run_lifespan(proxy.app):
        result = await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": "faux"})
    assert [(c["upstream"], c["verdict"]) for c in result["secret"]["consumers"]] == [
        ("one", "rejected"), ("two", "untested"),
    ]
    assert len(fixture.BUILDS) == 1


async def test_indeterminate_and_incomplete_do_not_stop_the_sequence(tmp_path):
    proxy = _Proxy(tmp_path, {
        "one": _api(instances=[{"password": {"volatile": KEY}}]),
        "two": _api(),
    })
    async with _run_lifespan(proxy.app):
        result = await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": fixture.UNREACHABLE})
        assert [c["verdict"] for c in result["secret"]["consumers"]] == ["indeterminate", "indeterminate"]
        assert "ConnectionError" in result["secret"]["consumers"][0]["detail"]
        result = await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": fixture.INCOMPLETE})
        assert [c["verdict"] for c in result["secret"]["consumers"]] == ["incomplete", "incomplete"]
        # Ni l'un ni l'autre n'est mémorisé comme refusé : retesté.
        result = await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": fixture.UNREACHABLE})
    assert len(fixture.BUILDS) == 6


async def test_three_instances_in_one_upstream_all_tested(tmp_path):
    proxy = _Proxy(tmp_path, {"api": _api(instances=[{"password": {"volatile": KEY}}] * 3)})
    async with _run_lifespan(proxy.app):
        await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": fixture.EXPECTED})
    assert fixture.BUILDS[0]["instances"] == [{"password": fixture.EXPECTED}] * 3
    assert fixture.BUILDS[0]["password"] == fixture.EXPECTED


async def test_hot_replacement_keeps_the_old_instance_on_refusal(tmp_path):
    proxy = _Proxy(tmp_path, {"api": _api()})
    async with _run_lifespan(proxy.app):
        await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": fixture.EXPECTED})
        old = proxy.upstreams["api"]._server
        result = await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": "faux"})
        assert _consumer(result)["state"] == "ready" and _consumer(result)["verdict"] == "rejected"
        assert result["secret"]["held"] is True
        assert proxy.upstreams["api"]._server is old
        assert (await call_tool(proxy.server, "api__query", {"url": "u"})).content[0].text == "moi u"


async def test_forget_discards_the_instance_and_waits_again(tmp_path):
    proxy = _Proxy(tmp_path, {"api": _api()})
    async with _run_lifespan(proxy.app):
        await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": fixture.EXPECTED})
        result = await proxy.rpc(vs.FORGET_METHOD, {"key": KEY})
        assert result["secret"] == {"key": KEY, "held": False, "consumers": [
            {"upstream": "api", "state": "pending", "verdict": None, "detail": None}]}
        with pytest.raises(MCPError) as info:
            await call_tool(proxy.server, "api__query", {"url": "u"})
        assert info.value.error.data["code"] == SECRET_REQUIRED
        # Les instructions publiées ne bougent pas.
        assert "Consigne du serveur à secret." in proxy.server.instructions
        # Re-saisie : reconstruit (l'empreinte acceptée a été oubliée).
        await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": fixture.EXPECTED})
    assert len(fixture.BUILDS) == 2


@pytest.mark.parametrize("params, code", [
    ({"key": KEY, "value": 1234}, vs.INVALID_PARAMS),
    ({"key": KEY, "value": ""}, vs.INVALID_PARAMS),
    ({"key": KEY}, vs.INVALID_PARAMS),
    ({"value": "x"}, vs.INVALID_PARAMS),
    ({"key": "inconnue", "value": "x"}, vs.UNKNOWN_KEY),
])
async def test_invalid_params_are_refused_without_echoing_the_value(tmp_path, params, code):
    proxy = _Proxy(tmp_path, {"api": _api()})
    async with _run_lifespan(proxy.app):
        err = await proxy.refused(vs.SUBMIT_METHOD, params)
    assert err.error.data == {"code": code}
    assert "1234" not in err.error.message
    assert fixture.BUILDS == []


async def test_value_never_leaks_through_a_failing_build(tmp_path, capsys):
    value = fixture.LEAKY_PREFIX + "Zx9-tres-secret"
    proxy = _Proxy(tmp_path, {"api": _api()})
    async with _run_lifespan(proxy.app):
        result = await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": value})
        status = await proxy.rpc(vs.STATUS_METHOD)
    detail = _consumer(result)["detail"]
    # L'exception citait la config : masquée, sa cause gardée.
    assert detail.startswith("ValueError: config invalide") and "***" in detail
    out = capsys.readouterr()
    for text in (json.dumps(result), json.dumps(status), out.out, out.err):
        assert value not in text
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert value not in path.read_text(errors="replace")


async def test_value_cited_by_the_server_keeps_it_out_of_the_cache(tmp_path, capsys):
    proxy = _Proxy(tmp_path, {"api": _api(cite=True)})
    async with _run_lifespan(proxy.app):
        await proxy.rpc(vs.SUBMIT_METHOD, {"key": KEY, "value": fixture.EXPECTED})
        await list_tools(proxy.server)
    assert proxy.upstreams["api"].cache_forbidden is True
    cache = tmp_path / "tools-cache.json"
    assert not cache.exists() or fixture.EXPECTED not in cache.read_text()
    assert "rien de lui n'est écrit dans le cache" in capsys.readouterr().err


def test_credentials_rejected_is_recognised_by_name_from_another_module():
    class CredentialsRejected(Exception):
        pass

    class Sub(mcp_base.CredentialsRejected):
        pass

    assert vs.classify_failure(CredentialsRejected("x")) == vs.REJECTED
    assert vs.classify_failure(Sub("x")) == vs.REJECTED
    assert vs.classify_failure(mcp_base.ConfigIncomplete("x")) == vs.INCOMPLETE
    assert vs.classify_failure(OSError("x")) == vs.INDETERMINATE
