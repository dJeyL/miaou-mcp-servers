#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.28.1,<2"]
# ///
"""
Appel réel d'un outil MCP sur un serveur déjà lancé (banc d'essai manuel).

Ce n'est PAS un test pytest : le nom du fichier ne commence pas par "test_",
il n'est donc jamais collecté malgré sa présence dans tests/. Il parle le vrai
transport streamable-http, comme MIAOU — initialize, notifications/initialized,
tools/call — et non le stack in-process des tests unitaires.

Lancement (le serveur visé doit déjà tourner) :
    uv run tests/live_call.py brave__brave_search '{"query": "chat"}'
    uv run tests/live_call.py --port 8769 ddg_search '{"query": "chat"}'
    uv run tests/live_call.py --list                       # liste les outils
    uv run tests/live_call.py --url http://127.0.0.1:8766/mcp echo '{"text": "hi"}'

Sans argument JSON, l'outil est appelé sans arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _render_content(block) -> str:
    """Rendu lisible d'un bloc de résultat (text / image / resource)."""
    kind = getattr(block, "type", "?")
    if kind == "text":
        return block.text
    if kind == "image":
        return f"[image {block.mimeType} — {len(block.data)} octets base64]"
    if kind == "resource":
        res = block.resource
        uri = getattr(res, "uri", "?")
        mime = getattr(res, "mimeType", "?")
        if getattr(res, "text", None) is not None:
            return f"[resource {uri} ({mime})]\n{res.text}"
        blob = getattr(res, "blob", "") or ""
        return f"[resource {uri} ({mime}) — {len(blob)} octets base64]"
    return repr(block)


async def run(url: str, tool: str | None, arguments: dict, list_only: bool) -> int:
    async with streamablehttp_client(url) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(
                f"→ connecté à {init.serverInfo.name} {init.serverInfo.version} ({url})",
                file=sys.stderr,
            )

            tools = (await session.list_tools()).tools
            if list_only:
                for t in tools:
                    print(f"{t.name}\n    {(t.description or '').splitlines()[0]}")
                return 0

            names = [t.name for t in tools]
            if tool not in names:
                print(f"outil inconnu : {tool}", file=sys.stderr)
                print(f"disponibles : {', '.join(names) or '(aucun)'}", file=sys.stderr)
                return 2

            result = await session.call_tool(tool, arguments)
            print(f"→ isError={result.isError}", file=sys.stderr)
            for block in result.content:
                print(_render_content(block))
            if getattr(result, "structuredContent", None):
                print("--- structuredContent ---")
                print(json.dumps(result.structuredContent, indent=2, ensure_ascii=False))
            return 1 if result.isError else 0


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
    parser.add_argument("--list", action="store_true", help="liste les outils exposés et sort")
    args = parser.parse_args()

    if not args.list and not args.tool:
        parser.error("préciser un outil, ou utiliser --list")

    try:
        arguments = json.loads(args.arguments)
    except json.JSONDecodeError as exc:
        print(f"arguments JSON invalides : {exc}", file=sys.stderr)
        return 2
    if not isinstance(arguments, dict):
        print("les arguments doivent être un objet JSON", file=sys.stderr)
        return 2

    url = args.url or f"http://{args.host}:{args.port}/mcp"

    try:
        return asyncio.run(run(url, args.tool, arguments, args.list))
    except KeyboardInterrupt:
        return 130
    except BaseException as exc:  # ExceptionGroup inclus (serveur injoignable)
        for line in _flatten(exc):
            print(f"échec : {line}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
