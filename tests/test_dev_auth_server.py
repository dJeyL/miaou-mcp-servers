"""Tests du serveur d'autorisation de développement (lot AB-1.3).

Ce serveur n'est pas un morceau du produit : c'est le banc d'essai sans lequel
le parcours OAuth complet n'est éprouvable ni à la main ni en Playwright. Ce
qu'on teste ici est donc surtout ce dont les AUTRES dépendent :

- le jeton émis porte le bon `aud`, sinon la validation d'audience du proxy
  (AB-1.2) refuse tout et rien n'est testable en aval ;
- PKCE est bien imposé — la garantie vient du SDK, mais on en dépend, donc on
  l'épingle plutôt que de la supposer ;
- un code d'autorisation ne sert qu'une fois.
"""
import base64
import hashlib
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import dev_auth_server as das

_ISSUER = "http://127.0.0.1:8787"
_RESOURCE = "http://127.0.0.1:8765/mcp"
_REDIRECT = "http://127.0.0.1:5173/callback"


@pytest.fixture(scope="module")
def keys():
    """Générée une fois : 2048 bits coûtent ~100 ms."""
    return das.DevKeyPair()


def _make(keys, auto_approve=True):
    provider = das.DevAuthProvider(_ISSUER, keys, auto_approve=auto_approve)
    app = das.build_app(provider, keys, _ISSUER)
    return provider, app


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=_ISSUER
    )


def _pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


async def _register(c, name="client-de-test"):
    r = await c.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [_REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _authorize(c, client_id, challenge, resource=_RESOURCE, scope="mcp:read"):
    params = {
        "client_id": client_id,
        "redirect_uri": _REDIRECT,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "etat-42",
    }
    if resource:
        params["resource"] = resource
    if scope:
        params["scope"] = scope
    return await c.get("/authorize", params=params)


def _code_from(response):
    assert response.status_code in (302, 307), response.text
    return parse_qs(urlparse(response.headers["location"]).query)["code"][0]


# ---------------------------------------------------------------------------
# Métadonnées et clefs — ce par quoi le proxy découvre l'AS
# ---------------------------------------------------------------------------

async def test_metadata_advertises_jwks_uri(keys):
    """Le modèle OAuthMetadata du SDK n'a PAS de champ jwks_uri : sans notre
    route, le proxy ne pourrait jamais découvrir les clefs publiques."""
    _, app = _make(keys)
    async with _client(app) as c:
        r = await c.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    assert r.json()["jwks_uri"] == f"{_ISSUER}/jwks"


async def test_openid_configuration_serves_the_same_metadata(keys):
    """Un client conforme sonde aussi le chemin OIDC."""
    _, app = _make(keys)
    async with _client(app) as c:
        a = await c.get("/.well-known/oauth-authorization-server")
        b = await c.get("/.well-known/openid-configuration")
    assert a.json() == b.json()


async def test_metadata_announces_registration_and_s256(keys):
    _, app = _make(keys)
    async with _client(app) as c:
        doc = (await c.get("/.well-known/oauth-authorization-server")).json()
    assert doc["registration_endpoint"] == f"{_ISSUER}/register"
    assert doc["code_challenge_methods_supported"] == ["S256"]


async def test_jwks_exposes_a_usable_rsa_key(keys):
    _, app = _make(keys)
    async with _client(app) as c:
        doc = (await c.get("/jwks")).json()
    key = doc["keys"][0]
    assert key["kty"] == "RSA" and key["alg"] == "RS256"
    assert key["kid"] and key["n"] and key["e"]


def test_jwks_encoding_roundtrips(keys):
    """Les champs n/e sont du base64url sans padding (RFC 7518 §6.3) : mal
    encodés, aucun client ne peut reconstruire la clef."""
    import jwt

    jwk = keys.jwks()["keys"][0]
    reconstructed = jwt.PyJWK(jwk).key
    expected = keys.private_key.public_key().public_numbers()
    assert reconstructed.public_numbers().n == expected.n
    assert reconstructed.public_numbers().e == expected.e


# ---------------------------------------------------------------------------
# Enregistrement dynamique (RFC 7591)
# ---------------------------------------------------------------------------

async def test_dynamic_registration_returns_a_client_id(keys):
    """ClientRegistrationOptions(enabled=True) : le défaut du SDK est False,
    la DCR ne s'active pas toute seule."""
    _, app = _make(keys)
    async with _client(app) as c:
        info = await _register(c)
    assert info["client_id"]


async def test_registered_client_is_retrievable(keys):
    provider, app = _make(keys)
    async with _client(app) as c:
        info = await _register(c)
    assert await provider.get_client(info["client_id"]) is not None


# ---------------------------------------------------------------------------
# Parcours complet — c'est ce que le lot doit rendre possible
# ---------------------------------------------------------------------------

async def test_full_flow_yields_a_token_bound_to_the_resource(keys):
    """LE test du lot : de l'enregistrement au jeton, avec l'`aud` attendu par
    la validation d'audience du proxy."""
    import jwt

    _, app = _make(keys, auto_approve=True)
    verifier, challenge = _pkce()
    async with _client(app) as c:
        info = await _register(c)
        code = _code_from(await _authorize(c, info["client_id"], challenge))
        r = await c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _REDIRECT,
                "client_id": info["client_id"],
                "client_secret": info.get("client_secret", ""),
                "code_verifier": verifier,
            },
        )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["token_type"] == "Bearer"
    claims = jwt.decode(
        payload["access_token"],
        jwt.PyJWK(keys.jwks()["keys"][0]).key,
        algorithms=["RS256"],
        audience=_RESOURCE,
        issuer=_ISSUER,
    )
    assert claims["aud"] == _RESOURCE
    assert claims["iss"] == _ISSUER
    assert "exp" in claims


async def test_state_is_returned_to_the_client(keys):
    """Sans `state` renvoyé, le client ne peut pas se protéger du CSRF."""
    _, app = _make(keys)
    _, challenge = _pkce()
    async with _client(app) as c:
        info = await _register(c)
        r = await _authorize(c, info["client_id"], challenge)
    assert parse_qs(urlparse(r.headers["location"]).query)["state"] == ["etat-42"]


async def test_authorization_code_is_single_use(keys):
    """Rejouer un code doit échouer : sinon un code intercepté reste
    monnayable indéfiniment."""
    _, app = _make(keys)
    verifier, challenge = _pkce()
    async with _client(app) as c:
        info = await _register(c)
        code = _code_from(await _authorize(c, info["client_id"], challenge))
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT,
            "client_id": info["client_id"],
            "client_secret": info.get("client_secret", ""),
            "code_verifier": verifier,
        }
        first = await c.post("/token", data=data)
        second = await c.post("/token", data=data)
    assert first.status_code == 200
    assert second.status_code == 400


async def test_wrong_code_verifier_is_refused(keys):
    """PKCE : la vérification est faite par le SDK, mais on en DÉPEND — un test
    l'épingle plutôt que de la supposer."""
    _, app = _make(keys)
    _, challenge = _pkce()
    other_verifier, _ = _pkce()
    async with _client(app) as c:
        info = await _register(c)
        code = _code_from(await _authorize(c, info["client_id"], challenge))
        r = await c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _REDIRECT,
                "client_id": info["client_id"],
                "client_secret": info.get("client_secret", ""),
                "code_verifier": other_verifier,
            },
        )
    assert r.status_code == 400


async def test_plain_code_challenge_method_is_refused(keys):
    """`plain` réduit PKCE à rien. Le SDK le refuse par typage
    (Literal["S256"]) ; on épingle la garantie."""
    _, app = _make(keys)
    _, challenge = _pkce()
    async with _client(app) as c:
        info = await _register(c)
        r = await c.get(
            "/authorize",
            params={
                "client_id": info["client_id"],
                "redirect_uri": _REDIRECT,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "plain",
            },
        )
    assert r.status_code != 200 or "error" in r.text
    if r.status_code in (302, 307):
        assert "code=" not in r.headers["location"]


async def test_token_without_resource_carries_no_audience(keys):
    """Pas de `resource` demandée = pas d'`aud`. Le proxy refusera un tel jeton
    (AB-1.2 : un jeton sans audience est utilisable partout) — c'est le
    comportement voulu, pas un manque de cet AS."""
    import jwt

    _, app = _make(keys)
    verifier, challenge = _pkce()
    async with _client(app) as c:
        info = await _register(c)
        code = _code_from(
            await _authorize(c, info["client_id"], challenge, resource=None)
        )
        r = await c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _REDIRECT,
                "client_id": info["client_id"],
                "client_secret": info.get("client_secret", ""),
                "code_verifier": verifier,
            },
        )
    claims = jwt.decode(
        r.json()["access_token"], options={"verify_signature": False}
    )
    assert "aud" not in claims


# ---------------------------------------------------------------------------
# Rafraîchissement
# ---------------------------------------------------------------------------

async def test_refresh_token_rotates_both_tokens(keys):
    """Rotation des deux jetons : l'ancien refresh devient inutilisable, ce qui
    rend un rejeu détectable."""
    _, app = _make(keys)
    verifier, challenge = _pkce()
    async with _client(app) as c:
        info = await _register(c)
        code = _code_from(await _authorize(c, info["client_id"], challenge))
        first = (
            await c.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _REDIRECT,
                    "client_id": info["client_id"],
                    "client_secret": info.get("client_secret", ""),
                    "code_verifier": verifier,
                },
            )
        ).json()
        refreshed = await c.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                "client_id": info["client_id"],
                "client_secret": info.get("client_secret", ""),
            },
        )
        replayed = await c.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                "client_id": info["client_id"],
                "client_secret": info.get("client_secret", ""),
            },
        )
    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_token"] != first["refresh_token"]
    assert replayed.status_code == 400


# ---------------------------------------------------------------------------
# Consentement — le seul écart entre les deux modes
# ---------------------------------------------------------------------------

async def test_consent_screen_is_shown_without_auto_approve(keys):
    """Sans --auto-approve, /authorize mène à l'écran, pas directement au code."""
    _, app = _make(keys, auto_approve=False)
    _, challenge = _pkce()
    async with _client(app) as c:
        r = await _authorize(c, (await _register(c))["client_id"], challenge)
    assert r.status_code in (302, 307)
    location = r.headers["location"]
    assert "/consent?" in location
    assert "code=" not in location


async def test_consent_page_names_client_and_resource(keys):
    """L'écran doit dire QUI demande quoi — un consentement qui ne nomme pas
    l'application n'en est pas un."""
    _, app = _make(keys, auto_approve=False)
    _, challenge = _pkce()
    async with _client(app) as c:
        r = await _authorize(c, (await _register(c, "vibe-cli"))["client_id"], challenge)
        page = await c.get(r.headers["location"])
    assert page.status_code == 200
    assert "vibe-cli" in page.text
    assert _RESOURCE in page.text


async def test_approving_consent_redirects_with_a_code(keys):
    _, app = _make(keys, auto_approve=False)
    _, challenge = _pkce()
    async with _client(app) as c:
        r = await _authorize(c, (await _register(c))["client_id"], challenge)
        handle = parse_qs(urlparse(r.headers["location"]).query)["handle"][0]
        done = await c.post("/consent", data={"handle": handle})
    assert done.status_code == 302
    assert "code=" in done.headers["location"]
    assert done.headers["location"].startswith(_REDIRECT)


async def test_consent_handle_is_single_use(keys):
    """Revalider un écran déjà traité ne doit pas émettre un second code."""
    _, app = _make(keys, auto_approve=False)
    _, challenge = _pkce()
    async with _client(app) as c:
        r = await _authorize(c, (await _register(c))["client_id"], challenge)
        handle = parse_qs(urlparse(r.headers["location"]).query)["handle"][0]
        await c.post("/consent", data={"handle": handle})
        again = await c.post("/consent", data={"handle": handle})
    assert again.status_code == 400


async def test_unknown_consent_handle_is_not_found(keys):
    _, app = _make(keys, auto_approve=False)
    async with _client(app) as c:
        r = await c.get("/consent", params={"handle": "inconnu"})
    assert r.status_code == 404


def test_auto_approve_is_the_only_divergence(keys):
    """Les deux modes partagent le même provider et la même émission : seul le
    consentement diffère. Dupliquer les composants ferait diverger la fixture
    Playwright de ce qu'on éprouve à la main."""
    manual, _ = _make(keys, auto_approve=False)
    auto, _ = _make(keys, auto_approve=True)
    assert manual.auto_approve is False and auto.auto_approve is True
    assert type(manual) is type(auto)
    assert manual.keys is auto.keys


# ---------------------------------------------------------------------------
# Révocation
# ---------------------------------------------------------------------------

async def test_revoked_access_token_stops_loading(keys):
    provider, app = _make(keys)
    verifier, challenge = _pkce()
    async with _client(app) as c:
        info = await _register(c)
        code = _code_from(await _authorize(c, info["client_id"], challenge))
        tok = (
            await c.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _REDIRECT,
                    "client_id": info["client_id"],
                    "client_secret": info.get("client_secret", ""),
                    "code_verifier": verifier,
                },
            )
        ).json()["access_token"]
        assert await provider.load_access_token(tok) is not None
        await c.post(
            "/revoke",
            data={
                "token": tok,
                "client_id": info["client_id"],
                "client_secret": info.get("client_secret", ""),
            },
        )
    assert await provider.load_access_token(tok) is None


async def test_expired_access_token_is_not_loaded(keys):
    """load_access_token nettoie ce qui a expiré plutôt que de le rendre."""
    provider, _ = _make(keys)
    from mcp.server.auth.provider import AccessToken

    provider.access_tokens["perime"] = AccessToken(
        token="perime", client_id="c", scopes=[], expires_at=1
    )
    assert await provider.load_access_token("perime") is None


# ---------------------------------------------------------------------------
# Le proxy accepte-t-il réellement un jeton de cet AS ?
# ---------------------------------------------------------------------------

async def test_proxy_verifier_accepts_a_token_from_this_server(keys):
    """La jonction AB-1.2 ↔ AB-1.3, testée pour de bon : le verifier du proxy
    accepte un jeton émis ici. Chaque moitié pourrait être verte séparément et
    le raccord faux — c'est le seul test qui exclut ça."""
    import mcp_proxy

    _, app = _make(keys)
    verifier_pkce, challenge = _pkce()
    async with _client(app) as c:
        info = await _register(c)
        code = _code_from(await _authorize(c, info["client_id"], challenge))
        token = (
            await c.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _REDIRECT,
                    "client_id": info["client_id"],
                    "client_secret": info.get("client_secret", ""),
                    "code_verifier": verifier_pkce,
                },
            )
        ).json()["access_token"]

    tv = mcp_proxy.JwtTokenVerifier(issuer_url=_ISSUER, resource_url=_RESOURCE)

    class _Stub:
        @staticmethod
        def get_signing_key_from_jwt(_):
            import jwt

            class _K:
                key = jwt.PyJWK(keys.jwks()["keys"][0]).key

            return _K()

    tv._jwk_client = _Stub()
    access = await tv.verify_token(token)
    assert access is not None
    assert access.resource == _RESOURCE


async def test_proxy_verifier_refuses_a_token_issued_for_elsewhere(keys):
    """Symétrique : un jeton de CE serveur, mais demandé pour une autre
    ressource, reste refusé par le proxy."""
    import mcp_proxy

    _, app = _make(keys)
    verifier_pkce, challenge = _pkce()
    async with _client(app) as c:
        info = await _register(c)
        code = _code_from(
            await _authorize(
                c, info["client_id"], challenge, resource="http://ailleurs.example/mcp"
            )
        )
        token = (
            await c.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _REDIRECT,
                    "client_id": info["client_id"],
                    "client_secret": info.get("client_secret", ""),
                    "code_verifier": verifier_pkce,
                },
            )
        ).json()["access_token"]

    tv = mcp_proxy.JwtTokenVerifier(issuer_url=_ISSUER, resource_url=_RESOURCE)

    class _Stub:
        @staticmethod
        def get_signing_key_from_jwt(_):
            import jwt

            class _K:
                key = jwt.PyJWK(keys.jwks()["keys"][0]).key

            return _K()

    tv._jwk_client = _Stub()
    assert await tv.verify_token(token) is None


async def test_refreshed_token_keeps_the_original_audience(keys):
    """Le jeton rafraîchi porte la MÊME audience que le jeton initial.

    Ce test remplace `test_refreshed_token_has_no_audience_yet`, qui figeait la
    limite inverse. Le diagnostic d'alors était faux : le client du SDK envoie
    bien `resource` au refresh, et son handler serveur le parse
    (RefreshTokenRequest le déclare). Ce qui manque est en aval — RefreshToken
    n'a pas de champ `resource` et le handler ne transmet pas la valeur à
    exchange_refresh_token. On la reporte donc depuis l'émission, ce qui est
    aussi plus sûr que de croire la requête de refresh sur parole.
    """
    import jwt
    from mcp.server.auth.provider import RefreshToken
    from mcp.shared.auth import OAuthClientInformationFull

    provider, _ = _make(keys)
    client = OAuthClientInformationFull(
        client_id="c1", redirect_uris=[_REDIRECT]
    )
    rt = RefreshToken(token="r1", client_id="c1", scopes=["mcp:read"], subject="u")
    provider.refresh_tokens["r1"] = rt
    provider.refresh_resources["r1"] = "http://127.0.0.1:8799/mcp"

    out = await provider.exchange_refresh_token(client, rt, ["mcp:read"])
    claims = jwt.decode(out.access_token, options={"verify_signature": False})
    assert claims["aud"] == "http://127.0.0.1:8799/mcp"


async def test_rotated_refresh_token_carries_the_resource_forward(keys):
    """La rotation ne doit pas perdre l'audience au deuxième tour : le jeton
    rafraîchi porte lui-même un nouveau refresh token, qui devra rafraîchir."""
    import jwt
    from mcp.server.auth.provider import RefreshToken
    from mcp.shared.auth import OAuthClientInformationFull

    provider, _ = _make(keys)
    client = OAuthClientInformationFull(client_id="c1", redirect_uris=[_REDIRECT])
    rt = RefreshToken(token="r1", client_id="c1", scopes=["mcp:read"], subject="u")
    provider.refresh_tokens["r1"] = rt
    provider.refresh_resources["r1"] = "http://127.0.0.1:8799/mcp"

    first = await provider.exchange_refresh_token(client, rt, ["mcp:read"])
    second_rt = provider.refresh_tokens[first.refresh_token]
    second = await provider.exchange_refresh_token(client, second_rt, ["mcp:read"])

    claims = jwt.decode(second.access_token, options={"verify_signature": False})
    assert claims["aud"] == "http://127.0.0.1:8799/mcp"


async def test_refresh_without_original_resource_stays_without_audience(keys):
    """Un parcours mené sans `resource` ne doit pas s'en voir inventer une."""
    import jwt
    from mcp.server.auth.provider import RefreshToken
    from mcp.shared.auth import OAuthClientInformationFull

    provider, _ = _make(keys)
    client = OAuthClientInformationFull(client_id="c1", redirect_uris=[_REDIRECT])
    rt = RefreshToken(token="r1", client_id="c1", scopes=["mcp:read"], subject="u")
    provider.refresh_tokens["r1"] = rt

    out = await provider.exchange_refresh_token(client, rt, ["mcp:read"])
    claims = jwt.decode(out.access_token, options={"verify_signature": False})
    assert "aud" not in claims


async def test_revoking_a_refresh_token_drops_its_resource(keys):
    """La table des ressources est indexée par jeton : sans ce nettoyage, une
    entrée fuirait à chaque révocation."""
    from mcp.server.auth.provider import RefreshToken
    from mcp.shared.auth import OAuthClientInformationFull

    provider, _ = _make(keys)
    rt = RefreshToken(token="r1", client_id="c1", scopes=["mcp:read"], subject="u")
    provider.refresh_tokens["r1"] = rt
    provider.refresh_resources["r1"] = "http://127.0.0.1:8799/mcp"

    await provider.revoke_token(rt)
    assert "r1" not in provider.refresh_resources


def test_banner_warns_and_is_shared_by_both_entry_points():
    """L'avertissement « jamais en production » est produit par UNE fonction,
    utilisée par le lancement autonome comme par `mcp_proxy --with-dev-auth`.
    Dupliqué, l'un des deux finirait par diverger — ou par manquer."""
    lines = das.banner_lines("http://127.0.0.1:8787", auto_approve=False)
    joined = "\n".join(lines)
    assert "jamais en production" in joined
    assert "Aucune authentification" in joined
    assert "http://127.0.0.1:8787" in joined
    assert "écran navigateur" in joined

    auto = "\n".join(das.banner_lines("http://127.0.0.1:8787", auto_approve=True))
    assert "AUTOMATIQUE" in auto
    assert "jamais en production" in auto


async def test_proxy_verifier_accepts_a_REFRESHED_token(keys):
    """La jonction complète, du premier jeton au rafraîchi.

    C'est le contrôle qui manquait à AB-1 : les tests d'unité pouvaient être
    verts des deux côtés pendant qu'un jeton rafraîchi partait sans audience et
    se faisait refuser par le proxy — à raison. Le parcours réel dure plus
    longtemps qu'un TTL, donc c'est ce jeton-là, pas le premier, qui porte
    l'usage courant.
    """
    import mcp_proxy

    _, app = _make(keys)
    verifier_pkce, challenge = _pkce()
    async with _client(app) as c:
        info = await _register(c)
        code = _code_from(await _authorize(c, info["client_id"], challenge))
        first = (
            await c.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _REDIRECT,
                    "client_id": info["client_id"],
                    "client_secret": info.get("client_secret", ""),
                    "code_verifier": verifier_pkce,
                },
            )
        ).json()

        refreshed = (
            await c.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": first["refresh_token"],
                    "client_id": info["client_id"],
                    "client_secret": info.get("client_secret", ""),
                },
            )
        ).json()

    # Pas d'assertion sur la DIFFÉRENCE des deux access tokens : émis dans la
    # même seconde avec les mêmes claims, ils sont byte-identiques (`iat` est en
    # secondes). Ce qui compte est ailleurs — le refresh token, lui, tourne, et
    # c'est l'audience du jeton rafraîchi qu'on vient vérifier.
    assert refreshed["refresh_token"] != first["refresh_token"]

    tv = mcp_proxy.JwtTokenVerifier(issuer_url=_ISSUER, resource_url=_RESOURCE)

    class _Stub:
        @staticmethod
        def get_signing_key_from_jwt(_):
            import jwt

            class _K:
                key = jwt.PyJWK(keys.jwks()["keys"][0]).key

            return _K()

    tv._jwk_client = _Stub()
    access = await tv.verify_token(refreshed["access_token"])
    assert access is not None
    assert access.resource == _RESOURCE
