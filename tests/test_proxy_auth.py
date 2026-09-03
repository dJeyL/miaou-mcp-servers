"""Tests de l'auth OAuth entrante du proxy (lot AB-1).

Le proxy est un *Resource Server* : il vérifie des jetons, il n'en émet jamais.
Deux propriétés gouvernent ces tests :

- sans clé `auth` dans la config, RIEN ne change (non-régression de la boucle
  de développement locale MIAOU ↔ proxy sans jeton) ;
- avec auth, le 401 doit porter de quoi trouver où s'authentifier — c'est
  précisément ce que le proxy ne savait pas faire avant ce lot.
"""
import sys
from pathlib import Path

import asyncio
import contextlib
from contextlib import asynccontextmanager

import httpx
import pytest

_ROOT = Path(__file__).parent.parent
_SERVERS = _ROOT / "servers"
for p in (_ROOT, _SERVERS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import mcp_proxy
from mcp_proxy import (
    AuthConfigError,
    build_app,
    build_proxy_server,
    resolve_auth_config,
)

_ISSUER = "http://127.0.0.1:8787"


class LifespanManager:
    """Démarre/arrête le lifespan d'une app ASGI. Évite une dépendance de test
    supplémentaire (asgi-lifespan) pour une poignée de lignes."""

    def __init__(self, app):
        self.app = app
        self._recv: asyncio.Queue = asyncio.Queue()
        self._sent: asyncio.Queue = asyncio.Queue()
        self._task = None

    async def __aenter__(self):
        async def _run():
            await self.app(
                {"type": "lifespan", "asgi": {"version": "3.0"}},
                self._recv.get,
                self._sent.put,
            )

        self._task = asyncio.create_task(_run())
        await self._recv.put({"type": "lifespan.startup"})
        msg = await self._sent.get()
        assert msg["type"] == "lifespan.startup.complete", msg
        return self.app

    async def __aexit__(self, *exc):
        await self._recv.put({"type": "lifespan.shutdown"})
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._sent.get(), timeout=5)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        return False


def _auth_cfg(**over):
    cfg = {"auth": {"issuer_url": _ISSUER}}
    cfg["auth"].update(over)
    return cfg


def _app(auth=None, token_verifier=None):
    upstreams: dict = {}
    server = build_proxy_server(upstreams, {})
    return build_app(server, upstreams, auth=auth, token_verifier=token_verifier)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@asynccontextmanager
async def _running_client(app):
    """Client sur une app dont le LIFESPAN a démarré.

    `StreamableHTTPSessionManager` lève « Task group is not initialized » tant
    que `session_manager.run()` n'a pas tourné — il vit dans le lifespan de
    build_app. Les tests qui n'atteignent jamais la couche MCP (401 posé en
    amont, route well-known) n'en ont pas besoin ; ceux qui la traversent, si.
    """
    async with LifespanManager(app):
        async with _client(app) as c:
            yield c


# ---------------------------------------------------------------------------
# resolve_auth_config — fonction pure
# ---------------------------------------------------------------------------

def test_auth_absent_from_config_disables_auth():
    assert resolve_auth_config({"port": 8765}) is None


def test_auth_disabled_flag_respected():
    assert resolve_auth_config(_auth_cfg(_disabled=True)) is None


def test_no_auth_cli_overrides_config():
    assert resolve_auth_config(_auth_cfg(), cli_auth=False) is None


def test_auth_cli_without_config_key_is_an_error():
    with pytest.raises(AuthConfigError):
        resolve_auth_config({"port": 8765}, cli_auth=True)


def test_auth_cli_forces_disabled_config():
    resolved = resolve_auth_config(_auth_cfg(_disabled=True), cli_auth=True)
    assert resolved is not None


def test_missing_issuer_url_is_an_error():
    with pytest.raises(AuthConfigError):
        resolve_auth_config({"auth": {}})


def test_resource_url_derived_from_listen_address():
    resolved = resolve_auth_config(_auth_cfg(), host="127.0.0.1", port=8765)
    assert resolved["resource_url"] == "http://127.0.0.1:8765/mcp"


def test_resource_url_default_targets_mcp_endpoint_not_root():
    """RFC 8707 : le client renvoie cette URL en paramètre `resource`, et c'est
    elle qu'on comparera à l'audience. Elle doit désigner l'endpoint MCP."""
    resolved = resolve_auth_config(_auth_cfg())
    assert resolved["resource_url"].endswith("/mcp")


def test_wildcard_listen_host_falls_back_to_loopback():
    """0.0.0.0 n'est l'adresse de personne : inutilisable comme identité
    publique renvoyée aux clients."""
    resolved = resolve_auth_config(_auth_cfg(), host="0.0.0.0", port=8765)
    assert resolved["resource_url"] == "http://127.0.0.1:8765/mcp"


def test_explicit_resource_url_wins_over_derivation():
    resolved = resolve_auth_config(_auth_cfg(resource_url="https://pub.example/mcp"))
    assert resolved["resource_url"] == "https://pub.example/mcp"


def test_authorization_servers_default_to_issuer():
    resolved = resolve_auth_config(_auth_cfg())
    assert resolved["authorization_servers"] == [_ISSUER]


def test_required_scopes_default_to_empty_list():
    assert resolve_auth_config(_auth_cfg())["required_scopes"] == []


def test_required_scopes_must_be_a_list():
    with pytest.raises(AuthConfigError):
        resolve_auth_config(_auth_cfg(required_scopes="read"))


# ---------------------------------------------------------------------------
# Auth désactivée — non-régression
# ---------------------------------------------------------------------------

async def test_without_auth_no_well_known_route():
    async with _client(_app(auth=None)) as c:
        r = await c.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 404


async def test_without_auth_mcp_endpoint_is_not_401():
    """Sans auth, l'endpoint MCP répond au transport comme avant le lot — un 406
    (Accept manquant) prouve qu'on a atteint la couche MCP, pas un mur d'auth."""
    async with _running_client(_app(auth=None)) as c:
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code != 401


# ---------------------------------------------------------------------------
# Auth active
# ---------------------------------------------------------------------------

async def test_well_known_is_public_when_auth_enabled():
    """Un client non authentifié DOIT pouvoir lire la découverte : c'est tout
    son objet. L'envelopper dans l'auth ferme la boucle."""
    auth = resolve_auth_config(_auth_cfg())
    async with _client(_app(auth=auth)) as c:
        r = await c.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    body = r.json()
    assert body["authorization_servers"] == [_ISSUER + "/"]
    assert body["resource"].endswith("/mcp")


async def test_mcp_without_token_is_401():
    auth = resolve_auth_config(_auth_cfg())
    async with _client(_app(auth=auth)) as c:
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401


async def test_401_carries_resource_metadata_pointer():
    """LE test du lot. Sans ce pointeur le client reçoit un 401 nu et ne sait
    pas où aller — c'est le symptôme mesuré sur mistral-vibe avant AB-1.

    La branche du SDK qui ajoute `resource_metadata` porte un
    `# pragma: no cover` : non couverte en amont, donc épinglée ici.
    """
    auth = resolve_auth_config(_auth_cfg())
    async with _client(_app(auth=auth)) as c:
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    header = r.headers["www-authenticate"]
    assert header.startswith("Bearer ")
    assert 'error="invalid_token"' in header
    assert "resource_metadata=" in header
    assert "/.well-known/oauth-protected-resource/mcp" in header


async def test_resource_metadata_pointer_is_fetchable():
    """Le pointeur du 401 doit désigner une route qui existe réellement : un
    pointeur vers un 404 est aussi inutile qu'un 401 nu."""
    auth = resolve_auth_config(_auth_cfg())
    app = _app(auth=auth)
    async with _client(app) as c:
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        url = r.headers["www-authenticate"].split('resource_metadata="')[1].split('"')[0]
        assert (await c.get(httpx.URL(url).path)).status_code == 200


async def test_invalid_token_is_401_not_500():
    """Un jeton refusé est un 401, jamais une erreur serveur."""
    auth = resolve_auth_config(_auth_cfg())
    async with _client(_app(auth=auth)) as c:
        r = await c.post(
            "/mcp",
            headers={"Authorization": "Bearer nimportequoi"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# CORS — ce qui fait marcher MIAOU en file://
# ---------------------------------------------------------------------------

async def test_www_authenticate_is_cors_exposed():
    """Sans expose_headers, un client navigateur cross-origin ne peut PAS lire
    le header du 401. Invisible en test curl, fatal en usage réel."""
    auth = resolve_auth_config(_auth_cfg())
    async with _client(_app(auth=auth)) as c:
        r = await c.post(
            "/mcp",
            headers={"Origin": "null"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
    exposed = r.headers.get("access-control-expose-headers", "")
    assert "WWW-Authenticate" in exposed
    assert "Mcp-Session-Id" in exposed


async def test_null_origin_is_allowed_and_credentials_stay_off():
    """MIAOU ouvert en file:// envoie `Origin: null`. C'est allow_origins=["*"]
    SANS allow_credentials qui le fait passer — le combo des deux est interdit
    par la spec CORS. Non-régression du scénario cible entier.
    """
    for auth in (None, resolve_auth_config(_auth_cfg())):
        async with _running_client(_app(auth=auth)) as c:
            r = await c.post(
                "/mcp",
                headers={"Origin": "null"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            )
        assert r.headers.get("access-control-allow-origin") == "*"
        assert "access-control-allow-credentials" not in r.headers


# ---------------------------------------------------------------------------
# JwtTokenVerifier — validation d'audience (AB-1.2)
#
# Le cœur du lot. `BearerAuthBackend` (SDK) appelle le verifier puis re-vérifie
# seulement `expires_at` : il ne regarde JAMAIS `AccessToken.resource`. Sans ce
# qui est testé ici, un jeton parfaitement valide émis pour un AUTRE Resource
# Server serait accepté — la confused deputy que RFC 8707 existe pour empêcher.
# ---------------------------------------------------------------------------

_RESOURCE = "http://127.0.0.1:8765/mcp"


@pytest.fixture(scope="module")
def rsa_key():
    """Paire RSA de test, générée une fois pour le module (la génération 2048
    bits coûte ~100 ms — la refaire par test se paierait)."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _issue(rsa_key, **claims):
    """Émet un JWT signé, avec des claims valides par défaut que chaque test
    surcharge sur le seul axe qu'il éprouve."""
    import time

    import jwt

    payload = {
        "iss": _ISSUER,
        "aud": _RESOURCE,
        "sub": "user-1",
        "client_id": "client-42",
        "exp": int(time.time()) + 300,
        "scope": "mcp:read mcp:write",
    }
    payload.update(claims)
    return jwt.encode(payload, rsa_key, algorithm="RS256")


def _verifier(rsa_key, resource_url=_RESOURCE, **over):
    """Verifier dont le JWKS est court-circuité : la clef publique est injectée
    directement. On teste NOTRE logique de vérification, pas la capacité de
    pyjwt à télécharger un JWKS — et surtout aucun test ne touche le réseau."""
    v = mcp_proxy.JwtTokenVerifier(
        issuer_url=_ISSUER, resource_url=resource_url, **over
    )

    class _StubJwkClient:
        @staticmethod
        def get_signing_key_from_jwt(token):
            class _K:
                key = rsa_key.public_key()

            return _K()

    v._jwk_client = _StubJwkClient()
    return v


async def test_valid_token_is_accepted(rsa_key):
    token = _issue(rsa_key)
    at = await _verifier(rsa_key).verify_token(token)
    assert at is not None
    assert at.client_id == "client-42"
    assert at.subject == "user-1"
    assert at.scopes == ["mcp:read", "mcp:write"]


async def test_foreign_audience_is_refused(rsa_key):
    """LE test qui justifie le lot : signature bonne, jeton non expiré, mais
    l'`aud` désigne un autre Resource Server."""
    token = _issue(rsa_key, aud="http://autre-serveur.example/mcp")
    assert await _verifier(rsa_key).verify_token(token) is None


async def test_audience_is_not_matched_by_prefix(rsa_key):
    """Comparaison EXACTE : une audience plus longue qui commence par la nôtre
    ne doit pas passer. Un match par sous-chaîne rendrait la vérification
    contournable par le simple choix d'un chemin."""
    token = _issue(rsa_key, aud=_RESOURCE + "-attacker")
    assert await _verifier(rsa_key).verify_token(token) is None


async def test_audience_list_containing_us_is_accepted(rsa_key):
    """`aud` peut être une liste (RFC 7519 §4.1.3)."""
    token = _issue(rsa_key, aud=["http://autre.example/mcp", _RESOURCE])
    assert await _verifier(rsa_key).verify_token(token) is not None


async def test_audience_trailing_slash_is_tolerated(rsa_key):
    """Les AS ne s'accordent pas sur le slash final ; c'est la seule
    normalisation admise."""
    token = _issue(rsa_key, aud=_RESOURCE + "/")
    assert await _verifier(rsa_key).verify_token(token) is not None


async def test_missing_audience_is_refused(rsa_key):
    """Pas d'`aud` du tout = pas d'audience vérifiable = refus. Un jeton sans
    audience est utilisable partout, ce que RFC 8707 interdit précisément."""
    token = _issue(rsa_key)
    import jwt

    payload = jwt.decode(token, options={"verify_signature": False})
    payload.pop("aud")
    unbound = jwt.encode(payload, rsa_key, algorithm="RS256")
    assert await _verifier(rsa_key).verify_token(unbound) is None


async def test_expired_token_is_refused(rsa_key):
    import time

    token = _issue(rsa_key, exp=int(time.time()) - 10)
    assert await _verifier(rsa_key).verify_token(token) is None


async def test_token_without_exp_is_refused(rsa_key):
    """`exp` est exigé (`require`), sinon un jeton sans expiration serait
    éternel — et `BearerAuthBackend` ne peut pas le rattraper : il ne teste
    `expires_at` que s'il est renseigné."""
    import jwt

    token = jwt.encode(
        {"iss": _ISSUER, "aud": _RESOURCE, "sub": "u"}, rsa_key, algorithm="RS256"
    )
    assert await _verifier(rsa_key).verify_token(token) is None


async def test_foreign_issuer_is_refused(rsa_key):
    token = _issue(rsa_key, iss="http://autre-as.example")
    assert await _verifier(rsa_key).verify_token(token) is None


async def test_token_signed_by_another_key_is_refused(rsa_key):
    """Signature invalide : la clef de l'AS légitime ne la vérifie pas."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    import jwt

    intruder = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    payload = {
        "iss": _ISSUER,
        "aud": _RESOURCE,
        "sub": "u",
        "exp": __import__("time").time() + 300,
    }
    token = jwt.encode(payload, intruder, algorithm="RS256")
    assert await _verifier(rsa_key).verify_token(token) is None


async def test_alg_none_is_refused(rsa_key):
    """`alg: none` — l'attaque classique sur les vérificateurs JWT permissifs.
    La liste d'algorithmes est fermée, pas déduite de l'en-tête du jeton."""
    import jwt

    token = jwt.encode(
        {
            "iss": _ISSUER,
            "aud": _RESOURCE,
            "sub": "u",
            "exp": __import__("time").time() + 300,
        },
        key="",
        algorithm="none",
    )
    assert await _verifier(rsa_key).verify_token(token) is None


async def test_garbage_token_returns_none_not_exception(rsa_key):
    """Aucune exception ne doit sortir de verify_token : elle remonterait en
    500 alors qu'un jeton invalide est un 401."""
    assert await _verifier(rsa_key).verify_token("pas-un-jwt") is None


async def test_access_token_carries_the_verified_resource(rsa_key):
    """`AccessToken.resource` porte l'audience qu'on a RÉELLEMENT vérifiée,
    pas celle que le jeton revendique — un consommateur en aval ne doit pas
    avoir à refaire la comparaison."""
    at = await _verifier(rsa_key).verify_token(_issue(rsa_key))
    assert at.resource == _RESOURCE


def test_build_token_verifier_wires_the_config():
    """Le verifier construit depuis la config vise bien la ressource résolue —
    sinon toute la validation d'audience porterait sur la mauvaise identité."""
    auth = resolve_auth_config(_auth_cfg(), port=8765)
    v = mcp_proxy.build_token_verifier(auth)
    assert isinstance(v, mcp_proxy.JwtTokenVerifier)
    assert v.resource_url == auth["resource_url"]
    assert v.issuer_url == _ISSUER


def test_audience_matches_helper_is_exact():
    """Fonction pure, testée directement sur ses cas limites."""
    m = mcp_proxy._audience_matches
    assert m("http://a/mcp", "http://a/mcp")
    assert m(["http://b", "http://a/mcp"], "http://a/mcp")
    assert m("http://a/mcp/", "http://a/mcp")
    assert m("http://a/mcp", "http://a/mcp/")
    assert m("http://a/mcp-x", "http://a/mcp") is False
    assert m("http://a", "http://a/mcp") is False
    assert m(None, "http://a/mcp") is False
    assert m([], "http://a/mcp") is False


# --- bout en bout : le jeton traverse réellement le middleware -------------

async def test_valid_token_passes_the_middleware(rsa_key):
    """Le verifier branché dans build_app laisse effectivement passer : la
    requête atteint la couche MCP au lieu d'être arrêtée en 401."""
    auth = resolve_auth_config(_auth_cfg())
    app = _app(auth=auth, token_verifier=_verifier(rsa_key, resource_url=auth["resource_url"]))
    token = _issue(rsa_key, aud=auth["resource_url"])
    async with _running_client(app) as c:
        r = await c.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )
    assert r.status_code == 200
    assert r.headers.get("mcp-session-id")


async def test_foreign_audience_is_401_through_the_middleware(rsa_key):
    """Le pendant HTTP du test d'audience : refusé par le verifier ⇒ 401
    porteur du pointeur de découverte, pas une erreur serveur."""
    auth = resolve_auth_config(_auth_cfg())
    app = _app(auth=auth, token_verifier=_verifier(rsa_key, resource_url=auth["resource_url"]))
    token = _issue(rsa_key, aud="http://autre-serveur.example/mcp")
    async with _client(app) as c:
        r = await c.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers.get("www-authenticate", "")


# --- découverte du JWKS ----------------------------------------------------

def test_configured_jwks_uri_skips_discovery():
    """Un `jwks_uri` en config évite tout appel de découverte."""
    auth = resolve_auth_config(_auth_cfg(jwks_uri="http://as.example/jwks"))
    v = mcp_proxy.build_token_verifier(auth)
    assert v._configured_jwks_uri == "http://as.example/jwks"


def test_discovery_probes_both_well_known_paths(monkeypatch):
    """RFC 8414 d'abord, puis OpenID Connect Discovery : un AS OIDC ne sert
    souvent que le second."""
    seen = []

    class _Resp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=None):
        seen.append(url)
        if url.endswith("/oauth-authorization-server"):
            raise OSError("404")
        return _Resp(b'{"jwks_uri": "http://as.example/keys"}')

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert mcp_proxy._discover_jwks_uri(_ISSUER) == "http://as.example/keys"
    assert seen == [
        f"{_ISSUER}/.well-known/oauth-authorization-server",
        f"{_ISSUER}/.well-known/openid-configuration",
    ]


def test_discovery_failure_is_reported(monkeypatch):
    def fake_urlopen(url, timeout=None):
        raise OSError("injoignable")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(mcp_proxy.JwtAudienceError):
        mcp_proxy._discover_jwks_uri(_ISSUER)


async def test_unreachable_jwks_refuses_instead_of_crashing(rsa_key, monkeypatch):
    """AS injoignable = refus (401), jamais une 500. Le proxy ne doit pas
    tomber parce que son AS dort."""
    def fake_urlopen(url, timeout=None):
        raise OSError("injoignable")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    v = mcp_proxy.JwtTokenVerifier(issuer_url=_ISSUER, resource_url=_RESOURCE)
    assert await v.verify_token(_issue(rsa_key)) is None


# ---------------------------------------------------------------------------
# --with-dev-auth : les deux serveurs dans un process (lot AB-1.4)
#
# Confort de banc d'essai. Ce qu'on protège ici : DEUX ports, donc deux
# origines. Le proxy reste un Resource Server qui vérifie, l'AS reste seul à
# émettre — le mode combiné est une commodité de lancement, pas une fusion.
# ---------------------------------------------------------------------------

def test_dev_auth_flag_makes_the_dev_server_the_issuer():
    """--with-dev-auth suffit : pas besoin d'éditer config.json."""
    cfg = {"port": 8765}
    cfg = {**cfg, "auth": {"issuer_url": "http://127.0.0.1:9001"}}
    auth = resolve_auth_config(cfg, cli_auth=True, port=8765)
    assert auth is not None
    assert auth["issuer_url"] == "http://127.0.0.1:9001"


def test_explicit_auth_config_wins_over_the_dev_server():
    """Une clé `auth` déjà présente n'est pas écrasée par la commodité : la
    config explicite de l'utilisateur prime toujours."""
    cfg = {"port": 8765, "auth": {"issuer_url": "https://as.exemple.test"}}
    auth = resolve_auth_config(cfg, cli_auth=True, port=8765)
    assert auth["issuer_url"] == "https://as.exemple.test"


def test_proxy_and_dev_auth_keep_distinct_origins():
    """Deux ports = deux origines. Si l'AS était monté DANS l'app du proxy, son
    issuer deviendrait une sous-route et la frontière Resource Server /
    Authorization Server s'effacerait."""
    auth = resolve_auth_config(
        {"port": 8799, "auth": {"issuer_url": "http://127.0.0.1:9001"}},
        cli_auth=True,
        port=8799,
    )
    assert auth["issuer_url"] == "http://127.0.0.1:9001"
    assert auth["resource_url"] == "http://127.0.0.1:8799/mcp"
    assert auth["issuer_url"] not in auth["resource_url"]


def test_run_with_dev_auth_is_exposed():
    """Le point d'entrée du mode combiné existe et prend l'issuer en argument —
    il ne le redevine pas de son côté (deux formules divergeraient)."""
    import inspect

    sig = inspect.signature(mcp_proxy.run_with_dev_auth)
    assert {"host", "port", "dev_auth_port", "issuer_url", "auto_approve"} <= set(
        sig.parameters
    )


# ---------------------------------------------------------------------------
# required_scopes — le 403 « insufficient_scope » (dette AB-1 soldée)
#
# Porté par la config depuis AB-1.1 et transmis à RequireAuthMiddleware, il
# n'était éprouvé par aucun test : l'AS de développement n'émettait pas encore
# de scopes quand ce chemin a été écrit, le cas était donc inatteignable. Il ne
# l'est plus — et le client OAuth du SDK (AB-2) sait désormais réagir à ce 403
# par un step-up, donc ce que ces tests figent est un contrat vivant, plus une
# branche dormante.
#
# La chaîne complète, qu'aucun test unitaire ne couvrait de bout en bout :
#   claim `scope` du JWT
#     → AccessToken.scopes (notre verify_token)
#     → AuthCredentials (BearerAuthBackend du SDK)
#     → comparaison de RequireAuthMiddleware
# Chaque maillon pouvait être juste isolément avec le raccord faux.
# ---------------------------------------------------------------------------

async def test_token_missing_a_required_scope_gets_403(rsa_key):
    """403, pas 401 : le jeton est parfaitement valide, c'est l'autorisation qui
    manque, pas l'authentification. Un 401 renverrait le client vers un nouveau
    parcours au lieu de lui faire demander le bon scope."""
    auth = resolve_auth_config(_auth_cfg(required_scopes=["mcp:admin"]))
    token = _issue(rsa_key, scope="mcp:read mcp:write")

    async with _running_client(
        _app(auth=auth, token_verifier=_verifier(rsa_key))
    ) as c:
        r = await c.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )

    assert r.status_code == 403
    www = r.headers.get("www-authenticate", "")
    assert "insufficient_scope" in www
    # Le scope manquant est NOMMÉ : c'est ce qui permet au client SDK de
    # redemander le bon (step-up), plutôt que de deviner.
    assert "mcp:admin" in www


async def test_token_carrying_the_required_scope_passes(rsa_key):
    """Le pendant du test précédent : sans lui, un 403 permanent — dû à
    n'importe quelle rupture de la chaîne — passerait pour un succès."""
    auth = resolve_auth_config(_auth_cfg(required_scopes=["mcp:read"]))
    token = _issue(rsa_key, scope="mcp:read mcp:write")

    async with _running_client(
        _app(auth=auth, token_verifier=_verifier(rsa_key))
    ) as c:
        r = await c.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )

    assert r.status_code != 403
    assert r.status_code != 401


async def test_all_required_scopes_must_be_present(rsa_key):
    """Conjonction, pas disjonction : porter l'un des scopes exigés ne suffit
    pas. Le SDK boucle sur required_scopes et refuse au premier manquant."""
    auth = resolve_auth_config(
        _auth_cfg(required_scopes=["mcp:read", "mcp:admin"])
    )
    token = _issue(rsa_key, scope="mcp:read")

    async with _running_client(
        _app(auth=auth, token_verifier=_verifier(rsa_key))
    ) as c:
        r = await c.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )

    assert r.status_code == 403
    assert "mcp:admin" in r.headers.get("www-authenticate", "")


async def test_no_required_scopes_accepts_a_scopeless_token(rsa_key):
    """Le défaut, et le comportement de toute la boucle de développement : sans
    `required_scopes`, un jeton valide passe, scopes ou pas."""
    auth = resolve_auth_config(_auth_cfg())
    assert auth["required_scopes"] == []
    token = _issue(rsa_key, scope="")

    async with _running_client(
        _app(auth=auth, token_verifier=_verifier(rsa_key))
    ) as c:
        r = await c.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )

    assert r.status_code != 403


async def test_missing_scope_is_refused_before_authentication_is_judged(rsa_key):
    """Un jeton SANS scope du tout n'est pas mieux traité qu'un jeton au
    mauvais scope : l'absence du claim ne doit pas valoir passe-droit."""
    auth = resolve_auth_config(_auth_cfg(required_scopes=["mcp:read"]))
    import jwt as _jwt
    import time as _time

    # Émis à la main pour omettre le claim `scope`, que _issue pose toujours.
    token = _jwt.encode(
        {
            "iss": _ISSUER,
            "aud": _RESOURCE,
            "sub": "user-1",
            "client_id": "client-42",
            "exp": int(_time.time()) + 300,
        },
        rsa_key,
        algorithm="RS256",
    )

    async with _running_client(
        _app(auth=auth, token_verifier=_verifier(rsa_key))
    ) as c:
        r = await c.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )

    assert r.status_code == 403


# --- le joint claim → AccessToken.scopes ----------------------------------

async def test_verifier_exposes_scopes_the_middleware_can_compare(rsa_key):
    """Le maillon que les tests d'audience ne regardaient pas : le claim `scope`
    est une CHAÎNE séparée par des espaces (RFC 6749 §3.3), alors que
    AuthCredentials attend une liste. Une chaîne laissée telle quelle donnerait
    une comparaison caractère par caractère, donc un 403 permanent."""
    access = await _verifier(rsa_key).verify_token(
        _issue(rsa_key, scope="mcp:read mcp:write")
    )
    assert access.scopes == ["mcp:read", "mcp:write"]


async def test_verifier_tolerates_a_list_shaped_scope_claim(rsa_key):
    """Certains AS émettent `scope` en tableau plutôt qu'en chaîne. Ce n'est pas
    conforme, mais c'est fréquent, et le refuser coûterait plus que l'accepter."""
    access = await _verifier(rsa_key).verify_token(
        _issue(rsa_key, scope=["mcp:read", "mcp:write"])
    )
    assert access.scopes == ["mcp:read", "mcp:write"]


# --- la config -------------------------------------------------------------

def test_required_scopes_must_be_a_list():
    with pytest.raises(mcp_proxy.AuthConfigError, match="required_scopes"):
        resolve_auth_config(_auth_cfg(required_scopes="mcp:read"))
