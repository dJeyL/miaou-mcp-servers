"""Tests de la recherche multi-moteurs de mcp_web (search, image_search) — tout
mocké, aucune clef ni requête réelle. Un opener unique route par URL : chaque
moteur reçoit sa réponse, et l'on compte qui a été interrogé."""
import io
import json
import math
import os
import sys
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from mcp import types
import mcp_brave
import mcp_ddg
import mcp_web
from mcp_web import search as web_search
from mcp_web.search import common as search_common
from mcp_web.search import ddg as search_ddg

_BRAVE_WEB = "api.search.brave.com/res/v1/web/search"
_BRAVE_IMAGES = "api.search.brave.com/res/v1/images/search"
_OLLAMA = "ollama.com/api/web_search"
_DDG = "html.duckduckgo.com/html/"

_BRAVE_BODY = {"web": {"results": [
    {"title": "Python", "url": "https://python.org", "description": "Le <strong>langage</strong> Python &amp; co."},
]}}
_BRAVE_IMAGES_BODY = {"results": [
    {"title": "Chat", "url": "https://p.example/chat", "properties": {"url": "https://i.example/chat.jpg"},
     "thumbnail": {"src": "https://t.example/chat.jpg"}, "source": "p.example"},
    {"title": "Sans image", "url": "https://p.example/vide", "properties": {}},
]}
_OLLAMA_BODY = {"results": [
    {"title": "Ollama", "url": "https://ollama.com", "content": "mot " * 500},
]}
_DDG_HTML = (
    '<a class="result__a" href="https://ddg.example">DDG</a>'
    '<a class="result__snippet">Extrait DDG.</a>'
).encode()
_DDG_CHALLENGE = b'<div class="anomaly-modal__modal"><form action="//duckduckgo.com/anomaly.js"></form></div>'


def _resp(body):
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read.return_value = body
    return mock


def _http_error(url: str, code: int, body: bytes = b""):
    return urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(body))


class _Router:
    """Opener factice : `routes` associe un fragment d'URL à un corps, ou à une
    exception à lever. Garde les requêtes reçues, et les HTTPError levées pour
    vérifier qu'elles ont été fermées par le serveur."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list = []
        self.errors: list = []

    def hits(self, fragment: str) -> int:
        return sum(fragment in r.full_url for r in self.requests)

    def __call__(self, req, *args, **kwargs):
        self.requests.append(req)
        for fragment, outcome in self.routes.items():
            if fragment in req.full_url:
                if isinstance(outcome, BaseException):
                    if isinstance(outcome, urllib.error.HTTPError):
                        self.errors.append(outcome)
                    raise outcome
                return _resp(outcome)
        raise AssertionError(f"requête inattendue : {req.full_url}")


@pytest.fixture(autouse=True)
def _reset_ddg(monkeypatch):
    """L'état DDG est celui du processus : remis à zéro, espacement coupé."""
    monkeypatch.setattr(search_ddg, "_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(search_ddg.ENGINE, "next_slot", 0.0)
    monkeypatch.setattr(search_ddg.ENGINE, "down_until", 0.0)
    monkeypatch.setattr(search_ddg.ENGINE, "down_reason", "")


@pytest.fixture(autouse=True)
def _no_env_keys(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)


def _server(order=None, brave="bk", ollama="ok"):
    search: dict = {}
    if order is not None:
        search["order"] = order
    if brave:
        search["brave"] = {"api_key": brave}
    if ollama:
        search["ollama"] = {"api_key": ollama}
    return mcp_web.WebServer({"search": search})


def _tool_names(server) -> set[str]:
    return {t.name for t in server.mcp._tool_manager.list_tools()}


async def _call_raw(server, router, tool="search", **args) -> types.CallToolResult:
    args.setdefault("query", "python")
    with patch("urllib.request.OpenerDirector.open", side_effect=router):
        result = await server.mcp._tool_manager.call_tool(tool, args, None)
    assert isinstance(result, types.CallToolResult)
    assert result.is_error is False and len(result.content) == 1
    return result


async def _call(server, router, tool="search", **args):
    """Le JSON du résultat, ou le texte d'un échec."""
    block = (await _call_raw(server, router, tool, **args)).content[0]
    if isinstance(block, types.EmbeddedResource):
        return json.loads(block.resource.text)
    return block.text


# --- Chaîne de repli ----------------------------------------------------------


async def test_first_engine_answers_and_others_are_not_called():
    router = _Router({_BRAVE_WEB: _BRAVE_BODY, _OLLAMA: _OLLAMA_BODY, _DDG: _DDG_HTML})
    out = await _call(_server(), router)
    assert out["engine"] == "brave"
    assert "fallback" not in out
    assert router.hits(_OLLAMA) == 0 and router.hits(_DDG) == 0


async def test_failure_falls_back_and_says_why():
    router = _Router({_BRAVE_WEB: _http_error(_BRAVE_WEB, 503), _OLLAMA: _OLLAMA_BODY})
    out = await _call(_server(), router)
    assert out["engine"] == "ollama"
    assert out["fallback"] == [{"engine": "brave", "reason": "HTTP 503 (x)"}]
    assert all(e.fp.closed for e in router.errors)  # fermées par le serveur


async def test_empty_result_is_an_answer_and_stops_the_chain():
    router = _Router({_BRAVE_WEB: {"web": {"results": []}}, _OLLAMA: _OLLAMA_BODY, _DDG: _DDG_HTML})
    out = await _call(_server(), router)
    assert out == {"engine": "brave", "results": []}
    assert router.hits(_OLLAMA) == 0 and router.hits(_DDG) == 0


async def test_brave_without_web_block_is_an_empty_answer():
    router = _Router({_BRAVE_WEB: {"query": {"original": "x"}}})
    out = await _call(_server(), router)
    assert out == {"engine": "brave", "results": []}


async def test_all_engines_failing_returns_a_message_naming_each():
    router = _Router({
        _BRAVE_WEB: _http_error(_BRAVE_WEB, 500),
        _OLLAMA: urllib.error.URLError("unreachable"),
        _DDG: _DDG_CHALLENGE,
    })
    out = await _call(_server(), router)
    assert isinstance(out, str)
    assert "brave : HTTP 500" in out and "ollama : erreur réseau" in out and "ddg : défi anti-bot" in out


async def test_order_from_config_is_respected():
    router = _Router({_BRAVE_WEB: _BRAVE_BODY, _DDG: _DDG_HTML})
    out = await _call(_server(order=["ddg", "brave"]), router)
    assert out["engine"] == "ddg"
    assert router.hits(_BRAVE_WEB) == 0


async def test_invalid_json_falls_back():
    router = _Router({_BRAVE_WEB: b"<html>", _OLLAMA: _OLLAMA_BODY})
    out = await _call(_server(), router)
    assert out["engine"] == "ollama"
    assert "JSON" in out["fallback"][0]["reason"]


# --- `_meta` pour le client --------------------------------------------------------


async def test_engine_is_also_in_result_meta():
    router = _Router({_BRAVE_WEB: _http_error(_BRAVE_WEB, 503), _OLLAMA: _OLLAMA_BODY})
    result = await _call_raw(_server(), router)
    assert result.meta == {"miaou/search": {"engine": "ollama"}}
    wire = result.model_dump(by_alias=True, exclude_none=True)
    assert wire["_meta"] == {"miaou/search": {"engine": "ollama"}}


async def test_image_search_meta_names_engine():
    result = await _call_raw(_server(), _Router({_BRAVE_IMAGES: _BRAVE_IMAGES_BODY}), tool="image_search")
    assert result.meta == {"miaou/search": {"engine": "brave"}}


async def test_no_meta_when_no_engine_answered():
    result = await _call_raw(_server(order=["ddg"]), _Router({_DDG: _DDG_CHALLENGE}))
    assert result.meta is None


# --- Pauses d'un appel à l'autre -------------------------------------------------


async def test_rejected_key_takes_engine_out_until_restart():
    server = _server()
    router = _Router({_BRAVE_WEB: _http_error(_BRAVE_WEB, 401), _OLLAMA: _OLLAMA_BODY})
    await _call(server, router)
    out = await _call(server, router)
    assert router.hits(_BRAVE_WEB) == 1  # pas retentée au second appel
    assert "redémarrage" in out["fallback"][0]["reason"]
    assert "clef refusée (HTTP 401)" in out["fallback"][0]["reason"]


# Corps mesuré le 2026-10-05, clef fausse.
_BRAVE_INVALID_KEY = (
    b'{"error":{"code":"SUBSCRIPTION_TOKEN_INVALID","detail":"The provided subscription token is invalid.",'
    b'"meta":{"component":"authentication"},"status":422},"type":"ErrorResponse"}'
)


async def test_brave_invalid_key_422_is_a_rejected_key():
    server = _server()
    router = _Router({_BRAVE_WEB: _http_error(_BRAVE_WEB, 422, _BRAVE_INVALID_KEY), _OLLAMA: _OLLAMA_BODY})
    out = await _call(server, router)
    assert out["fallback"][0]["reason"] == "clef refusée (HTTP 422)"
    assert math.isinf(server.search_chain.engines[0].down_until)
    assert all(e.fp.closed for e in router.errors)


async def test_brave_other_422_is_transient():
    server = _server()
    body = b'{"error":{"code":"VALIDATION","meta":{"component":"validation"}}}'
    router = _Router({_BRAVE_WEB: _http_error(_BRAVE_WEB, 422, body), _OLLAMA: _OLLAMA_BODY})
    out = await _call(server, router)
    assert out["fallback"][0]["reason"] == "HTTP 422 (x)"
    assert not math.isinf(server.search_chain.engines[0].down_until)


async def test_quota_pauses_engine_then_retries_after_cooldown():
    server = _server()
    router = _Router({_BRAVE_WEB: _http_error(_BRAVE_WEB, 429), _OLLAMA: _OLLAMA_BODY})
    await _call(server, router)
    brave = server.search_chain.engines[0]
    remaining = brave.down_until - time.monotonic()
    assert search_common.QUOTA_COOLDOWN_S - 5 < remaining <= search_common.QUOTA_COOLDOWN_S
    out = await _call(server, router)
    assert router.hits(_BRAVE_WEB) == 1
    assert "en pause encore 10 min" in out["fallback"][0]["reason"]
    brave.down_until = time.monotonic() - 1  # pause écoulée
    router.routes[_BRAVE_WEB] = _BRAVE_BODY
    out = await _call(server, router)
    assert out["engine"] == "brave"


async def test_ddg_challenge_pauses_ddg_across_server_instances():
    """Le défi vise l'adresse IP : la pause vaut pour toutes les instances du processus."""
    router = _Router({_DDG: _DDG_CHALLENGE})
    await _call(_server(order=["ddg"]), router)
    remaining = search_ddg.ENGINE.down_until - time.monotonic()
    assert search_ddg.ANOMALY_COOLDOWN_S - 5 < remaining <= search_ddg.ANOMALY_COOLDOWN_S
    out = await _call(_server(order=["ddg"]), router)
    assert router.hits(_DDG) == 1
    assert out.startswith("Aucun moteur") and "en pause encore 2 h 30" in out


async def test_ddg_throttle_refusal_does_not_pause_the_engine(monkeypatch):
    monkeypatch.setattr(search_ddg, "_MIN_INTERVAL_S", 60.0)
    server = _server(order=["ddg"])
    router = _Router({_DDG: _DDG_HTML})
    await _call(server, router)
    out = await _call(server, router)
    assert "espacées" in out
    assert search_ddg.ENGINE.down_until == 0.0


# --- Budget de temps ---------------------------------------------------------------


def test_search_budget_fits_miaou_timeout():
    assert web_search.SEARCH_BUDGET_S < 30


async def test_engine_timeout_is_shaved_to_remaining_budget(monkeypatch):
    monkeypatch.setattr(web_search, "SEARCH_BUDGET_S", 4.0)
    router = _Router({_BRAVE_WEB: _BRAVE_BODY})
    timeouts = []

    def opener(req, *a, timeout=None, **k):
        timeouts.append(timeout)
        return router(req)

    with patch("urllib.request.OpenerDirector.open", side_effect=opener):
        await _server().mcp._tool_manager.call_tool("search", {"query": "q"}, None)
    assert 3 < timeouts[0] <= 4


async def test_ddg_wait_beyond_remaining_budget_is_refused_without_request(monkeypatch):
    """DDG en fin de chaîne : son attente ne doit pas déborder le budget de l'appel."""
    monkeypatch.setattr(web_search, "SEARCH_BUDGET_S", 10.0)
    monkeypatch.setattr(search_ddg.ENGINE, "next_slot", time.monotonic() + 9.0)
    router = _Router({_DDG: _DDG_HTML})
    out = await _call(_server(order=["ddg"]), router)
    assert router.hits(_DDG) == 0
    assert "espacées" in out


async def test_ddg_waits_its_slot_within_budget(monkeypatch):
    waits = []

    async def fake_sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(search_ddg.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(search_ddg.ENGINE, "next_slot", time.monotonic() + 5.0)
    out = await _call(_server(order=["ddg"]), _Router({_DDG: _DDG_HTML}))
    assert out["engine"] == "ddg"
    assert len(waits) == 1 and 4 < waits[0] <= 5


# --- Normalisation des résultats ---------------------------------------------------


async def test_results_are_normalised_and_snippets_capped():
    out = await _call(_server(order=["brave"]), _Router({_BRAVE_WEB: _BRAVE_BODY}))
    assert out["results"] == [{"title": "Python", "url": "https://python.org", "snippet": "Le langage Python & co."}]
    out = await _call(_server(order=["ollama"]), _Router({_OLLAMA: _OLLAMA_BODY}))
    snippet = out["results"][0]["snippet"]
    assert len(snippet) <= search_common.SNIPPET_MAX_CHARS + 1 and snippet.endswith("…")


async def test_ddg_results_shape():
    out = await _call(_server(order=["ddg"]), _Router({_DDG: _DDG_HTML}))
    assert out["results"] == [{"title": "DDG", "url": "https://ddg.example", "snippet": "Extrait DDG."}]


async def test_max_results_clamped_to_ten_for_every_engine():
    router = _Router({_BRAVE_WEB: _BRAVE_BODY})
    await _call(_server(), router, max_results=500)
    assert "count=10" in router.requests[-1].full_url
    router = _Router({_OLLAMA: _OLLAMA_BODY})
    await _call(_server(order=["ollama"]), router, max_results=500)
    assert json.loads(router.requests[-1].data)["max_results"] == 10
    router = _Router({_BRAVE_WEB: _BRAVE_BODY})
    await _call(_server(), router, max_results=-3)
    assert "count=1" in router.requests[-1].full_url


async def test_ollama_request_shape():
    router = _Router({_OLLAMA: _OLLAMA_BODY})
    await _call(_server(order=["ollama"], ollama="secret"), router, query="chat")
    req = router.requests[0]
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer secret"
    assert json.loads(req.data) == {"query": "chat", "max_results": 5}


async def test_image_search_uses_image_capable_engines_only():
    router = _Router({_BRAVE_IMAGES: _BRAVE_IMAGES_BODY})
    out = await _call(_server(), router, tool="image_search", query="chat")
    assert out["engine"] == "brave"
    assert out["results"] == [{
        "title": "Chat", "page_url": "https://p.example/chat", "image_url": "https://i.example/chat.jpg",
        "thumbnail_url": "https://t.example/chat.jpg", "source": "p.example",
    }]


# --- Listage et config ----------------------------------------------------------


def test_image_search_listed_only_with_an_image_engine():
    assert "image_search" in _tool_names(_server())
    assert "image_search" not in _tool_names(_server(brave=None))
    assert "image_search" not in _tool_names(_server(order=["ollama", "ddg"]))


def test_search_not_listed_without_any_engine():
    names = _tool_names(_server(order=[]))
    assert "search" not in names and "image_search" not in names
    assert "fetch_url" in names


def test_no_usable_engine_refuses_construction_like_brave():
    """Moteurs demandés mais aucun configuré : refus, avec la cause de chacun."""
    with pytest.raises(web_search.SearchConfigError) as exc:
        mcp_web.build({"search": {"order": ["brave", "ollama"]}})
    assert "aucun moteur de recherche utilisable" in str(exc.value)
    assert "BRAVE_API_KEY" in str(exc.value) and "OLLAMA_API_KEY" in str(exc.value)


def test_default_order_and_skipped_engines_on_boot_line():
    server = _server(brave=None)
    assert server.search_chain.names("web") == ["ollama", "ddg"]
    summary = server.search_chain.summary()
    assert "recherche via ollama → ddg" in summary
    assert "brave écarté (aucune clef" in summary and "BRAVE_API_KEY" in summary


def test_env_keys_are_used_when_config_has_none(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "env-brave")
    server = mcp_web.WebServer({})
    assert server.search_chain.names("web") == ["brave", "ddg"]


def test_config_key_takes_precedence_over_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "env")
    assert search_common.resolve_api_key({"api_key": "cfg"}, "OLLAMA_API_KEY") == "cfg"
    assert search_common.resolve_api_key({"api_key": "  "}, "OLLAMA_API_KEY") == "env"


@pytest.mark.parametrize(
    "search_cfg, fragment",
    [
        ({"order": ["brave", "bing"]}, "moteur inconnu « bing »"),
        ({"order": ["ddg", "ddg"]}, "« ddg » répété"),
        ({"order": "ddg"}, "liste"),
        ("ddg", "objet attendu"),
    ],
)
def test_invalid_search_config_fails_construction(search_cfg, fragment):
    with pytest.raises(web_search.SearchConfigError, match=fragment):
        mcp_web.build({"search": search_cfg})


def test_build_announces_the_chain(capsys):
    mcp_web.build({"search": {"order": ["ddg"]}})
    assert "miaou-web : recherche via ddg" in capsys.readouterr().err


def test_ddg_engine_is_one_per_process():
    a, b = _server(order=["ddg"]), _server(order=["ddg"])
    assert a.search_chain.engines[0] is b.search_chain.engines[0] is search_ddg.ENGINE


def test_search_description_names_the_order_and_ddg_spacing(monkeypatch):
    monkeypatch.setattr(search_ddg, "_MIN_INTERVAL_S", 15.0)
    tools = {t.name: t for t in _server().mcp._tool_manager.list_tools()}
    desc = tools["search"].description
    assert "brave → ollama → ddg" in desc
    assert "[1, 10]" in desc and "15 s" in desc
    assert "15 s" not in {t.name: t for t in _server(order=["brave"]).mcp._tool_manager.list_tools()}["search"].description


# --- Serveurs dépréciés ----------------------------------------------------------


def test_deprecated_servers_announce_it(capsys, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    mcp_brave.build(None)
    assert mcp_ddg.build(None) is mcp_ddg.mcp
    err = capsys.readouterr().err
    assert "mcp_brave est déprécié" in err and "mcp_ddg est déprécié" in err
