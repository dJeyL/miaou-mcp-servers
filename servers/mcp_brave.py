#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2", "uvicorn", "starlette"]
# ///
"""
Serveur MCP Brave Search pour MIAOU.

Transport streamable-http (single endpoint POST, réponses en SSE). CORS ouvert
pour permettre au navigateur de l'atteindre directement depuis dist/miaou.html.

Outils exposés :
  - brave_search(query, count=5) : recherche web via l'API Brave Search.
    Résultats : JSON array {title, url, description}.
    Requiert BRAVE_API_KEY dans l'environnement (ou via config.json → env).
  - brave_image_search(query, count=5) : recherche d'images via l'API Brave Search.
    Résultats : JSON array {title, page_url, image_url, thumbnail_url, source}.
    Requiert BRAVE_API_KEY dans l'environnement (ou via config.json → env).

Lancement :
    BRAVE_API_KEY=<key> uv run servers/mcp_brave.py       # HTTP sur 127.0.0.1:8770
    uv run servers/mcp_brave.py --transport stdio          # stdin/stdout

Dans MIAOU → Paramètres → Serveurs MCP → Ajouter :
    Nom       : brave
    URL       : http://127.0.0.1:8770/mcp
    Transport : streamable-http   (deviné depuis /mcp)
    Activé    : oui
"""

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from mcp import types

from mcp_base import MiaouMCPBase, make_opener

_BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_IMAGES_API_URL = "https://api.search.brave.com/res/v1/images/search"


def _clamp_count(count: int) -> int:
    return max(1, min(count, 20))


def _fetch_brave_bytes(req: urllib.request.Request) -> bytes:
    """I/O bloquante isolée pour asyncio.to_thread (T2)."""
    opener = make_opener()
    with opener.open(req, timeout=10) as resp:
        return resp.read(2 * 1024 * 1024)


async def _brave_call(api_url: str, query: str, count: int, label: str) -> bytes | str:
    """Requête GET commune à brave_search/brave_image_search : clé API,
    construction de la requête, chaîne d'erreurs réseau. Renvoie le corps brut
    en cas de succès, ou un message d'erreur clair en français en cas d'échec."""
    api_key = os.environ.get("BRAVE_API_KEY", "")
    if not api_key:
        return "Erreur : BRAVE_API_KEY absent ou vide. Configurer la variable d'environnement (ou via config.json → env pour le mode inprocess)."

    params = urllib.parse.urlencode({"q": query, "count": count})
    req = urllib.request.Request(
        f"{api_url}?{params}",
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        },
    )
    try:
        return await asyncio.to_thread(_fetch_brave_bytes, req)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "Erreur 401 : clé API Brave invalide ou expirée."
        if e.code == 429:
            return f"Erreur 429 : quota {label} dépassé."
        return f"Erreur HTTP {e.code} ({e.reason}) — {label}"
    except urllib.error.URLError as e:
        return f"Erreur réseau ({e.reason}) — {label}"
    except TimeoutError:
        return f"Timeout (10 s) — {label}"
    except Exception as e:
        return f"Erreur inattendue ({type(e).__name__}: {e}) — {label}"


class BraveServer(MiaouMCPBase):
    def __init__(self) -> None:
        super().__init__("miaou-brave", default_port=8770)

        @self.mcp.tool()
        async def brave_search(
            query: str,
            count: int = 5,
        ) -> str | types.EmbeddedResource:
            """Recherche web via l'API Brave Search. Renvoie un tableau JSON [{title, url, description}]. Requiert BRAVE_API_KEY dans l'environnement."""
            raw = await _brave_call(_BRAVE_API_URL, query, _clamp_count(count), "Brave Search")
            if isinstance(raw, str):
                return raw

            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as e:
                return f"Réponse invalide de Brave Search (JSON malformé : {e})."

            web_results = data.get("web", {}).get("results", [])
            results = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "description": r.get("description", ""),
                }
                for r in web_results
            ]

            return types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri=f"miaou://brave/{urllib.parse.quote(query)}",  # type: ignore[arg-type]
                    mimeType="application/json",
                    text=json.dumps(results, ensure_ascii=False),
                ),
            )

        @self.mcp.tool()
        async def brave_image_search(
            query: str,
            count: int = 5,
        ) -> str | types.EmbeddedResource:
            """Recherche d'images via l'API Brave Search. Renvoie un tableau JSON [{title, page_url, image_url, thumbnail_url, source}] — index d'URLs seulement, pas les données binaires. Requiert BRAVE_API_KEY dans l'environnement."""
            raw = await _brave_call(
                _BRAVE_IMAGES_API_URL, query, _clamp_count(count), "Brave Image Search"
            )
            if isinstance(raw, str):
                return raw

            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as e:
                return f"Réponse invalide de Brave Image Search (JSON malformé : {e})."

            raw_results = data.get("results", [])
            results = [
                {
                    "title": r.get("title", ""),
                    "page_url": r.get("url", ""),
                    "image_url": r["properties"]["url"],  # absent → entry dropped (no usable image)
                    "thumbnail_url": (r.get("thumbnail") or {}).get("src", ""),
                    "source": r.get("source", ""),
                }
                for r in raw_results
                if r.get("properties", {}).get("url")
            ]

            return types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri=f"miaou://brave-images/{urllib.parse.quote(query)}",  # type: ignore[arg-type]
                    mimeType="application/json",
                    text=json.dumps(results, ensure_ascii=False),
                ),
            )


server = BraveServer()
mcp = server.mcp  # exposé pour le proxy in-process

if __name__ == "__main__":
    server.main()
