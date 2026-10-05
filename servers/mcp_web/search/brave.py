"""Moteur Brave Search (API à clef) : web et images.

Reprise de servers/mcp_brave.py (déprécié), mapping normalisé sur la forme
commune de `search`/`image_search`.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from .common import EngineFailure, clean_snippet, http_read, key_rejected_by_status

API_KEY_ENV = "BRAVE_API_KEY"
_WEB_URL = "https://api.search.brave.com/res/v1/web/search"
_IMAGES_URL = "https://api.search.brave.com/res/v1/images/search"


def _key_rejected(code: int, body: bytes) -> bool:
    """Brave refuse une clef invalide en 422, pas en 401 (mesuré le 2026-10-05 :
    `{"error": {"code": "SUBSCRIPTION_TOKEN_INVALID", "meta": {"component":
    "authentication"}}}`). Un 422 sert aussi aux paramètres invalides : seul le
    composant `authentication` désigne la clef."""
    if key_rejected_by_status(code, body):
        return True
    if code != 422:
        return False
    try:
        error = json.loads(body.decode("utf-8", errors="replace")).get("error", {})
        return error.get("meta", {}).get("component") == "authentication"
    except (ValueError, AttributeError):
        return False


class BraveEngine:
    name = "brave"
    kinds = frozenset({"web", "images"})

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self.down_until = 0.0
        self.down_reason = ""

    async def search(self, kind: str, query: str, n: int, budget_s: float) -> list[dict]:
        url = _IMAGES_URL if kind == "images" else _WEB_URL
        params = urllib.parse.urlencode({"q": query, "count": n})
        req = urllib.request.Request(
            f"{url}?{params}",
            headers={"Accept": "application/json", "X-Subscription-Token": self._api_key},
        )
        raw = await http_read(req, budget_s, key_rejected=_key_rejected)
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            raise EngineFailure("réponse illisible (JSON malformé)") from None
        if not isinstance(data, dict):
            raise EngineFailure("réponse illisible (JSON inattendu)")
        if kind == "images":
            return _map_images(data)[:n]
        return _map_web(data)[:n]


def _map_web(data: dict) -> list[dict]:
    web = data.get("web")
    # Pas de bloc `web` : Brave n'a rien trouvé, c'est une réponse.
    results = web.get("results", []) if isinstance(web, dict) else []
    if not isinstance(results, list):
        raise EngineFailure("réponse illisible (champ 'results' inattendu)")
    return [
        {
            "title": clean_snippet(r.get("title")),
            "url": r.get("url", ""),
            "snippet": clean_snippet(r.get("description")),
        }
        for r in results
        if isinstance(r, dict) and r.get("url")
    ]


def _map_images(data: dict) -> list[dict]:
    results = data.get("results", [])
    if not isinstance(results, list):
        raise EngineFailure("réponse illisible (champ 'results' inattendu)")
    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        props = r.get("properties")
        image_url = props.get("url") if isinstance(props, dict) else None
        if not image_url:
            continue  # pas d'image exploitable
        thumb = r.get("thumbnail")
        out.append({
            "title": clean_snippet(r.get("title")),
            "page_url": r.get("url", ""),
            "image_url": image_url,
            "thumbnail_url": thumb.get("src", "") if isinstance(thumb, dict) else "",
            "source": r.get("source", ""),
        })
    return out
