"""Secrets volatils : marqueurs `{"volatile": "<clef>"}` de la config (lot AU).

Une valeur que l'utilisateur saisit dans MIAOU, que le proxy tient en mémoire
seulement, et qu'il substitue au marqueur dans le `config` d'un upstream
`inprocess` juste avant son `build(config)`. Le serveur reçoit une config
ordinaire, une chaîne à la place du marqueur : il ne sait rien du mécanisme.

Ce module porte le côté CONFIG, pur : reconnaître un marqueur, les recenser,
résoudre une copie de la config, masquer la valeur dans un texte.

Un marqueur est un objet dont la SEULE clé est `volatile`, et dont la valeur,
la clef, est une chaîne non vide (libre, espaces admis : elle sert aussi de
libellé). Un objet qui porte `volatile` à côté d'autres clés n'en est pas un —
une config peut avoir une clé de ce nom —, il passe tel quel. Les clés à
souligné en tête (`_comment`, `_example_*`) ne sont jamais parcourues, comme
pour `${VAR}` : elles citent la syntaxe sans l'employer.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

MARKER_KEY = "volatile"

Path = tuple[str | int, ...]


class VolatileMarkerError(ValueError):
    """Marqueur mal formé : `{"volatile": …}` seul, sans clef utilisable."""


def marker_key(value: Any) -> str | None:
    """La clef si `value` est un marqueur, None sinon. Lève sur un objet de
    la forme d'un marqueur dont la clef n'est pas une chaîne non vide."""
    if not isinstance(value, dict) or len(value) != 1 or MARKER_KEY not in value:
        return None
    key = value[MARKER_KEY]
    if not isinstance(key, str) or not key.strip():
        raise VolatileMarkerError(
            f"marqueur {{\"{MARKER_KEY}\": …}} sans clef (chaîne non vide attendue, "
            f"reçu {key!r})"
        )
    return key


def format_path(path: Path) -> str:
    out = ""
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else part)
    return out


def iter_markers(value: Any, path: Path = (), strict: bool = True) -> Iterator[tuple[Path, str]]:
    """(chemin, clef) de chaque marqueur, à toute profondeur, listes comprises.

    `strict` : un marqueur mal formé lève `VolatileMarkerError` (chemin dans le
    message) ; sinon il est sauté — pour un parcours de diagnostic qui ne doit
    rien refuser."""
    try:
        key = marker_key(value)
    except VolatileMarkerError as e:
        if strict:
            where = format_path(path) or "(racine)"
            raise VolatileMarkerError(f"{where} : {e}") from None
        return
    if key is not None:
        yield path, key
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and k.startswith("_"):
                continue
            yield from iter_markers(v, (*path, k), strict)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from iter_markers(v, (*path, i), strict)


def keys_in(value: Any) -> list[str]:
    """Clefs distinctes, dans l'ordre de première apparition."""
    seen: list[str] = []
    for _, key in iter_markers(value):
        if key not in seen:
            seen.append(key)
    return seen


def get_at(value: Any, path: Path) -> Any:
    """Valeur au chemin `path`, ou `_MISSING` si le chemin n'existe pas."""
    for part in path:
        if isinstance(part, int):
            if not isinstance(value, list) or part >= len(value):
                return _MISSING
        elif not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


_MISSING = object()


def resolve(value: Any, secret: str) -> Any:
    """Copie de `value` où chaque marqueur devient `secret`. Une clef par
    upstream (v1) : tous les marqueurs d'une config désignent la même. Les clés
    à souligné sont recopiées telles quelles."""
    if marker_key(value) is not None:
        return secret
    if isinstance(value, dict):
        return {
            k: (v if isinstance(k, str) and k.startswith("_") else resolve(v, secret))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [resolve(v, secret) for v in value]
    return value


def forms(secret: str | None) -> list[str]:
    """Les formes sous lesquelles `secret` peut se retrouver dans un texte : brute,
    `repr` (antislash doublé, apostrophe échappée, `\\n`), JSON ASCII (`\\uXXXX`) et
    JSON brut. Un serveur qui cite sa config dans une exception ou une
    description l'écrit sous l'une d'elles, et un fichier JSON écrit la valeur
    échappée. Les plus longues d'abord, sans doublon : à masquer dans cet ordre."""
    if not secret:
        return []
    found = {
        secret,
        repr(secret)[1:-1],
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
    }
    return sorted((f for f in found if f), key=lambda f: (-len(f), f))


def cites(text: str, secret: str | None) -> bool:
    """Vrai si `text` contient `secret` sous l'une de ses `forms`."""
    return any(form in text for form in forms(secret))


def mask(text: str, secret: str | None) -> str:
    """`text` sans aucune occurrence de `secret`, brute ou échappée (`forms`). À
    appliquer à TOUT texte tiré d'un échec de construction (message d'exception
    d'un serveur qui citerait sa config) avant de le journaliser ou de le rendre
    à un client. Le texte d'exception reste (indice de diagnostic) : c'est le
    masquage qui doit tout couvrir."""
    for form in forms(secret):
        text = text.replace(form, "***")
    return text
