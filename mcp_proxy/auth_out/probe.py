"""Choix de l'outil de sonde et lecture de ce que l'upstream répond.

Ce que `UpstreamAuthorizer` a besoin de savoir pour provoquer un refus sans
rien casser chez l'upstream : quel outil appeler (jamais un outil d'écriture)
et comment interpréter la réponse obtenue.
"""

from __future__ import annotations

import json

_MCP_PROTOCOL_VERSION = "2025-06-18"
"""Version annoncée par la requête d'amorçage d'`UpstreamAuthorizer`.

Ne concerne QUE cette poignée de requêtes : les sessions réelles sont ouvertes
par le SDK, qui négocie sa propre version. Un serveur qui n'accepterait pas
celle-ci répondrait une erreur de protocole — laquelle n'empêche pas le 401 de
`tools/call`, seul événement recherché ici.
"""

_AUTH_PROBE_TOOL = "__miaou_authorization_probe__"

"""Nom d'outil de repli, quand `tools/list` n'apprend rien.

**Ce n'est PAS le choix par défaut, et l'histoire vaut d'être connue.** Il l'a
été, sur la foi d'une mesure mal lue : un 401 obtenu sur `tools/call` semblait
prouver que le refus d'autorisation précédait la résolution du nom. Il ne le
prouvait pas — cette mesure portait sur un outil qui EXISTE. Reprise avec ce
nom-ci, elle rend `403 No matching resource found in the API` : la passerelle
(WSO2) route par ressource et rejette un nom inconnu **avant** toute question
d'autorisation. Un nom inventé ne peut donc jamais amorcer le parcours là-bas.

On préfère un outil réel, choisi en lecture seule (cf. `_pick_probe_tool`). Ce
repli ne sert que si aucun n'est trouvable, où il vaut mieux qu'une requête non
émise : sur un serveur qui, lui, refuse avant de résoudre, il fonctionne.
"""

# Verbes trahissant un outil à EFFET DE BORD. La sonde d'autorisation en appelle
# un pour se faire refuser : ce doit être une LECTURE. Un faux positif ne coûte
# qu'un candidat écarté ; un faux négatif crée un ticket ou envoie un message.
#
# Liste dupliquée dans `tests/live_auth_probe.py`, qui est un script autonome
# (bloc PEP 723) : l'importer d'ici y tirerait tout le proxy. Même arbitrage que
# le helper TLS de `live_call.py` — toute évolution est à répercuter.
_MUTATING_HINTS = (
    "add", "append", "archive", "assign", "cancel", "clear", "close", "comment",
    "create", "delete", "destroy", "edit", "insert", "move", "patch", "post",
    "publish", "purge", "push", "put", "remove", "rename", "replace", "reset",
    "restore", "run", "send", "set", "start", "stop", "submit", "transition",
    "trigger", "update", "upload", "write",
)


def _looks_mutating(name: str) -> bool:
    """Le nom d'outil évoque-t-il une écriture ?

    Découpage sur les séparateurs usuels (`__`, `_`, `-`, `.`) plus les
    frontières de casse, pour attraper `createIssue` comme `create_issue` ou
    `jira__add-comment`. On compare des SEGMENTS, jamais des sous-chaînes :
    `update` doit écarter `update_issue` sans écarter `list_updates`.
    """
    import re

    segments = re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", name)
    return any(seg.lower() in _MUTATING_HINTS for seg in segments if seg)


def _pick_probe_tool(listed_body: str) -> str | None:
    """Un outil RÉEL et en lecture seule dans une réponse `tools/list`.

    Réel parce qu'une passerelle qui route par ressource rejette un nom inconnu
    avant d'évaluer l'autorisation ; en lecture seule parce qu'obtenir un jeton
    ne doit pas exécuter une action que personne n'a demandée. Aucun candidat
    sûr → `None`, et l'appelant garde son repli : mieux vaut une sonde qui
    échoue qu'une sonde qui écrit.

    Le corps est lu en texte plutôt que parsé : `tools/list` arrive en SSE
    (`event: message\ndata: {...}`) sur ce transport, et on ne cherche qu'une
    liste de noms.
    """
    import re

    if not listed_body:
        return None
    for name in re.findall(r'"name"\s*:\s*"([^"]+)"', listed_body):
        if not _looks_mutating(name):
            return name
    return None
