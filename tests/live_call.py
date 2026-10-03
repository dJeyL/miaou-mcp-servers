#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.2,<3", "truststore"]
# ///
"""
Appel réel d'un outil MCP sur un serveur déjà lancé (banc d'essai manuel).

Ce n'est PAS un test pytest : le nom du fichier ne commence pas par "test_",
il n'est donc jamais collecté malgré sa présence dans tests/. Il parle le vrai
transport streamable-http, comme MIAOU — initialize, notifications/initialized,
tools/call — et non le stack in-process des tests unitaires.

`--modern` négocie la révision 2026-07-28 (`server/discover`) au lieu de la
poignée de main `initialize` de MIAOU ; c'est la seule ère où un serveur publie
ses extensions (Skills). `--method` envoie une requête JSON-RPC quelconque
(`skills/list`, `resources/read`…) et affiche le résultat brut.

Lancement (le serveur visé doit déjà tourner) :
    uv run tests/live_call.py brave__brave_search '{"query": "chat"}'
    uv run tests/live_call.py --port 8769 ddg_search '{"query": "chat"}'
    uv run tests/live_call.py --list                       # liste les outils
    uv run tests/live_call.py --url http://127.0.0.1:8766/mcp echo '{"text": "hi"}'
    uv run tests/live_call.py -H 'Authorization: Bearer xxx' --list
    uv run tests/live_call.py -H 'X-Tenant: acme' -H 'X-Trace: 1' --list
    uv run tests/live_call.py --modern --method skills/list
    uv run tests/live_call.py --method resources/read --params '{"uri": "skill://bench/bench/SKILL.md"}'

Sans argument JSON, l'outil est appelé sans arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import asynccontextmanager

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


def enable_system_trust_store() -> bool:
    """Vérifie les certificats TLS contre le magasin de confiance du système.

    Recopie délibérée du helper de `servers/mcp_base.py` : ce script est un
    CLIENT autonome (bloc PEP 723), l'importer depuis servers/ tirerait le SDK serveur
    et starlette pour quatre lignes. Toute évolution du helper d'origine doit
    être répercutée ici — la doc de référence reste `docs/tls.md`.

    Sans cette injection, viser un serveur HTTPS dont le certificat est signé
    par une AC d'entreprise interne échoue en CERTIFICATE_VERIFY_FAILED, alors
    que le proxy lui-même sait joindre ses upstreams : le client de banc d'essai
    doit se comporter comme les serveurs, sinon il diagnostique un faux négatif.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
        return True
    except Exception as e:  # ImportError, ou plateforme non supportée
        print(
            f"Avertissement : magasin de confiance système non activé ({e}). "
            "Les certificats signés par une AC interne peuvent échouer à la "
            "vérification ; installer `truststore` corrige ce cas.",
            file=sys.stderr,
        )
        return False


def _parse_headers(raw: list[str]) -> dict[str, str] | None:
    """Transforme une liste "Nom: valeur" en dict, ou lève ValueError.

    La valeur est strippée à gauche seulement : un header dont la valeur
    contient des espaces significatifs en fin reste transmis tel quel.
    """
    headers: dict[str, str] = {}
    for item in raw:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"header mal formé (attendu \'Nom: valeur\') : {item!r}")
        headers[name.strip()] = value.lstrip()
    return headers or None


def _render_content(block) -> str:
    """Rendu lisible d'un bloc de résultat (text / image / resource)."""
    kind = getattr(block, "type", "?")
    if kind == "text":
        return block.text
    if kind == "image":
        return f"[image {block.mime_type} — {len(block.data)} octets base64]"
    if kind == "resource":
        res = block.resource
        uri = getattr(res, "uri", "?")
        mime = getattr(res, "mime_type", "?")
        if getattr(res, "text", None) is not None:
            return f"[resource {uri} ({mime})]\n{res.text}"
        blob = getattr(res, "blob", "") or ""
        return f"[resource {uri} ({mime}) — {len(blob)} octets base64]"
    return repr(block)


@asynccontextmanager
async def _session(url: str, headers: dict[str, str] | None, modern: bool):
    """Session ouverte sur le serveur, et un résumé de la négociation."""
    import httpx2

    # En-têtes et délais se posent sur le client HTTP depuis le SDK 2.x. Le délai
    # de lecture reprend celui que l'ancien transport appliquait d'office : sans
    # lui, httpx2 retombe sur 5 s à plat, trop court pour le flux SSE.
    http_client = httpx2.AsyncClient(
        headers=headers, timeout=httpx2.Timeout(30, read=300)
    )
    async with http_client:
        if modern:
            from mcp.client import Client

            # `auto` sonde `server/discover` (et retomberait sur initialize
            # devant un serveur ancien) : la version affichée dit laquelle a
            # été retenue.
            transport = streamable_http_client(url, http_client=http_client)
            async with Client(transport, mode="auto") as client:
                info = client.server_info
                caps = client.server_capabilities
                summary = (
                    f"→ connecté à {info.name if info else '?'} ({url}), "
                    f"révision {client.protocol_version}, "
                    f"extensions={json.dumps(caps.extensions or {}, ensure_ascii=False)}"
                )
                yield client.session, summary
            return
        async with streamable_http_client(url, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                summary = (
                    f"→ connecté à {init.server_info.name} {init.server_info.version} "
                    f"({url}), révision {init.protocol_version}"
                )
                yield session, summary


async def run(
    url: str,
    tool: str | None,
    arguments: dict,
    list_only: bool,
    headers: dict[str, str] | None = None,
    modern: bool = False,
    method: str | None = None,
) -> int:
    async with _session(url, headers, modern) as (session, summary):
        print(summary, file=sys.stderr)

        if method is not None:
            from typing import Any

            import mcp.types as types
            from mcp.shared.exceptions import MCPError
            from pydantic import TypeAdapter

            request = types.Request[Any, str](method=method, params=arguments)
            try:
                result = await session.send_request(request, TypeAdapter(dict[str, Any]))
            except MCPError as e:
                print(f"→ erreur JSON-RPC {e.error.code} : {e.error.message}", file=sys.stderr)
                return 1
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        tools = (await session.list_tools()).tools
        if list_only:
            for t in tools:
                print(f"{t.name}\n    {(t.description or '').splitlines()[0] if t.description else ''}")
                if t.meta:
                    print(f"    _meta {json.dumps(t.meta, ensure_ascii=False)}")
            return 0

        names = [t.name for t in tools]
        if tool not in names:
            print(f"outil inconnu : {tool}", file=sys.stderr)
            print(f"disponibles : {', '.join(names) or '(aucun)'}", file=sys.stderr)
            return 2

        result = await session.call_tool(tool, arguments)
        print(f"→ isError={result.is_error}", file=sys.stderr)
        for block in result.content:
            print(_render_content(block))
        if getattr(result, "structured_content", None):
            print("--- structuredContent ---")
            print(json.dumps(result.structured_content, indent=2, ensure_ascii=False))
        if result.meta:
            # Surface adressée au client, jamais au modèle (ex. `miaou/web`
            # de fetch_url) : c'est ici qu'on vérifie qu'elle traverse le fil.
            print("--- _meta ---")
            print(json.dumps(result.meta, indent=2, ensure_ascii=False))
        return 1 if result.is_error else 0


def _flatten(exc: BaseException) -> list[str]:
    """Aplatit un ExceptionGroup — anyio enveloppe une simple ConnectionRefusedError."""
    subs = getattr(exc, "exceptions", None)
    if subs:
        return [line for sub in subs for line in _flatten(sub)]
    return [f"{type(exc).__name__}: {exc}"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Appelle un outil sur un serveur MCP en streamable-http.",
    )
    parser.add_argument("tool", nargs="?", help='nom de l\'outil (ex. "brave__brave_search")')
    parser.add_argument("arguments", nargs="?", default="{}", help='arguments JSON (ex. \'{"query": "chat"}\')')
    parser.add_argument("--port", type=int, default=8765, help="port du serveur (défaut 8765, le proxy)")
    parser.add_argument("--host", default="127.0.0.1", help="hôte du serveur (défaut 127.0.0.1)")
    parser.add_argument("--url", help="URL complète du endpoint /mcp (prime sur --host/--port)")
    parser.add_argument("--list", action="store_true", help="liste les outils exposés (et leur _meta) et sort")
    parser.add_argument(
        "--modern",
        action="store_true",
        help="négocie la révision 2026-07-28 (server/discover) au lieu d'initialize",
    )
    parser.add_argument("--method", help="méthode JSON-RPC à envoyer (ex. skills/list), au lieu d'un outil")
    parser.add_argument("--params", default="{}", help="paramètres JSON de --method")
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        metavar="'Nom: valeur'",
        help="header HTTP libre, répétable (ex. -H 'Authorization: Bearer xxx')",
    )
    args = parser.parse_args()

    if not args.list and not args.tool and not args.method:
        parser.error("préciser un outil, ou utiliser --list ou --method")

    try:
        arguments = json.loads(args.params if args.method else args.arguments)
    except json.JSONDecodeError as exc:
        print(f"arguments JSON invalides : {exc}", file=sys.stderr)
        return 2
    if not isinstance(arguments, dict):
        print("les arguments doivent être un objet JSON", file=sys.stderr)
        return 2

    try:
        headers = _parse_headers(args.header)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    url = args.url or f"http://{args.host}:{args.port}/mcp"

    # Avant toute construction de contexte SSL, comme dans MiaouMCPBase.main()
    # et mcp_proxy.main() : un contexte déjà créé garde la classe d'origine.
    enable_system_trust_store()

    try:
        return asyncio.run(
            run(url, args.tool, arguments, args.list, headers, args.modern, args.method)
        )
    except KeyboardInterrupt:
        return 130
    except BaseException as exc:  # ExceptionGroup inclus (serveur injoignable)
        for line in _flatten(exc):
            print(f"échec : {line}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
