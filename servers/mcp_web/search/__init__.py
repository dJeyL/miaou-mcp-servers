"""Recherche multi-moteurs de mcp_web : config, chaîne de repli, pauses.

Config (bloc `config` de l'entrée `web` de config.json, clé `search`) :

    "search": {
      "order": ["brave", "ollama", "ddg"],
      "brave":  {"api_key": "..."},
      "ollama": {"api_key": "..."}
    }

`order` absent : DEFAULT_ORDER. Un moteur absent de `order` est désactivé ; un
moteur à clef sans clef (ni dans son bloc, ni dans l'environnement) est écarté
au démarrage, avec une ligne qui le dit. Si `order` cite des moteurs et
qu'AUCUN n'est utilisable, la construction échoue, comme mcp_brave sans clef :
pas de serveur qui démarre sans la recherche qu'on lui a demandée — et le proxy
écarte alors l'upstream ENTIER, outils fetch_* compris. `order: []` coupe la
recherche exprès et ne lève pas. Un nom inconnu ou répété fait aussi échouer la
construction : une faute de frappe qui désactiverait un moteur en silence serait
pire.

Un appel essaie les moteurs dans l'ordre et s'arrête au premier qui RÉPOND,
liste vide comprise : un vide est une réponse, et replier dessus ferait finir
sur DDG chaque recherche sans résultat. Un échec fait passer au suivant et peut
mettre le moteur en pause pour les appels suivants (cf. common.EngineFailure).
"""

from __future__ import annotations

import math
import time

from . import brave, ddg, ollama
from .common import EngineFailure, resolve_api_key

ENGINE_NAMES = ("brave", "ollama", "ddg")
DEFAULT_ORDER = ENGINE_NAMES
_KEYED = {
    "brave": (brave.BraveEngine, brave.API_KEY_ENV),
    "ollama": (ollama.OllamaEngine, ollama.API_KEY_ENV),
}

MAX_RESULTS = 10
SEARCH_META_KEY = "miaou/search"
"""Clé du `_meta` de search/image_search : `{"engine": <nom>}`, pour que le
client affiche le moteur sans fouiller le contenu. Distincte de `miaou/web`,
que MIAOU lit comme l'en-tête de page de fetch_url (champs tous facultatifs :
un `{engine}` y passerait pour une source vide)."""
# Durée totale d'un appel `search`, chaîne entière : sous les 30 s de timeout
# MIAOU→MCP suggérés par défaut. Chaque moteur reçoit ce qui reste.
SEARCH_BUDGET_S = 25.0
# En deçà, un moteur n'est même pas tenté.
_MIN_ATTEMPT_S = 2.0


class SearchConfigError(ValueError):
    """Config `search` invalide (nom de moteur inconnu, répété, type)."""


def _format_duration(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d}"


class SearchChain:
    def __init__(self, engines: list, notes: list[str]) -> None:
        self.engines = engines
        # Lignes de démarrage : moteurs écartés et pourquoi.
        self.notes = notes

    def names(self, kind: str) -> list[str]:
        return [e.name for e in self.engines if kind in e.kinds]

    def summary(self) -> str:
        web = " → ".join(self.names("web")) or "aucun moteur"
        line = f"recherche via {web}"
        images = self.names("images")
        if images:
            line += f" ; images via {' → '.join(images)}"
        return "; ".join([line, *self.notes])

    async def run(self, kind: str, query: str, n: int) -> dict:
        """`{"engine", "results"}` du premier moteur qui répond, plus
        `fallback` (moteurs écartés pour cet appel, et pourquoi) s'il y en a.
        `engine` vaut None si aucun n'a répondu."""
        deadline = time.monotonic() + SEARCH_BUDGET_S
        fallback: list[dict] = []
        for engine in self.engines:
            if kind not in engine.kinds:
                continue
            now = time.monotonic()
            if engine.down_until > now:
                if math.isinf(engine.down_until):
                    pause = "hors circuit jusqu'au redémarrage du serveur"
                else:
                    pause = f"en pause encore {_format_duration(engine.down_until - now)}"
                fallback.append({"engine": engine.name, "reason": f"{pause} — {engine.down_reason}"})
                continue
            remaining = deadline - now
            if remaining < _MIN_ATTEMPT_S:
                fallback.append({"engine": engine.name, "reason": "plus assez de temps dans l'appel"})
                continue
            try:
                results = await engine.search(kind, query, n, remaining)
            except EngineFailure as failure:
                if failure.cooldown_s > 0:
                    engine.down_until = time.monotonic() + failure.cooldown_s
                    engine.down_reason = failure.reason
                fallback.append({"engine": engine.name, "reason": failure.reason})
                continue
            outcome: dict = {"engine": engine.name, "results": results}
            if fallback:
                outcome["fallback"] = fallback
            return outcome
        return {"engine": None, "results": [], "fallback": fallback}


def build_chain(config: dict | None) -> SearchChain:
    """Chaîne de moteurs depuis le bloc `config` de l'entrée `web`.
    Lève SearchConfigError sur une config `search` invalide."""
    search_cfg = (config or {}).get("search", {})
    if not isinstance(search_cfg, dict):
        raise SearchConfigError("config « search » : objet attendu")
    order = search_cfg.get("order", DEFAULT_ORDER)
    if not isinstance(order, (list, tuple)) or not all(isinstance(x, str) for x in order):
        raise SearchConfigError("config « search.order » : liste de noms de moteurs attendue")
    known = ", ".join(ENGINE_NAMES)
    seen: set[str] = set()
    for name in order:
        if name not in ENGINE_NAMES:
            raise SearchConfigError(f"config « search.order » : moteur inconnu « {name} » (connus : {known})")
        if name in seen:
            raise SearchConfigError(f"config « search.order » : moteur « {name} » répété")
        seen.add(name)

    engines: list = []
    notes: list[str] = []
    for name in order:
        if name == "ddg":
            engines.append(ddg.ENGINE)
            continue
        engine_cls, env_var = _KEYED[name]
        key = resolve_api_key(search_cfg.get(name), env_var)
        if not key:
            notes.append(f"{name} écarté (aucune clef : config search.{name}.api_key ou {env_var})")
            continue
        engines.append(engine_cls(key))
    if order and not engines:
        # Comme mcp_brave sans clef : refuser plutôt que démarrer sans la
        # recherche demandée. `order: []` reste le moyen de la couper exprès.
        raise SearchConfigError("aucun moteur de recherche utilisable — " + "; ".join(notes))
    return SearchChain(engines, notes)
