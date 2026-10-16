"""Extension `miaou/lifecycle` (lot AV) : la config a-t-elle changé depuis le
démarrage, et redémarrage du proxy à la demande.

Deux méthodes JSON-RPC hors outils, sur le patron de `miaou/self-update`
(`updater.py`) : absentes de `tools/list`, invisibles du modèle. **Ne jamais en
faire des outils.** Ère 2026-07-28 seulement pour la découverte.

Publiée par défaut, retirée par la clef de config `lifecycle: false`
(`configwatch.resolve_lifecycle`) : `status` est en lecture seule, et
`restart` n'est accepté que sous un superviseur qui relance sur
`RESTART_EXIT_CODE` (`restartable`, la même condition que le SIGUSR1). **Aucune garde
d'origine** (décision AV-g, comme AT) : MIAOU ouvert en `file://` doit pouvoir
s'en servir. Limite assumée : toute page ouverte dans le navigateur peut
demander un redémarrage (CORS `*`) ; pas de fuite, mais des coupures et des
secrets volatils à renvoyer. Le bloc `auth`, quand il est posé, est la
barrière.

`status` relit la config à chaque appel (`ConfigWatch.check`) : vérifier,
c'est relire, sans sonde ni cadence côté proxy.

`restart` ne fait jamais d'`execv` : il sort en `RESTART_EXIT_CODE`, et c'est
le superviseur qui relance, par `uv run`. Refus, dans cet ordre :
superviseur absent, mise à jour en cours d'application, config invalide (le
`bind` d'essai refait), appels d'outils en vol (`force: true` ne lève que
celui-là). Un `restart` déjà accepté, ou une sortie déjà demandée par une
mise à jour, rend la même réponse.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import mcp.types as types
from mcp.server import Server
from mcp.shared.exceptions import MCPError

from .configwatch import INVALID, ConfigWatch
from .logging import _log
from .server import InFlight

EXTENSION_ID = "miaou/lifecycle"
STATUS_METHOD = "miaou/lifecycle/status"
RESTART_METHOD = "miaou/lifecycle/restart"

#: Chemins changés publiés au plus ; `changed_total` donne le compte entier.
MAX_CHANGED_PATHS = 20

# Refus de `restart` : `error.data.code`, testé par égalité de constante.
NOT_SUPERVISED = "RESTART_NOT_SUPERVISED"
UPDATE_IN_PROGRESS = "RESTART_UPDATE_IN_PROGRESS"
CONFIG_INVALID = "RESTART_CONFIG_INVALID"
TOOLS_IN_FLIGHT = "RESTART_TOOLS_IN_FLIGHT"


@dataclass(frozen=True)
class ProcessIdentity:
    """L'identité du processus, tirée UNE fois au démarrage : `instance` est
    la même dans `miaou/lifecycle/status` et `miaou/secrets/status`. Un client
    reconnaît le processus relancé à un `instance` différent."""

    instance: str = field(default_factory=lambda: secrets.token_hex(8))
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class StatusParams(types.RequestParams):
    pass


class RestartParams(types.RequestParams):
    force: bool = False


class StatusResult(types.Result):
    status: dict[str, Any]


class RestartResult(types.Result):
    restarting: bool
    instance: str


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


class Lifecycle:
    """L'état publié et les deux handlers.

    `request_exit(code)` demande la sortie (`entry.ExitRequest`) ;
    `exit_pending()` dit si une sortie est déjà demandée, par ce `restart` ou
    par une mise à jour appliquée. `restartable()` est lu à chaque appel :
    c'est la condition du chemin SIGUSR1, une seule source. `applying()` :
    une mise à jour est en cours d'application (None sans `self_update`).
    """

    def __init__(
        self,
        identity: ProcessIdentity,
        watch: ConfigWatch,
        in_flight: InFlight,
        request_exit: Callable[[int], None],
        exit_pending: Callable[[], bool],
        restartable: Callable[[], bool],
        exit_code: int,
        applying: Callable[[], bool] | None = None,
    ) -> None:
        self.identity = identity
        self.watch = watch
        self.in_flight = in_flight
        self.request_exit = request_exit
        self.exit_pending = exit_pending
        self.restartable = restartable
        self.exit_code = exit_code
        self.applying = applying
        self._lock = asyncio.Lock()

    async def snapshot(self) -> dict[str, Any]:
        check = await self.watch.check()
        return {
            "instance": self.identity.instance,
            "started_at": _iso(self.identity.started_at),
            "restartable": self.restartable(),
            "restarting": self.exit_pending(),
            "config": {
                "state": check.state,
                "changed": check.changed[:MAX_CHANGED_PATHS],
                "changed_total": len(check.changed),
                "detail": check.detail,
                "layers": check.layers,
                "checked_at": _iso(check.checked_at),
            },
        }

    async def handle_status(self, ctx: Any, params: StatusParams | None) -> StatusResult:
        return StatusResult(status=await self.snapshot())

    def _accepted(self) -> RestartResult:
        return RestartResult(restarting=True, instance=self.identity.instance)

    async def handle_restart(self, ctx: Any, params: RestartParams | None) -> RestartResult:
        force = bool(params and params.force)
        # Sérialisé : un second `restart` (autre onglet) attend le premier,
        # puis le trouve accepté.
        async with self._lock:
            if self.exit_pending():
                return self._accepted()
            if not self.restartable():
                raise _refusal(
                    NOT_SUPERVISED,
                    "Le proxy n'est pas lancé par un superviseur qui le relance "
                    "(scripts/supervise.sh) : il ne redémarrerait pas. Le redémarrer à la main.",
                )
            if self.applying is not None and self.applying():
                raise _refusal(UPDATE_IN_PROGRESS, "Une mise à jour du proxy est en cours d'application.")
            check = await self.watch.check(fresh_bind=True)
            if check.state == INVALID:
                raise _refusal(
                    CONFIG_INVALID,
                    f"La config ne permettrait pas au proxy de redémarrer : {check.detail}",
                    detail=check.detail,
                )
            count = self.in_flight.count
            if count and not force:
                raise _refusal(
                    TOOLS_IN_FLIGHT, f"{count} appel(s) d'outil en cours.", count=count,
                )
            _log(
                f"Redémarrage demandé par un client ({RESTART_METHOD})"
                + (f", {count} appel(s) d'outil en cours coupé(s)" if count else "")
                + f", config {check.state}."
            )
            self.request_exit(self.exit_code)
            return self._accepted()

    def install(self, server: Server) -> None:
        server.add_request_handler(STATUS_METHOD, StatusParams, self.handle_status)
        server.add_request_handler(RESTART_METHOD, RestartParams, self.handle_restart)
        server.extensions[EXTENSION_ID] = {}


def _refusal(code: str, message: str, level: int = types.INVALID_REQUEST, **extra: Any) -> MCPError:
    return MCPError(level, message, data={"code": code, **extra})
