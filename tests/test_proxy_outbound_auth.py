"""Tests de l'auth OAuth SORTANTE du proxy (lot AB-2).

Symétrique de `test_proxy_auth.py`, et sans rapport avec lui : là, le proxy
vérifie les jetons de ses clients ; ici, il en obtient auprès de serveurs tiers
et les détient à la place de MIAOU.

Aucun test ne touche le réseau : on injecte plutôt qu'on ne stube HTTP
(patron du dépôt, cf. `_verifier()` dans `test_proxy_auth.py`).
"""
import json
import os
import stat
import sys
import time
from pathlib import Path

from unittest.mock import AsyncMock, patch

import pytest

_ROOT = Path(__file__).parent.parent
_SERVERS = _ROOT / "servers"
for p in (_ROOT, _SERVERS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import mcp_proxy
from mcp_proxy import (
    UpstreamTokenStorage,
    build_client_info_override,
    _default_tokens_path,
    _write_secret_file,
)


def _token(**kw):
    from mcp.shared.auth import OAuthToken

    base = {"access_token": "at-1", "token_type": "Bearer"}
    base.update(kw)
    return OAuthToken(**base)


# ---------------------------------------------------------------------------
# Emplacement et écriture du fichier
# ---------------------------------------------------------------------------

def test_tokens_path_is_beside_config_not_inside_it():
    """config.json est ouvert et édité à la main : un refresh token n'y a rien
    à faire. Fichier distinct, nom dérivé pour rester trouvable."""
    path = _default_tokens_path("/etc/miaou/config.json")
    assert path == Path("/etc/miaou/config-tokens.json")
    assert path != Path("/etc/miaou/config.json")


def test_secret_file_is_created_with_restricted_mode(tmp_path):
    """0600 posé À LA CRÉATION, pas par un chmod après coup : entre le write
    et le chmod, le refresh token serait lisible par tout le monde."""
    target = tmp_path / "t.json"
    _write_secret_file(target, {"a": 1})

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600
    assert json.loads(target.read_text()) == {"a": 1}


def test_secret_file_write_is_atomic(tmp_path):
    """Une écriture interrompue ne doit pas laisser un fichier tronqué : ça
    coûterait une ré-autorisation manuelle de tous les upstreams."""
    target = tmp_path / "t.json"
    _write_secret_file(target, {"first": True})

    class _Boom(Exception):
        pass

    def _explode(*a, **kw):
        raise _Boom()

    original = json.dump
    json.dump = _explode
    try:
        with pytest.raises(_Boom):
            _write_secret_file(target, {"second": True})
    finally:
        json.dump = original

    # L'ancien contenu a survécu, et aucun fichier temporaire ne traîne.
    assert json.loads(target.read_text()) == {"first": True}
    assert [p.name for p in tmp_path.iterdir()] == ["t.json"]


def test_secret_file_creates_parent_directory(tmp_path):
    target = tmp_path / "sub" / "dir" / "t.json"
    _write_secret_file(target, {"a": 1})
    assert target.exists()


# ---------------------------------------------------------------------------
# Round-trip du protocol TokenStorage
# ---------------------------------------------------------------------------

async def test_tokens_round_trip(tmp_path):
    store = UpstreamTokenStorage(tmp_path / "t.json", "remote")
    assert await store.get_tokens() is None

    await store.set_tokens(_token(refresh_token="rt-1", scope="mcp:read"))
    got = await store.get_tokens()
    assert got.access_token == "at-1"
    assert got.refresh_token == "rt-1"
    assert got.scope == "mcp:read"


async def test_client_info_round_trip(tmp_path):
    from mcp.shared.auth import OAuthClientInformationFull

    store = UpstreamTokenStorage(tmp_path / "t.json", "remote")
    assert await store.get_client_info() is None

    info = OAuthClientInformationFull(
        client_id="c-1", client_secret="s-1", redirect_uris=["http://127.0.0.1:8799/callback"]
    )
    await store.set_client_info(info)
    got = await store.get_client_info()
    assert got.client_id == "c-1"
    assert got.client_secret == "s-1"


async def test_upstreams_do_not_overwrite_each_other(tmp_path):
    """Un seul fichier, une entrée par upstream : écrire pour l'un ne doit pas
    effacer les jetons de l'autre."""
    path = tmp_path / "t.json"
    a = UpstreamTokenStorage(path, "alpha")
    b = UpstreamTokenStorage(path, "beta")

    await a.set_tokens(_token(access_token="at-alpha"))
    await b.set_tokens(_token(access_token="at-beta"))

    assert (await a.get_tokens()).access_token == "at-alpha"
    assert (await b.get_tokens()).access_token == "at-beta"


async def test_set_tokens_preserves_client_info(tmp_path):
    """Les deux moitiés d'une entrée sont écrites séparément : rafraîchir un
    jeton ne doit pas effacer l'enregistrement du client."""
    from mcp.shared.auth import OAuthClientInformationFull

    store = UpstreamTokenStorage(tmp_path / "t.json", "remote")
    await store.set_client_info(
        OAuthClientInformationFull(client_id="c-1", redirect_uris=["http://x/cb"])
    )
    await store.set_tokens(_token())

    assert (await store.get_client_info()).client_id == "c-1"
    assert (await store.get_tokens()).access_token == "at-1"


# ---------------------------------------------------------------------------
# Expiration : une durée relative ne survit pas à un redémarrage
# ---------------------------------------------------------------------------

async def test_expires_in_is_recomputed_from_absolute_instant(tmp_path):
    """`expires_in` est une DURÉE. La relire telle quelle après un redémarrage
    la ferait courir à nouveau depuis maintenant — un jeton mort passerait pour
    frais. On persiste l'instant absolu et on recalcule le reste."""
    path = tmp_path / "t.json"
    store = UpstreamTokenStorage(path, "remote")
    await store.set_tokens(_token(expires_in=3600))

    # On rembobine l'instant d'expiration de 30 minutes, comme si le proxy
    # avait redémarré une demi-heure plus tard.
    data = json.loads(path.read_text())
    data["remote"]["expires_at"] -= 1800
    path.write_text(json.dumps(data))

    got = await UpstreamTokenStorage(path, "remote").get_tokens()
    assert 1700 <= got.expires_in <= 1800


async def test_expired_token_reloads_as_zero_not_fresh(tmp_path):
    """Le cas qui compte vraiment : le SDK charge les jetons dans _initialize()
    SANS repasser par update_token_expiry(), donc token_expiry_time reste None
    et is_token_valid() rendrait True pour un jeton expiré depuis des heures.
    Rendre 0 est ce qui fait basculer le SDK vers le refresh."""
    path = tmp_path / "t.json"
    store = UpstreamTokenStorage(path, "remote")
    await store.set_tokens(_token(expires_in=60, refresh_token="rt-1"))

    data = json.loads(path.read_text())
    data["remote"]["expires_at"] = time.time() - 7200
    path.write_text(json.dumps(data))

    got = await UpstreamTokenStorage(path, "remote").get_tokens()
    assert got.expires_in == 0


async def test_token_without_expiry_stays_without_expiry(tmp_path):
    path = tmp_path / "t.json"
    store = UpstreamTokenStorage(path, "remote")
    await store.set_tokens(_token())

    got = await UpstreamTokenStorage(path, "remote").get_tokens()
    assert got.expires_in is None


# ---------------------------------------------------------------------------
# Robustesse : un fichier abîmé ne doit pas empêcher le proxy de démarrer
# ---------------------------------------------------------------------------

async def test_unreadable_file_yields_no_tokens_not_a_crash(tmp_path, capsys):
    path = tmp_path / "t.json"
    path.write_text("{ pas du json")

    store = UpstreamTokenStorage(path, "remote")
    assert await store.get_tokens() is None
    assert await store.get_client_info() is None
    assert "illisible" in capsys.readouterr().err


async def test_corrupt_entry_yields_no_tokens(tmp_path, capsys):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"remote": {"tokens": {"nope": 1}}}))

    assert await UpstreamTokenStorage(path, "remote").get_tokens() is None
    assert "illisible" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Credentials pré-provisionnés (AS sans enregistrement dynamique)
# ---------------------------------------------------------------------------

def test_no_client_id_means_no_override():
    assert build_client_info_override(None) is None
    assert build_client_info_override({}) is None
    assert build_client_info_override({"scope": "mcp:read"}) is None


def test_client_info_override_built_from_config():
    info = build_client_info_override(
        {
            "client_id": "c-1",
            "client_secret": "s-1",
            "redirect_uri": "http://127.0.0.1:8799/callback",
            "scope": "mcp:read",
        }
    )
    assert info.client_id == "c-1"
    assert info.client_secret == "s-1"
    assert info.scope == "mcp:read"
    # Un client à secret s'authentifie ; un client public ne peut pas.
    assert info.token_endpoint_auth_method == "client_secret_post"


def test_public_client_override_has_no_auth_method():
    info = build_client_info_override({"client_id": "c-1"})
    assert info.token_endpoint_auth_method == "none"


async def test_override_short_circuits_dynamic_registration(tmp_path):
    """C'est le point d'entrée qui évite d'ajouter une branche au SDK : celui-ci
    ne fait de DCR que `if not self.context.client_info:`, alimenté par
    get_client_info(). Rendre l'override suffit donc à court-circuiter la DCR."""
    override = build_client_info_override({"client_id": "from-config"})
    store = UpstreamTokenStorage(tmp_path / "t.json", "remote", client_info_override=override)

    assert (await store.get_client_info()).client_id == "from-config"


async def test_config_override_wins_over_stored_registration(tmp_path, capsys):
    """La config est l'intention explicite de l'utilisateur : elle gagne sur un
    enregistrement mémorisé. L'enregistrement reste écrit, pour qu'on y retombe
    si l'override disparaît de la config."""
    from mcp.shared.auth import OAuthClientInformationFull

    path = tmp_path / "t.json"
    override = build_client_info_override({"client_id": "from-config"})
    store = UpstreamTokenStorage(path, "remote", client_info_override=override)

    await store.set_client_info(
        OAuthClientInformationFull(client_id="from-dcr", redirect_uris=["http://x/cb"])
    )
    assert (await store.get_client_info()).client_id == "from-config"

    # Sans override, on retombe sur l'enregistrement mémorisé.
    plain = UpstreamTokenStorage(path, "remote")
    assert (await plain.get_client_info()).client_id == "from-dcr"


def test_storage_satisfies_sdk_token_storage_protocol():
    """Le SDK type le stockage par un Protocol : si une méthode manque ou change
    de nom, le parcours OAuth casse au runtime, pas à l'import.

    `TokenStorage` n'est pas @runtime_checkable, donc pas d'isinstance : on
    compare la surface réellement exigée, ce qui a le mérite de casser aussi si
    le SDK ajoute une méthode au protocol.
    """
    import inspect

    from mcp.client.auth import TokenStorage

    expected = {
        name
        for name, member in inspect.getmembers(TokenStorage, inspect.isfunction)
        if not name.startswith("_")
    }
    assert expected  # le protocol n'est pas vide, sinon ce test ne prouve rien

    store = UpstreamTokenStorage("/tmp/x.json", "remote")
    for name in expected:
        assert inspect.iscoroutinefunction(getattr(store, name)), name


# ---------------------------------------------------------------------------
# Parcours OAuth : lien copiable, /callback, unicité du provider (AB-2.3)
# ---------------------------------------------------------------------------

def _authorizer(tmp_path, name="remote", interactive=True, **kw):
    """`interactive=True` par défaut ICI seulement : ces tests exercent le
    parcours. En vrai le drapeau est faux au démarrage (cf.
    test_boot_refuses_instead_of_waiting_for_a_click)."""
    storage = UpstreamTokenStorage(tmp_path / "t.json", name)
    authorizer = mcp_proxy.UpstreamAuthorizer(
        name=name,
        server_url="https://example.test/mcp",
        storage=storage,
        callback_url="http://127.0.0.1:8799/callback",
        **kw,
    )
    authorizer.interactive = interactive
    return authorizer


def test_callback_url_is_a_fixed_loopback_port():
    """RFC 8252 §7.3 demande à l'AS d'ignorer le port d'un redirect loopback,
    mais certains le comparent strictement : port fixe, donc, pas éphémère."""
    assert mcp_proxy.build_callback_url("127.0.0.1", 8799) == "http://127.0.0.1:8799/callback"


def test_callback_url_falls_back_when_listening_on_all_interfaces():
    """0.0.0.0 n'est l'adresse de personne : inutilisable comme redirect URI."""
    assert mcp_proxy.build_callback_url("0.0.0.0", 8799) == "http://127.0.0.1:8799/callback"


def test_provider_is_built_once_and_reused(tmp_path):
    """LE test qui protège l'écrivain unique du refresh.

    OAuthContext porte un anyio.Lock pris pour tout async_auth_flow : c'est lui
    qui sérialise les refresh. Un provider par requête rendrait ce verrou
    inopérant, et deux refresh concurrents feraient voir un rejeu à un AS à
    rotation, qui révoquerait toute la famille de jetons.
    """
    authorizer = _authorizer(tmp_path)
    assert authorizer.provider() is authorizer.provider()


def test_provider_advertises_the_callback_as_redirect_uri(tmp_path):
    provider = _authorizer(tmp_path).provider()
    uris = [str(u) for u in provider.context.client_metadata.redirect_uris]
    assert uris == ["http://127.0.0.1:8799/callback"]


def test_provider_requests_refresh_token_grant(tmp_path):
    """Sans le grant refresh_token, chaque expiration relancerait un parcours
    interactif — inutilisable pour un proxy qui tourne sans surveillance."""
    metadata = _authorizer(tmp_path).provider().context.client_metadata
    assert "refresh_token" in metadata.grant_types
    assert "authorization_code" in metadata.grant_types


async def test_redirect_handler_prints_a_copyable_link(tmp_path, capsys):
    """Le lien copiable est le mécanisme de référence, pas un repli : mcp_proxy
    est une CLI, et l'OS ne garantit ni le bon navigateur ni le bon profil."""
    authorizer = _authorizer(tmp_path)
    url = "https://as.test/authorize?state=abc123&client_id=c1"
    await authorizer._on_redirect(url)

    err = capsys.readouterr().err
    assert url in err
    assert "remote" in err
    assert authorizer.pending is not None
    assert authorizer.pending.state == "abc123"


async def test_redirect_handler_does_not_open_browser_by_default(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))

    await _authorizer(tmp_path)._on_redirect("https://as.test/authorize?state=s")
    assert opened == []

    await _authorizer(tmp_path, open_browser=True)._on_redirect(
        "https://as.test/authorize?state=s"
    )
    assert opened == ["https://as.test/authorize?state=s"]


async def test_callback_resolves_the_pending_wait(tmp_path):
    import anyio

    authorizer = _authorizer(tmp_path)
    await authorizer._on_redirect("https://as.test/authorize?state=s1")

    async with anyio.create_task_group() as tg:
        async def _resolve():
            await anyio.sleep(0.01)
            authorizer.pending.resolve("code-1", "s1")

        tg.start_soon(_resolve)
        code, state = await authorizer._on_callback()

    assert (code, state) == ("code-1", "s1")
    # Le rendez-vous est consommé : un second callback ne doit pas rejouer.
    assert authorizer.pending is None


async def test_pending_wait_is_bounded(tmp_path):
    """Sans borne, un upstream jamais autorisé retiendrait indéfiniment la
    tâche qui l'attend. Le timeout d'OAuthContext ne couvre pas ce handler."""
    authorizer = _authorizer(tmp_path, wait_timeout=0.05)
    await authorizer._on_redirect("https://as.test/authorize?state=s1")

    with pytest.raises(TimeoutError, match="remote"):
        await authorizer._on_callback()


async def test_denied_authorization_raises_instead_of_hanging(tmp_path):
    authorizer = _authorizer(tmp_path, wait_timeout=5)
    await authorizer._on_redirect("https://as.test/authorize?state=s1")
    authorizer.pending.resolve(None, "s1", error="access_denied")

    with pytest.raises(RuntimeError, match="access_denied"):
        await authorizer._on_callback()


def test_pending_resolve_is_idempotent(tmp_path):
    """Recharger l'onglet du callback ne doit pas écraser un résultat reçu."""
    pending = mcp_proxy.PendingAuthorization("remote", 5)
    pending.resolve("code-1", "s1")
    pending.resolve("code-2", "s1")
    assert pending._code == "code-1"


# --- la page rendue au navigateur -----------------------------------------

def test_callback_page_reports_success():
    html = mcp_proxy.render_callback_page("remote", None)
    assert "remote" in html
    assert "fermer cet onglet" in html


def test_callback_page_is_rendered_on_refusal_too():
    """Un onglet blanc après un refus laisserait croire à une panne du proxy,
    alors que le refus vient de l'utilisateur."""
    html = mcp_proxy.render_callback_page("remote", "access_denied")
    assert "access_denied" in html
    assert "refusée" in html


# --- la route -------------------------------------------------------------

def _callback_client(authorizers):
    """Même patron que test_proxy_auth.py : ASGITransport, pas TestClient
    (déprécié côté Starlette avec httpx 0.x)."""
    import httpx
    from starlette.applications import Starlette

    app = Starlette(routes=[mcp_proxy.build_callback_route(authorizers)])
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def test_callback_route_routes_by_exact_state(tmp_path):
    a = _authorizer(tmp_path, name="alpha")
    b = _authorizer(tmp_path, name="beta")
    await a._on_redirect("https://as.test/authorize?state=state-alpha")
    await b._on_redirect("https://as.test/authorize?state=state-beta")

    async with _callback_client({"alpha": a, "beta": b}) as client:
        resp = await client.get("/callback", params={"code": "c-1", "state": "state-beta"})

    assert resp.status_code == 200
    assert b.pending._code == "c-1"
    assert a.pending._code is None


async def test_callback_route_reports_error_status(tmp_path):
    a = _authorizer(tmp_path)
    await a._on_redirect("https://as.test/authorize?state=s1")

    async with _callback_client({"remote": a}) as client:
        resp = await client.get("/callback", params={"error": "access_denied", "state": "s1"})

    assert resp.status_code == 400
    assert "access_denied" in resp.text


async def test_callback_route_without_pending_authorization(tmp_path):
    async with _callback_client({"remote": _authorizer(tmp_path)}) as client:
        resp = await client.get("/callback", params={"code": "c-1", "state": "s1"})
    assert resp.status_code == 400


# --- câblage --------------------------------------------------------------

def test_authorizers_built_only_for_http_upstreams_with_auth(tmp_path):
    cfg = {
        "port": 8799,
        "mcpServers": {
            "plain": {"type": "http", "url": "http://x/mcp"},
            "guarded": {"type": "http", "url": "http://y/mcp", "auth": {}},
            "local": {"type": "inprocess", "module": "mcp_bench"},
        },
    }
    upstreams = mcp_proxy.build_upstreams(cfg)
    authorizers = mcp_proxy.build_upstream_authorizers(
        cfg, upstreams, tmp_path / "t.json", "http://127.0.0.1:8799/callback"
    )
    assert set(authorizers) == {"guarded"}


def test_auth_wires_the_provider_into_the_transport(tmp_path):
    """Le seul endroit où l'auth entre dans le transport : le paramètre
    httpx.Auth de HttpUpstream. Sans ce câblage, tout le reste tourne à vide."""
    cfg = {
        "port": 8799,
        "mcpServers": {"guarded": {"type": "http", "url": "http://y/mcp", "auth": {}}},
    }
    upstreams = mcp_proxy.build_upstreams(cfg)
    assert upstreams["guarded"]._auth is None

    authorizers = mcp_proxy.build_upstream_authorizers(
        cfg, upstreams, tmp_path / "t.json", "http://127.0.0.1:8799/callback"
    )
    assert upstreams["guarded"]._auth is authorizers["guarded"].provider()


def test_auth_on_a_non_http_upstream_is_refused(tmp_path):
    """Une clé `auth` sur un inprocess ou un stdio ne veut rien dire : le dire
    plutôt que de l'ignorer en silence."""
    cfg = {
        "port": 8799,
        "mcpServers": {"local": {"type": "inprocess", "module": "mcp_bench", "auth": {}}},
    }
    upstreams = mcp_proxy.build_upstreams(cfg)
    with pytest.raises(ValueError, match="http"):
        mcp_proxy.build_upstream_authorizers(
            cfg, upstreams, tmp_path / "t.json", "http://127.0.0.1:8799/callback"
        )


def test_disabled_auth_block_is_ignored(tmp_path):
    cfg = {
        "port": 8799,
        "mcpServers": {
            "guarded": {"type": "http", "url": "http://y/mcp", "auth": {"_disabled": True}}
        },
    }
    upstreams = mcp_proxy.build_upstreams(cfg)
    assert mcp_proxy.build_upstream_authorizers(
        cfg, upstreams, tmp_path / "t.json", "http://127.0.0.1:8799/callback"
    ) == {}


def test_config_credentials_reach_the_provider_storage(tmp_path):
    cfg = {
        "port": 8799,
        "mcpServers": {
            "guarded": {
                "type": "http",
                "url": "http://y/mcp",
                "auth": {"client_id": "c-1", "client_secret": "s-1"},
            }
        },
    }
    upstreams = mcp_proxy.build_upstreams(cfg)
    authorizers = mcp_proxy.build_upstream_authorizers(
        cfg, upstreams, tmp_path / "t.json", "http://127.0.0.1:8799/callback"
    )
    override = authorizers["guarded"]._storage._client_info_override
    assert override.client_id == "c-1"


# ---------------------------------------------------------------------------
# Le démarrage ne bloque jamais sur une autorisation
# ---------------------------------------------------------------------------

async def test_boot_refuses_instead_of_waiting_for_a_click(tmp_path, capsys):
    """LE défaut trouvé en vérification réelle, et le seul qui rendait tout le
    reste inutilisable.

    start() d'un upstream tourne dans le lifespan, AVANT qu'uvicorn n'ouvre le
    port. Attendre là un clic sur /callback est un interblocage : le proxy
    attend une redirection vers une route qu'il ne sert pas encore, donc le
    port n'ouvre jamais, donc le clic ne peut pas aboutir. Trouvé en lançant
    réellement les deux proxys, pas en relisant le code.
    """
    authorizer = _authorizer(tmp_path, interactive=False)

    with pytest.raises(mcp_proxy.AuthorizationRequired, match="remote"):
        await authorizer._on_redirect("https://as.test/authorize?state=s1")

    # Rien n'attend, et l'URL reste connue pour qui la demandera.
    assert authorizer.pending is None
    assert authorizer.last_authorization_url == "https://as.test/authorize?state=s1"


async def test_authorize_lifts_the_flag_only_for_the_attempt(tmp_path):
    """Un échec plus tard ne doit pas rouvrir un parcours à l'insu de tous."""
    authorizer = _authorizer(tmp_path, interactive=False)

    class _Upstream:
        def __init__(self):
            self.seen = None

        async def start(self):
            self.seen = authorizer.interactive

    upstream = _Upstream()
    await authorizer.authorize(upstream)

    assert upstream.seen is True
    assert authorizer.interactive is False


async def test_authorize_lowers_the_flag_even_on_failure(tmp_path):
    authorizer = _authorizer(tmp_path, interactive=False)

    class _Failing:
        async def start(self):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await authorizer.authorize(_Failing())
    assert authorizer.interactive is False


# ---------------------------------------------------------------------------
# Troisième état : « connu mais pas autorisé » (AB-2.5)
# ---------------------------------------------------------------------------

class _FakeAuthorizer:
    def __init__(self, url="https://as.test/authorize?state=s1"):
        self.last_authorization_url = url
        self.last_error = None


def _unauthorized_upstream():
    """HttpUpstream sans session : exactement l'état d'un upstream dont le
    parcours n'a pas encore été mené."""
    return mcp_proxy.HttpUpstream("https://example.test/mcp")


def _tool(name="echo", description="Renvoie le texte."):
    import mcp.types as types

    return types.Tool(name=name, description=description, inputSchema={})


# --- le cache d'outils ----------------------------------------------------

def test_catalog_round_trip(tmp_path):
    cat = mcp_proxy.ToolCatalogCache(tmp_path / "c.json")
    assert cat.recall("remote") == ([], None)

    cat.remember("remote", [_tool(), _tool("add", "Additionne.")])
    tools, known_at = cat.recall("remote")

    assert [t.name for t in tools] == ["echo", "add"]
    assert known_at is not None


def test_catalog_survives_a_corrupt_file(tmp_path, capsys):
    path = tmp_path / "c.json"
    path.write_text("{ pas du json")
    assert mcp_proxy.ToolCatalogCache(path).recall("remote") == ([], None)
    assert "illisible" in capsys.readouterr().err


def test_catalog_keeps_upstreams_apart(tmp_path):
    cat = mcp_proxy.ToolCatalogCache(tmp_path / "c.json")
    cat.remember("alpha", [_tool("a")])
    cat.remember("beta", [_tool("b")])
    assert [t.name for t in cat.recall("alpha")[0]] == ["a"]
    assert [t.name for t in cat.recall("beta")[0]] == ["b"]


# --- le marquage des outils resservis -------------------------------------

def test_stale_description_warns_the_model():
    """Présenter une liste périmée comme vivante serait mentir au modèle, qui
    n'a aucun autre moyen de le savoir."""
    out = mcp_proxy.format_stale_description("Renvoie le texte.", None)
    assert "non autorisé" in out
    assert mcp_proxy.AUTHORIZATION_REQUIRED in out
    assert "Renvoie le texte." in out


def test_stale_description_is_stable_across_calls():
    """La date est absolue, pas relative : un texte qui changerait à chaque
    tour invaliderait le cache KV du modèle."""
    import time as _t

    known = _t.time() - 3600
    assert mcp_proxy.format_stale_description("d", known) == (
        mcp_proxy.format_stale_description("d", known)
    )


# --- listing et refus -----------------------------------------------------

async def test_unauthorized_upstream_still_lists_its_tools(tmp_path):
    """Le cœur du troisième état : les outils restent VISIBLES au lieu de
    disparaître silencieusement de tools/list."""
    import mcp.types as types

    cat = mcp_proxy.ToolCatalogCache(tmp_path / "c.json")
    cat.remember("remote", [_tool()])

    upstreams = {"remote": _unauthorized_upstream()}
    server = mcp_proxy.build_proxy_server(
        upstreams, {}, authorizers={"remote": _FakeAuthorizer()}, catalog=cat
    )
    result = await server.request_handlers[types.ListToolsRequest](
        types.ListToolsRequest(method="tools/list")
    )
    names = [t.name for t in result.root.tools]
    assert "remote__echo" in names

    described = next(t for t in result.root.tools if t.name == "remote__echo")
    assert "non autorisé" in described.description


async def test_calling_an_unauthorized_tool_raises_the_contract(tmp_path):
    """LE contrat consommé par MIAOU (AB-3) : une vraie erreur JSON-RPC dont
    `data.code` est testable par ÉGALITÉ de constante."""
    import mcp.types as types
    from mcp.shared.exceptions import McpError

    upstreams = {"remote": _unauthorized_upstream()}
    server = mcp_proxy.build_proxy_server(
        upstreams, {}, authorizers={"remote": _FakeAuthorizer()}, catalog=None
    )
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="remote__echo", arguments={}),
    )
    with pytest.raises(McpError) as excinfo:
        await server.request_handlers[types.CallToolRequest](req)

    data = excinfo.value.error.data
    assert data["code"] == mcp_proxy.AUTHORIZATION_REQUIRED
    assert data["upstream"] == "remote"
    assert data["authorization_url"] == "https://as.test/authorize?state=s1"


async def test_refusal_message_does_not_leak_the_internal_sentinel(tmp_path):
    """Le sentinel est une plomberie interne : il traverse le `except Exception`
    du SDK, il n'a rien à faire dans un message rendu au client."""
    import mcp.types as types
    from mcp.shared.exceptions import McpError

    upstreams = {"remote": _unauthorized_upstream()}
    server = mcp_proxy.build_proxy_server(
        upstreams, {}, authorizers={"remote": _FakeAuthorizer()}
    )
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="remote__echo", arguments={}),
    )
    with pytest.raises(McpError) as excinfo:
        await server.request_handlers[types.CallToolRequest](req)

    assert mcp_proxy._AUTHORIZATION_SENTINEL not in excinfo.value.error.message
    assert "autorisation" in excinfo.value.error.message


async def test_live_upstream_is_never_refused():
    """Non-régression : un upstream vivant ne doit rien connaître de tout ça."""
    import mcp.types as types

    upstreams = {"bench": mcp_proxy.InProcessUpstream("mcp_bench")}
    await upstreams["bench"].start()
    server = mcp_proxy.build_proxy_server(upstreams, {}, authorizers={})

    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(
            name="bench__echo", arguments={"text": "salut"}
        ),
    )
    with patch("asyncio.sleep", new=AsyncMock()):
        result = await server.request_handlers[types.CallToolRequest](req)
    assert result.root.isError is False


# --- l'outil status -------------------------------------------------------

async def test_status_tool_is_listed_without_a_prefix(tmp_path):
    """Nom NU : MIAOU préfixe déjà par le nom de la carte serveur, donc
    `proxy__status` donnerait `miaou-proxy__proxy__status`."""
    import mcp.types as types

    upstreams = {"remote": _unauthorized_upstream()}
    server = mcp_proxy.build_proxy_server(
        upstreams, {}, authorizers={"remote": _FakeAuthorizer()}
    )
    result = await server.request_handlers[types.ListToolsRequest](
        types.ListToolsRequest(method="tools/list")
    )
    assert "status" in [t.name for t in result.root.tools]


async def test_status_is_routed_despite_having_no_prefix(tmp_path):
    """Le piège d'implémentation annoncé : la table résout tout par préfixe,
    donc un nom sans `__` partirait chercher un upstream nommé « status »."""
    import mcp.types as types

    upstreams = {"remote": _unauthorized_upstream()}
    server = mcp_proxy.build_proxy_server(
        upstreams, {}, authorizers={"remote": _FakeAuthorizer()}
    )
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="status", arguments={}),
    )
    result = await server.request_handlers[types.CallToolRequest](req)
    assert result.root.isError is False
    assert "remote" in result.root.content[0].text


def test_status_report_names_the_authorization_url():
    upstreams = {"remote": _unauthorized_upstream()}
    report = mcp_proxy.build_status_report(
        upstreams, {"remote": _FakeAuthorizer()}, catalog=None
    )
    assert "NON AUTORISÉ" in report
    assert "https://as.test/authorize?state=s1" in report
    assert mcp_proxy.AUTHORIZATION_REQUIRED in report


def test_status_report_counts_remembered_tools(tmp_path):
    cat = mcp_proxy.ToolCatalogCache(tmp_path / "c.json")
    cat.remember("remote", [_tool(), _tool("add")])
    report = mcp_proxy.build_status_report(
        {"remote": _unauthorized_upstream()}, {"remote": _FakeAuthorizer()}, cat
    )
    assert "2 outil(s) connus" in report


def test_status_report_is_empty_without_upstreams():
    assert "Aucun serveur" in mcp_proxy.build_status_report({}, {}, None)


# --- non-régression : sans auth sortante, rien ne change ------------------

async def test_no_status_tool_without_outbound_auth():
    """Un proxy sans upstream OAuth doit exposer exactement ce qu'il exposait
    avant ce lot — `status` compris, qui ne doit PAS apparaître."""
    import mcp.types as types

    upstreams = {"bench": mcp_proxy.InProcessUpstream("mcp_bench")}
    await upstreams["bench"].start()
    server = mcp_proxy.build_proxy_server(upstreams, {})

    result = await server.request_handlers[types.ListToolsRequest](
        types.ListToolsRequest(method="tools/list")
    )
    assert "status" not in [t.name for t in result.root.tools]


# ---------------------------------------------------------------------------
# Diagnostic d'un échec d'autorisation (scope insuffisant)
# ---------------------------------------------------------------------------

def test_status_reports_why_the_last_attempt_failed():
    """Un parcours peut aboutir ET l'appel rester refusé : jeton obtenu, mais
    scopes insuffisants pour le serveur (403). Sans cette distinction, `status`
    présente la même chose qu'une autorisation jamais faite, et l'exploitant
    reclique sur un lien qui ne peut rien réparer."""
    authorizer = _FakeAuthorizer()
    authorizer.last_error = "Client error '403 Forbidden' for url 'http://x/mcp'"

    report = mcp_proxy.build_status_report(
        {"remote": _unauthorized_upstream()}, {"remote": authorizer}, None
    )
    assert "403" in report
    assert "scopes sont insuffisants" in report
    assert "required_scopes" in report


def test_status_stays_quiet_when_nothing_has_failed():
    """Pas de bruit sur un upstream simplement jamais autorisé : l'absence de
    tentative n'est pas un échec."""
    report = mcp_proxy.build_status_report(
        {"remote": _unauthorized_upstream()}, {"remote": _FakeAuthorizer()}, None
    )
    assert "Dernière tentative" not in report


def test_non_403_failure_is_reported_without_the_scope_hint():
    """Un échec réseau ne doit pas être attribué aux scopes : le conseil serait
    faux, et un mauvais diagnostic coûte plus qu'aucun diagnostic."""
    authorizer = _FakeAuthorizer()
    authorizer.last_error = "ConnectError: connection refused"

    report = mcp_proxy.build_status_report(
        {"remote": _unauthorized_upstream()}, {"remote": authorizer}, None
    )
    assert "connection refused" in report
    assert "scopes sont insuffisants" not in report
