"""Parler à un `Server` MCP en mémoire comme le ferait un client réel.

Pas un module de test (le nom ne commence pas par "test_") : un harnais partagé
par les suites qui exercent `build_proxy_server`.

Chaque appel passe par `Client(server, mode="legacy")` : le vrai chemin JSON-RPC
de la poignée de main `initialize`, celui que parle MIAOU. Le mode par défaut
(`auto`) négocierait la révision 2026-07-28 et dispatcherait EN DIRECT, sans
sérialisation JSON-RPC — tout sauf ce que voit MIAOU, si bien qu'un test vert y
prouverait le mauvais chemin.

Ce que ça garantit en plus d'un appel direct au handler : le résultat a été
sérialisé sur le fil puis relu par un client. Un `_meta` mal aliasé, un champ
hors schéma, une erreur protocolaire dont `data` se perdrait — tout cela se voit
ici, et ne se voyait pas sur l'objet Python rendu par le handler.

Les erreurs JSON-RPC remontent en `MCPError` (`e.error.code`, `e.error.data`),
exactement comme côté client.
"""

from __future__ import annotations

from typing import Any

from mcp.client import Client

try:  # Python ≥ 3.11
    _BaseExceptionGroup = BaseExceptionGroup
except NameError:  # pragma: no cover — 3.10 : le backport que tire anyio
    from exceptiongroup import BaseExceptionGroup as _BaseExceptionGroup


def _single_cause(group: Any) -> BaseException | None:
    """La cause d'un ExceptionGroup à cause unique, à travers les imbrications.

    Le transport en mémoire du client passe par des task groups anyio, qui
    emballent l'exception levée DANS le `async with` : sans ce déballage, un
    `pytest.raises(MCPError)` verrait un ExceptionGroup et non l'erreur que
    le client lève réellement."""
    exc: BaseException = group
    while isinstance(exc, _BaseExceptionGroup):
        if len(exc.exceptions) != 1:
            return None
        exc = exc.exceptions[0]
    return exc


async def list_tools(server: Any) -> Any:
    try:
        async with Client(server, mode="legacy") as client:
            return await client.list_tools()
    except _BaseExceptionGroup as group:
        cause = _single_cause(group)
        if cause is None:
            raise
        raise cause from None


async def call_tool(server: Any, name: str, arguments: dict | None = None) -> Any:
    try:
        async with Client(server, mode="legacy") as client:
            return await client.call_tool(name, arguments or {})
    except _BaseExceptionGroup as group:
        cause = _single_cause(group)
        if cause is None:
            raise
        raise cause from None
