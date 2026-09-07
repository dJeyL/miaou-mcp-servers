#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx", "truststore"]
# ///
"""
Ce qu'un upstream MCP répond quand on lui parle SANS jeton (banc manuel).

Ce n'est PAS un test pytest : le nom du fichier ne commence pas par "test_",
il n'est donc jamais collecté malgré sa présence dans tests/.

À quoi il sert : le parcours d'autorisation du proxy repose entièrement sur le
fait que l'upstream REFUSE quelque chose (un 401 porteur d'un
`WWW-Authenticate`). C'est ce refus que le SDK MCP transforme en découverte de
l'AS puis en redirection. Un upstream qui répond 200 à tout, ou qui refuse par
un autre code, ne déclenche aucun parcours — et le proxy n'a alors rien à
proposer, quoi qu'il fasse.

Ce script pose donc la question directement, sans le proxy, sans le SDK, sans
rien qui puisse masquer la réponse : quatre requêtes, et le code + les en-têtes
d'authentification obtenus pour chacune.

    uv run tests/live_auth_probe.py https://jira.exemple/mcp
    uv run tests/live_auth_probe.py https://jira.exemple/mcp -H 'X-Tenant: acme'
    uv run tests/live_auth_probe.py https://jira.exemple/mcp \\
        --tool jira_list_projects --args '{"results": 1}'

Nommer l'outil avec --tool vaut mieux que laisser le script en choisir un :
derrière un proxy qui agrège plusieurs upstreams, le premier outil de
tools/list peut venir d'un AUTRE serveur, et le diagnostic porterait alors sur
celui-là.

Rien n'est écrit, aucun jeton n'est lu : le script ne fait que lire des
réponses. Il peut être lancé sur un serveur de production sans effet de bord.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

_MCP_ACCEPT = "application/json, text/event-stream"

# Ce que le SDK lit sur un 401 pour savoir OÙ autoriser. Sans au moins l'un des
# deux, un refus reste un refus opaque : il n'y a aucun AS à découvrir.
_AUTH_HEADERS = ("www-authenticate", "proxy-authenticate")

# Verbes qui trahissent un outil à EFFET DE BORD. La découverte automatique les
# écarte : ce script mesure une autorisation, il ne doit pas créer de ticket,
# supprimer une page ni envoyer un message pour y arriver. Un faux positif ne
# coûte qu'un outil ignoré ; un faux négatif écrit dans un système de
# production. Dans le doute, on n'appelle pas — `--tool` reste là pour désigner
# explicitement ce qu'on accepte d'exécuter.
_MUTATING_HINTS = (
    "add", "append", "archive", "assign", "cancel", "clear", "close", "comment",
    "create", "delete", "destroy", "edit", "insert", "move", "patch", "post",
    "publish", "purge", "push", "put", "remove", "rename", "replace", "reset",
    "restore", "run", "send", "set", "start", "stop", "submit", "transition",
    "trigger", "update", "upload", "write",
)


def _looks_mutating(name: str) -> bool:
    """Le nom d'outil évoque-t-il une écriture ?

    Découpage sur les séparateurs usuels (`__`, `_`, `-`, `.`) plus les
    frontières de casse, pour attraper `createIssue` comme `create_issue` ou
    `jira__add-comment`. On compare des SEGMENTS, jamais des sous-chaînes :
    `update` doit écarter `update_issue` sans écarter `list_updates`.
    """
    import re

    segments = re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", name)
    return any(seg.lower() in _MUTATING_HINTS for seg in segments if seg)


def enable_system_trust_store() -> bool:
    """Vérifie les certificats TLS contre le magasin de confiance du système.

    Recopie délibérée du helper de `servers/mcp_base.py`, pour la même raison
    que dans `live_call.py` : ce script est un client autonome (bloc PEP 723).
    Sans elle, une AC d'entreprise interne échoue en CERTIFICATE_VERIFY_FAILED
    et le diagnostic porterait sur le mauvais problème.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
        return True
    except Exception as e:
        print(
            f"Avertissement : magasin de confiance système non activé ({e}).",
            file=sys.stderr,
        )
        return False


def _parse_headers(raw: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw or []:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"header mal formé (attendu 'Nom: valeur') : {item!r}")
        headers[name.strip()] = value.lstrip()
    return headers


def _report(label: str, response: httpx.Response) -> None:
    print(f"\n--- {label}")
    print(f"    HTTP {response.status_code}")
    for name in _AUTH_HEADERS:
        if name in response.headers:
            print(f"    {name}: {response.headers[name]}")
    body = (response.text or "").strip()
    if body:
        print(f"    corps  : {body[:300]}")


def _rpc(method: str, params: dict | None = None) -> dict:
    payload: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ce qu'un upstream MCP répond sans jeton."
    )
    parser.add_argument("url", help="URL /mcp de l'upstream")
    parser.add_argument(
        "-H", "--header", action="append", default=[],
        help="En-tête supplémentaire (répétable), ex. -H 'X-Tenant: acme'",
    )
    parser.add_argument(
        "--tool",
        help="Outil à appeler (défaut : le premier vu dans tools/list). "
             "Le nommer évite de mesurer un outil venu d'un AUTRE upstream, "
             "ce qui porterait le diagnostic sur le mauvais serveur.",
    )
    parser.add_argument(
        "--args", default=None,
        help="Arguments JSON de l'outil, ex. \'{\"results\": 1}\'. "
             "Défaut : aucun argument.",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="Délai par requête (défaut 30 s)"
    )
    args = parser.parse_args()

    enable_system_trust_store()
    try:
        extra = _parse_headers(args.header)
    except ValueError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 2

    try:
        tool_args = json.loads(args.args) if args.args else {}
    except json.JSONDecodeError as e:
        print(f"Erreur : --args n'est pas du JSON valide ({e})", file=sys.stderr)
        return 2
    if not isinstance(tool_args, dict):
        print("Erreur : --args doit être un objet JSON.", file=sys.stderr)
        return 2

    headers = {"Accept": _MCP_ACCEPT, "Content-Type": "application/json", **extra}

    print(f"Cible : {args.url}")
    print("Aucun jeton n'est envoyé : c'est tout l'objet de la mesure.")

    initialize = _rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "miaou-auth-probe", "version": "1"},
    })

    seen_401 = False
    tool_name: str | None = None

    with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:

        def _send(label, method, payload=None, extra_headers=None):
            nonlocal seen_401
            sent = dict(headers)
            if extra_headers:
                sent.update(extra_headers)
            try:
                response = client.request(
                    method, args.url, headers=sent,
                    content=json.dumps(payload) if payload else None,
                )
            except Exception as e:
                print(f"\n--- {label}\n    ÉCHEC : {type(e).__name__}: {e}")
                return None
            _report(label, response)
            if response.status_code == 401:
                seen_401 = True
            return response

        _send("GET sur l'URL MCP", "GET")

        # `initialize` ouvre la SESSION. Le transport streamable-http renvoie un
        # `Mcp-Session-Id` qu'il faut rejouer sur toute requête suivante : sans
        # lui, `tools/list` et `tools/call` sont rejetés hors de toute question
        # d'autorisation, et la mesure ne dirait rien (défaut de la première
        # version de ce script).
        response = _send("POST initialize", "POST", initialize)
        session: dict[str, str] = {}
        if response is not None:
            sid = response.headers.get("mcp-session-id")
            if sid:
                session["Mcp-Session-Id"] = sid
                print(f"    session : {sid}")
            else:
                print("    session : AUCUN Mcp-Session-Id renvoyé")
            # Le serveur attend la notification avant de servir la suite.
            client.request(
                "POST", args.url, headers={**headers, **session},
                content=json.dumps(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}
                ),
            )

        listed = _send("POST tools/list", "POST", _rpc("tools/list", {}), session)
        body = listed.text if (listed is not None and listed.status_code == 200) else ""

        if args.tool:
            tool_name = args.tool
            # Le nom peut être préfixé par l'upstream côté proxy
            # (`jira__list_projects`) ou nu en direct : on signale seulement, on
            # n'essaie pas de deviner — c'est la réponse qui tranchera.
            if body and f'"{tool_name}"' not in body:
                print(f"\n    Note : '{tool_name}' ne figure pas dans tools/list.")
                print("    L'appel est tenté quand même — un refus d'autorisation")
                print("    précède en général la résolution du nom.")
        elif body:
            # Repli : un outil RÉEL, car un outil inexistant se fait refuser pour
            # une raison sans rapport avec l'autorisation, et le diagnostic
            # porterait alors sur le mauvais refus.
            #
            # RÉEL mais aussi INOFFENSIF : on écarte tout nom qui évoque une
            # écriture. Sonder une autorisation ne doit jamais créer un ticket
            # ni supprimer quoi que ce soit — et un outil de lecture répond
            # exactement la même chose sur la question qui nous intéresse.
            import re

            candidates = re.findall(r'"name"\s*:\s*"([^"]+)"', body)
            safe = [n for n in candidates if not _looks_mutating(n)]
            skipped = len(candidates) - len(safe)
            if safe:
                tool_name = safe[0]
                if skipped:
                    print(f"\n    {skipped} outil(s) écarté(s) : nom évoquant "
                          f"une écriture.")
            elif candidates:
                print("\n    Tous les outils listés évoquent une écriture — "
                      "aucun appelé.")
                print("    Utiliser --tool pour en désigner un explicitement.")

        if tool_name:
            shown = json.dumps(tool_args) if tool_args else "sans arguments"
            _send(
                f"POST tools/call ({tool_name}, {shown})",
                "POST",
                _rpc("tools/call", {"name": tool_name, "arguments": tool_args}),
                session,
            )
        else:
            print("\n--- POST tools/call")
            print("    IGNORÉ : aucun outil réel n'a pu être lu dans tools/list,")
            print("    et --tool n'a pas été fourni. Appeler un outil inexistant")
            print("    ne dirait rien de l'autorisation.")

    print("\n" + "=" * 60)
    if seen_401:
        print("VERDICT : au moins un 401 — le parcours OAuth peut être déclenché.")
        print("Si l'en-tête www-authenticate est absent ci-dessus, le SDK n'a")
        print("cependant aucun AS à découvrir : c'est alors le point à corriger")
        print("côté serveur.")
    else:
        print("VERDICT : AUCUN 401. Cet upstream n'exige rien par le canal que")
        print("le SDK MCP sait interpréter, donc aucun parcours d'autorisation")
        print("ne peut s'amorcer côté client.")
        print()
        print("Lire alors le code obtenu sur tools/call :")
        print("  403 « No matching resource found in the API » → ce n'est PAS")
        print("      le serveur MCP qui refuse, c'est une passerelle (WSO2)")
        print("      qui ne route pas cette requête. À corriger côté")
        print("      passerelle : la méthode n'est pas exposée sur l'API.")
        print("  403 autre message → refus applicatif du serveur (droits,")
        print("      scopes) ; un parcours OAuth client n'y changerait rien.")
        print("  200 → l'appel passe : le blocage observé vient d'ailleurs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
