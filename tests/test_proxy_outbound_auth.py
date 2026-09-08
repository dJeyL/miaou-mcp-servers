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

class _NoopUpstream:
    """`authorize()` ne redémarre l'upstream que s'il a obtenu un jeton."""

    def __init__(self):
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1


def _patch_authorize_transport(monkeypatch, handler):
    """Intercepte la requête d'amorçage d'`authorize()` — aucun réseau.

    `authorize()` provoque désormais le parcours par une vraie requête HTTP
    portant le provider en `auth` (c'est ce qui fait dérouler au SDK son chemin
    nominal). Les tests remplacent donc le transport, jamais `start()`.
    """
    import httpx

    original = httpx.AsyncClient.__init__

    def _init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)


def _unauthorized(request):
    import httpx

    return httpx.Response(401)


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


async def test_authorize_lifts_the_flag_only_for_the_attempt(tmp_path, monkeypatch):
    """Un échec plus tard ne doit pas rouvrir un parcours à l'insu de tous.

    Le drapeau doit être vrai PENDANT la requête d'amorçage — c'est lui qui
    autorise `_on_redirect` à ouvrir un parcours plutôt qu'à refuser."""
    authorizer = _authorizer(tmp_path, interactive=False)
    seen = []

    def _handler(request):
        import httpx

        seen.append(authorizer.interactive)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {}})

    _patch_authorize_transport(monkeypatch, _handler)
    await authorizer.authorize(_NoopUpstream())

    # `interactive` doit être vrai pour TOUTE la séquence : c'est le dernier
    # appel (tools/call) qui est refusé sur un serveur d'entreprise, donc celui
    # qui amorce le parcours. Sans session renvoyée par le serveur, la
    # notification est sautée — d'où trois requêtes ici (initialize,
    # tools/list, tools/call).
    assert seen == [True, True, True]
    assert authorizer.interactive is False


@pytest.mark.anyio
async def test_the_probe_replays_the_session_and_ends_on_a_tool_call(tmp_path, monkeypatch):
    """La séquence doit aller JUSQU'À `tools/call`, session rejouée.

    Mesuré sur un déploiement d'entreprise : `initialize` répond 200 et seul
    `tools/call` renvoie le 401 porteur du `WWW-Authenticate`. Une requête
    d'amorçage arbitraire n'était donc jamais refusée, et aucun parcours ne
    démarrait. Et `tools/call` ne s'envoie pas nu : sans `Mcp-Session-Id`, il
    est rejeté hors de toute question d'autorisation."""
    seen = []

    def _handler(request):
        import httpx

        body = json.loads(request.content.decode())
        seen.append((body.get("method"), request.headers.get("mcp-session-id")))
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": "sess-42"},
            json={"jsonrpc": "2.0", "id": 1, "result": {}},
        )

    authorizer = _authorizer(tmp_path, interactive=False)
    _patch_authorize_transport(monkeypatch, _handler)
    await authorizer.authorize(_NoopUpstream())

    assert [m for m, _ in seen] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call",
    ]
    # La session obtenue à l'initialize est rejouée sur les suivantes.
    assert [sid for _, sid in seen] == [None, "sess-42", "sess-42", "sess-42"]


def _tools_list_response(names):
    """Réponse `tools/list` au format SSE de ce transport."""
    import httpx

    tools = ", ".join(
        f'{{"name":"{n}","description":"","inputSchema":{{}}}}' for n in names
    )
    return httpx.Response(
        200,
        text=f'event: message\ndata: {{"jsonrpc":"2.0","id":2,'
             f'"result":{{"tools":[{tools}]}}}}\n\n',
    )


def _probe_handler(names, called, session="S1"):
    """Serveur qui liste `names` et note l'outil appelé."""
    def _handler(request):
        import httpx

        body = json.loads(request.content.decode())
        method = body.get("method")
        if method == "tools/list":
            return _tools_list_response(names)
        if method == "tools/call":
            called.append(body["params"]["name"])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 3, "result": {}})
        return httpx.Response(
            200, headers={"Mcp-Session-Id": session},
            json={"jsonrpc": "2.0", "id": 1, "result": {}},
        )

    return _handler


@pytest.mark.anyio
async def test_the_probe_calls_a_real_read_only_tool(tmp_path, monkeypatch):
    """Une passerelle qui route par ressource (WSO2, mesuré le 2026-09-07)
    rejette un nom d'outil INCONNU avant d'évaluer l'autorisation : elle rend
    `403 No matching resource found in the API` là où un outil réel obtient le
    401 recherché. La sonde doit donc nommer un outil qui existe."""
    called = []
    authorizer = _authorizer(tmp_path, interactive=False)
    _patch_authorize_transport(
        monkeypatch, _probe_handler(["jira_list_projects"], called)
    )
    await authorizer.authorize(_NoopUpstream())

    assert called == ["jira_list_projects"]


@pytest.mark.anyio
async def test_the_probe_never_calls_a_tool_that_writes(tmp_path, monkeypatch):
    """Obtenir un jeton ne doit pas créer un ticket. Les outils d'écriture sont
    écartés même s'ils viennent en premier dans la liste."""
    called = []
    authorizer = _authorizer(tmp_path, interactive=False)
    _patch_authorize_transport(monkeypatch, _probe_handler(
        ["jira_create_issue", "jira_add_comment", "jira_search"], called
    ))
    await authorizer.authorize(_NoopUpstream())

    assert called == ["jira_search"]


@pytest.mark.anyio
async def test_the_probe_falls_back_when_every_tool_writes(tmp_path, monkeypatch):
    """Aucun candidat sûr : on garde le nom de repli plutôt que d'appeler un
    outil qui écrit. Une sonde qui échoue vaut mieux qu'une sonde qui agit."""
    called = []
    authorizer = _authorizer(tmp_path, interactive=False)
    _patch_authorize_transport(
        monkeypatch, _probe_handler(["jira_create_issue", "jira_delete"], called)
    )
    await authorizer.authorize(_NoopUpstream())

    assert called == [mcp_proxy._AUTH_PROBE_TOOL]


def test_pick_probe_tool_reads_names_and_skips_writers():
    body = ('data: {"result":{"tools":[{"name":"add_item"},'
            '{"name":"list_updates"},{"name":"get_page"}]}}')
    assert mcp_proxy._pick_probe_tool(body) == "list_updates"
    assert mcp_proxy._pick_probe_tool("") is None
    assert mcp_proxy._pick_probe_tool('{"name":"createIssue"}') is None


async def test_authorize_lowers_the_flag_even_on_failure(tmp_path, monkeypatch):
    def _boom(request):
        raise RuntimeError("boom")

    authorizer = _authorizer(tmp_path, interactive=False)
    _patch_authorize_transport(monkeypatch, _boom)

    with pytest.raises(RuntimeError):
        await authorizer.authorize(_NoopUpstream())
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


class _LiveUpstream:
    """Upstream qui répond : ni HttpUpstream, ni session — `upstream_is_live`
    rend True pour tout ce qui n'est pas un HttpUpstream."""

    async def list_tools(self):
        return [_tool()]


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


# --- la dérivation du chemin d'autorisation (AB-4.1) ----------------------

def test_authorize_path_is_relative():
    """Le proxy ne connaît que son adresse d'écoute : publier une URL absolue
    donnerait un lien injoignable à un client qui l'atteint par un reverse
    proxy. C'est au client de composer l'origine."""
    path = mcp_proxy.authorize_path("jira")
    assert path == "/authorize/jira"
    assert "://" not in path


def test_authorize_path_matches_the_route_actually_served():
    """Épingle la dérivation sur le pattern de la route : renommer /authorize
    doit casser un test, pas le parcours.

    Sans ça, la seule chose qui lie les deux est qu'on ait écrit deux fois la
    même chaîne — exactement le genre de couplage qui se défait en silence."""
    route = mcp_proxy.build_authorize_route({}, {})
    from starlette.routing import Match

    scope = {
        "type": "http",
        "method": "GET",
        "path": mcp_proxy.authorize_path("jira"),
        "path_params": {},
        "headers": [],
    }
    match, child = route.matches(scope)
    assert match == Match.FULL
    assert child["path_params"]["name"] == "jira"


# --- la surface machine : _meta sur tools/list (AB-4.2) --------------------

UNAUTHORIZED_META_KEY = mcp_proxy.UNAUTHORIZED_UPSTREAMS_META_KEY
"""La constante du module, pas une copie : la clé est le contrat lu par MIAOU,
et deux littéraux dériveraient sans que rien ne le signale. Sa valeur exacte
est épinglée une fois, juste en dessous."""


def test_the_meta_key_is_namespaced():
    """`_meta` est un espace partagé : une clé nue collisionnerait avec une
    extension future du SDK ou d'un autre agrégateur."""
    assert UNAUTHORIZED_META_KEY == "miaou/unauthorized_upstreams"


async def _list_tools_result(server):
    """Passe par le vrai handler de requête du SDK, pas par la fonction
    décorée : c'est lui qui enveloppe un retour de style ancien, et donc lui
    qui décide si un `_meta` survit."""
    import mcp.types as types

    req = types.ListToolsRequest(method="tools/list")
    return (await server.request_handlers[types.ListToolsRequest](req)).root


async def test_unauthorized_upstreams_reach_the_wire_under_the_meta_key(tmp_path):
    """Le test qui compte, et le seul qui puisse échouer sur le défaut visé.

    Pydantic ne sérialise sous l'alias que si le champ a été peuplé PAR
    l'alias : `ListToolsResult(meta={...})` produit la clé `meta`, pas `_meta`
    — silencieusement invalide, silencieusement ignoré par le client. Les deux
    formes rendent le même `result.meta` côté Python, donc un test qui
    assertionne sur l'objet passe sur la mauvaise. Il faut regarder la CHAÎNE
    JSON réellement émise.
    """
    cat = mcp_proxy.ToolCatalogCache(tmp_path / "c.json")
    cat.remember("remote", [_tool()])
    server = mcp_proxy.build_proxy_server(
        {"remote": _unauthorized_upstream()},
        {},
        authorizers={"remote": _FakeAuthorizer()},
        catalog=cat,
    )

    result = await _list_tools_result(server)
    wire = result.model_dump_json(by_alias=True, exclude_none=True)

    assert '"_meta"' in wire
    assert '"meta"' not in wire

    payload = json.loads(wire)["_meta"][UNAUTHORIZED_META_KEY]
    assert payload == [{"name": "remote", "authorize_path": "/authorize/remote"}]


async def test_meta_round_trips_back_into_a_client_side_model(tmp_path):
    """Ce que fait un client conforme : re-valider le JSON reçu. Sans ce
    round-trip, on épinglerait la sérialisation sans savoir si elle se relit."""
    import mcp.types as types

    cat = mcp_proxy.ToolCatalogCache(tmp_path / "c.json")
    cat.remember("remote", [_tool()])
    server = mcp_proxy.build_proxy_server(
        {"remote": _unauthorized_upstream()},
        {},
        authorizers={"remote": _FakeAuthorizer()},
        catalog=cat,
    )

    wire = (await _list_tools_result(server)).model_dump_json(
        by_alias=True, exclude_none=True
    )
    client_side = types.ListToolsResult.model_validate(json.loads(wire))

    assert client_side.meta[UNAUTHORIZED_META_KEY][0]["name"] == "remote"


async def test_no_meta_key_when_every_upstream_answers():
    """Absence de clé plutôt que tableau vide : un client lit « rien à
    signaler » pareil dans les deux cas, et un proxy sain n'a pas à publier un
    `_meta` à chaque tools/list."""
    server = mcp_proxy.build_proxy_server(
        {"bench": _LiveUpstream()}, {}, authorizers={}, catalog=None
    )
    wire = (await _list_tools_result(server)).model_dump_json(
        by_alias=True, exclude_none=True
    )
    assert UNAUTHORIZED_META_KEY not in wire


async def test_several_unauthorized_upstreams_all_appear(tmp_path):
    """N upstreams d'un même proxy peuvent être non autorisés simultanément :
    la surface porte un tableau dès la première version."""
    server = mcp_proxy.build_proxy_server(
        {
            "jira": _unauthorized_upstream(),
            "confluence": _unauthorized_upstream(),
        },
        {},
        authorizers={
            "jira": _FakeAuthorizer(),
            "confluence": _FakeAuthorizer(),
        },
        catalog=None,
    )
    wire = (await _list_tools_result(server)).model_dump_json(
        by_alias=True, exclude_none=True
    )
    entries = json.loads(wire)["_meta"][UNAUTHORIZED_META_KEY]

    assert {e["name"] for e in entries} == {"jira", "confluence"}
    assert {e["authorize_path"] for e in entries} == {
        "/authorize/jira",
        "/authorize/confluence",
    }


async def test_an_unreachable_upstream_without_authorizer_is_not_listed():
    """Un HttpUpstream sans session mais sans bloc `auth` est injoignable, pas
    non autorisé : il n'a aucun parcours à proposer, et le publier enverrait le
    client sur un /authorize/{name} qui répond 404."""
    server = mcp_proxy.build_proxy_server(
        {"dead": _unauthorized_upstream()}, {}, authorizers={}, catalog=None
    )
    wire = (await _list_tools_result(server)).model_dump_json(
        by_alias=True, exclude_none=True
    )
    assert UNAUTHORIZED_META_KEY not in wire


async def test_listing_a_healthy_proxy_is_unchanged_by_the_migration():
    """Non-régression de la migration du handler au style nouveau : mêmes
    outils, même préfixage."""
    server = mcp_proxy.build_proxy_server({"bench": _LiveUpstream()}, {})
    result = await _list_tools_result(server)
    assert [t.name for t in result.tools] == ["bench__echo"]


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
    # Chemin RELATIF, dérivé par authorize_path — et non plus l'URL d'un
    # parcours avorté, qui menait à un callback orphelin.
    assert data["authorization_url"] == "/authorize/remote"


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


def test_status_report_names_who_can_authorize():
    """Ce rapport est lu par un modèle : il ne peut pas ouvrir un lien ni
    résoudre un chemin relatif. Il doit apprendre QUI peut agir, et pouvoir
    citer le chemin à cette personne."""
    upstreams = {"remote": _unauthorized_upstream()}
    report = mcp_proxy.build_status_report(
        upstreams, {"remote": _FakeAuthorizer()}, catalog=None
    )
    assert "NON AUTORISÉ" in report
    assert "utilisateur" in report
    assert "/authorize/remote" in report
    assert mcp_proxy.AUTHORIZATION_REQUIRED in report


def test_status_report_never_publishes_the_aborted_url():
    """`last_authorization_url` reste utile en diagnostic mais cesse d'être
    publiée comme cible d'action : elle porte le state d'une transaction que le
    provider a abandonnée, et la suivre mène à /callback sans pending."""
    report = mcp_proxy.build_status_report(
        {"remote": _unauthorized_upstream()},
        {"remote": _FakeAuthorizer("https://as.test/authorize?state=s1")},
        catalog=None,
    )
    assert "state=s1" not in report


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


# ---------------------------------------------------------------------------
# Session ouverte MAIS autorisation manquante (correctif du 2026-09-07)
#
# Le lot AB-5 jugeait « cet upstream répond-il ? » sur la seule présence d'une
# session. Un Jira d'entreprise, observé en production, accepte `initialize` ET
# `tools/list` sans jeton et n'exige l'autorisation qu'au premier `tools/call` :
# la session existe, le prédicat le déclarait vivant, et les trois surfaces
# concluaient toutes « il va bien » — pas de `_meta`, pas de refus avant appel,
# `status` muet.
# ---------------------------------------------------------------------------

class _PendingAuthorizer(_FakeAuthorizer):
    """Autorisation réclamée par l'AS et pas encore accordée."""

    def __init__(self, url="https://as.test/authorize?state=s1"):
        super().__init__(url)
        self.authorization_pending = True


def _live_http_upstream():
    """HttpUpstream AVEC session : `initialize` a réussi, le transport est
    ouvert. C'est l'état exact d'un upstream qui n'exige son jeton qu'à
    l'appel."""
    up = mcp_proxy.HttpUpstream("https://example.test/mcp")
    up._session = object()
    return up


def test_a_live_session_is_not_enough_to_be_live():
    """Le prédicat répond « non » dès qu'une autorisation est due, même
    transport ouvert. C'est la condition qui manquait."""
    upstream = _live_http_upstream()
    assert mcp_proxy.upstream_is_live(upstream) is True
    assert mcp_proxy.upstream_is_live(upstream, _PendingAuthorizer()) is False


def test_an_upstream_without_authorizer_keeps_the_previous_verdict():
    """Pas d'authorizer (upstream sans OAuth, appelant historique) → le
    comportement d'avant, à l'octet près."""
    assert mcp_proxy.upstream_is_live(_live_http_upstream()) is True
    assert mcp_proxy.upstream_is_live(_unauthorized_upstream()) is False


def test_status_reports_a_live_but_unauthorized_upstream():
    """`status` doit le dire : c'est une des trois surfaces qui mentaient."""
    report = mcp_proxy.build_status_report(
        {"jira": _live_http_upstream()}, {"jira": _PendingAuthorizer()}, None
    )
    assert "NON AUTORISÉ" in report
    assert "/authorize/jira" in report


@pytest.mark.anyio
async def test_list_tools_meta_names_a_live_but_unauthorized_upstream():
    """La surface `_meta` est ce qui allume la pastille de MIAOU AVANT tout
    appel. Sans le correctif, elle reste vide pour un upstream qui liste ses
    outils sans jeton — exactement le cas où on en a le plus besoin."""
    upstream = _live_http_upstream()

    async def _list_tools():
        return [_tool()]

    upstream.list_tools = _list_tools

    server = mcp_proxy.build_proxy_server(
        {"jira": upstream},
        {},
        authorizers={"jira": _PendingAuthorizer()},
    )
    import mcp.types as types

    handler = server.request_handlers[types.ListToolsRequest]
    result = await handler(
        types.ListToolsRequest(method="tools/list", params=None)
    )
    meta = result.root.meta or {}
    entries = meta.get(mcp_proxy.UNAUTHORIZED_UPSTREAMS_META_KEY) or []
    assert [e["name"] for e in entries] == ["jira"]
    assert entries[0]["authorize_path"] == "/authorize/jira"


# ---------------------------------------------------------------------------
# has_usable_token : savoir sans rien émettre
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_no_token_at_all_is_not_usable(tmp_path):
    storage = UpstreamTokenStorage(tmp_path / "t.json", "jira")
    assert await storage.has_usable_token() is False


@pytest.mark.anyio
async def test_a_fresh_token_is_usable(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({
        "jira": {
            "tokens": {"access_token": "a", "token_type": "Bearer", "expires_in": 3600},
            "expires_at": time.time() + 3600,
        }
    }))
    assert await UpstreamTokenStorage(path, "jira").has_usable_token() is True


@pytest.mark.anyio
async def test_an_expired_token_with_a_refresh_token_stays_usable(tmp_path):
    """Le SDK le rafraîchit seul, sans parcours interactif : envoyer
    l'utilisateur cliquer serait lui faire régler un problème qui n'existe
    pas."""
    path = tmp_path / "t.json"
    path.write_text(json.dumps({
        "jira": {
            "tokens": {
                "access_token": "a", "token_type": "Bearer",
                "expires_in": 3600, "refresh_token": "r",
            },
            "expires_at": time.time() - 10,
        }
    }))
    assert await UpstreamTokenStorage(path, "jira").has_usable_token() is True


@pytest.mark.anyio
async def test_an_expired_token_without_refresh_is_not_usable(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({
        "jira": {
            "tokens": {"access_token": "a", "token_type": "Bearer", "expires_in": 3600},
            "expires_at": time.time() - 10,
        }
    }))
    assert await UpstreamTokenStorage(path, "jira").has_usable_token() is False


# ---------------------------------------------------------------------------
# Le drapeau, posé et levé
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_an_inhibited_redirect_marks_the_authorization_as_due(tmp_path):
    """`_on_redirect` non interactif est le seul moment où l'AS nous apprend
    qu'un jeton manque. Le noter est ce qui permet aux surfaces de le dire sans
    re-provoquer l'échec."""
    authorizer = mcp_proxy.UpstreamAuthorizer(
        "jira", "https://example.test/mcp",
        UpstreamTokenStorage(tmp_path / "t.json", "jira"),
        "http://127.0.0.1:8765/callback",
    )
    assert authorizer.authorization_pending is False

    with pytest.raises(mcp_proxy.AuthorizationRequired):
        await authorizer._on_redirect("https://as.test/authorize?state=s1")

    assert authorizer.authorization_pending is True
    assert authorizer.last_authorization_url == "https://as.test/authorize?state=s1"


@pytest.mark.anyio
async def test_a_completed_authorization_clears_the_flag(tmp_path, monkeypatch):
    """Le témoin est le JETON, jamais l'absence d'exception."""
    path = tmp_path / "t.json"
    authorizer = mcp_proxy.UpstreamAuthorizer(
        "jira", "https://example.test/mcp",
        UpstreamTokenStorage(path, "jira"),
        "http://127.0.0.1:8765/callback",
    )
    authorizer.authorization_pending = True

    def _grant(request):
        import httpx

        # Ce que fait un parcours abouti : le jeton est en stockage.
        path.write_text(json.dumps({
            "jira": {
                "tokens": {
                    "access_token": "AT", "token_type": "Bearer",
                    "expires_in": 3600,
                },
                "expires_at": time.time() + 3600,
            }
        }))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {}})

    _patch_authorize_transport(monkeypatch, _grant)
    upstream = _NoopUpstream()
    await authorizer.authorize(upstream)

    assert authorizer.authorization_pending is False
    # La session courante a été ouverte SANS jeton : elle doit être rouverte,
    # sans quoi elle continue de ne porter aucun en-tête Authorization.
    assert upstream.stopped == 1
    assert upstream.started == 1


@pytest.mark.anyio
async def test_an_upstream_that_asks_for_nothing_grants_nothing(tmp_path, monkeypatch):
    """LE cas payé en production : `start()` réussissait sans qu'aucune requête
    ne soit refusée, donc sans déclencher l'OAuth, et l'on annonçait une
    autorisation accordée pendant qu'aucun jeton n'était écrit ni aucun appel
    émis vers l'AS. Répondre 200 n'accorde rien."""
    authorizer = mcp_proxy.UpstreamAuthorizer(
        "jira", "https://example.test/mcp",
        UpstreamTokenStorage(tmp_path / "t.json", "jira"),
        "http://127.0.0.1:8765/callback",
    )
    authorizer.authorization_pending = True

    def _ok(request):
        import httpx

        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {}})

    _patch_authorize_transport(monkeypatch, _ok)
    upstream = _NoopUpstream()
    await authorizer.authorize(upstream)

    assert authorizer.authorization_pending is True
    assert upstream.started == 0


@pytest.mark.anyio
async def test_a_failed_authorization_keeps_the_flag(tmp_path, monkeypatch):
    """Un parcours qui échoue ne doit pas faire croire que c'est réglé."""
    authorizer = mcp_proxy.UpstreamAuthorizer(
        "jira", "https://example.test/mcp",
        UpstreamTokenStorage(tmp_path / "t.json", "jira"),
        "http://127.0.0.1:8765/callback",
    )
    authorizer.authorization_pending = True

    def _boom(request):
        raise RuntimeError("scope refusé")

    _patch_authorize_transport(monkeypatch, _boom)

    with pytest.raises(RuntimeError):
        await authorizer.authorize(_NoopUpstream())
    assert authorizer.authorization_pending is True


# ---------------------------------------------------------------------------
# La route /authorize attend l'URL, elle ne la chronomètre pas
#
# Elle rendait sa réponse après un `sleep(0.1)` fixe : derrière un portail
# d'entreprise, la découverte de l'AS prend des secondes, et la route concluait
# « serveur d'autorisation injoignable » alors qu'il répondait très bien.
# Diagnostic faux, et faux dans le sens qui décourage de réessayer.
# ---------------------------------------------------------------------------

class _SlowAuthorizer:
    """AS qui met plus longtemps que l'ancien délai fixe à produire son URL."""

    def __init__(self, name="jira", delay=0.6, failure=None):
        self.name = name
        self._delay = delay
        self._failure = failure
        self.pending = None
        self.last_error = None
        self.last_authorization_url = None
        self.authorization_pending = True
        self.redirect_ready = None

    async def authorize(self, upstream):
        import anyio

        await anyio.sleep(self._delay)
        if self._failure:
            raise RuntimeError(self._failure)
        pending = mcp_proxy.PendingAuthorization(self.name, 300.0)
        pending.authorization_url = "https://as.test/authorize?state=s1"
        self.pending = pending
        if self.redirect_ready is not None:
            self.redirect_ready.set()
        await anyio.sleep(30)  # attend le retour sur /callback


def _authorize_client(authorizer):
    from contextlib import asynccontextmanager

    import anyio
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    route = mcp_proxy.build_authorize_route({"jira": authorizer}, {"jira": object()})

    @asynccontextmanager
    async def lifespan(app):
        async with anyio.create_task_group() as tg:
            app.state.task_group = tg
            yield
            tg.cancel_scope.cancel()

    return TestClient(Starlette(routes=[route], lifespan=lifespan))


def test_a_slow_authorization_server_still_redirects():
    """Le cas payé en production : l'AS répond, mais pas en 100 ms."""
    with _authorize_client(_SlowAuthorizer()) as client:
        response = client.get("/authorize/jira", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "https://as.test/authorize?state=s1"


def test_a_failed_flow_says_why_instead_of_blaming_the_network():
    """Un enregistrement refusé n'est pas une panne réseau. Le dire évite de
    renvoyer l'exploitant vers un lien qui ne peut rien réparer."""
    failure = "Registration failed: 403 insufficient_scope"
    with _authorize_client(_SlowAuthorizer(delay=0.05, failure=failure)) as client:
        response = client.get("/authorize/jira", follow_redirects=False)

    assert response.status_code == 502
    assert "insufficient_scope" in response.text
    assert "n'a pas pu être joint" not in response.text


def test_an_unknown_upstream_name_is_escaped():
    """La route est PUBLIQUE et son `name` vient du chemin d'URL."""
    with _authorize_client(_SlowAuthorizer()) as client:
        response = client.get("/authorize/%3Cimg%20src=x%3E", follow_redirects=False)

    assert response.status_code == 404
    assert "<img src=x>" not in response.text


# ---------------------------------------------------------------------------
# Le parcours qui ABOUTIT SANS redirection (2026-09-07, seconde passe)
#
# Un `refresh_token` encore valide, ou un AS qui accorde sans interaction, et le
# SDK obtient son jeton sans passer par `_on_redirect` — donc sans `pending`.
# La route ne connaissait que « redirige » ou « échoue » : elle répondait « le
# serveur d'autorisation n'a pas répondu à temps », en moins de deux secondes,
# une ligne de log après « Upstream autorisé ».
# ---------------------------------------------------------------------------

class _SilentlyGrantedAuthorizer:
    """`authorize()` réussit sans jamais rediriger."""

    def __init__(self, name="jira"):
        self.name = name
        self.pending = None
        self.last_error = None
        self.last_authorization_url = None
        self.authorization_pending = True
        self.redirect_ready = None

    async def authorize(self, upstream):
        import anyio

        await anyio.sleep(0.05)
        self.authorization_pending = False   # ce que fait le vrai authorize()


def test_a_flow_that_succeeds_without_redirecting_is_not_an_error():
    with _authorize_client(_SilentlyGrantedAuthorizer()) as client:
        response = client.get("/authorize/jira", follow_redirects=False)

    assert response.status_code == 200
    assert "autorisé" in response.text
    assert "n'a pas répondu à temps" not in response.text


def test_the_redirect_is_served_without_waiting_for_the_callback(tmp_path, monkeypatch):
    """`authorize()` bloque sur le retour du navigateur APRÈS avoir produit son
    URL. La route doit répondre dès l'URL connue, pas à la fin du parcours —
    sinon elle tient jusqu'à sa borne pour une redirection déjà décidée.

    Le VRAI `UpstreamAuthorizer` est utilisé ici, pas un double : c'est
    `_on_redirect` qui doit signaler l'événement, et un stub qui le signale à sa
    place teste le stub. Un double avait justement masqué l'absence de ce
    signal — la suite était verte sur un code où il manquait.

    La borne est à 20 s ; le parcours n'aboutit jamais (il attend un callback
    qui ne viendra pas), donc une route qui attendrait sa fin dépasserait
    largement le seuil mesuré ici."""
    import time

    import anyio

    authorizer = mcp_proxy.UpstreamAuthorizer(
        "jira", "https://example.test/mcp",
        UpstreamTokenStorage(tmp_path / "t.json", "jira"),
        "http://127.0.0.1:8765/callback",
    )

    async def _handler(request):
        # Ce que fait le SDK au 401 : il redirige, puis attend le callback.
        await authorizer._on_redirect("https://as.test/authorize?state=s1")
        await anyio.sleep(30)  # pragma: no cover - jamais atteint

    _patch_authorize_transport(monkeypatch, _handler)

    started = time.monotonic()
    with _authorize_client(authorizer) as client:
        response = client.get("/authorize/jira", follow_redirects=False)
    elapsed = time.monotonic() - started

    assert response.status_code == 302
    assert response.headers["location"] == "https://as.test/authorize?state=s1"
    assert elapsed < 5, f"la route a attendu {elapsed:.1f}s au lieu de répondre"


# ---------------------------------------------------------------------------
# La session meurt PENDANT l'appel (2026-09-07, troisième passe)
#
# La session vit dans `_serve()`, une autre tâche. Une exception levée par le
# transport de cette tâche y est capturée et rangée dans `_failure`, puis
# `_serve` sort de ses contextes : le stream se ferme sous les pieds de
# l'appelant, sans réponse ni erreur POUR LUI. `session.call_tool` attendait
# donc son propre timeout — client suspendu, refus jamais rendu.
# ---------------------------------------------------------------------------

class _BlockingSession:
    """Un appel dont la réponse n'arrivera jamais."""

    def __init__(self):
        import anyio

        self._never = anyio.Event()

    async def call_tool(self, name, arguments):
        await self._never.wait()  # pragma: no cover - jamais réveillé


def _upstream_with_blocking_session():
    import anyio

    upstream = mcp_proxy.HttpUpstream("https://jira.test/mcp")
    upstream._session = _BlockingSession()
    upstream._stopped = anyio.Event()
    upstream._serving = True
    return upstream


@pytest.mark.anyio
async def test_a_service_task_dying_mid_call_wakes_the_caller():
    """Sans ça, l'appelant attend son timeout pour un échec déjà connu."""
    import anyio

    upstream = _upstream_with_blocking_session()

    async def _kill():
        await anyio.sleep(0.05)
        # L'ordre de `_serve` : le `except` pose la cause, le `finally` signale.
        upstream._failure = mcp_proxy.AuthorizationRequired("jira")
        upstream._session = None
        upstream._stopped.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_kill)
        with anyio.move_on_after(5) as scope:
            with pytest.raises(mcp_proxy.AuthorizationRequired):
                await upstream.call_tool("search", {})

    assert not scope.cancelled_caught, "l'appelant est resté bloqué"


@pytest.mark.anyio
async def test_a_dying_service_without_a_cause_still_wakes_the_caller():
    """Rien à relever, mais surtout pas une attente indéfinie."""
    import anyio

    upstream = _upstream_with_blocking_session()

    async def _kill():
        await anyio.sleep(0.05)
        upstream._session = None
        upstream._stopped.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_kill)
        with anyio.move_on_after(5) as scope:
            with pytest.raises(RuntimeError):
                await upstream.call_tool("search", {})

    assert not scope.cancelled_caught, "l'appelant est resté bloqué"


@pytest.mark.anyio
async def test_a_normal_call_is_unaffected():
    """La surveillance ne doit rien coûter au chemin nominal."""
    import anyio

    class _OkSession:
        async def call_tool(self, name, arguments):
            class _Result:
                content = ["ok"]

            return _Result()

    upstream = mcp_proxy.HttpUpstream("https://jira.test/mcp")
    upstream._session = _OkSession()
    upstream._stopped = anyio.Event()
    upstream._serving = True

    with anyio.move_on_after(5) as scope:
        assert await upstream.call_tool("search", {}) == ["ok"]
    assert not scope.cancelled_caught


@pytest.mark.anyio
async def test_calling_without_a_session_raises_the_stored_cause():
    upstream = mcp_proxy.HttpUpstream("https://jira.test/mcp")
    upstream._failure = mcp_proxy.AuthorizationRequired("jira")

    with pytest.raises(mcp_proxy.AuthorizationRequired):
        await upstream.call_tool("search", {})


# ---------------------------------------------------------------------------
# Mode debug du parcours (--debug-auth)
# ---------------------------------------------------------------------------

def test_redaction_hides_secrets_and_keeps_what_diagnoses():
    """Un log d'OAuth qui fuite un jeton serait pire que pas de log. Mais
    masquer `state` rendrait le log inutile : c'est lui qui corrèle un
    callback à son parcours."""
    redacted = mcp_proxy._redact_url(
        "https://as.test/cb?code=AUTHCODE&state=s1&access_token=LEAK"
        "&code_challenge=xyz"
    )
    assert "AUTHCODE" not in redacted
    assert "LEAK" not in redacted
    assert "state=s1" in redacted
    assert "code_challenge=xyz" in redacted


def test_redaction_leaves_a_url_without_query_alone():
    url = "https://jira.test/mcp"
    assert mcp_proxy._redact_url(url) == url


def test_redaction_is_readable():
    """`***` et non `%2A%2A%2A` : un log illisible ne se lit pas."""
    assert "***" in mcp_proxy._redact_url("https://as.test/t?code=x")


@pytest.mark.anyio
async def test_debug_mode_names_the_absence_of_a_refusal(tmp_path, monkeypatch, capsys):
    """LE cas que le mode debug existe pour rendre visible : un upstream qui ne
    refuse rien, donc aucun parcours possible — trois causes distinctes se
    présentaient jusque-là sous le même symptôme muet."""
    monkeypatch.setattr(mcp_proxy, "_AUTH_DEBUG", True)

    def _never_refuses(request):
        import httpx

        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    authorizer = _authorizer(tmp_path, interactive=True)
    _patch_authorize_transport(monkeypatch, _never_refuses)

    import httpx

    async with httpx.AsyncClient() as client:
        await authorizer._provoke_refusal(client)

    err = capsys.readouterr().err
    assert "VERDICT" in err
    assert "tools/call -> HTTP 200" in err


def _probe_verdict(monkeypatch, tmp_path, capsys, handler):
    """Déroule la sonde en mode debug et rend ce qui a été journalisé."""
    import anyio
    import httpx

    monkeypatch.setattr(mcp_proxy, "_AUTH_DEBUG", True)
    authorizer = _authorizer(tmp_path, interactive=True)
    _patch_authorize_transport(monkeypatch, handler)

    async def _run():
        async with httpx.AsyncClient() as client:
            await authorizer._provoke_refusal(client)

    anyio.run(_run)
    return capsys.readouterr().err


def test_debug_mode_separates_a_bare_401_from_a_usable_one(monkeypatch, tmp_path, capsys):
    """Un 401 sans `www-authenticate` ne donne au client AUCUN serveur
    d'autorisation à découvrir : les deux cas ne se corrigent pas au même
    endroit, donc ils ne doivent pas se lire pareil."""
    def _bare(request):
        import httpx

        if b"tools/call" in (request.content or b""):
            return httpx.Response(401)
        return httpx.Response(200, headers={"Mcp-Session-Id": "S1"},
                              json={"jsonrpc": "2.0", "id": 1, "result": {}})

    err = _probe_verdict(monkeypatch, tmp_path, capsys, _bare)
    assert "SANS www-authenticate" in err


def test_debug_mode_flags_a_403_that_carried_a_token(monkeypatch, tmp_path, capsys):
    """403 AVEC jeton envoyé et 403 sans ne mènent pas au même diagnostic : le
    premier accuse le jeton, le second la requête elle-même."""
    def _forbidden(request):
        import httpx

        if b"tools/call" in (request.content or b""):
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, headers={"Mcp-Session-Id": "S1"},
                              json={"jsonrpc": "2.0", "id": 1, "result": {}})

    err = _probe_verdict(monkeypatch, tmp_path, capsys, _forbidden)
    assert "403 sans jeton envoyé" in err
    assert "passerelle" in err


def test_debug_mode_never_logs_a_token_value(monkeypatch, tmp_path, capsys):
    """Le mode debug nomme les en-têtes, jamais leurs valeurs."""
    secret = "SUPERSECRETTOKENVALUE"

    def _with_token(request):
        import httpx

        return httpx.Response(200, headers={"Mcp-Session-Id": "S1"},
                              json={"jsonrpc": "2.0", "id": 1, "result": {}})

    monkeypatch.setattr(mcp_proxy, "_AUTH_DEBUG", True)
    authorizer = _authorizer(tmp_path, interactive=True)
    _patch_authorize_transport(monkeypatch, _with_token)

    import anyio
    import httpx

    async def _run():
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {secret}"}
        ) as client:
            await authorizer._provoke_refusal(client)

    anyio.run(_run)
    err = capsys.readouterr().err
    assert secret not in err
    assert "authorization" in err  # le NOM de l'en-tête, lui, est utile


# ---------------------------------------------------------------------------
# `auth.redirect_uri` gouverne l'URL réellement annoncée
#
# Elle existait dans la config et n'alimentait que `build_client_info_override`,
# donc l'URL DÉCLARÉE au client pré-provisionné. Le parcours, lui, annonçait
# l'URL dérivée de l'adresse d'écoute : les deux pouvaient se contredire en
# silence. Un AS d'entreprise qui refuse le loopback en clair (WSO2, rencontré
# en production) rejette alors la redirection sans que rien ne l'explique.
# ---------------------------------------------------------------------------

def _authorizers_for(auth_block, tmp_path):
    cfg = {"mcpServers": {"jira": {
        "url": "https://jira.test/mcp", "transport": "http", "auth": auth_block,
    }}}
    upstreams = {"jira": mcp_proxy.HttpUpstream("https://jira.test/mcp")}
    return mcp_proxy.build_upstream_authorizers(
        cfg, upstreams, tmp_path / "t.json", "http://127.0.0.1:8765/callback"
    )


def test_a_configured_redirect_uri_is_the_one_announced(tmp_path):
    declared = "https://proxy.exemple.fr/callback"
    authorizers = _authorizers_for({"redirect_uri": declared}, tmp_path)

    assert authorizers["jira"].callback_url == declared


def test_without_configuration_the_listening_address_still_wins(tmp_path):
    """Le défaut ne bouge pas : un déploiement local n'a rien à déclarer."""
    authorizers = _authorizers_for({}, tmp_path)

    assert authorizers["jira"].callback_url == "http://127.0.0.1:8765/callback"


@pytest.mark.anyio
async def test_the_announced_and_declared_redirect_uris_agree(tmp_path):
    """Les DEUX endroits doivent dire la même chose.

    L'un est annoncé à l'AS dans la requête d'autorisation, l'autre déclaré
    dans les credentials pré-provisionnés. Une divergence ne se voit nulle
    part : elle produit un refus côté AS, loin de sa cause."""
    declared = "https://proxy.exemple.fr/callback"
    auth_block = {"client_id": "abc", "redirect_uri": declared}
    authorizers = _authorizers_for(auth_block, tmp_path)

    announced = authorizers["jira"].callback_url
    client_info = await authorizers["jira"]._storage.get_client_info()

    assert announced == declared
    assert [str(u) for u in client_info.redirect_uris] == [declared]


@pytest.mark.anyio
async def test_debug_mode_shows_the_redirect_uri_actually_sent(tmp_path, monkeypatch, capsys):
    """Ce que l'AS REÇOIT, pas ce qu'on croit lui envoyer.

    Un refus de `redirect_uri` se règle en comparant cette valeur à celle
    enregistrée dans le client OAuth ; la lire dans la config ne prouve rien,
    seule celle-ci part réellement."""
    monkeypatch.setattr(mcp_proxy, "_AUTH_DEBUG", True)
    authorizer = _authorizer(tmp_path, interactive=False)

    with pytest.raises(mcp_proxy.AuthorizationRequired):
        await authorizer._on_redirect(
            "https://as.test/authorize?response_type=code&client_id=ABC"
            "&redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback&state=s1"
        )

    err = capsys.readouterr().err
    assert "redirect_uri : http://localhost:8765/callback" in err
    assert "client_id    : ABC" in err


# ---------------------------------------------------------------------------
# Endpoints de l'AS déclarés en config (lot AB-3)
#
# Le SDK ne connaît le token endpoint que par `context.oauth_metadata`, peuplé
# par la seule découverte `/.well-known/...`, laquelle échoue sur un AS qui ne
# les sert pas aux chemins essayés — et n'est de toute façon jamais persistée.
# Son repli vise alors `<hôte-du-serveur-MCP>/token`, donc le Jira au lieu de
# l'AS : le refresh échoue, le refresh token est jeté, l'utilisateur reclique.
# ---------------------------------------------------------------------------

def test_declared_endpoints_become_oauth_metadata():
    meta = mcp_proxy.build_oauth_metadata_override({
        "authorization_endpoint": "https://as.test/realms/w/protocol/openid-connect/auth",
        "token_endpoint": "https://as.test/realms/w/protocol/openid-connect/token",
    })

    assert str(meta.token_endpoint) == (
        "https://as.test/realms/w/protocol/openid-connect/token"
    )
    assert str(meta.authorization_endpoint) == (
        "https://as.test/realms/w/protocol/openid-connect/auth"
    )


def test_an_issuer_is_derived_when_not_declared():
    """`issuer` est exigé par le modèle mais n'a pas d'usage propre ici : le
    déduire évite une clé de config de plus, sans rien décider."""
    meta = mcp_proxy.build_oauth_metadata_override({
        "authorization_endpoint": "https://as.test/realms/w/protocol/openid-connect/auth",
        "token_endpoint": "https://as.test/realms/w/protocol/openid-connect/token",
    })

    assert str(meta.issuer).rstrip("/") == "https://as.test"


def test_a_lone_token_endpoint_is_ignored():
    """Les deux ou rien : un token endpoint seul laisserait le parcours initial
    rediriger vers un AS découvert, et rafraîchir auprès d'un autre."""
    assert mcp_proxy.build_oauth_metadata_override(
        {"token_endpoint": "https://as.test/token"}
    ) is None
    assert mcp_proxy.build_oauth_metadata_override(
        {"authorization_endpoint": "https://as.test/auth"}
    ) is None


def test_without_declaration_nothing_is_overridden():
    """Le défaut ne bouge pas : la découverte reste le chemin nominal."""
    assert mcp_proxy.build_oauth_metadata_override({}) is None
    assert mcp_proxy.build_oauth_metadata_override(None) is None


def test_the_provider_carries_the_declared_token_endpoint(tmp_path):
    """LE test de non-régression du refresh : sans ça, `_refresh_token()` du
    SDK se replie sur l'hôte du serveur MCP et poste vers un /token qui
    n'existe pas."""
    authorizers = _authorizers_for({
        "client_id": "opencode",
        "authorization_endpoint": "https://as.test/realms/w/protocol/openid-connect/auth",
        "token_endpoint": "https://as.test/realms/w/protocol/openid-connect/token",
    }, tmp_path)

    context = authorizers["jira"].provider().context

    assert context.oauth_metadata is not None
    assert str(context.oauth_metadata.token_endpoint) == (
        "https://as.test/realms/w/protocol/openid-connect/token"
    )
    # Et surtout : PAS l'hôte du serveur MCP.
    assert "jira.test" not in str(context.oauth_metadata.token_endpoint)


def test_an_undeclared_as_leaves_the_sdk_to_its_discovery(tmp_path):
    authorizers = _authorizers_for({"client_id": "opencode"}, tmp_path)

    assert authorizers["jira"].provider().context.oauth_metadata is None


# ---------------------------------------------------------------------------
# Rafraîchissement PROACTIF (lot AB-3)
#
# Le refresh du SDK est passif : il n'a lieu qu'au passage d'une requête. Un
# upstream inutilisé assez longtemps perd son access token PUIS son refresh
# token, et redemande une autorisation manuelle. C'est cette inactivité — et
# elle seule — que la boucle couvre.
# ---------------------------------------------------------------------------

def _store_token(tmp_path, name="remote", *, expires_in, refresh="r",
                 lifetime=3600):
    """`expires_in` = ce qu'il RESTE ; `lifetime` = ce que l'AS a émis.

    Les deux sont distincts et tous deux nécessaires : la marge de
    renouvellement se calibre sur la durée de vie émise, pas sur le reliquat.
    """
    tokens = {"access_token": "a", "token_type": "Bearer", "expires_in": lifetime}
    if refresh:
        tokens["refresh_token"] = refresh
    (tmp_path / "t.json").write_text(json.dumps({
        name: {
            "tokens": tokens,
            "expires_at": time.time() + expires_in,
            "lifetime": lifetime,
        }
    }))


@pytest.mark.anyio
async def test_a_token_valid_for_a_long_time_is_left_alone(tmp_path):
    """Une boucle de fond qui renouvelle sans raison ferait tourner
    inutilement un AS à rotation, et multiplierait les écritures."""
    _store_token(tmp_path, expires_in=mcp_proxy._REFRESH_MARGIN_S + 3600)
    authorizer = _authorizer(tmp_path, interactive=False)

    with patch("httpx.AsyncClient") as client:
        assert await authorizer.refresh_if_due() is False
    client.assert_not_called()


@pytest.mark.anyio
async def test_a_token_without_refresh_token_is_not_chased(tmp_path):
    """Rien à renouveler : c'est une autorisation à refaire, pas un refresh à
    tenter, et les surfaces le disent déjà."""
    _store_token(tmp_path, expires_in=60, refresh=None)
    authorizer = _authorizer(tmp_path, interactive=False)

    with patch("httpx.AsyncClient") as client:
        assert await authorizer.refresh_if_due() is False
    client.assert_not_called()


@pytest.mark.anyio
async def test_a_token_about_to_expire_is_renewed_through_the_provider(tmp_path):
    """Le renouvellement passe par le provider PARTAGÉ, sous le verrou du SDK :
    un second chemin d'écriture ferait voir un rejeu à un AS à rotation, qui
    révoquerait toute la famille de jetons."""
    _store_token(tmp_path, expires_in=60)
    authorizer = _authorizer(tmp_path, interactive=False)
    seen = {}

    async def _post(*a, **kw):
        # Ce que ferait le SDK au passage de la requête : renouveler et écrire.
        _store_token(tmp_path, expires_in=mcp_proxy._REFRESH_MARGIN_S + 3600)
        return object()

    class _Client:
        def __init__(self, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        post = staticmethod(_post)

    with patch("httpx.AsyncClient", _Client):
        assert await authorizer.refresh_if_due() is True

    # L'auth passée au client est bien le provider partagé, pas un neuf.
    assert seen["auth"] is authorizer.provider()
    assert authorizer.authorization_pending is False


@pytest.mark.anyio
async def test_a_background_renewal_never_opens_an_interactive_flow(tmp_path):
    """Une boucle de fond ne doit JAMAIS ouvrir un parcours que personne n'a
    demandé : elle marque l'autorisation comme due et s'arrête là."""
    _store_token(tmp_path, expires_in=60)
    authorizer = _authorizer(tmp_path, interactive=False)

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise mcp_proxy.AuthorizationRequired("remote")

    with patch("httpx.AsyncClient", _Client):
        assert await authorizer.refresh_if_due() is False

    assert authorizer.interactive is False
    assert authorizer.authorization_pending is True


@pytest.mark.anyio
async def test_an_unreachable_as_does_not_cost_the_authorization(tmp_path):
    """Le jeton courant reste valable jusqu'à son terme : marquer
    l'autorisation comme due enverrait cliquer pour une panne réseau."""
    _store_token(tmp_path, expires_in=60)
    authorizer = _authorizer(tmp_path, interactive=False)

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise OSError("réseau injoignable")

    with patch("httpx.AsyncClient", _Client):
        assert await authorizer.refresh_if_due() is False

    assert authorizer.authorization_pending is False


def test_the_poll_interval_stays_under_the_shortest_margin():
    """L'INVARIANT : plusieurs réveils doivent tomber dans la fenêtre « bientôt
    expiré » de chaque upstream. Des constantes fixes ne peuvent pas le tenir —
    sur des jetons de 5 min la marge vaut 150 s, sous l'intervalle par défaut de
    300 s, et la fenêtre serait sautée en entier."""
    class _S:
        def __init__(self, lifetime):
            self._lifetime = lifetime

        def observed_lifetime(self):
            return self._lifetime

    class _A:
        def __init__(self, lifetime):
            self._storage = _S(lifetime)

    short = mcp_proxy._refresh_poll_interval({"jira": _A(300)})
    assert short <= 300 / 2 / 2

    # Un AS généreux n'est pas sondé plus souvent que le plafond.
    assert mcp_proxy._refresh_poll_interval({"jira": _A(3600)}) == (
        mcp_proxy._REFRESH_POLL_INTERVAL_S
    )
    # Rien d'observé encore : le plafond, pas une boucle serrée.
    assert mcp_proxy._refresh_poll_interval({}) == mcp_proxy._REFRESH_POLL_INTERVAL_S


@pytest.mark.anyio
async def test_a_short_lived_token_is_not_renewed_on_every_wake(tmp_path):
    """Une marge fixe de 15 min rendrait « bientôt expiré » tout jeton d'un AS
    qui en émet de 5 min, dès son émission : la boucle renouvellerait à chaque
    réveil, ce qui est le rejeu qu'on veut éviter devant un AS à rotation."""
    _store_token(tmp_path, expires_in=280, lifetime=300)
    authorizer = _authorizer(tmp_path, interactive=False)

    with patch("httpx.AsyncClient") as client:
        assert await authorizer.refresh_if_due() is False
    client.assert_not_called()


@pytest.mark.anyio
async def test_a_renewal_is_judged_on_the_deadline_moving(tmp_path):
    """Le témoin est que l'échéance ait AVANCÉ, jamais qu'elle dépasse une
    marge : exiger d'un jeton frais qu'il vive plus de 15 min classait en échec
    un renouvellement réussi sur un AS qui en émet de 5 — rien n'était
    journalisé, et la boucle recommençait au réveil suivant."""
    _store_token(tmp_path, expires_in=30, lifetime=300)
    authorizer = _authorizer(tmp_path, interactive=False)

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            # Renouvellement réussi : 300 s, bien en deçà de _REFRESH_MARGIN_S.
            _store_token(tmp_path, expires_in=300, lifetime=300)
            return object()

    with patch("httpx.AsyncClient", _Client):
        assert await authorizer.refresh_if_due() is True


@pytest.mark.anyio
async def test_the_issued_lifetime_is_remembered(tmp_path):
    """`expires_in` relu plus tard est ce qu'il RESTE : la durée de vie ne se
    lit qu'à l'émission, et c'est elle qui calibre la marge."""
    storage = UpstreamTokenStorage(tmp_path / "t.json", "jira")

    await storage.set_tokens(_token(expires_in=300, refresh_token="r"))

    assert storage.observed_lifetime() == 300


@pytest.mark.anyio
async def test_storing_a_token_without_refresh_is_said_out_loud(
    tmp_path, monkeypatch, capsys
):
    """Le savoir à l'obtention plutôt qu'à l'expiration : c'est la différence
    entre un réglage à corriger côté AS et une panne subie des heures plus
    tard."""
    monkeypatch.setattr(mcp_proxy, "_AUTH_DEBUG", True)
    storage = UpstreamTokenStorage(tmp_path / "t.json", "jira")

    await storage.set_tokens(_token(expires_in=3600))

    err = capsys.readouterr().err
    assert "SANS refresh_token" in err
    assert "offline_access" in err


@pytest.mark.anyio
async def test_storing_a_refreshable_token_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mcp_proxy, "_AUTH_DEBUG", True)
    storage = UpstreamTokenStorage(tmp_path / "t.json", "jira")

    await storage.set_tokens(_token(expires_in=3600, refresh_token="r"))

    err = capsys.readouterr().err
    assert "refresh_token présent" in err


# ---------------------------------------------------------------------------
# Le boot ne se contente pas de constater (lot AB-3)
# ---------------------------------------------------------------------------

class _BootAuthorizer:
    """Authorizer minimal : `has_usable_token` et `refresh_if_due` observés."""

    def __init__(self, usable=True):
        self.name = "jira"
        self.authorization_pending = False
        self.refreshed = False
        self._storage = self

        async def _probe():
            return usable

        self.has_usable_token = _probe

    async def refresh_if_due(self):
        self.refreshed = True
        return True


async def _boot(authorizer):
    """Déroule le lifespan jusqu'au yield, puis le referme.

    Par le protocole ASGI : `build_app` rend le wrapper qui évite le 307 sur
    /mcp, pas l'app Starlette — c'est ce wrapper que sert uvicorn, donc c'est
    lui qu'on démarre ici."""
    import anyio

    app = mcp_proxy.build_app(
        mcp_proxy.build_proxy_server({}, {}), {},
        authorizers={"jira": authorizer},
    )
    send_stream, receive_stream = anyio.create_memory_object_stream(8)
    events = []

    async def receive():
        return await receive_stream.receive()

    async def send(message):
        events.append(message)

    async with anyio.create_task_group() as tg:
        tg.start_soon(app, {"type": "lifespan"}, receive, send)
        await send_stream.send({"type": "lifespan.startup"})
        while not any(e["type"].startswith("lifespan.startup.") for e in events):
            await anyio.sleep(0)
        await send_stream.send({"type": "lifespan.shutdown"})


@pytest.mark.anyio
async def test_an_expiring_token_is_renewed_at_boot():
    """« Utilisable » inclut un jeton EXPIRÉ porteur d'un refresh token :
    utilisable au sens où il se renouvelle sans l'utilisateur, pas au sens où
    il partirait tel quel. Sans renouvellement au boot, le proxy annonce un
    upstream disponible dont le premier appel d'outil échoue — mesuré sur le
    terrain le 2026-09-08."""
    authorizer = _BootAuthorizer(usable=True)

    await _boot(authorizer)

    assert authorizer.refreshed is True
    assert authorizer.authorization_pending is False


@pytest.mark.anyio
async def test_nothing_is_renewed_when_there_is_no_token():
    """Rien à renouveler : c'est une autorisation à demander, et la tenter
    ferait une requête sortante inutile au démarrage."""
    authorizer = _BootAuthorizer(usable=False)

    await _boot(authorizer)

    assert authorizer.refreshed is False
    assert authorizer.authorization_pending is True


# ---------------------------------------------------------------------------
# Le jeton relu ne doit pas se faire passer pour un jeton frais
#
# `get_tokens()` écrase `expires_in` par le RESTANT (le SDK ne repasse pas par
# update_token_expiry() au chargement). Le SDK garde cet objet dégradé en
# mémoire et il lui arrive de le réécrire tel quel : le prendre pour une
# émission donnait `lifetime: 0` et une échéance dans le passé — « valide 0s »
# sur un jeton tout juste renouvelé, mesuré le 2026-09-08.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_rewriting_a_stale_token_does_not_erase_the_lifetime(tmp_path):
    """Une durée de vie ne rétrécit jamais : seule la plus longue vue compte,
    ce qui ignore les réécritures dégradées sans deviner d'où vient l'objet."""
    _store_token(tmp_path, "jira", expires_in=-10, lifetime=300)
    storage = UpstreamTokenStorage(tmp_path / "t.json", "jira")

    stale = await storage.get_tokens()
    assert stale.expires_in == 0        # le restant, pas la durée de vie
    await storage.set_tokens(stale)

    assert storage.observed_lifetime() == 300


@pytest.mark.anyio
async def test_rewriting_the_same_token_never_moves_its_deadline_back(tmp_path):
    """Le seul cas où une échéance doit se rapprocher est une révocation, qui
    ne passe pas par ici."""
    _store_token(tmp_path, "jira", expires_in=120, lifetime=300)
    storage = UpstreamTokenStorage(tmp_path / "t.json", "jira")
    before = json.loads((tmp_path / "t.json").read_text())["jira"]["expires_at"]

    reloaded = await storage.get_tokens()
    await storage.set_tokens(reloaded)

    after = json.loads((tmp_path / "t.json").read_text())["jira"]["expires_at"]
    assert after >= before


@pytest.mark.anyio
async def test_a_genuinely_new_token_carries_its_own_deadline(tmp_path):
    """La garde ne doit pas figer l'échéance : un jeton DIFFÉRENT repart de
    celle qu'il annonce, y compris plus courte."""
    _store_token(tmp_path, "jira", expires_in=3000, lifetime=3000)
    storage = UpstreamTokenStorage(tmp_path / "t.json", "jira")

    await storage.set_tokens(
        _token(access_token="tout-autre", expires_in=300, refresh_token="r2")
    )

    renewed = await storage.get_tokens()
    assert 290 <= renewed.expires_in <= 300


# ---------------------------------------------------------------------------
# Un refresh qui échoue est MUET : ni exception, ni fichier modifié
#
# Le SDK appelle `clear_tokens()` (contexte seulement), repose `_initialized`
# et laisse partir la requête SANS en-tête Authorization. Sur un upstream qui
# accepte `initialize` sans jeton, la sonde répond 200 : l'échec ne se voit
# nulle part, sauf dans le contexte du provider.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_silently_refused_refresh_is_not_a_success(tmp_path):
    """Se fier au fichier faisait annoncer « renouvelé » et lever le drapeau
    alors que rien n'était autorisé — l'échec ressortait au premier
    tools/call."""
    _store_token(tmp_path, expires_in=-10, lifetime=300)
    authorizer = _authorizer(tmp_path, interactive=False)

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            # Ce que fait le SDK quand le refresh est refusé : il a chargé les
            # jetons (_initialized), puis vide le CONTEXTE sans toucher au
            # fichier, et n'élève rien.
            provider = authorizer.provider()
            provider._initialized = True
            provider.context.current_tokens = None
            return object()

    with patch("httpx.AsyncClient", _Client):
        assert await authorizer.refresh_if_due() is False

    assert authorizer.authorization_pending is True


@pytest.mark.anyio
async def test_a_token_still_expired_afterwards_is_not_a_renewal(tmp_path):
    """Partant d'un jeton expiré, une échéance qui « avance » jusqu'à
    maintenant n'est pas un renouvellement : `valide 0s` était annoncé sur un
    jeton mort."""
    _store_token(tmp_path, expires_in=-10, lifetime=300)
    authorizer = _authorizer(tmp_path, interactive=False)
    # Contexte intact et chargé : seul le stockage décide ici.
    provider = authorizer.provider()
    provider._initialized = True
    provider.context.current_tokens = _token(refresh_token="r")

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            _store_token(tmp_path, expires_in=0, lifetime=300)
            return object()

    with patch("httpx.AsyncClient", _Client):
        assert await authorizer.refresh_if_due() is False

    assert authorizer.authorization_pending is True
