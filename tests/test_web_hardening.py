"""Lot L2 de la campagne de relecture (constats Z7-*, C-3, P1) pour mcp_web :
frontières d'erreur de fetch_url, nettoyage du cache, bombe de décompression,
sonde de favicon, redirections, alias de cache par URL finale, filtre de
`canonical_url`, URL de résultats de recherche. Aucun appel réseau : les
réponses sont routées sur un `OpenerDirector.open` patché ; le seul vrai socket
est un serveur loopback éphémère (Z7-6)."""
import base64
import gzip
import http.server
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from mcp import types
import mcp_web
from mcp_web import cache as web_cache
from mcp_web import netguard, pagemeta
from mcp_web.pagemeta import META_KEY, build_web_meta
from mcp_web.search import common as search_common

_TM = mcp_web.server.mcp._tool_manager

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(web_cache, "WORKDIR", tmp_path)
    mcp_web.favicon_cache.clear()


def _resp(body: bytes, content_type: str = "text/html; charset=utf-8",
          final_url: str | None = None, encoding: str | None = None):
    headers = MagicMock()

    def _get(key, default=""):
        if key == "Content-Type":
            return content_type
        if key == "Content-Encoding":
            return encoding if encoding is not None else default
        return default

    headers.get.side_effect = _get
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read.side_effect = lambda n=-1: body if n < 0 else body[:n]
    mock.headers = headers
    mock.geturl.return_value = final_url
    return mock


class _Router:
    def __init__(self, routes: dict):
        self.routes = routes
        self.requested: list[str] = []

    def __call__(self, req, *args, **kwargs):
        url = req.full_url
        self.requested.append(url)
        outcome = self.routes.get(url)
        if outcome is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _page(head: str = "", body: str = "<p>Corps</p>") -> bytes:
    return f"<html><head>{head}</head><body>{body}</body></html>".encode()


async def _call(tool: str, args: dict, router):
    with patch("urllib.request.OpenerDirector.open", side_effect=router):
        return await _TM.call_tool(tool, args, None)


# ---------------------------------------------------------------------------
# Z7-2 — exceptions qui fuyaient de fetch_url
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["fetch_url", "fetch_resource"])
async def test_malformed_url_is_a_message_not_an_exception(tool):
    result = await _call(tool, {"url": "http://[::1"}, _Router({}))
    text = result.content[0].text if isinstance(result, types.CallToolResult) else result
    assert "URL invalide" in text


async def test_charset_idna_does_not_crash_fetch_url():
    router = _Router({"https://s.test/": _resp(_page(), "text/html; charset=idna")})
    result = await _call("fetch_url", {"url": "https://s.test/"}, router)
    assert "Corps" in result.content[0].resource.text


async def test_charset_idna_does_not_crash_on_textual_branch():
    router = _Router({"https://s.test/t": _resp(b"du texte", "text/plain; charset=idna")})
    result = await _call("fetch_url", {"url": "https://s.test/t"}, router)
    assert "du texte" in result.content[0].resource.text


async def test_unparsable_icon_href_does_not_fail_fetch_url():
    """Même classe que Z7-2 : un href de <link rel=icon> forgé (« http://[::1 »)
    faisait lever urljoin dans la sonde de favicon, donc échouer tout le fetch."""
    router = _Router({
        "https://s.test/": _resp(_page('<link rel="icon" href="http://[::1">'), final_url="https://s.test/"),
    })
    result = await _call("fetch_url", {"url": "https://s.test/"}, router)
    assert "Corps" in result.content[0].resource.text


# ---------------------------------------------------------------------------
# Z7-3 — sweep_expired ne touche que les fichiers du cache
# ---------------------------------------------------------------------------

def test_sweep_leaves_foreign_files_alone(tmp_path):
    old = time.time() - 48 * 3600
    foreign = tmp_path / "notes.txt"
    foreign.write_text("à moi")
    also_foreign = tmp_path / ("a" * 63 + ".txt")  # 63 hexa : pas une clef
    also_foreign.write_text("x")
    mine = web_cache.entry_path("https://s.test/vieux")
    mine.write_text("périmé")
    for p in (foreign, also_foreign, mine):
        os.utime(p, (old, old))
    web_cache.sweep_expired()
    assert foreign.exists() and also_foreign.exists()
    assert not mine.exists()


# ---------------------------------------------------------------------------
# A1 / Z7-1 — plafond décompressé = max_bytes
# ---------------------------------------------------------------------------

class _SpyDecompressor:
    """Enveloppe d'une decompressobj qui note la taille de chaque sortie."""

    sizes: list[int] = []

    def __init__(self, inner):
        self._inner = inner

    def decompress(self, *a, **kw):
        out = self._inner.decompress(*a, **kw)
        _SpyDecompressor.sizes.append(len(out))
        return out

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
def spy_zlib(monkeypatch):
    real = mcp_web.zlib.decompressobj
    _SpyDecompressor.sizes = []
    monkeypatch.setattr(
        mcp_web.zlib, "decompressobj", lambda *a, **kw: _SpyDecompressor(real(*a, **kw))
    )
    return _SpyDecompressor


async def test_gzip_bomb_is_cut_at_max_bytes_and_never_materialised(spy_zlib):
    bomb = gzip.compress(b"\0" * 50_000_000)
    router = _Router({"https://s.test/b": _resp(bomb, "text/plain", encoding="gzip")})
    result = await _call("fetch_url", {"url": "https://s.test/b", "max_bytes": 1000}, router)
    text = result.content[0].resource.text
    assert text.startswith("\0" * 1000) and "[Tronqué à 1000 octets]" in text
    assert max(spy_zlib.sizes) <= 1000 + 4096, spy_zlib.sizes


async def test_gzip_bomb_in_fetch_resource_is_cut_at_max_bytes(spy_zlib):
    bomb = gzip.compress(b"\0" * 50_000_000)
    router = _Router({"https://s.test/b": _resp(bomb, "application/octet-stream", encoding="gzip")})
    result = await _call("fetch_resource", {"url": "https://s.test/b", "max_bytes": 1000}, router)
    descriptor, blob = result
    assert len(base64.b64decode(blob.resource.blob)) == 1000
    assert "Tronqué à 1000 octets" in descriptor.text
    assert max(spy_zlib.sizes) <= 1000 + 4096


async def test_gzip_body_of_exactly_max_bytes_is_not_truncated():
    body = gzip.compress(b"y" * 1000)
    router = _Router({"https://s.test/e": _resp(body, "text/plain", encoding="gzip")})
    result = await _call("fetch_url", {"url": "https://s.test/e", "max_bytes": 1000}, router)
    assert "Tronqué" not in result.content[0].resource.text


async def test_gzip_one_byte_over_max_bytes_is_truncated():
    body = gzip.compress(b"y" * 1001)
    router = _Router({"https://s.test/e": _resp(body, "text/plain", encoding="gzip")})
    result = await _call("fetch_url", {"url": "https://s.test/e", "max_bytes": 1000}, router)
    text = result.content[0].resource.text
    assert "[Tronqué à 1000 octets]" in text and text.count("y") == 1000


def test_tool_texts_say_max_bytes_counts_useful_bytes():
    tools = {t.name: t for t in _TM.list_tools()}
    for name in ("fetch_url", "fetch_resource"):
        desc = tools[name].parameters["properties"]["max_bytes"]["description"]
        assert "décompress" in desc, (name, desc)
        assert "décompress" in tools[name].description, name


# ---------------------------------------------------------------------------
# A2 / Z7-4 — la sonde de favicon refuse les hôtes IP et localhost
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/favicon.ico",
    "http://127.1/f.ico",
    "http://2130706433/f.ico",
    "http://0x7f.0.0.1/f.ico",
    "http://0x7f000001/f.ico",
    "http://017700000001/f.ico",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1:8080/x",
    "http://1.2.3/x",
    "http://[::1]/f.ico",
    "http://[::ffff:127.0.0.1]/f.ico",
    "http://localhost/f.ico",
    "http://LOCALHOST./f.ico",
    "http://app.localhost:8765/f.ico",
    "http://a.b.Localhost./f.ico",
    "http://user:pw@127.0.0.1/f.ico",
    "http://１２７.0.0.1/f.ico",   # chiffres pleine chasse : IDNA les ramène à 127
    "http://127。0。0。1/f.ico",   # points idéographiques
    "http:///f.ico",                           # pas d'hôte
])
def test_ip_or_localhost_hosts_are_refused(url):
    assert netguard.is_forbidden_probe_host(url)


@pytest.mark.parametrize("url", [
    "https://example.org/favicon.ico",
    "https://s.test/f.png",
    "https://localhost.example.org/f.ico",
    "https://notlocalhost/f.ico",
    "https://1.example.org/f.ico",
    "https://cdn.example.com:8443/f.ico",
    "https://xn--bcher-kva.example/f.ico",
])
def test_ordinary_names_are_allowed(url):
    assert not netguard.is_forbidden_probe_host(url)


async def test_icon_pointing_at_internal_addresses_is_never_requested():
    head = (
        '<link rel="icon" href="http://127.0.0.1:9/admin/restart">'
        '<link rel="icon" href="http://169.254.169.254/latest/meta-data/">'
        '<link rel="icon" href="http://localhost:9/x">'
    )
    router = _Router({
        "https://s.test/p": _resp(_page(head), final_url="https://s.test/p"),
        "https://s.test/favicon.ico": _resp(PNG, "image/png"),
    })
    result = await _call("fetch_url", {"url": "https://s.test/p"}, router)
    assert router.requested == ["https://s.test/p", "https://s.test/favicon.ico"]
    assert result.meta[META_KEY]["favicon"].startswith("data:image/png")


async def test_page_served_from_an_ip_gets_no_favicon_probe():
    """fetch_url lui-même n'est pas restreint (banc local légitime) ; seule la
    sonde l'est, donc pas de favicon pour une page servie par une adresse IP."""
    router = _Router({"http://127.0.0.1:8000/p": _resp(_page(), final_url="http://127.0.0.1:8000/p")})
    result = await _call("fetch_url", {"url": "http://127.0.0.1:8000/p"}, router)
    assert "Corps" in result.content[0].resource.text
    assert router.requested == ["http://127.0.0.1:8000/p"]


def _redirect(handler, location: str):
    headers = Message()
    headers["Location"] = location
    req = urllib.request.Request("https://s.test/f.ico")
    try:
        return handler.http_error_302(req, io.BytesIO(b""), 302, "Found", headers)
    except urllib.error.HTTPError as e:
        e.close()  # comme _guarded_fetch / _probe_bytes : l'erreur EST la réponse
        raise


@pytest.mark.parametrize("location", [
    "http://127.0.0.1/x", "http://[::1]/x", "http://2130706433/x", "http://localhost/x",
    "http://0x7f.1/x", "ftp://example.org/x", "file:///etc/hosts",
])
def test_probe_redirect_to_forbidden_target_is_refused(location):
    with pytest.raises(urllib.error.HTTPError):
        _redirect(netguard.ProbeRedirectHandler(), location)


def test_probe_redirect_to_an_ordinary_name_is_followed():
    new = netguard.ProbeRedirectHandler().redirect_request(
        urllib.request.Request("https://s.test/f.ico"), io.BytesIO(b""), 302, "Found", {},
        "https://cdn.example.org/f.ico",
    )
    assert new is not None and new.full_url == "https://cdn.example.org/f.ico"


def test_probe_opener_carries_the_probe_redirect_handler():
    opener = netguard.favicon_opener()
    assert any(isinstance(h, netguard.ProbeRedirectHandler) for h in opener.handlers)


def test_favicon_fetch_checks_the_url_itself_too():
    with patch("urllib.request.OpenerDirector.open") as op:
        assert pagemeta._fetch_favicon_bytes("http://127.0.0.1/f.ico", "ua") is None
    op.assert_not_called()


# ---------------------------------------------------------------------------
# Z7-6 — pas de redirection vers autre chose que http(s)
# ---------------------------------------------------------------------------

def test_fetch_opener_has_no_ftp_file_or_data_handler():
    names = {type(h).__name__ for h in netguard.web_opener().handlers}
    assert not names & {"FTPHandler", "FileHandler", "DataHandler", "CacheFTPHandler"}


@pytest.mark.parametrize("location", ["ftp://example.org/x", "file:///etc/hosts", "data:text/plain,x"])
def test_fetch_redirect_to_non_http_scheme_is_refused(location):
    # file: et data: sont déjà refusés par urllib lui-même ; ftp: l'était SUIVI.
    with pytest.raises(urllib.error.HTTPError) as exc:
        _redirect(netguard.SchemeRedirectHandler(), location)
    if location.startswith("ftp:"):
        assert "schéma" in exc.value.reason


def test_fetch_redirect_to_loopback_is_still_followed():
    """fetch_url n'est pas restreint côté adresse : seul le schéma est contrôlé."""
    new = netguard.SchemeRedirectHandler().redirect_request(
        urllib.request.Request("https://s.test/"), io.BytesIO(b""), 302, "Found", {},
        "http://127.0.0.1:8000/x",
    )
    assert new is not None


class _Redirecting(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(302)
        self.send_header("Location", "ftp://127.0.0.1:1/x")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


async def test_real_redirect_to_ftp_is_refused_without_an_ftp_attempt(monkeypatch):
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Redirecting)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        result = await _TM.call_tool("fetch_url", {"url": f"http://127.0.0.1:{srv.server_port}/"}, None)
    finally:
        srv.shutdown()
        srv.server_close()
    text = result.content[0].text
    assert "ftp error" not in text and "schéma" in text, text


# ---------------------------------------------------------------------------
# Z7-8 — échecs transitoires de favicon : TTL court
# ---------------------------------------------------------------------------

def test_cache_put_accepts_a_shorter_ttl(monkeypatch):
    c = pagemeta._FaviconCache(max_entries=4, ttl_s=3600)
    now = [1000.0]
    monkeypatch.setattr(pagemeta.time, "time", lambda: now[0])
    c.put("https://a", None, ttl_s=60)
    c.put("https://b", None)
    now[0] += 61
    assert c.get("https://a") == (False, None)
    assert c.get("https://b") == (True, None)


def test_probe_reports_transient_vs_definitive_failure():
    def probe(outcome):
        router = _Router({"https://s.test/favicon.ico": outcome})
        with patch("urllib.request.OpenerDirector.open", side_effect=router):
            return pagemeta.probe_favicon("https://s.test/", [], "ua")

    def http_error(code):
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b""))

    assert probe(urllib.error.URLError("timed out")) == (None, True)
    assert probe(TimeoutError()) == (None, True)
    assert probe(http_error(503)) == (None, True)
    assert probe(http_error(429)) == (None, True)
    assert probe(http_error(404)) == (None, False)
    assert probe(_resp(b"<html>nope</html>", "image/x-icon")) == (None, False)
    value, transient = probe(_resp(PNG, "image/png"))
    assert value and not transient


async def test_transient_failure_is_retried_after_the_short_ttl_but_not_a_404(monkeypatch):
    router = _Router({
        "https://down.test/": _resp(_page(), final_url="https://down.test/"),
        "https://down.test/favicon.ico": urllib.error.URLError("timed out"),
        "https://gone.test/": _resp(_page(), final_url="https://gone.test/"),
    })
    await _call("fetch_url", {"url": "https://down.test/"}, router)
    await _call("fetch_url", {"url": "https://gone.test/"}, router)
    real = time.time()
    monkeypatch.setattr(pagemeta.time, "time", lambda: real + pagemeta.FAVICON_FAILURE_TTL_S + 1)
    assert mcp_web.favicon_cache.get("https://down.test") == (False, None)
    assert mcp_web.favicon_cache.get("https://gone.test") == (True, None)


# ---------------------------------------------------------------------------
# Z7-9 — URL de résultats de recherche
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,ok", [
    ("https://example.org/a", True),
    ("http://example.org", True),
    ("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org", True),  # lien de redirection DDG, sans schéma
    ("javascript:alert(1)", False),
    (" JavaScript:alert(1)", False),
    ("java\tscript:alert(1)", False),
    ("data:text/html,<b>x</b>", False),
    ("file:///etc/passwd", False),
    ("ftp://example.org/x", False),
    ("", False),
    (None, False),
    (42, False),
    ({"a": 1}, False),
])
def test_result_url_rule(value, ok):
    assert search_common.is_result_url(value) is ok


def _server(order):
    return mcp_web.WebServer(
        {"search": {"order": order, "brave": {"api_key": "bk"}, "ollama": {"api_key": "ok"}}}
    )


async def _search(server, routes, tool="search", **args):
    def opener(req, *a, **kw):
        for fragment, body in routes.items():
            if fragment in req.full_url:
                m = MagicMock()
                m.__enter__ = lambda s: s
                m.__exit__ = MagicMock(return_value=False)
                m.read.return_value = body if isinstance(body, bytes) else json.dumps(body).encode()
                return m
        raise AssertionError(req.full_url)

    with patch("urllib.request.OpenerDirector.open", side_effect=opener):
        result = await server.mcp._tool_manager.call_tool(tool, {"query": "q", **args}, None)
    return json.loads(result.content[0].resource.text)


_HOSTILE = [
    {"title": "bon", "url": "https://ok.example/", "description": "d", "content": "d"},
    {"title": "js", "url": "javascript:alert(1)", "description": "d", "content": "d"},
    {"title": "data", "url": "data:text/html,x", "description": "d", "content": "d"},
    {"title": "int", "url": 7, "description": "d", "content": "d"},
    {"title": "obj", "url": {"a": 1}, "description": "d", "content": "d"},
]


async def test_brave_drops_non_http_result_urls():
    out = await _search(_server(["brave"]), {"res/v1/web/search": {"web": {"results": _HOSTILE}}})
    assert [r["url"] for r in out["results"]] == ["https://ok.example/"]


async def test_ollama_drops_non_http_result_urls():
    out = await _search(_server(["ollama"]), {"api/web_search": {"results": _HOSTILE}})
    assert [r["url"] for r in out["results"]] == ["https://ok.example/"]


async def test_ddg_drops_non_http_result_urls(monkeypatch):
    from mcp_web.search import ddg as search_ddg

    monkeypatch.setattr(search_ddg, "_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(search_ddg.ENGINE, "next_slot", 0.0)
    monkeypatch.setattr(search_ddg.ENGINE, "down_until", 0.0)
    html = (
        '<a class="result__a" href="https://ok.example/">bon</a>'
        '<a class="result__a" href="javascript:alert(1)">js</a>'
        '<a class="result__a" href="data:text/html,x">data</a>'
    ).encode()
    out = await _search(_server(["ddg"]), {"html.duckduckgo.com": html})
    assert [r["url"] for r in out["results"]] == ["https://ok.example/"]


async def test_brave_images_drop_non_http_urls():
    body = {"results": [
        {"title": "ok", "url": "https://p.example/", "properties": {"url": "https://i.example/a.jpg"},
         "thumbnail": {"src": "https://t.example/a.jpg"}, "source": "p"},
        {"title": "js image", "url": "https://p.example/", "properties": {"url": "javascript:alert(1)"}},
        {"title": "js page", "url": "javascript:alert(1)", "properties": {"url": "https://i.example/b.jpg"},
         "thumbnail": {"src": "data:image/png;base64,AAAA"}, "source": 5},
    ]}
    out = await _search(_server(["brave"]), {"res/v1/images/search": body}, tool="image_search")
    assert [r["image_url"] for r in out["results"]] == ["https://i.example/a.jpg", "https://i.example/b.jpg"]
    assert out["results"][1]["page_url"] == "" and out["results"][1]["thumbnail_url"] == ""
    assert out["results"][1]["source"] == ""


# ---------------------------------------------------------------------------
# P1 (verrou) et C-3 — canonical_url
# ---------------------------------------------------------------------------

async def test_canonical_url_is_the_final_url_never_the_pages_own_claim():
    """P1, verrou : une page qui déclare <link rel=canonical> et og:url ailleurs
    n'impose rien — `canonical_url` reste l'URL réellement lue."""
    head = (
        '<title>T</title>'
        '<link rel="canonical" href="https://evil.example/x">'
        '<meta property="og:url" content="https://evil.example/y">'
        '<meta property="og:site_name" content="Trusted">'
    )
    router = _Router({"https://s.test/start": _resp(_page(head), final_url="https://s.test/final")})
    result = await _call("fetch_url", {"url": "https://s.test/start"}, router)
    assert result.meta[META_KEY]["canonical_url"] == "https://s.test/final"
    assert "evil.example" not in json.dumps(result.meta)


@pytest.mark.parametrize("url", [
    "https://s.test/a b",
    "https://s.test/a\tb",
    "https://s.test/a\nb",
    "https://s.test/a\x01b",
    "https://s.test/a]b",
    "https://s.test/a b",
    "https://s.test/a b",
    "https://s.test/a﻿b",
    "https://s.test/" + "a" * 2040,
    "https://",
])
def test_canonical_url_the_client_would_drop_is_omitted(url):
    assert build_web_meta(canonical_url=url, head={"title": "T"}) == {"title": "T"}


def test_canonical_url_at_the_limit_is_kept():
    url = "https://s.test/" + "a" * (2048 - len("https://s.test/"))
    assert len(url) == 2048
    assert build_web_meta(canonical_url=url) == {"canonical_url": url}


def test_canonical_url_length_is_counted_in_utf16_units_like_the_client():
    astral = "\U0001F600"  # 1 point de code, 2 unités UTF-16 côté JS
    url = "https://s.test/" + astral * 1020  # 1035 points, 2055 unités
    assert len(url) < 2048 < len(url.encode("utf-16-le")) // 2
    assert "canonical_url" not in build_web_meta(canonical_url=url)


async def test_fetch_url_omits_unusable_final_url_but_keeps_the_rest():
    router = _Router({
        "https://s.test/p": _resp(_page("<title>T</title>"), final_url="https://s.test/a]b"),
    })
    result = await _call("fetch_url", {"url": "https://s.test/p"}, router)
    assert result.meta[META_KEY]["title"] == "T"
    assert "canonical_url" not in result.meta[META_KEY]
    assert "Corps" in result.content[0].resource.text


# ---------------------------------------------------------------------------
# A12-b / Z7-7 b — fetch_read(<URL finale>) retrouve le cache
# ---------------------------------------------------------------------------

async def test_fetch_read_and_list_find_the_cache_under_the_final_url():
    router = _Router({
        "https://s.test/start": _resp(
            _page("<title>T</title>", "<h1>Titre</h1><p>Contenu final</p>"),
            final_url="https://s.test/final",
        ),
    })
    await _call("fetch_url", {"url": "https://s.test/start"}, router)
    read = await _TM.call_tool("fetch_read", {"url": "https://s.test/final"}, None)
    assert "Contenu final" in read
    listing = await _TM.call_tool("fetch_list", {"url": "https://s.test/final"}, None)
    assert "Titre" in listing
    # Et l'URL de départ continue de marcher.
    assert "Contenu final" in await _TM.call_tool("fetch_read", {"url": "https://s.test/start"}, None)


async def test_final_url_alias_works_for_textual_content_too():
    router = _Router({"https://s.test/a": _resp(b"du texte", "text/plain", final_url="https://s.test/b")})
    await _call("fetch_url", {"url": "https://s.test/a"}, router)
    assert "du texte" in await _TM.call_tool("fetch_read", {"url": "https://s.test/b"}, None)


async def test_own_entry_wins_over_an_alias():
    router = _Router({
        "https://s.test/a": _resp(b"via a", "text/plain", final_url="https://s.test/b"),
        "https://s.test/b": _resp(b"direct b", "text/plain", final_url="https://s.test/b"),
    })
    await _call("fetch_url", {"url": "https://s.test/a"}, router)
    await _call("fetch_url", {"url": "https://s.test/b"}, router)
    assert "direct b" in await _TM.call_tool("fetch_read", {"url": "https://s.test/b"}, None)


async def test_alias_expires_with_its_ttl_and_unknown_urls_still_miss(tmp_path):
    router = _Router({"https://s.test/a": _resp(b"du texte", "text/plain", final_url="https://s.test/b")})
    await _call("fetch_url", {"url": "https://s.test/a"}, router)
    old = time.time() - 48 * 3600
    for p in tmp_path.iterdir():
        os.utime(p, (old, old))
    assert "Aucun contenu en cache" in await _TM.call_tool("fetch_read", {"url": "https://s.test/b"}, None)
    assert not list(tmp_path.iterdir())
    assert "Aucun contenu en cache" in await _TM.call_tool("fetch_read", {"url": "https://s.test/zzz"}, None)


async def test_binary_refetch_leaves_nothing_readable_under_the_alias():
    router = _Router({"https://s.test/a": _resp(b"du texte", "text/plain", final_url="https://s.test/b")})
    await _call("fetch_url", {"url": "https://s.test/a"}, router)
    router = _Router({"https://s.test/a": _resp(PNG, "image/png", final_url="https://s.test/b")})
    await _call("fetch_url", {"url": "https://s.test/a"}, router)
    assert "Aucun contenu en cache" in await _TM.call_tool("fetch_read", {"url": "https://s.test/b"}, None)
