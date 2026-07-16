#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.28.1", "uvicorn", "starlette"]
# ///
"""
Serveur MCP DuckDuckGo pour MIAOU.

Transport streamable-http (single endpoint POST, réponses en SSE). CORS ouvert
pour permettre au navigateur de l'atteindre directement depuis dist/miaou.html.

Outils exposés :
  - ddg_search(query, max_results=5) : recherche web via l'endpoint HTML de DDG
    (pas de clé API requise). Résultats : JSON array {title, url, snippet}.

Note : basé sur le scraping HTML de html.duckduckgo.com — fragile si DDG change
son markup (classes result__a / result__snippet au moment de l'écriture).

Lancement :
    uv run servers/mcp_ddg.py                          # HTTP sur 127.0.0.1:8769
    uv run servers/mcp_ddg.py --transport stdio        # stdin/stdout
    uv run servers/mcp_ddg.py --host 0.0.0.0           # HTTP sur toutes interfaces

Dans MIAOU → Paramètres → Serveurs MCP → Ajouter :
    Nom       : ddg
    URL       : http://127.0.0.1:8769/mcp
    Transport : streamable-http   (deviné depuis /mcp)
    Activé    : oui
"""

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from mcp import types

from mcp_base import MiaouMCPBase, make_opener

_DDG_URL = "https://html.duckduckgo.com/html/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class _DDGParser(HTMLParser):
    """Extrait les résultats de recherche du markup HTML de DuckDuckGo."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._results: list[dict[str, str]] = []
        self._capture: str | None = None  # "title" | "snippet"
        self._capture_tag: str | None = None
        self._current: dict[str, str] | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capture is not None:
            return
        attr_dict = dict(attrs)
        classes = attr_dict.get("class", "") or ""

        if "result__a" in classes:
            href = attr_dict.get("href", "") or ""
            self._current = {"title": "", "url": href, "snippet": ""}
            self._capture = "title"
            self._capture_tag = tag
            self._buf = []
        elif "result__snippet" in classes and self._current is not None:
            self._capture = "snippet"
            self._capture_tag = tag
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if self._capture is None or tag != self._capture_tag:
            return
        text = "".join(self._buf).strip()
        if self._capture == "title" and self._current is not None:
            self._current["title"] = text
            self._results.append(self._current)
        elif self._capture == "snippet" and self._results:
            self._results[-1]["snippet"] = text
        self._capture = None
        self._capture_tag = None
        self._buf = []

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buf.append(data)

    def results(self) -> list[dict[str, str]]:
        return self._results


def _fetch_ddg_html(req: urllib.request.Request) -> str:
    """I/O bloquante isolée pour asyncio.to_thread (T2)."""
    opener = make_opener()
    with opener.open(req, timeout=10) as resp:
        return resp.read(2 * 1024 * 1024).decode("utf-8", errors="replace")


class DDGServer(MiaouMCPBase):
    def __init__(self) -> None:
        super().__init__("miaou-ddg", default_port=8769)

        @self.mcp.tool()
        async def ddg_search(
            query: str,
            max_results: int = 5,
        ) -> str | types.EmbeddedResource:
            """Recherche sur DuckDuckGo (endpoint HTML, pas de clé API). Renvoie un tableau JSON [{title, url, snippet}]. max_results borné à [1, 30]. Fragile si DDG change son markup."""
            max_results = max(1, min(max_results, 30))
            body = urllib.parse.urlencode({"q": query, "b": ""}).encode()
            req = urllib.request.Request(
                _DDG_URL,
                data=body,
                headers={
                    "User-Agent": _UA,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            )
            try:
                html = await asyncio.to_thread(_fetch_ddg_html, req)
            except urllib.error.HTTPError as e:
                return f"Erreur HTTP {e.code} ({e.reason}) — DuckDuckGo"
            except urllib.error.URLError as e:
                return f"Erreur réseau ({e.reason}) — DuckDuckGo"
            except TimeoutError:
                return "Timeout (10 s) — DuckDuckGo"
            except Exception as e:
                return f"Erreur inattendue ({type(e).__name__}: {e}) — DuckDuckGo"

            parser = _DDGParser()
            parser.feed(html)
            results = parser.results()[:max_results]

            return types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri=f"miaou://ddg/{urllib.parse.quote(query)}",  # type: ignore[arg-type]
                    mimeType="application/json",
                    text=json.dumps(results, ensure_ascii=False),
                ),
            )

        self.finalize_tools()


server = DDGServer()
mcp = server.mcp  # exposé pour le proxy in-process

if __name__ == "__main__":
    server.main()
