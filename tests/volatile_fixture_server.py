"""Upstream inprocess de test à secret volatil (lot AU). Non collecté.

`check_config` exige un `login` ; `build` teste le mot de passe de chaque
instance (`instances`, ou la config elle-même) : `EXPECTED` est accepté,
`UNREACHABLE` simule un annuaire injoignable, une valeur qui commence par
`LEAKY_PREFIX` fait lever un message qui CITE la config (et donc la valeur),
`INCOMPLETE` lève `ConfigIncomplete`, tout le reste lève
`CredentialsRejected`. `"cite": true` fait citer la valeur dans la
description de l'outil. `CHECKS` et `BUILDS` gardent ce que
le proxy a passé à chacun.
"""

from __future__ import annotations

from typing import Any

from mcp_base import ConfigIncomplete, CredentialsRejected, MiaouMCPBase

EXPECTED = "bonne-valeur"
UNREACHABLE = "annuaire-injoignable"
INCOMPLETE = "config-incomplete"
LEAKY_PREFIX = "fuite-"

CHECKS: list[Any] = []
BUILDS: list[Any] = []


def _check(config: Any) -> None:
    if not isinstance(config, dict) or not config.get("login"):
        raise ConfigIncomplete("login absent")


def check_config(config: Any) -> None:
    CHECKS.append(config)
    _check(config)


def build(config: dict | None = None) -> Any:
    BUILDS.append(config)
    _check(config)
    for instance in (config or {}).get("instances") or [config]:
        password = instance.get("password")
        if password == UNREACHABLE:
            raise ConnectionError("annuaire injoignable")
        if password == INCOMPLETE:
            raise ConfigIncomplete("champ requis absent")
        if isinstance(password, str) and password.startswith(LEAKY_PREFIX):
            raise ValueError(f"config invalide : {config}")
        if password != EXPECTED:
            raise CredentialsRejected("identifiants refusés")
    server = MiaouMCPBase(
        "volatile-fixture", default_port=0, config=config,
        instructions="Consigne du serveur à secret.",
    )
    description = "Interroge le service."
    if config.get("cite"):
        description += f" Mot de passe : {config.get('password')}"

    # Propriétés dans un ordre NON alphabétique : un cache trié les
    # réordonnerait.
    @server.mcp.tool(description=description)
    async def query(url: str, max_bytes: int = 10, count: int = 1) -> str:
        return f"{config['login']} {url}"

    server.finalize_tools()
    return server.mcp
