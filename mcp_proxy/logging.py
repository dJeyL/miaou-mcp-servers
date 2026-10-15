"""Log au format uvicorn, partagé par tout le paquet.

Module volontairement sans dépendance interne : `_log` est appelé depuis le
serveur, l'auth entrante et l'auth sortante, et le poser plus haut créerait un
cycle d'imports entre ces modules.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


# Couleurs d'uvicorn.logging.ColourizedFormatter : vert pour INFO, jaune pour
# WARNING (le rouge y est ERROR : un avertissement rouge se lirait en panne).
_COLOURS = {"INFO": "32", "WARNING": "33"}


# Journal coupé le temps d'un `muted()` : la validation d'une config modifiée
# (lot AV) rejoue les fonctions du démarrage, qui journalisent ce qu'elles
# construisent. Ces lignes décriraient un démarrage qui n'a pas lieu. Une
# variable de contexte et non un drapeau global : `asyncio.to_thread` la
# recopie, et une requête servie pendant ce temps journalise normalement.
_muted: ContextVar[bool] = ContextVar("miaou_proxy_log_muted", default=False)


@contextmanager
def muted() -> Iterator[None]:
    token = _muted.set(True)
    try:
        yield
    finally:
        _muted.reset(token)


def _emit(level: str, message: str) -> None:
    if _muted.get():
        return
    levelname = level
    if sys.stderr.isatty():
        # Comme uvicorn.logging.ColourizedFormatter : seul le levelname est
        # colorisé, le ':' et le séparateur restent neutres.
        levelname = f"\033[{_COLOURS[level]}m{levelname}\033[0m"
    # uvicorn : séparateur de (8 - len(levelname)) espaces dans levelprefix,
    # plus l'espace du format "%(levelprefix)s %(message)s" — soit 5 pour INFO,
    # 2 pour WARNING.
    separator = " " * (8 - len(level)) + " "
    print(f"{levelname}:{separator}{message}", file=sys.stderr)


def _log(message: str) -> None:
    """Ligne de log au format uvicorn (préfixe `INFO:` vert), sur stderr.

    Couleur seulement si stderr est un TTY : redirigé vers un fichier ou un
    pipe, on ne veut pas d'échappements ANSI dans le log.
    """
    _emit("INFO", message)


def _warn(message: str) -> None:
    """Comme `_log`, au niveau `WARNING:` (jaune si TTY) : ce que l'opérateur
    doit corriger, sans que le démarrage en soit empêché."""
    _emit("WARNING", message)
