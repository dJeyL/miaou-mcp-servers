"""Briques partagées par les moteurs de recherche de mcp_web.

Un moteur expose `name`, `kinds` (sous-ensemble de {"web", "images"}) et
`async search(kind, query, n, budget_s)`, qui rend une liste de résultats
normalisés ou lève `EngineFailure`. Le moteur ne décide rien de la chaîne :
c'est `SearchChain` (search/__init__.py) qui passe au suivant et qui tient les
pauses.
"""

from __future__ import annotations

import asyncio
import html
import math
import os
import re
import urllib.error
import urllib.request
from typing import Callable

from mcp_base import make_opener

# Timeout d'une requête de moteur, raboté au temps restant de l'appel `search`.
# Il porte sur chaque opération socket, pas sur la requête entière (urllib) :
# une marge, pas une garantie.
ENGINE_TIMEOUT_S = 10.0

# Pauses d'un moteur après un échec, d'un appel à l'autre. Calibrées à
# l'aveugle, sauf le défi DDG (cf. ddg.py) : aucun quota n'a été mesuré.
KEY_REJECTED_COOLDOWN_S = math.inf  # clef refusée : jusqu'au redémarrage
QUOTA_COOLDOWN_S = 600.0            # 429 : quota ou débit dépassé
TRANSIENT_COOLDOWN_S = 60.0         # réseau, timeout, 5xx, réponse illisible

# Extrait d'un résultat : la recherche d'Ollama rend du contenu de page
# (« des milliers de tokens » selon sa doc), Brave et DDG une phrase ou deux.
SNIPPET_MAX_CHARS = 400

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


class EngineFailure(Exception):
    """Échec d'un moteur pour CET appel. `cooldown_s` > 0 le met en pause pour
    les appels suivants ; 0 le laisse disponible (refus d'espacement, p.ex.)."""

    def __init__(self, reason: str, cooldown_s: float = TRANSIENT_COOLDOWN_S) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cooldown_s = cooldown_s


def clean_snippet(text: object) -> str:
    """Texte brut d'un extrait : balises retirées (Brave surligne en <strong>),
    entités décodées, blancs écrasés, coupé à SNIPPET_MAX_CHARS avec « … »."""
    if not isinstance(text, str):
        return ""
    text = _SPACE_RE.sub(" ", html.unescape(_TAG_RE.sub("", text))).strip()
    if len(text) <= SNIPPET_MAX_CHARS:
        return text
    cut = text[:SNIPPET_MAX_CHARS]
    space = cut.rfind(" ")
    if space > SNIPPET_MAX_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


def resolve_api_key(engine_config: object, env_var: str) -> str:
    """Clef d'un moteur : `api_key` de son bloc dans la config `search` d'abord,
    sinon la variable d'environnement. Vide ou blanche = absente."""
    if isinstance(engine_config, dict):
        key = engine_config.get("api_key")
        if isinstance(key, str) and key.strip():
            return key.strip()
    return os.environ.get(env_var, "").strip()


def _read_blocking(req: urllib.request.Request, timeout: float) -> bytes:
    """I/O bloquante isolée pour asyncio.to_thread."""
    opener = make_opener()
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(_MAX_RESPONSE_BYTES)


def key_rejected_by_status(code: int, body: bytes) -> bool:
    """Clef refusée, pour un moteur qui le dit en 401/403 (Ollama : 401, mesuré
    le 2026-10-05, clef fausse comme absente)."""
    return code in (401, 403)


def _error_body(e: urllib.error.HTTPError) -> bytes:
    try:
        return e.read(4096) or b""
    except Exception:  # noqa: BLE001 — le corps n'est qu'un indice
        return b""


async def http_read(
    req: urllib.request.Request,
    timeout: float,
    *,
    key_rejected: Callable[[int, bytes], bool] | None,
) -> bytes:
    """Corps de la réponse, ou EngineFailure avec la pause adaptée.

    `key_rejected(code, corps)` dit si une erreur HTTP signifie « clef
    refusée » (None : moteur sans clef). Ce cas met le moteur hors circuit
    jusqu'au redémarrage — la retenter à chaque appel ne ferait que repayer
    l'aller-retour."""
    try:
        return await asyncio.to_thread(_read_blocking, req, timeout)
    except urllib.error.HTTPError as e:
        body = _error_body(e)
        # Une HTTPError EST la réponse, socket comprise : la fermer, sinon
        # elle reste ouverte jusqu'au GC.
        e.close()
        if key_rejected is not None and key_rejected(e.code, body):
            raise EngineFailure(f"clef refusée (HTTP {e.code})", KEY_REJECTED_COOLDOWN_S) from None
        if e.code == 429:
            raise EngineFailure("quota dépassé (HTTP 429)", QUOTA_COOLDOWN_S) from None
        raise EngineFailure(f"HTTP {e.code} ({e.reason})") from None
    except urllib.error.URLError as e:
        raise EngineFailure(f"erreur réseau ({e.reason})") from None
    except TimeoutError:
        raise EngineFailure(f"timeout ({timeout:.0f} s)") from None
    except Exception as e:  # noqa: BLE001 — jamais d'exception stdlib au client
        raise EngineFailure(f"erreur inattendue ({type(e).__name__}: {e})") from None
