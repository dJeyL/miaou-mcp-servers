"""MIAOU servi par le proxy : clé `miaou_dist`, routes `/app/` (lot AO-1).

Trois niveaux :

- les purs de webapp.py (résolution de la clé, garde de sécurité, ce qui manque
  dans le dossier) ;
- l'app de `build_app` sur `httpx2.ASGITransport` (le wrapper ASGI compris) :
  routes, priorité de `/mcp` et OAuth, auth active, types, 304, traversée ;
- un VRAI uvicorn sur un port éphémère, interrogé par `http.client` : les
  en-têtes réellement émis sur le fil, et la revalidation après un `git pull`
  simulé. Le transport ASGI en mémoire ne prouve pas ce qui part sur la socket.
"""
import http.client
import json
import os
import sys
import threading
import time
from pathlib import Path

import httpx2
import pytest

_ROOT = Path(__file__).parent.parent
_SERVERS = _ROOT / "servers"
for p in (_ROOT, _SERVERS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import mcp_proxy
from mcp_proxy import build_app, build_proxy_server, resolve_auth_config
from mcp_proxy.webapp import (
    MANIFEST,
    MIAOU_HTML,
    media_type_for,
    miaou_dist_warnings,
    resolve_miaou_dist,
)

_HTML = "<!doctype html><title>MIAOU</title>"
_ISSUER = "http://127.0.0.1:8787"


def _dist(tmp_path: Path, *, manifest: dict | None = None, icons: tuple[str, ...] = ()) -> Path:
    """Un dossier dist/ minimal : miaou.html, et au besoin manifeste + icônes."""
    dist = tmp_path / "miaou" / "dist"
    dist.mkdir(parents=True)
    (dist / MIAOU_HTML).write_text(_HTML, encoding="utf-8")
    if manifest is not None:
        (dist / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    for icon in icons:
        target = dist / icon
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG\r\n\x1a\n")
    return dist


def _app(miaou_dist: Path | None, auth=None):
    upstreams: dict = {}
    server = build_proxy_server(upstreams, {})
    return build_app(server, upstreams, auth=auth, miaou_dist=miaou_dist)


def _client(app):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver")


# ---------------------------------------------------------------------------
# resolve_miaou_dist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cfg", [{}, {"miaou_dist": None}, {"miaou_dist": ""}])
def test_absent_key_serves_nothing(tmp_path, cfg):
    assert resolve_miaou_dist(cfg, tmp_path / "config.json") is None


@pytest.mark.parametrize("value", [42, ["dist"], {"path": "dist"}, True])
def test_non_string_key_is_refused(tmp_path, value):
    with pytest.raises(ValueError, match="miaou_dist"):
        resolve_miaou_dist({"miaou_dist": value}, tmp_path / "config.json")


def test_relative_path_resolves_against_the_config_file_not_the_cwd(tmp_path, monkeypatch):
    conf_dir = tmp_path / "proxy"
    conf_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    resolved = resolve_miaou_dist({"miaou_dist": "../miaou/dist"}, conf_dir / "config.json")
    assert resolved == (tmp_path / "miaou" / "dist").resolve()


def test_relative_config_path_is_anchored_on_the_cwd_once(tmp_path, monkeypatch):
    """`--config proxy/config.json` lancé depuis tmp_path : la base est le
    dossier du fichier, résolu depuis le cwd du lancement."""
    (tmp_path / "proxy").mkdir()
    monkeypatch.chdir(tmp_path)
    resolved = resolve_miaou_dist({"miaou_dist": "miaou_dist"}, Path("proxy/config.json"))
    assert resolved == (tmp_path / "proxy" / "miaou_dist").resolve()


def test_absolute_path_and_home_are_kept(tmp_path, monkeypatch):
    absolute = tmp_path / "abs"
    assert resolve_miaou_dist({"miaou_dist": str(absolute)}, "/x/config.json") == absolute.resolve()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert resolve_miaou_dist({"miaou_dist": "~/d"}, "/x/config.json") == (tmp_path / "d").resolve()


def test_missing_directory_is_not_an_error(tmp_path):
    """Introuvable = avertissement au démarrage, pas un refus (cf. warnings)."""
    resolved = resolve_miaou_dist({"miaou_dist": "nope"}, tmp_path / "config.json")
    assert resolved == (tmp_path / "nope").resolve()


@pytest.mark.parametrize("entry", ["config.json", ".git"])
def test_directory_holding_a_config_or_a_repo_is_refused(tmp_path, entry):
    """La racine du dépôt MIAOU au lieu de son dist/ : la servir exposerait sa
    config au réseau. Refus de démarrer, l'erreur nomme ce qu'elle a trouvé."""
    root = tmp_path / "miaou"
    root.mkdir()
    (root / MIAOU_HTML).write_text(_HTML)
    if entry == ".git":
        (root / entry).mkdir()
    else:
        (root / entry).write_text("{}")
    with pytest.raises(ValueError, match=entry.replace(".", r"\.")):
        resolve_miaou_dist({"miaou_dist": "miaou"}, tmp_path / "config.json")


# ---------------------------------------------------------------------------
# miaou_dist_warnings
# ---------------------------------------------------------------------------

def test_complete_directory_has_no_warning(tmp_path):
    dist = _dist(
        tmp_path,
        manifest={"icons": [{"src": "icon-192.png"}, {"src": "icons/icon-512.png"}]},
        icons=("icon-192.png", "icons/icon-512.png"),
    )
    assert miaou_dist_warnings(dist) == []


def test_missing_directory_is_warned(tmp_path):
    [warning] = miaou_dist_warnings(tmp_path / "nope")
    assert "introuvable" in warning and "nope" in warning


def test_missing_html_and_manifest_are_both_warned(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    warnings = miaou_dist_warnings(dist)
    assert any(MIAOU_HTML in w and "404" in w for w in warnings)
    assert any(MANIFEST in w and "installé" in w for w in warnings)


def test_html_without_manifest_warns_only_about_installation(tmp_path):
    """L'état d'avant AO-2 : MIAOU servi, pas installable."""
    [warning] = miaou_dist_warnings(_dist(tmp_path))
    assert MANIFEST in warning and "installé" in warning


def test_unreadable_manifest_is_warned(tmp_path):
    dist = _dist(tmp_path)
    (dist / MANIFEST).write_text("{pas du json")
    [warning] = miaou_dist_warnings(dist)
    assert "illisible" in warning


@pytest.mark.parametrize("manifest", [{}, {"icons": []}, {"icons": "x.png"}, []])
def test_manifest_without_icons_is_warned(tmp_path, manifest):
    [warning] = miaou_dist_warnings(_dist(tmp_path, manifest=manifest))
    assert "aucune icône" in warning


def test_icon_names_come_from_the_manifest(tmp_path):
    """Les noms d'icônes ne sont pas figés dans le proxy : seule celle citée et
    absente est signalée, sous la forme où le manifeste l'écrit."""
    dist = _dist(
        tmp_path,
        manifest={"icons": [{"src": "a.png"}, {"src": "./b.png"}, {"src": "/app/c.png"}]},
        icons=("a.png", "c.png"),
    )
    assert miaou_dist_warnings(dist) == [f"icône introuvable : ./b.png (citée par {MANIFEST})."]


def test_icon_outside_app_cannot_be_served(tmp_path):
    dist = _dist(tmp_path, manifest={"icons": [{"src": "/icon.png"}]}, icons=("icon.png",))
    [warning] = miaou_dist_warnings(dist)
    assert "/icon.png" in warning


def test_icon_on_another_origin_is_not_checked(tmp_path):
    dist = _dist(tmp_path, manifest={"icons": [{"src": "https://cdn.example/i.png"}]})
    assert miaou_dist_warnings(dist) == []


def test_icon_without_src_is_warned(tmp_path):
    [warning] = miaou_dist_warnings(_dist(tmp_path, manifest={"icons": [{"sizes": "192x192"}]}))
    assert "sans 'src'" in warning


# ---------------------------------------------------------------------------
# Types de contenu
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("miaou.html", "text/html"),
        ("sw.js", "text/javascript"),
        ("version.json", "application/json"),
        ("manifest.webmanifest", "application/manifest+json"),
        ("icon.PNG", "image/png"),
        ("cat.svg", "image/svg+xml"),
        ("favicon.ico", "image/x-icon"),
        ("notes.txt", None),
    ],
)
def test_media_type_table(name, expected):
    assert media_type_for(name) == expected


# ---------------------------------------------------------------------------
# Routes, à travers build_app (wrapper ASGI compris)
# ---------------------------------------------------------------------------

async def test_without_key_nothing_new_is_served(tmp_path):
    async with _client(_app(None)) as c:
        assert (await c.get("/")).status_code == 404
        assert (await c.get("/app/")).status_code == 404


async def test_root_redirects_to_app_with_a_302(tmp_path):
    async with _client(_app(_dist(tmp_path))) as c:
        r = await c.get("/")
    # 302 et non 301 : un 301 survivrait, dans le cache du navigateur, au
    # retrait de la clé.
    assert r.status_code == 302
    assert r.headers["location"] == "/app/"


async def test_app_without_slash_redirects_to_app(tmp_path):
    async with _client(_app(_dist(tmp_path))) as c:
        r = await c.get("/app")
    assert r.status_code in (307, 308)
    assert r.headers["location"].endswith("/app/")


async def test_app_serves_miaou_html_without_cache_freshness(tmp_path):
    async with _client(_app(_dist(tmp_path))) as c:
        r = await c.get("/app/")
    assert r.status_code == 200
    assert r.text == _HTML
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["etag"]
    assert r.headers["last-modified"]


async def test_other_files_are_served_with_the_table_type_whatever_mimetypes_says(
    tmp_path, monkeypatch
):
    """Le cas Windows : le registre fait répondre `text/plain` à `mimetypes`
    pour `.js`. Simulé en remplaçant `guess_type` : la table doit l'emporter,
    sans quoi le navigateur refuserait d'enregistrer le service worker. Une
    extension hors table retombe sur le module."""
    dist = _dist(tmp_path, manifest={"icons": []})
    (dist / "sw.js").write_text("self.addEventListener('fetch', () => {});")
    (dist / "notes.txt").write_text("x")
    # `starlette.responses` importe `guess_type` par son nom : c'est cette
    # référence-là qu'il faut remplacer, `mimetypes.guess_type` n'y change rien.
    import starlette.responses

    monkeypatch.setattr(
        starlette.responses, "guess_type", lambda *a, **kw: ("text/plain", None)
    )
    async with _client(_app(dist)) as c:
        sw = await c.get("/app/sw.js")
        manifest = await c.get(f"/app/{MANIFEST}")
        txt = await c.get("/app/notes.txt")
    assert sw.headers["content-type"] == "text/javascript; charset=utf-8"
    assert manifest.headers["content-type"] == "application/manifest+json"
    assert txt.headers["content-type"].startswith("text/plain")  # repli sur le module
    assert sw.headers["cache-control"] == manifest.headers["cache-control"] == "no-cache"


async def test_matching_etag_gets_a_304_that_keeps_no_cache(tmp_path):
    async with _client(_app(_dist(tmp_path))) as c:
        etag = (await c.get("/app/")).headers["etag"]
        r = await c.get("/app/", headers={"If-None-Match": etag})
    assert r.status_code == 304
    assert r.headers["cache-control"] == "no-cache"


async def test_nothing_outside_the_directory_is_reachable(tmp_path):
    dist = _dist(tmp_path)
    (dist.parent / "config.json").write_text('{"secret": 1}')
    async with _client(_app(dist)) as c:
        for path in ("/app/../config.json", "/app/..%2Fconfig.json", "/app/%2e%2e/config.json"):
            r = await c.get(path)
            assert r.status_code == 404, path
            assert "secret" not in r.text


async def test_hidden_files_are_not_served(tmp_path):
    dist = _dist(tmp_path)
    (dist / ".gitkeep").write_text("")
    (dist / "sub").mkdir()
    (dist / "sub" / ".DS_Store").write_text("x")
    async with _client(_app(dist)) as c:
        assert (await c.get("/app/.gitkeep")).status_code == 404
        assert (await c.get("/app/sub/.DS_Store")).status_code == 404


async def test_missing_directory_is_a_404_then_served_once_it_appears(tmp_path):
    """Dossier absent au démarrage : 404, jamais 500 (le `check_config` de
    Starlette lèverait à la première requête). Rempli ensuite, il est servi
    sans redémarrage."""
    dist = tmp_path / "miaou" / "dist"
    app = _app(dist)
    async with _client(app) as c:
        assert (await c.get("/app/")).status_code == 404
        _dist(tmp_path)
        r = await c.get("/app/")
    assert r.status_code == 200 and r.text == _HTML


async def test_app_is_read_only(tmp_path):
    async with _client(_app(_dist(tmp_path))) as c:
        assert (await c.post("/app/", content=b"x")).status_code == 405


async def test_app_stays_public_when_inbound_auth_is_active(tmp_path):
    """L'appli doit charger pour lancer son propre parcours OAuth : seul
    l'endpoint MCP exige un jeton. Les routes OAuth gardent la priorité."""
    auth = resolve_auth_config({"auth": {"issuer_url": _ISSUER}})
    async with _client(_app(_dist(tmp_path), auth=auth)) as c:
        mcp = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        well_known = await c.get("/.well-known/oauth-protected-resource/mcp")
        root = await c.get("/")
        app_page = await c.get("/app/")
    assert mcp.status_code == 401
    assert well_known.status_code == 200
    assert root.status_code == 302
    assert app_page.status_code == 200 and app_page.text == _HTML


# ---------------------------------------------------------------------------
# main() : refus et avertissements au démarrage
# ---------------------------------------------------------------------------

def _run_main(tmp_path, monkeypatch, cfg: dict) -> dict:
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"port": 8765, "mcpServers": {}, **cfg}))
    monkeypatch.setattr(sys, "argv", ["mcp_proxy", "--config", str(conf)])
    monkeypatch.setattr(mcp_proxy.entry, "enable_system_trust_store", lambda: None)
    seen: dict = {}
    real_build_app = mcp_proxy.entry.build_app

    def spy(*a, **kw):
        seen["miaou_dist"] = kw.get("miaou_dist")
        return real_build_app(*a, **kw)

    monkeypatch.setattr(mcp_proxy.entry, "build_app", spy)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    mcp_proxy.main()
    return seen


def test_main_passes_the_resolved_directory_and_warns(tmp_path, monkeypatch, capsys):
    seen = _run_main(tmp_path, monkeypatch, {"miaou_dist": "miaou/dist"})
    out = capsys.readouterr()
    assert seen["miaou_dist"] == (tmp_path / "miaou" / "dist").resolve()
    assert "introuvable" in out.err
    assert "http://127.0.0.1:8765/app/" in out.out


def test_main_without_key_passes_nothing_and_says_nothing(tmp_path, monkeypatch, capsys):
    seen = _run_main(tmp_path, monkeypatch, {})
    out = capsys.readouterr()
    assert seen["miaou_dist"] is None
    assert "/app/" not in out.out + out.err


def test_main_refuses_a_directory_holding_a_config(tmp_path, monkeypatch, capsys):
    (tmp_path / "miaou").mkdir()
    (tmp_path / "miaou" / "config.json").write_text("{}")
    with pytest.raises(SystemExit) as exc:
        _run_main(tmp_path, monkeypatch, {"miaou_dist": "miaou"})
    assert exc.value.code == 1
    assert "config.json" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Vrai transport HTTP : uvicorn sur un port éphémère
# ---------------------------------------------------------------------------

@pytest.fixture
def live_proxy(tmp_path):
    """Le proxy servi par un vrai uvicorn (lifespan compris), dans un thread."""
    import uvicorn

    dist = _dist(tmp_path, manifest={"icons": [{"src": "icon.png"}]}, icons=("icon.png",))
    server = uvicorn.Server(
        uvicorn.Config(_app(dist), host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn n'a pas démarré"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield port, dist
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _get(port: int, path: str, method: str = "GET", headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


def test_wire_headers_of_the_page(live_proxy):
    port, _ = live_proxy
    status, headers, body = _get(port, "/app/")
    assert status == 200
    assert body.decode() == _HTML
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert headers["cache-control"] == "no-cache"
    assert headers["content-length"] == str(len(_HTML.encode()))
    assert headers["etag"] and headers["last-modified"]


def test_wire_redirect_and_manifest_and_head(live_proxy):
    port, _ = live_proxy
    status, headers, _ = _get(port, "/")
    assert status == 302 and headers["location"] == "/app/"
    status, headers, _ = _get(port, f"/app/{MANIFEST}")
    assert status == 200 and headers["content-type"] == "application/manifest+json"
    status, headers, body = _get(port, "/app/icon.png", method="HEAD")
    assert status == 200 and headers["content-type"] == "image/png" and body == b""


def test_wire_revalidation_sees_a_git_pull(live_proxy):
    """Ce que le navigateur fait au rechargement, `no-cache` aidant : il
    revalide. Inchangé → 304 ; fichier remplacé (git pull de MIAOU) → 200 et
    le nouveau contenu, même avec l'ancien validateur."""
    port, dist = live_proxy
    _, headers, _ = _get(port, "/app/")
    etag, last_modified = headers["etag"], headers["last-modified"]

    status, headers, body = _get(port, "/app/", headers={"If-None-Match": etag})
    assert status == 304 and body == b"" and headers["cache-control"] == "no-cache"

    page = dist / MIAOU_HTML
    page.write_text(_HTML + "<!-- v2 -->", encoding="utf-8")
    later = time.time() + 5
    os.utime(page, (later, later))

    status, headers, body = _get(
        port, "/app/", headers={"If-None-Match": etag, "If-Modified-Since": last_modified}
    )
    assert status == 200
    assert body.decode().endswith("<!-- v2 -->")
    assert headers["etag"] != etag
