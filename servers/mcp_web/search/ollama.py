"""Moteur de recherche web d'Ollama (API à clef, compte ollama.com gratuit).

`POST https://ollama.com/api/web_search`, `Authorization: Bearer <clef>`,
corps `{query, max_results}` (défaut 5, plafond 10 côté API), réponse
`{results: [{title, url, content}]}`. `content` est du contenu de page, long :
il est réduit à un extrait comme celui des autres moteurs.
"""

from __future__ import annotations

import json
import urllib.request

from .common import EngineFailure, clean_snippet, http_read, key_rejected_by_status

API_KEY_ENV = "OLLAMA_API_KEY"
_URL = "https://ollama.com/api/web_search"


class OllamaEngine:
    name = "ollama"
    kinds = frozenset({"web"})

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self.down_until = 0.0
        self.down_reason = ""

    async def search(self, kind: str, query: str, n: int, budget_s: float) -> list[dict]:
        req = urllib.request.Request(
            _URL,
            data=json.dumps({"query": query, "max_results": n}).encode(),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        raw = await http_read(req, budget_s, key_rejected=key_rejected_by_status)
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            raise EngineFailure("réponse illisible (JSON malformé)") from None
        results = data.get("results", []) if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise EngineFailure("réponse illisible (champ 'results' inattendu)")
        return [
            {
                "title": clean_snippet(r.get("title")),
                "url": r.get("url", ""),
                "snippet": clean_snippet(r.get("content")),
            }
            for r in results
            if isinstance(r, dict) and r.get("url")
        ][:n]
