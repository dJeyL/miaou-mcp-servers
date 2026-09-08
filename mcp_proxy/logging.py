"""Log au format uvicorn, partagé par tout le paquet.

Module volontairement sans dépendance interne : `_log` est appelé depuis le
serveur, l'auth entrante et l'auth sortante, et le poser plus haut créerait un
cycle d'imports entre ces modules.
"""

from __future__ import annotations

import sys


def _log(message: str) -> None:
    """Ligne de log au format uvicorn (préfixe `INFO:` vert), sur stderr.

    Couleur seulement si stderr est un TTY : redirigé vers un fichier ou un
    pipe, on ne veut pas d'échappements ANSI dans le log.
    """
    levelname = "INFO"
    if sys.stderr.isatty():
        # Comme uvicorn.logging.ColourizedFormatter : seul le levelname est
        # colorisé (vert pour INFO), le ':' et le séparateur restent neutres.
        levelname = f"\033[32m{levelname}\033[0m"
    # uvicorn : séparateur de (8 - len(levelname)) espaces dans levelprefix,
    # plus l'espace du format "%(levelprefix)s %(message)s" — soit 5 pour INFO.
    separator = " " * (8 - len("INFO")) + " "
    print(f"{levelname}:{separator}{message}", file=sys.stderr)
