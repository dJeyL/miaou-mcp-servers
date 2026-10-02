#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp==2.2.0"]
# ///
"""Ce que la découverte OAuth du SDK MCP 2.x conclurait sur un upstream (banc manuel).

Ce n'est PAS un test pytest : le nom du fichier ne commence pas par "test_",
il n'est donc jamais collecté malgré sa présence dans tests/.

À quoi il sert : préparer la migration du proxy vers le SDK MCP 2.x
sans l'avoir faite. La 2.x durcit sa découverte OAuth sur des points qu'aucune
lecture de code ne tranche, parce qu'ils dépendent de ce que l'AUTRE bout
répond :

- une métadonnée de ressource protégée (PRM) en 5xx ou 429 fait échouer le
  parcours (`OAuthFlowError`), là où la 1.x passait au candidat suivant ;
- l'`issuer` des métadonnées d'AS doit égaler, à l'octet près, celui qu'on
  attend (RFC 8414 §3.3) — aucun contournement côté client ;
- `offline_access`, s'il figure dans les `scopes_supported` de l'AS, est
  ajouté d'office à la demande, avec `prompt=consent` ;
- le paramètre `iss` de la redirection d'autorisation (RFC 9207) est comparé à
  l'`issuer` des métadonnées EFFECTIVES — celles découvertes, ou à défaut celles
  que le proxy construit depuis sa config (`build_oauth_metadata_override`),
  dont l'`issuer` est dérivé de l'authorization endpoint quand il n'est pas
  déclaré. En 1.x cet `issuer` dérivé ne servait à rien ; en 2.x, un écart d'un
  octet fait échouer l'autorisation si l'AS renvoie `iss`.

Le script rejoue donc la découverte de la 2.x avec LES FONCTIONS DU SDK
lui-même — mêmes URL candidates, même lecture des réponses, même comparaison
d'issuer —, et non une recopie qui pourrait diverger. D'où la version ÉPINGLÉE
(`mcp==2.2.0`) : le script mesure ce que fera la version vers laquelle on
migre, pas la dernière parue. `_origin_issuer` est privé : c'est l'épinglage
qui rend son import acceptable.

    uv run tests/live_discovery_probe.py https://jira.exemple/mcp
    uv run tests/live_discovery_probe.py https://jira.exemple/mcp \\
        --authorization-endpoint https://sso.exemple/realms/r/protocol/openid-connect/auth
    uv run tests/live_discovery_probe.py https://jira.exemple/mcp \\
        --oidc https://sso.exemple/realms/r --issuer https://sso.exemple/realms/r

`--authorization-endpoint` et `--issuer` sont les valeurs des clés de même nom
du bloc `auth` de l'upstream dans `config.json` (`--issuer` seulement s'il y
est déclaré). `--oidc` désigne le realm, ou directement son
`openid-configuration` : c'est là qu'on lit l'`issuer` RÉEL de l'AS, à
déclarer en config s'il diffère de celui que le proxy dérive. Sans `--oidc`,
le script le déduit d'un authorization endpoint de forme Keycloak
(`…/protocol/openid-connect/auth`).

Rien n'est écrit, aucun jeton n'est lu ni envoyé : uniquement un POST
`initialize` sans jeton (pour lire le `WWW-Authenticate` d'un éventuel 401) et
des GET de métadonnées publiques. Lançable sur un serveur de production sans
effet de bord. Les certificats sont vérifiés contre le magasin du système :
httpx2, le client HTTP du SDK 2.x, le fait de lui-même (via truststore).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from urllib.parse import urlparse

import httpx2
from mcp.client.auth.oauth2 import _origin_issuer
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_oauth_metadata_request,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    issuers_match,
    validate_metadata_issuer,
)
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url

_MCP_ACCEPT = "application/json, text/event-stream"
_OIDC_SUFFIX = "/.well-known/openid-configuration"
_KEYCLOAK_AUTHZ_MARKER = "/protocol/openid-connect/"


def _parse_headers(raw: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw or []:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"header mal formé (attendu 'Nom: valeur') : {item!r}")
        headers[name.strip()] = value.lstrip()
    return headers


def _derived_issuer(authorization_endpoint: str) -> str:
    """L'`issuer` que le proxy pose dans son override quand la config n'en déclare pas.

    Recopie de la dérivation de `build_oauth_metadata_override`
    (mcp_proxy/auth_out/authorizer.py) — une ligne, et le script est autonome
    (bloc PEP 723, sans le paquet mcp_proxy). Si la dérivation du proxy change,
    celle-ci doit suivre.
    """
    parsed = urlparse(authorization_endpoint)
    return f"{parsed.scheme}://{parsed.netloc}"


def _oidc_url(oidc: str | None, authorization_endpoint: str | None) -> str | None:
    if oidc:
        return oidc if oidc.endswith(_OIDC_SUFFIX) else oidc.rstrip("/") + _OIDC_SUFFIX
    if authorization_endpoint and _KEYCLOAK_AUTHZ_MARKER in authorization_endpoint:
        realm = authorization_endpoint.split(_KEYCLOAK_AUTHZ_MARKER, 1)[0]
        return realm + _OIDC_SUFFIX
    return None


def _show_asm_fields(meta: dict | object) -> dict:
    """Les champs d'une métadonnée d'AS qui décident du comportement 2.x."""
    get = meta.get if isinstance(meta, dict) else (lambda k: getattr(meta, k, None))
    scopes = get("scopes_supported")
    fields = {
        "issuer": str(get("issuer")) if get("issuer") is not None else None,
        "authorization_endpoint": get("authorization_endpoint"),
        "token_endpoint": get("token_endpoint"),
        "offline_access annoncé": bool(scopes and "offline_access" in scopes),
        "iss annoncé (RFC 9207)": bool(
            get("authorization_response_iss_parameter_supported")
        ),
    }
    for key, value in fields.items():
        print(f"    {key} : {value}")
    return fields


async def _get(client: httpx2.AsyncClient, url: str, label: str):
    print(f"\n--- {label}\n    GET {url}")
    try:
        response = await client.send(create_oauth_metadata_request(url))
    except Exception as e:
        print(f"    ÉCHEC réseau : {type(e).__name__}: {e}")
        return None
    print(f"    HTTP {response.status_code}")
    return response


async def probe(args: argparse.Namespace, extra: dict[str, str]) -> int:
    url = args.url
    findings: list[str] = []   # risques bloquants pour la 2.x
    notes: list[str] = []      # constats sans effet bloquant

    print(f"Cible : {url}")
    print("Aucun jeton n'est envoyé ; seules des métadonnées publiques sont lues.")

    async with httpx2.AsyncClient(timeout=args.timeout, follow_redirects=False) as client:
        # 1. Le refus qui amorce la découverte, et ce qu'il désigne.
        print("\n--- POST initialize sans jeton")
        www_auth_url = None
        try:
            response = await client.post(
                url,
                headers={"Accept": _MCP_ACCEPT, "Content-Type": "application/json", **extra},
                content=json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "miaou-discovery-probe", "version": "1"},
                    },
                }),
            )
            print(f"    HTTP {response.status_code}")
            if response.status_code == 401:
                www_auth_url = extract_resource_metadata_from_www_auth(response)
                print(f"    www-authenticate : {response.headers.get('www-authenticate')}")
                print(f"    resource_metadata désigné : {www_auth_url}")
                print(f"    scope désigné : {extract_scope_from_www_auth(response)}")
            else:
                notes.append(
                    "initialize n'est pas refusé : la découverte n'aura lieu qu'au "
                    "premier 401 (tools/list ou tools/call). Elle partira alors des "
                    "mêmes URL candidates que ci-dessous, sauf si ce 401-là désigne "
                    "un resource_metadata (cf. tests/live_auth_probe.py)."
                )
        except Exception as e:
            # Un hôte injoignable ne dit rien de la 2.x : conclure à un risque
            # ferait accuser la migration d'une panne réseau.
            print(f"    ÉCHEC réseau : {type(e).__name__}: {e}")
            print("\n" + "=" * 60)
            print("VERDICT : upstream injoignable depuis ce poste — aucune mesure "
                  "possible. Vérifier l'URL, le VPN, le proxy réseau.")
            return 2

        # 2. Métadonnée de ressource protégée (PRM), dans l'ordre du SDK.
        auth_server_url = None
        prm_failed_status = None
        for candidate in build_protected_resource_metadata_discovery_urls(www_auth_url, url):
            response = await _get(client, candidate, "PRM (ressource protégée)")
            if response is None:
                notes.append(
                    f"PRM : échec réseau sur {candidate} — la suite de la mesure "
                    "porte sur une découverte incomplète."
                )
                break
            if response.status_code >= 500 or response.status_code == 429:
                prm_failed_status = response.status_code
            prm = await handle_protected_resource_response(response)
            if prm:
                resource = str(prm.resource) if prm.resource else None
                servers = [str(s) for s in prm.authorization_servers]
                print(f"    resource : {resource}")
                print(f"    authorization_servers : {servers}")
                expected_resource = resource_url_from_server_url(url)
                if resource and not check_resource_allowed(
                    requested_resource=expected_resource, configured_resource=resource
                ):
                    findings.append(
                        f"PRM : resource {resource} ne couvre pas {expected_resource} "
                        "(RFC 8707) — la 2.x lève OAuthFlowError."
                    )
                auth_server_url = servers[0]
                break
        else:
            if prm_failed_status is not None:
                findings.append(
                    f"PRM : aucune métadonnée, et au moins un candidat a répondu "
                    f"{prm_failed_status}. La 2.x lève alors OAuthFlowError "
                    "(« Protected resource metadata request failed ») au lieu de "
                    "passer au chemin sans PRM."
                )
            else:
                notes.append(
                    "PRM : aucune métadonnée publiée (4xx partout). La 2.x prend le "
                    "chemin sans PRM : issuer attendu = origine du serveur MCP."
                )

        expected_issuer = auth_server_url or _origin_issuer(url)
        print(f"\n    issuer attendu par la 2.x : {expected_issuer}")

        # 3. Métadonnée d'AS, dans l'ordre du SDK, avec SA comparaison d'issuer.
        discovered = None
        for candidate in build_oauth_authorization_server_metadata_discovery_urls(
            auth_server_url, url
        ):
            response = await _get(client, candidate, "Métadonnée d'AS")
            if response is None:
                break
            ok, asm = await handle_auth_metadata_response(response)
            if not ok:
                notes.append(
                    f"Métadonnée d'AS : {candidate} a répondu {response.status_code} ; "
                    "la 2.x arrête là sa recherche et garde les métadonnées qu'elle "
                    "avait déjà — l'override de la config, s'il est déclaré."
                )
                break
            if asm:
                if auth_server_url is None and issuers_match(str(asm.issuer), expected_issuer):
                    expected_issuer = str(asm.issuer)
                fields = _show_asm_fields(asm)
                try:
                    validate_metadata_issuer(asm, expected_issuer)
                    print("    issuer : conforme à l'attendu")
                except Exception as e:
                    findings.append(
                        f"Métadonnée d'AS : {e}. La 2.x interrompt le parcours ; "
                        "rien ne se corrige côté client (aligner l'issuer de l'AS, "
                        "ou les authorization_servers de la PRM)."
                    )
                if fields["offline_access annoncé"]:
                    findings.append(
                        "offline_access annoncé par l'AS découvert : la 2.x l'ajoute "
                        "à la demande, avec prompt=consent (écran de consentement à "
                        "chaque autorisation, ou invalid_scope si l'AS le refuse)."
                    )
                discovered = asm
                break
        if discovered is None:
            notes.append(
                "Aucune métadonnée d'AS découverte : ce sont celles de la config "
                "(override) qui font foi — authorization_endpoint et token_endpoint "
                "doivent y être déclarés."
            )

        # 4. L'issuer réel du realm, et celui que verrait le contrôle de `iss`.
        oidc = _oidc_url(args.oidc, args.authorization_endpoint)
        real = None
        if oidc:
            response = await _get(client, oidc, "Configuration OpenID du realm")
            if response is not None and response.status_code == 200:
                try:
                    real = _show_asm_fields(response.json())
                except ValueError:
                    print("    corps illisible (JSON attendu)")
        else:
            notes.append(
                "Realm non désigné (--oidc) et authorization endpoint absent ou non "
                "Keycloak : issuer réel non lu."
            )

        if discovered is not None:
            effective, source = str(discovered.issuer), "métadonnées découvertes"
        elif args.issuer:
            effective, source = args.issuer, "config (issuer déclaré)"
        elif args.authorization_endpoint:
            effective = _derived_issuer(args.authorization_endpoint)
            source = "config (issuer dérivé de l'authorization endpoint)"
        else:
            effective, source = None, None

        print("\n--- Contrôle du paramètre `iss` de la redirection (RFC 9207)")
        print(f"    issuer effectif : {effective}  [{source}]")
        if real is not None:
            print(f"    issuer réel     : {real['issuer']}")
        if effective is not None and real is not None and real["issuer"]:
            if effective == real["issuer"]:
                print("    identiques : le contrôle de `iss` passera.")
            else:
                verb = (
                    "L'AS annonce renvoyer `iss`"
                    if real["iss annoncé (RFC 9207)"]
                    else "L'AS n'annonce pas `iss`, mais s'il le renvoie quand même,"
                )
                findings.append(
                    f"issuer effectif {effective!r} ≠ issuer réel {real['issuer']!r}. "
                    f"{verb} : la 2.x refusera la redirection. Remède : déclarer "
                    f"\"issuer\": \"{real['issuer']}\" dans le bloc auth de l'upstream."
                )
            if discovered is None and real["offline_access annoncé"]:
                notes.append(
                    "offline_access annoncé par le realm, mais la 2.x ne le voit que "
                    "par la découverte, qui n'aboutit pas ici : sans effet, tant que "
                    "le `scope` de la config ne le contient pas."
                )
        elif effective is None:
            notes.append(
                "Ni découverte, ni --authorization-endpoint/--issuer : impossible "
                "de dire quel issuer le contrôle de `iss` utiliserait."
            )

    print("\n" + "=" * 60)
    for note in notes:
        print(f"NOTE : {note}")
    if findings:
        print()
        for finding in findings:
            print(f"RISQUE 2.x : {finding}")
        print("\nVERDICT : la migration casserait l'auth sortante sur cet upstream "
              "en l'état — voir les risques ci-dessus.")
        return 1
    print("\nVERDICT : rien, dans ce que publie cet upstream, ne fait échouer la "
          "découverte de la 2.x. Reste à éprouver le parcours réel (redirection, "
          "échange du code, renouvellement) avec le proxy migré.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ce que la découverte OAuth du SDK MCP 2.x conclurait sur un upstream."
    )
    parser.add_argument("url", help="URL /mcp de l'upstream")
    parser.add_argument(
        "-H", "--header", action="append", default=[],
        help="En-tête supplémentaire (répétable), ex. -H 'X-Tenant: acme'",
    )
    parser.add_argument(
        "--authorization-endpoint",
        help="Valeur de auth.authorization_endpoint dans config.json",
    )
    parser.add_argument(
        "--issuer", help="Valeur de auth.issuer dans config.json, si déclarée"
    )
    parser.add_argument(
        "--oidc",
        help="URL du realm (ou de son openid-configuration), pour lire l'issuer réel",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="Délai par requête (défaut 30 s)"
    )
    args = parser.parse_args()
    try:
        extra = _parse_headers(args.header)
    except ValueError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 2
    return asyncio.run(probe(args, extra))


if __name__ == "__main__":
    raise SystemExit(main())
