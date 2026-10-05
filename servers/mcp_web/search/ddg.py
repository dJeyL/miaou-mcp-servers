"""Moteur DuckDuckGo (endpoint HTML scrapé, sans clef) : web seulement.

Reprise de servers/mcp_ddg.py (déprécié), protections comprises : défi
anti-bot reconnu au lieu d'un `[]` muet, et requêtes sortantes espacées.

L'état (créneau suivant, pause après un défi) est celui de l'ADRESSE IP
SORTANTE, pas d'une config : un seul moteur `ENGINE` par processus, partagé par
toutes les instances de mcp_web. Il ne l'est pas avec mcp_ddg, qui garde son
propre espacement s'il tourne encore à côté.
"""

from __future__ import annotations

import asyncio
import math
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from .common import ENGINE_TIMEOUT_S, EngineFailure, clean_snippet, http_read

_URL = "https://html.duckduckgo.com/html/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Classe CSS de la page de défi anti-bot servie à la place des résultats (mesuré
# le 2026-10-05 : HTTP 202, formulaire vers duckduckgo.com/anomaly.js).
_ANOMALY_MARKER = "anomaly-modal"
# Pause après un défi : le blocage mesuré le 2026-10-05 a duré 2 h 15 à 2 h 30.
# Seuil et durée pour une autre IP inconnus ; le reconsulter pendant ce temps
# n'apprend rien et risque de le prolonger.
ANOMALY_COOLDOWN_S = 2.5 * 3600

# Espacement minimal entre deux requêtes sortantes du processus : une rafale de
# trois requêtes en une à deux minutes a suffi à déclencher le défi (mesuré le
# 2026-10-05 ; seuil réel inconnu). Un appel attend son créneau, au plus
# _MAX_WAIT_S et jamais au point de ne plus laisser _MIN_FETCH_S de requête dans
# le temps restant de l'appel `search` ; sinon refus, sans requête.
_MIN_INTERVAL_S = 15.0
_MAX_WAIT_S = 15.0
_MIN_FETCH_S = 3.0


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


class DDGEngine:
    name = "ddg"
    kinds = frozenset({"web"})

    def __init__(self) -> None:
        self.down_until = 0.0
        self.down_reason = ""
        # Instant monotonic à partir duquel la prochaine requête peut partir.
        self.next_slot = 0.0

    async def search(self, kind: str, query: str, n: int, budget_s: float) -> list[dict]:
        # Réservation du créneau sans await entre lecture et écriture :
        # atomique sur l'event loop, aucun verrou nécessaire.
        now = time.monotonic()
        slot = max(now, self.next_slot)
        wait = slot - now
        if wait > _MAX_WAIT_S or wait + _MIN_FETCH_S > budget_s:
            raise EngineFailure(
                f"requêtes espacées d'au moins {_MIN_INTERVAL_S:.0f} s, "
                f"prochain créneau dans {max(1, math.ceil(wait))} s",
                0,
            )
        self.next_slot = slot + _MIN_INTERVAL_S
        if wait > 0:
            await asyncio.sleep(wait)

        req = urllib.request.Request(
            _URL,
            data=urllib.parse.urlencode({"q": query, "b": ""}).encode(),
            headers={
                "User-Agent": _UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        raw = await http_read(req, min(ENGINE_TIMEOUT_S, budget_s - wait), key_rejected=None)
        html_text = raw.decode("utf-8", errors="replace")
        parser = _DDGParser()
        parser.feed(html_text)
        results = parser.results()
        if not results and _ANOMALY_MARKER in html_text:
            # Défi anti-bot (HTTP 202, captcha « select the ducks ») : sans ce
            # test, la recherche rendait [] — indiscernable d'un vrai vide.
            raise EngineFailure("défi anti-bot sur l'adresse IP sortante", ANOMALY_COOLDOWN_S)
        return [
            {"title": clean_snippet(r["title"]), "url": r["url"], "snippet": clean_snippet(r["snippet"])}
            for r in results
            if r["url"]
        ][:n]


ENGINE = DDGEngine()
