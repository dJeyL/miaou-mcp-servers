"""Auth entrante — le proxy est Resource Server OAuth 2.1 (lot AB-1).

Validation du jeton présenté par le client MCP : découverte JWKS, vérification
de signature et d'audience (RFC 8707, non désactivable).
"""

from __future__ import annotations

import json
from typing import Any

from .logging import _log


# Le proxy est un *Resource Server* : il VÉRIFIE des jetons, il n'en émet
# jamais. L'émission appartient à un Authorization Server distinct (cf.
# dev_auth_server.py pour celui de développement). La révision 2025-03-26 de la
# spec MCP faisait du serveur MCP son propre AS ; c'est abandonné depuis, ne pas
# le réintroduire par commodité.
#
# Auth DÉSACTIVÉE par défaut : sans clé "auth" dans la config, le proxy se
# comporte exactement comme avant ce lot. La boucle de développement locale
# (MIAOU ↔ proxy sans jeton) ne doit pas se mettre à exiger une autorisation.


class AuthConfigError(ValueError):
    """Config d'auth présente mais inexploitable (clé manquante, URL invalide)."""


def resolve_auth_config(
    cfg: dict[str, Any],
    cli_auth: bool | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> dict[str, Any] | None:
    """Normalise la clé `auth` de la config en un dict prêt à consommer, ou None
    si l'auth est désactivée.

    `cli_auth` (--auth/--no-auth) l'emporte sur la config :
      - None  : la config décide (absente → désactivé) ;
      - False : désactivé quoi qu'en dise la config ;
      - True  : activé — exige que la config porte de quoi le faire.

    `host`/`port` servent à dériver `resource_url` quand il n'est pas donné :
    c'est l'URL publique de CE serveur, et elle doit désigner l'endpoint MCP
    (`/mcp`), pas la racine — le client la renvoie en paramètre `resource`
    (RFC 8707) et c'est elle qu'on comparera à l'audience du jeton.

    Fonction PURE : aucun accès réseau, aucune lecture de fichier. Testable
    directement.
    """
    if cli_auth is False:
        return None
    raw = cfg.get("auth")
    if raw is None:
        if cli_auth is True:
            raise AuthConfigError(
                "--auth demandé mais la config ne contient pas de clé 'auth'."
            )
        return None
    if not isinstance(raw, dict):
        raise AuthConfigError("La clé 'auth' de la config doit être un objet.")
    if raw.get("_disabled") and cli_auth is not True:
        return None

    issuer_url = raw.get("issuer_url")
    if not issuer_url:
        raise AuthConfigError(
            "La clé 'auth' doit contenir 'issuer_url' (l'Authorization Server "
            "dont on accepte les jetons)."
        )

    # Défaut dérivé de l'écoute réelle. Un host d'écoute 0.0.0.0 ne peut pas
    # servir d'identité publique (ce n'est l'adresse de personne) : on retombe
    # sur 127.0.0.1, qui est ce que le client atteint en développement local.
    advertised_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    resource_url = raw.get("resource_url") or f"http://{advertised_host}:{port}/mcp"

    authorization_servers = raw.get("authorization_servers") or [issuer_url]
    if not isinstance(authorization_servers, list) or not authorization_servers:
        raise AuthConfigError(
            "'authorization_servers' doit être une liste non vide d'URLs."
        )

    required_scopes = raw.get("required_scopes") or []
    if not isinstance(required_scopes, list):
        raise AuthConfigError("'required_scopes' doit être une liste.")

    return {
        "issuer_url": issuer_url,
        "resource_url": resource_url,
        "authorization_servers": authorization_servers,
        "required_scopes": required_scopes,
        "scopes_supported": raw.get("scopes_supported"),
        "jwks_uri": raw.get("jwks_uri"),
        "algorithms": raw.get("algorithms") or ["RS256"],
    }


class JwtAudienceError(Exception):
    """Jeton refusé par le verifier. Interne : jamais propagée hors de
    verify_token(), qui renvoie None (un jeton invalide est un 401, pas un 500)."""


def _discover_jwks_uri(issuer_url: str, timeout: float = 10.0) -> str:
    """Trouve le `jwks_uri` d'un Authorization Server par sa métadonnée.

    Sonde les deux chemins bien connus, dans l'ordre où la spec les impose :
    RFC 8414 (`/.well-known/oauth-authorization-server`) puis OpenID Connect
    Discovery (`/.well-known/openid-configuration`) — un AS OIDC ne sert
    souvent que le second, et un client MCP conforme sonde les deux.

    Bloquant (urllib) : appeler via asyncio.to_thread, jamais directement dans
    un chemin async.
    """
    import urllib.error
    import urllib.request

    base = issuer_url.rstrip("/")
    candidates = (
        f"{base}/.well-known/oauth-authorization-server",
        f"{base}/.well-known/openid-configuration",
    )
    errors: list[str] = []
    for url in candidates:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                meta = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            errors.append(f"{url}: {e}")
            continue
        jwks_uri = meta.get("jwks_uri")
        if jwks_uri:
            return str(jwks_uri)
        errors.append(f"{url}: métadonnée sans 'jwks_uri'")
    raise JwtAudienceError(
        "Impossible de découvrir le jwks_uri de l'émetteur "
        f"{issuer_url} — " + " ; ".join(errors)
    )


def _audience_matches(claim: Any, expected: str) -> bool:
    """`aud` est soit une chaîne, soit une liste de chaînes (RFC 7519 §4.1.3).

    Comparaison EXACTE, jamais par préfixe ni par sous-chaîne : une audience
    `https://evil.example/mcp-attacker` ne doit pas passer pour
    `https://evil.example/mcp`. Le slash final est la seule normalisation
    tolérée — les AS ne s'accordent pas dessus.
    """
    if claim is None:
        return False
    values = [claim] if isinstance(claim, str) else list(claim)
    wanted = expected.rstrip("/")
    return any(isinstance(v, str) and v.rstrip("/") == wanted for v in values)


class JwtTokenVerifier:
    """TokenVerifier qui valide un JWT signé par l'AS déclaré, et surtout son
    AUDIENCE (RFC 8707).

    C'est le point que le SDK ne couvre pas : `BearerAuthBackend` appelle ce
    verifier puis re-vérifie seulement `expires_at`. Il ne regarde JAMAIS
    `AccessToken.resource`. Sans la vérification faite ici, un jeton parfaitement
    valide émis pour un AUTRE Resource Server serait accepté — c'est la
    confused deputy que RFC 8707 existe pour empêcher, et le défaut le plus
    fréquent des implémentations MCP.

    La clef publique vient du JWKS de l'émetteur : `jwks_uri` s'il est en config,
    sinon découvert depuis les métadonnées de l'issuer. `PyJWKClient` met le jeu
    de clefs en cache — pas un appel réseau par requête.
    """

    def __init__(
        self,
        issuer_url: str,
        resource_url: str,
        algorithms: list[str] | None = None,
        jwks_uri: str | None = None,
        required_scopes: list[str] | None = None,
    ):
        self.issuer_url = issuer_url
        self.resource_url = resource_url
        self.algorithms = algorithms or ["RS256"]
        self._configured_jwks_uri = jwks_uri
        self.required_scopes = required_scopes or []
        self._jwk_client: Any = None

    def _client(self) -> Any:
        """Construit (une fois) le PyJWKClient. Découvre le jwks_uri au premier
        appel si la config ne le donne pas : au démarrage l'AS peut ne pas être
        joignable encore, et un proxy qui refuse de démarrer parce que son AS
        dort est un mauvais compromis."""
        if self._jwk_client is None:
            from jwt import PyJWKClient

            uri = self._configured_jwks_uri or _discover_jwks_uri(self.issuer_url)
            self._jwk_client = PyJWKClient(uri, cache_keys=True)
        return self._jwk_client

    def _verify_sync(self, token: str) -> Any:
        """Tout le travail bloquant (récupération JWKS + vérification crypto),
        isolé pour asyncio.to_thread — pattern du dépôt pour l'I/O en contexte
        async."""
        import jwt

        signing_key = self._client().get_signing_key_from_jwt(token).key
        # `verify_aud=False` : pyjwt sait comparer une audience, mais son échec
        # ne se distingue pas d'un autre. On veut refuser l'audience étrangère
        # explicitement, avec un message propre — et surtout peupler
        # AccessToken.resource depuis ce qu'on a réellement vérifié.
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=self.algorithms,
            issuer=self.issuer_url,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": False,
                "require": ["exp"],
            },
        )
        if not _audience_matches(claims.get("aud"), self.resource_url):
            raise JwtAudienceError(
                f"audience {claims.get('aud')!r} ne désigne pas ce Resource "
                f"Server ({self.resource_url})"
            )
        return claims

    async def verify_token(self, token: str) -> Any:
        """Renvoie un AccessToken, ou None sur TOUT échec.

        Jamais d'exception : elle remonterait en 500 alors qu'un jeton invalide
        est un 401. Le refus est journalisé — sans trace, un jeton rejeté à
        tort est indébuggable côté client, qui ne voit qu'un 401 nu.
        """
        import asyncio

        from mcp.server.auth.provider import AccessToken

        try:
            claims = await asyncio.to_thread(self._verify_sync, token)
        except Exception as e:  # jeton invalide, JWKS injoignable, algo refusé…
            _log(f"Jeton refusé : {type(e).__name__}: {e}")
            return None

        scopes = claims.get("scope") or ""
        if isinstance(scopes, str):
            scope_list = scopes.split()
        else:
            scope_list = list(scopes)

        expires_at = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or claims.get("azp") or claims.get("sub") or ""),
            scopes=scope_list,
            expires_at=int(expires_at) if expires_at is not None else None,
            resource=self.resource_url,
            subject=claims.get("sub"),
            claims=claims,
        )


def build_token_verifier(auth: dict[str, Any]) -> Any:
    """Construit le TokenVerifier depuis la config d'auth résolue.

    Voir JwtTokenVerifier : signature, expiration et surtout AUDIENCE. La
    validation d'audience n'est pas optionnelle — c'est la raison d'être du
    mode auth, et un serveur qui l'omet accepte les jetons destinés à autrui.
    """
    return JwtTokenVerifier(
        issuer_url=auth["issuer_url"],
        resource_url=auth["resource_url"],
        algorithms=auth.get("algorithms"),
        jwks_uri=auth.get("jwks_uri"),
        required_scopes=auth.get("required_scopes"),
    )
