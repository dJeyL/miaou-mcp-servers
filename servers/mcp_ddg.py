#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.2,<3", "uvicorn", "starlette", "truststore"]
# ///
"""
Serveur MCP DuckDuckGo pour MIAOU — DÉPRÉCIÉ : la recherche passe par l'outil
`search` de mcp_web (moteur `ddg`, mêmes protections). Conservé le temps de la
transition, il le signale à chaque démarrage.

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
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Annotated

from mcp import types
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from mcp_base import MiaouMCPBase, make_opener

_DDG_URL = "https://html.duckduckgo.com/html/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Classe CSS de la page de défi anti-bot servie à la place des résultats (mesuré
# le 2026-10-05 : HTTP 202, formulaire vers duckduckgo.com/anomaly.js).
_ANOMALY_MARKER = "anomaly-modal"

# Espacement minimal entre deux requêtes sortantes de CE processus : une rafale
# de trois requêtes en une à deux minutes a suffi à déclencher le défi, levé
# plus de 2 h plus tard (mesuré le 2026-10-05 ; seuil réel inconnu). Un appel
# attend son créneau, sauf si l'attente dépasserait _MAX_WAIT_S : refus
# immédiat. Budget : _MAX_WAIT_S + _FETCH_TIMEOUT_S reste sous les 30 s de
# timeout MIAOU→MCP suggérés par défaut (le timeout urllib porte sur chaque
# opération socket, pas sur la requête entière : marge, pas garantie).
_MIN_INTERVAL_S = 15.0
_MAX_WAIT_S = 15.0
_FETCH_TIMEOUT_S = 10


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
    with opener.open(req, timeout=_FETCH_TIMEOUT_S) as resp:
        return resp.read(2 * 1024 * 1024).decode("utf-8", errors="replace")


class DDGServer(MiaouMCPBase):
    def __init__(self) -> None:
        super().__init__("miaou-ddg", default_port=8769)
        # Instant monotonic à partir duquel la prochaine requête peut partir.
        self._next_slot = 0.0

        async def ddg_search(
            query: str,
            max_results: Annotated[
                int,
                Field(description="Nombre maximal de résultats, silencieusement ramené dans [1, 30]."),
            ] = 5,
        ) -> str | types.EmbeddedResource:
            max_results = max(1, min(max_results, 30))
            # Réservation du créneau sans await entre lecture et écriture :
            # atomique sur l'event loop, aucun verrou nécessaire.
            now = time.monotonic()
            slot = max(now, self._next_slot)
            wait = slot - now
            if wait > _MAX_WAIT_S:
                return (
                    f"Trop de recherches rapprochées — réessayer dans {max(1, math.ceil(wait - _MAX_WAIT_S))} s "
                    f"(au plus une requête toutes les {_MIN_INTERVAL_S:.0f} s vers DuckDuckGo, "
                    "qui bloque l'adresse IP pendant des heures après une rafale)"
                )
            self._next_slot = slot + _MIN_INTERVAL_S
            if wait > 0:
                await asyncio.sleep(wait)
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
                # Une HTTPError EST la réponse, socket comprise : la fermer, sinon
                # elle reste ouverte jusqu'au GC (même correctif que mcp_web).
                e.close()
                return f"Erreur HTTP {e.code} ({e.reason}) — DuckDuckGo"
            except urllib.error.URLError as e:
                return f"Erreur réseau ({e.reason}) — DuckDuckGo"
            except TimeoutError:
                return f"Timeout ({_FETCH_TIMEOUT_S} s) — DuckDuckGo"
            except Exception as e:
                return f"Erreur inattendue ({type(e).__name__}: {e}) — DuckDuckGo"

            parser = _DDGParser()
            parser.feed(html)
            results = parser.results()[:max_results]
            if not results and _ANOMALY_MARKER in html:
                # Défi anti-bot (HTTP 202, captcha « select the ducks ») : sans ce
                # test, l'outil rendait [] — indiscernable d'une recherche vide.
                return (
                    "Recherche bloquée par DuckDuckGo (défi anti-bot sur l'adresse IP "
                    "sortante, souvent après une rafale de requêtes) — réessayer plus tard"
                )

            return types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri=f"miaou://ddg/{urllib.parse.quote(query)}",  # type: ignore[arg-type]
                    mimeType="application/json",
                    text=json.dumps(results, ensure_ascii=False),
                ),
            )

        ddg_search.__doc__ = f"""Recherche sur DuckDuckGo (endpoint HTML, pas de clé API). Renvoie un tableau JSON [{{title, url, snippet}}]. max_results borné à [1, 30]. Fragile si DDG change son markup. Requêtes espacées d'au moins {_MIN_INTERVAL_S:.0f} s : un appel attend son tour jusqu'à {_MAX_WAIT_S:.0f} s, au-delà il est refusé — lancer les recherches une par une plutôt qu'en rafale."""
        self.mcp.tool(name="ddg_search")(ddg_search)

        self.finalize_tools()


DEPRECATION_NOTICE = (
    "Attention : mcp_ddg est déprécié — la recherche passe par l'outil search de "
    "mcp_web (moteur ddg). Actif en même temps que lui, il ne partage pas son "
    "espacement des requêtes : DuckDuckGo reçoit les deux flux depuis la même adresse IP."
)


def build(config: dict | None = None) -> MCPServer:
    """Factory appelée par InProcessUpstream.start() du proxy : signale la
    dépréciation et rend le singleton — une seule instance, donc un seul
    espacement, quel que soit le nombre d'entrées."""
    print(DEPRECATION_NOTICE, file=sys.stderr)
    return server.mcp


server = DDGServer()
mcp = server.mcp  # exposé pour le proxy in-process

if __name__ == "__main__":
    print(DEPRECATION_NOTICE, file=sys.stderr)
    server.main()
