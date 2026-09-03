#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.28.1,<2", "uvicorn", "starlette"]
# ///
"""
Serveur MCP Brave Search pour MIAOU.

Transport streamable-http (single endpoint POST, réponses en SSE). CORS ouvert
pour permettre au navigateur de l'atteindre directement depuis dist/miaou.html.

Outils exposés :
  - brave_search(query, count=5) : recherche web via l'API Brave Search.
    Résultats : JSON array {title, url, description}.
  - brave_image_search(query, count=5) : recherche d'images via l'API Brave Search.
    Résultats : JSON array {title, page_url, image_url, thumbnail_url, source}.

Clef d'API : BRAVE_API_KEY dans l'environnement, ou clé "api_key" du bloc
"config" de l'entrée config.json (mode inprocess). Le serveur refuse de
s'initialiser sans clef — voir build() plus bas.

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
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Annotated

from mcp import types
from mcp.server.fastmcp import FastMCP
from pydantic import Field

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


def resolve_api_key(config: dict | None = None) -> str:
    """Clef d'API Brave : bloc "config" de l'entrée config.json d'abord, sinon
    l'environnement. Chaîne vide si aucune des deux sources n'en fournit une.

    Le bloc config prime pour permettre plusieurs entrées inprocess du même
    module avec des clefs différentes (cf. pattern build(config) du proxy) :
    os.environ est partagé par tout le process, il ne peut pas les distinguer."""
    if config:
        key = config.get("api_key")
        if isinstance(key, str) and key.strip():
            return key.strip()
    return os.environ.get("BRAVE_API_KEY", "").strip()


async def _brave_call(
    api_url: str, query: str, count: int, label: str, api_key: str
) -> bytes | str:
    """Requête GET commune à brave_search/brave_image_search : construction de
    la requête et chaîne d'erreurs réseau. Renvoie le corps brut en cas de
    succès, ou un message d'erreur clair en français en cas d'échec."""
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


class MissingAPIKeyError(RuntimeError):
    """Clef d'API Brave absente à l'initialisation du serveur."""


class BraveServer(MiaouMCPBase):
    def __init__(self, config: dict | None = None, *, require_api_key: bool = True) -> None:
        super().__init__("miaou-brave", default_port=8770, config=config)
        self.api_key = resolve_api_key(self.config)
        if require_api_key and not self.api_key:
            raise MissingAPIKeyError(
                "clef d'API Brave absente : renseigner BRAVE_API_KEY dans "
                "l'environnement, ou la clé \"api_key\" du bloc \"config\" de "
                "l'entrée brave dans config.json."
            )

        @self.mcp.tool()
        async def brave_search(
            query: str,
            count: Annotated[
                int,
                Field(description="Nombre de résultats, silencieusement ramené dans [1, 20]."),
            ] = 5,
        ) -> str | types.EmbeddedResource:
            """Recherche web via l'API Brave Search. Renvoie un tableau JSON [{title, url, description}]. count borné à [1, 20]."""
            raw = await _brave_call(
                _BRAVE_API_URL, query, _clamp_count(count), "Brave Search", self.api_key
            )
            if isinstance(raw, str):
                return raw

            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as e:
                return f"Réponse invalide de Brave Search (JSON malformé : {e})."

            web = data.get("web")
            web_results = web.get("results", []) if isinstance(web, dict) else []
            if not isinstance(web_results, list):
                return "Réponse invalide de Brave Search (JSON malformé : champ 'results' inattendu)."
            results = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "description": r.get("description", ""),
                }
                for r in web_results
                if isinstance(r, dict)
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
            count: Annotated[
                int,
                Field(description="Nombre d'images, silencieusement ramené dans [1, 20]."),
            ] = 5,
        ) -> str | types.EmbeddedResource:
            """Recherche d'images via l'API Brave Search. Renvoie un tableau JSON [{title, page_url, image_url, thumbnail_url, source}] — index d'URLs seulement, pas les données binaires. count borné à [1, 20]."""
            raw = await _brave_call(
                _BRAVE_IMAGES_API_URL,
                query,
                _clamp_count(count),
                "Brave Image Search",
                self.api_key,
            )
            if isinstance(raw, str):
                return raw

            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as e:
                return f"Réponse invalide de Brave Image Search (JSON malformé : {e})."

            raw_results = data.get("results", [])
            if not isinstance(raw_results, list):
                return "Réponse invalide de Brave Image Search (JSON malformé : champ 'results' inattendu)."
            results = [
                {
                    "title": r.get("title", ""),
                    "page_url": r.get("url", ""),
                    "image_url": r["properties"]["url"],  # absent → entry dropped (no usable image)
                    "thumbnail_url": (r.get("thumbnail") or {}).get("src", ""),
                    "source": r.get("source", ""),
                }
                for r in raw_results
                if isinstance(r, dict) and r.get("properties", {}).get("url")
            ]

            return types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri=f"miaou://brave-images/{urllib.parse.quote(query)}",  # type: ignore[arg-type]
                    mimeType="application/json",
                    text=json.dumps(results, ensure_ascii=False),
                ),
            )

        self.finalize_tools()


def build(config: dict | None = None) -> FastMCP:
    """Factory appelée par InProcessUpstream.start() du proxy.

    Lève MissingAPIKeyError si aucune clef n'est disponible : le serveur refuse
    de s'initialiser plutôt que d'exposer des outils qui échoueraient à chaque
    appel. Le proxy rattrape cette erreur au démarrage et continue de servir les
    autres upstreams.
    """
    return BraveServer(config).mcp


# Singleton de compatibilité (import direct, mode standalone, tests). Contrairement
# à build(), il ne lève pas à l'import : `import mcp_brave` doit rester possible
# sans clef, sinon un simple import — y compris depuis un autre serveur ou un test —
# casserait. Le refus sans clef est porté par build() côté proxy et par main()
# côté standalone.
server = BraveServer(require_api_key=False)
mcp = server.mcp  # exposé pour le proxy in-process

if __name__ == "__main__":
    if not server.api_key:
        print(
            "Erreur : clef d'API Brave absente. Renseigner BRAVE_API_KEY dans "
            "l'environnement avant de lancer le serveur.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    server.main()
