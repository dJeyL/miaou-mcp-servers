#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.28.1,<2", "uvicorn", "starlette", "pyjwt[crypto]"]
# ///
"""Serveur d'autorisation OAuth 2.1 de DÉVELOPPEMENT — pour éprouver l'auth
entrante de mcp_proxy (lot AB-1).

╔══════════════════════════════════════════════════════════════════════════╗
║  NE JAMAIS UTILISER EN PRODUCTION.                                       ║
║                                                                          ║
║  Il approuve toute demande d'autorisation sans authentifier personne :   ║
║  il n'y a ni compte, ni mot de passe, ni session. L'écran de             ║
║  consentement demande un accord, pas une identité. Ses clefs sont        ║
║  générées à chaque démarrage et son stockage est en mémoire — tout       ║
║  disparaît à l'arrêt, ce qui est voulu : rien à nettoyer, rien à faire   ║
║  fuiter, et aucun jeton émis ici ne survit au banc d'essai.              ║
╚══════════════════════════════════════════════════════════════════════════╝

Ce qu'il sert à vérifier, et qu'on ne peut pas vérifier autrement : le parcours
COMPLET d'un vrai client MCP (enregistrement dynamique, redirection,
consentement, échange du code, appel authentifié). Contre un serveur tiers réel
l'AS existe déjà — ce composant n'est pas un morceau du produit, c'est
l'équivalent d'un banc d'essai.

Lancement :
    uv run dev_auth_server.py                    # port 8787
    uv run dev_auth_server.py --port 9001
    uv run dev_auth_server.py --auto-approve     # sans écran de consentement

`--auto-approve` sert les tests automatisés (Playwright), qui ne peuvent pas
cliquer. C'est le SEUL écart entre le mode « manipulable au navigateur » et le
mode « fixture déterministe » : tout le reste — enregistrement, PKCE, émission,
signature, audience — est partagé, pour que la fixture éprouve exactement ce
qu'on éprouve à la main.
"""

from __future__ import annotations

import argparse
import base64
import secrets
import sys
import time
from typing import Any
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

ACCESS_TOKEN_TTL = 3600
AUTH_CODE_TTL = 300


def _b64u_uint(value: int) -> str:
    """Encode un entier en base64url sans padding — format des champs `n`/`e`
    d'une clef RSA dans un JWKS (RFC 7518 §6.3)."""
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class DevKeyPair:
    """Paire RSA générée au démarrage, jamais persistée.

    Une clef éphémère est un CHOIX, pas un raccourci : un AS de développement
    dont la clef privée traînerait sur disque finirait tôt ou tard réutilisé
    ailleurs. Redémarrer invalide tous les jetons émis, ce qui est exactement le
    comportement voulu ici.
    """

    def __init__(self, kid: str = "dev-key-1"):
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self) -> dict[str, Any]:
        numbers = self.private_key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": self.kid,
                    "n": _b64u_uint(numbers.n),
                    "e": _b64u_uint(numbers.e),
                }
            ]
        }

    def sign(self, claims: dict[str, Any]) -> str:
        return jwt.encode(
            claims, self.private_key, algorithm="RS256", headers={"kid": self.kid}
        )


class DevAuthProvider:
    """Implémente OAuthAuthorizationServerProvider (protocol du SDK).

    Stockage entièrement en mémoire. Le SDK fournit les routes, la validation
    des requêtes et — c'est important — la vérification PKCE : il n'accepte que
    `S256` (typé `Literal["S256"]` dans son handler) et compare lui-même le
    `code_verifier` au challenge. Rien à réimplémenter ici, mais un test
    l'épingle : c'est une garantie dont on dépend.
    """

    def __init__(self, issuer_url: str, keys: DevKeyPair, auto_approve: bool = False):
        self.issuer_url = issuer_url.rstrip("/")
        self.keys = keys
        self.auto_approve = auto_approve
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.auth_codes: dict[str, AuthorizationCode] = {}
        self.refresh_tokens: dict[str, RefreshToken] = {}
        # Ressource (RFC 8707) liée à chaque refresh token, dans une table à
        # part : le modèle RefreshToken du SDK n'a PAS de champ `resource`,
        # contrairement à AuthorizationCode. Table parallèle plutôt que
        # sous-classe — le SDK construit lui-même des RefreshToken ailleurs,
        # une sous-classe ne serait donc pas garantie partout.
        self.refresh_resources: dict[str, str | None] = {}
        self.access_tokens: dict[str, AccessToken] = {}
        # Demandes en attente de consentement, le temps d'un aller-retour
        # navigateur. Clef = identifiant opaque porté par l'URL de l'écran.
        self.pending: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams]] = {}

    # --- enregistrement dynamique (RFC 7591) -------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.clients[client_info.client_id] = client_info

    # --- autorisation ------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Renvoie l'URL où envoyer le navigateur.

        En auto-approve on court-circuite vers le redirect_uri du client avec un
        code frais ; sinon on passe par l'écran de consentement, qui rappellera
        `complete_authorization` une fois l'accord donné.
        """
        if self.auto_approve:
            return self._issue_code_redirect(client, params)
        handle = secrets.token_urlsafe(16)
        self.pending[handle] = (client, params)
        return f"{self.issuer_url}/consent?{urlencode({'handle': handle})}"

    def _issue_code_redirect(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Génère le code d'autorisation et construit la redirection vers le
        client. 256 bits d'entropie — la spec en exige 128 au minimum, en
        recommande 160 (RFC 6749 §10.10)."""
        code = secrets.token_urlsafe(32)
        self.auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="dev-user",
        )
        query = {"code": code}
        if params.state:
            query["state"] = params.state
        sep = "&" if "?" in str(params.redirect_uri) else "?"
        return f"{params.redirect_uri}{sep}{urlencode(query)}"

    def complete_authorization(self, handle: str) -> str | None:
        """Consentement accordé à l'écran : consomme la demande en attente et
        renvoie l'URL de redirection vers le client. `None` si le handle est
        inconnu (rechargement d'un écran déjà validé, lien périmé)."""
        entry = self.pending.pop(handle, None)
        if entry is None:
            return None
        client, params = entry
        return self._issue_code_redirect(client, params)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.auth_codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    # --- émission ----------------------------------------------------------

    def _issue_access_token(
        self, client_id: str, scopes: list[str], resource: str | None, subject: str | None
    ) -> str:
        """Émet le JWT.

        `aud` vient du paramètre `resource` de la requête (RFC 8707) : c'est LE
        point qui relie ce serveur à la validation d'audience du proxy. Sans
        lui, le jeton serait valide mais destiné à personne, et le proxy le
        refuserait — à raison.
        """
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer_url,
            "sub": subject or "dev-user",
            "client_id": client_id,
            "iat": now,
            "exp": now + ACCESS_TOKEN_TTL,
            "scope": " ".join(scopes),
        }
        if resource:
            claims["aud"] = resource
        token = self.keys.sign(claims)
        self.access_tokens[token] = AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
            resource=resource,
            subject=subject,
        )
        return token

    def _issue_refresh_token(
        self,
        client_id: str,
        scopes: list[str],
        subject: str | None,
        resource: str | None = None,
    ) -> str:
        token = secrets.token_urlsafe(32)
        self.refresh_tokens[token] = RefreshToken(
            token=token, client_id=client_id, scopes=scopes, subject=subject
        )
        self.refresh_resources[token] = resource
        return token

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Le SDK a déjà vérifié le PKCE et l'expiration avant d'arriver ici.

        Le code est consommé (retiré du stockage) : un code d'autorisation est
        à usage unique, et le rejouer doit échouer.
        """
        if self.auth_codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "code d'autorisation déjà utilisé")
        access = self._issue_access_token(
            client.client_id,
            authorization_code.scopes,
            authorization_code.resource,
            authorization_code.subject,
        )
        refresh = self._issue_refresh_token(
            client.client_id,
            authorization_code.scopes,
            authorization_code.subject,
            authorization_code.resource,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh,
            scope=" ".join(authorization_code.scopes),
        )

    # --- rafraîchissement --------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token = self.refresh_tokens.get(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotation des DEUX jetons, comme le recommande le SDK : l'ancien
        refresh token est invalidé, ce qui rend un rejeu détectable."""
        self.refresh_tokens.pop(refresh_token.token, None)
        granted = scopes or refresh_token.scopes
        # Ressource reportée depuis l'ÉMISSION, pas depuis la requête de
        # refresh. Deux raisons. D'abord le SDK ne nous la donne pas : son
        # handler parse bien `resource` (RefreshTokenRequest le déclare) mais
        # appelle exchange_refresh_token(client, refresh_token, scopes) sans le
        # transmettre, et RefreshToken n'a pas de champ pour le porter. Ensuite
        # c'est plus sûr : un client qui demanderait au refresh une ressource
        # différente de celle du code initial obtiendrait sinon une élévation
        # silencieuse.
        resource = self.refresh_resources.pop(refresh_token.token, None)
        access = self._issue_access_token(
            client.client_id, granted, resource, refresh_token.subject
        )
        new_refresh = self._issue_refresh_token(
            client.client_id, granted, refresh_token.subject, resource
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=new_refresh,
            scope=" ".join(granted),
        )

    # --- introspection / révocation ----------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self.access_tokens.get(token)
        if access is None:
            return None
        if access.expires_at and access.expires_at < int(time.time()):
            self.access_tokens.pop(token, None)
            return None
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.access_tokens.pop(token.token, None)
        self.refresh_tokens.pop(token.token, None)
        # La table des ressources est indexée par jeton : la laisser derrière
        # ferait fuir une entrée à chaque révocation.
        self.refresh_resources.pop(token.token, None)


_CONSENT_PAGE = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>Autorisation — serveur de développement</title>
<style>
 body {{ font-family: system-ui, sans-serif; max-width: 34rem; margin: 4rem auto;
        padding: 0 1.5rem; line-height: 1.6; color: #1a1a1a; }}
 .warn {{ background: #fff4e5; border-left: 4px solid #d97706; padding: .75rem 1rem;
          margin: 1.5rem 0; }}
 dl {{ background: #f6f6f6; padding: 1rem; }} dt {{ font-weight: 600; }}
 dd {{ margin: 0 0 .75rem; font-family: ui-monospace, monospace; font-size: .9em;
       word-break: break-all; }}
 button {{ font-size: 1rem; padding: .6rem 1.4rem; cursor: pointer; }}
</style></head><body>
<h1>Autoriser {client}&nbsp;?</h1>
<p>Cette application demande un accès à&nbsp;:</p>
<dl>
  <dt>Ressource</dt><dd>{resource}</dd>
  <dt>Portées</dt><dd>{scopes}</dd>
</dl>
<div class="warn"><strong>Serveur de développement.</strong> Personne n'est
authentifié ici&nbsp;: aucun compte, aucun mot de passe. Approuver n'engage que
ce banc d'essai.</div>
<form method="post" action="/consent">
  <input type="hidden" name="handle" value="{handle}">
  <button type="submit">Autoriser</button>
</form>
</body></html>"""


def build_app(provider: DevAuthProvider, keys: DevKeyPair, issuer_url: str) -> Starlette:
    """Assemble l'AS : routes du SDK, plus les trois qui lui manquent.

    Le SDK monte /authorize, /token, /register et /revoke, et sert une
    métadonnée RFC 8414. Mais son modèle `OAuthMetadata` **n'a pas de champ
    `jwks_uri`** — or c'est par lui que le proxy découvre les clefs publiques.
    On sert donc notre propre métadonnée, enrichie, en la posant AVANT celle du
    SDK : Starlette retient la première route qui matche. Écraser explicitement
    vaut mieux que laisser deux routes se disputer le chemin selon leur ordre.
    """
    sdk_routes = create_auth_routes(
        provider=provider,
        issuer_url=AnyHttpUrl(issuer_url),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,  # défaut False dans le SDK : la DCR ne s'active pas seule
            valid_scopes=["mcp:read", "mcp:write"],
            default_scopes=["mcp:read"],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )

    async def metadata(request):
        base = issuer_url.rstrip("/")
        doc = {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "revocation_endpoint": f"{base}/revoke",
            "jwks_uri": f"{base}/jwks",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["mcp:read", "mcp:write"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
                "none",
            ],
        }
        return JSONResponse(doc, headers={"Cache-Control": "no-store"})

    async def jwks(request):
        return JSONResponse(keys.jwks(), headers={"Cache-Control": "no-store"})

    async def consent(request):
        if request.method == "POST":
            form = await request.form()
            target = provider.complete_authorization(str(form.get("handle", "")))
            if target is None:
                return HTMLResponse(
                    "<h1>Demande inconnue ou déjà traitée</h1>", status_code=400
                )
            return RedirectResponse(target, status_code=302)
        handle = request.query_params.get("handle", "")
        entry = provider.pending.get(handle)
        if entry is None:
            return HTMLResponse("<h1>Demande inconnue ou expirée</h1>", status_code=404)
        client, params = entry
        return HTMLResponse(
            _CONSENT_PAGE.format(
                client=client.client_name or client.client_id,
                resource=params.resource or "(non précisée)",
                scopes=" ".join(params.scopes or []) or "(aucune)",
                handle=handle,
            )
        )

    routes = [
        Route("/.well-known/oauth-authorization-server", endpoint=metadata, methods=["GET"]),
        Route("/.well-known/openid-configuration", endpoint=metadata, methods=["GET"]),
        Route("/jwks", endpoint=jwks, methods=["GET"]),
        Route("/consent", endpoint=consent, methods=["GET", "POST"]),
        *sdk_routes,
    ]
    return Starlette(routes=routes)


def banner_lines(issuer_url: str, auto_approve: bool) -> list[str]:
    """Bannière d'avertissement, partagée par les deux points d'entrée.

    `mcp_proxy --with-dev-auth` lance ce serveur dans son propre process : sans
    fonction partagée, l'avertissement « jamais en production » existerait en
    deux exemplaires, et l'un des deux finirait par diverger — ou par manquer.
    """
    return [
        "=" * 74,
        "  Serveur d'autorisation de DÉVELOPPEMENT — jamais en production.",
        "  Aucune authentification : toute demande approuvée est accordée.",
        "=" * 74,
        f"  Émetteur     : {issuer_url}",
        f"  Consentement : {'AUTOMATIQUE (--auto-approve)' if auto_approve else 'écran navigateur'}",
        f'  Config proxy : "auth": {{"issuer_url": "{issuer_url}"}}',
        "=" * 74,
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serveur d'autorisation OAuth 2.1 de développement (JAMAIS en production)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="approuve sans écran de consentement (tests automatisés)",
    )
    parser.add_argument(
        "--issuer-url",
        default=None,
        help="identité publique de cet AS (défaut : http://<host>:<port>)",
    )
    args = parser.parse_args()

    advertised = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    issuer_url = args.issuer_url or f"http://{advertised}:{args.port}"

    keys = DevKeyPair()
    provider = DevAuthProvider(issuer_url, keys, auto_approve=args.auto_approve)
    app = build_app(provider, keys, issuer_url)

    # `flush=True` et `file=sys.stderr` : redirigée vers un fichier, une sortie
    # standard bufferisée retiendrait la bannière jusqu'à l'arrêt du process —
    # or c'est l'avertissement « jamais en production », celui qu'on ne peut
    # pas se permettre de perdre. uvicorn journalise sur stderr, on s'y range.
    for line in banner_lines(issuer_url, args.auto_approve):
        print(line, file=sys.stderr, flush=True)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
