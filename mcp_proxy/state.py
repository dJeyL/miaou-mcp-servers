"""Fichiers que le proxy écrit lui-même, rangés sous `state/`.

Deux natures, nommées pour ne pas se confondre : `tokens.json` est un ÉTAT —
le perdre jette les refresh tokens et redemande chaque autorisation à la main —,
`tools-cache.json` un vrai cache, qui se reconstruit. Aucun ne s'appelle plus
`config-*.json` : ils ne sont pas de la config, et ce préfixe les mettait à
portée de tout glob qui vise les couches (`COPY config*.json` d'un Dockerfile).

`state/` vit à côté du maillon local de la chaîne (`config.json` par défaut).
Un maillon local d'un autre nom (`--config autre.json`) range son état sous
`state/<nom>/` : deux instances lancées du même dossier ne partagent pas leurs
jetons, comme le garantissait l'ancien `<config>-tokens.json`.
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_DIR = "state"
TOKENS_FILE = "tokens.json"
TOOLS_CACHE_FILE = "tools-cache.json"
_DEFAULT_STEM = "config"


def state_dir(local_config: str | Path) -> Path:
    local = Path(local_config)
    base = local.parent / STATE_DIR
    return base if local.stem == _DEFAULT_STEM else base / local.stem


def default_tokens_path(local_config: str | Path) -> Path:
    return state_dir(local_config) / TOKENS_FILE


def default_tools_cache_path(local_config: str | Path) -> Path:
    return state_dir(local_config) / TOOLS_CACHE_FILE


def tools_cache_beside(tokens_path: str | Path) -> Path:
    """Cache d'outils d'un `--tokens-file` explicite : à côté de lui, sous
    le nom qu'il avait avant `state/` — un emplacement choisi par l'opérateur
    ne se déplace pas dans son dos."""
    path = Path(tokens_path)
    return path.with_name(path.stem.replace("-tokens", "") + "-tools.json")


def migrate_legacy_state(local_config: str | Path) -> list[str]:
    """Déplace `<config>-tokens.json` / `<config>-tools.json` vers `state/`.

    Un renommage, jamais une réécriture : le contenu (et le mode 0600 des
    jetons) suit tel quel. Sans effet si la cible existe déjà — l'ancien
    fichier est alors laissé en place et signalé, plutôt que d'écraser un état
    plus récent. Rend les lignes à journaliser.
    """
    local = Path(local_config)
    moves = (
        (local.with_name(f"{local.stem}-tokens.json"), default_tokens_path(local)),
        (local.with_name(f"{local.stem}-tools.json"), default_tools_cache_path(local)),
    )
    lines: list[str] = []
    for legacy, target in moves:
        if not legacy.exists():
            continue
        if target.exists():
            lines.append(
                f"'{legacy}' ignoré : '{target}' existe déjà. Supprimer l'ancien "
                f"fichier une fois vérifié qu'il ne sert plus."
            )
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(legacy, target)
        lines.append(f"'{legacy}' déplacé vers '{target}'.")
    return lines
