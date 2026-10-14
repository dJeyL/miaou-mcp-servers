#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.2,<3", "uvicorn", "starlette", "truststore"]
# ///
"""
Serveur MCP de démonstration des secrets volatils (lot AU) : de quoi essayer,
de bout en bout et sans service réel, un mot de passe saisi dans MIAOU et
tenu en mémoire par le proxy.

Il se comporte comme un serveur qui s'authentifie au démarrage : `build`
« teste » le mot de passe reçu (le marqueur `{"volatile": "<clef>"}` déjà
remplacé par le proxy) en le comparant à une valeur attendue, et refuse de se
construire sinon.

Config (bloc "config" de l'entrée inprocess) :
  - login     : obligatoire ; absent ou vide, `check_config` lève
                ConfigIncomplete et le proxy écarte l'upstream au démarrage ;
  - password  : le mot de passe, normalement `{"volatile": "<clef>"}` ;
  - instances : facultatif, liste de sous-blocs portant chacun son `password`
                (le cas d'un serveur qui sert plusieurs instances d'un même
                service avec le même compte) ; chacun est testé, le premier
                refus fait tout refuser ;
  - expected  : valeur attendue (défaut : MIAOU_SECRET_DEMO_EXPECTED, sinon
                « miaou ») ;
  - delay_s   : latence simulée de l'« annuaire », bornée à [0, 10] s.

Issues de `build` : la valeur attendue est acceptée ; `UNREACHABLE`
(« injoignable ») simule un annuaire injoignable (ConnectionError, verdict
indéterminé) ; toute autre valeur lève CredentialsRejected.

Outil exposé :
  - whoami() : login et nombre d'instances. Jamais le mot de passe.

Lancement autonome (sans proxy, le mot de passe vient de l'environnement) :
    MIAOU_SECRET_DEMO_PASSWORD=miaou uv run servers/mcp_secret_demo.py   # HTTP 127.0.0.1:8772
"""

import math
import os
import sys
import time
from typing import Any

from mcp.server.mcpserver import MCPServer

from mcp_base import ConfigIncomplete, CredentialsRejected, MiaouMCPBase

DEFAULT_EXPECTED = "miaou"
UNREACHABLE = "injoignable"
MAX_DELAY_S = 10.0

INSTRUCTIONS = (
    "Serveur de démonstration : son outil whoami ne sert qu'à vérifier qu'un mot "
    "de passe saisi par l'utilisateur dans MIAOU a été accepté."
)


def _instances(config: dict) -> list[dict]:
    instances = config.get("instances")
    if isinstance(instances, list) and instances:
        return [i if isinstance(i, dict) else {} for i in instances]
    return [config]


def check_config(config: Any) -> None:
    """Complétude, sans rien contacter. Appelé par le proxy au démarrage avec
    les marqueurs encore en place, et par `build` en premier."""
    if not isinstance(config, dict) or not isinstance(config.get("login"), str) or not config["login"].strip():
        raise ConfigIncomplete("'login' absent ou vide dans le bloc config")
    for i, instance in enumerate(_instances(config)):
        if instance.get("password") in (None, ""):
            where = f"instances[{i}].password" if instance is not config else "password"
            raise ConfigIncomplete(f"'{where}' absent")


def _delay(config: dict) -> float:
    try:
        delay = float(config.get("delay_s", 0))
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(delay):
        return 0.0
    return max(0.0, min(delay, MAX_DELAY_S))


def _authenticate(config: dict) -> None:
    """L'« annuaire » : une comparaison, précédée de la latence demandée.
    Synchrone, comme un vrai appel bloquant — le proxy appelle `build` hors de
    sa boucle d'événements."""
    expected = config.get("expected") or os.environ.get("MIAOU_SECRET_DEMO_EXPECTED") or DEFAULT_EXPECTED
    time.sleep(_delay(config))
    for instance in _instances(config):
        password = instance.get("password")
        if password == UNREACHABLE:
            raise ConnectionError("annuaire de démonstration hors d'atteinte (valeur réservée)")
        if password != expected:
            # Jamais la valeur dans le message.
            raise CredentialsRejected(f"identifiants refusés pour « {config['login']} »")


class SecretDemoServer(MiaouMCPBase):
    def __init__(self, config: dict) -> None:
        super().__init__("miaou-secret-demo", default_port=8772, config=config, instructions=INSTRUCTIONS)
        login = config["login"]
        count = len(_instances(config))

        @self.mcp.tool()
        async def whoami() -> str:
            """Compte avec lequel ce serveur s'est authentifié, et nombre d'instances
            servies. Ne rend jamais le mot de passe."""
            return f"Authentifié en tant que « {login} » ({count} instance{'s' if count > 1 else ''})."

        self.finalize_tools()


def build(config: dict | None = None) -> MCPServer:
    """Factory appelée par le proxy, une fois le secret saisi dans MIAOU :
    contrôles de complétude, puis authentification, puis construction."""
    config = config or {}
    check_config(config)
    _authenticate(config)
    return SecretDemoServer(config).mcp


if __name__ == "__main__":
    password = os.environ.get("MIAOU_SECRET_DEMO_PASSWORD")
    if not password:
        print("Erreur : MIAOU_SECRET_DEMO_PASSWORD absente.", file=sys.stderr)
        raise SystemExit(1)
    config = {"login": os.environ.get("MIAOU_SECRET_DEMO_LOGIN", "demo"), "password": password}
    try:
        check_config(config)
        _authenticate(config)
    except (ConfigIncomplete, CredentialsRejected, ConnectionError) as e:
        print(f"Erreur : {e}", file=sys.stderr)
        raise SystemExit(1)
    SecretDemoServer(config).main()
