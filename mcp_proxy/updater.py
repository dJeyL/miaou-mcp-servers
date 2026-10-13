"""Extension `miaou/self-update` : état de mise à jour du proxy, et son application.

Deux méthodes JSON-RPC hors outils, sur le modèle de `skills/list` : elles
n'apparaissent pas dans `tools/list`, ne coûtent rien en contexte, et le modèle
ne peut ni les voir ni les appeler. **Ne jamais en faire des outils.** Aucune
réservation à MIAOU n'est tentée (un autre client voit l'extension et peut
l'appeler) : les barrières sont le jeton du bloc `auth` quand il est posé, la
double clef (`self_update` ET `MIAOU_PROXY_SUPERVISED=1`), et l'effet borné de
l'appel — un fast-forward vers la branche suivie, configurée par le
propriétaire du clone.

Ère 2026-07-28 seulement : en legacy, `initialize` ne publie jamais
`extensions`, donc un client legacy ne découvre rien (il peut appeler les
méthodes, mais ne sait pas qu'elles existent).

`self_update` à `false` (défaut) : ni handler, ni extension, ni boucle de
sonde — la surface est celle d'avant le lot, à l'octet près.

Le proxy porte la cadence (une sonde au démarrage, puis toutes les
`PROBE_INTERVAL_S`) ; `miaou/update/status` lit le cache et ne touche au réseau
que sur `refresh`, borné par `REFRESH_MIN_INTERVAL_S`.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import mcp.types as types
from mcp.server import Server
from mcp.shared.exceptions import MCPError

from . import selfupdate
from .logging import _log
from .server import InFlight

CONFIG_KEY = "self_update"
SUPERVISED_ENV = "MIAOU_PROXY_SUPERVISED"
EXTENSION_ID = "miaou/self-update"
STATUS_METHOD = "miaou/update/status"
APPLY_METHOD = "miaou/update/apply"

PROBE_INTERVAL_S = 300
REFRESH_MIN_INTERVAL_S = 30

#: État publié tant que la première sonde n'a pas répondu : elle tourne en
#: arrière-plan, pour ne pas retarder le démarrage d'un appel réseau.
CHECKING = "checking"

# Refus d'`apply` : `error.data.code`, testé par égalité de constante (même
# contrat qu'AUTHORIZATION_REQUIRED), jamais par sous-chaîne du message.
NOT_SUPERVISED = "UPDATE_NOT_SUPERVISED"
IN_PROGRESS = "UPDATE_IN_PROGRESS"
TOOLS_IN_FLIGHT = "UPDATE_TOOLS_IN_FLIGHT"
INVALID_TARGET = "UPDATE_INVALID_TARGET"
STALE = "UPDATE_STALE"
REFUSED = "UPDATE_REFUSED"
ROLLED_BACK = "UPDATE_ROLLED_BACK"
ROLLBACK_FAILED = "UPDATE_ROLLBACK_FAILED"

_OUTCOME_CODES = {
    selfupdate.REFUSED: REFUSED,
    selfupdate.STALE: STALE,
    selfupdate.BLOCKED: TOOLS_IN_FLIGHT,
    selfupdate.ROLLED_BACK: ROLLED_BACK,
    selfupdate.ROLLBACK_FAILED: ROLLBACK_FAILED,
}


def resolve_self_update(cfg: dict[str, Any]) -> bool:
    """La clef `self_update` : booléen, absente = `false`. Une chaîne (un
    `${VAR}` par exemple) est refusée : l'attestation du superviseur est une
    variable d'environnement à part, pas une valeur de config."""
    value = cfg.get(CONFIG_KEY, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(
            f"'{CONFIG_KEY}' doit être true ou false, reçu {type(value).__name__}."
        )
    return value


def is_supervised(environ: dict[str, str] | None = None) -> bool:
    return (environ if environ is not None else os.environ).get(SUPERVISED_ENV) == "1"


def served_build(miaou_dist: Path | None) -> str | None:
    """L'empreinte de MIAOU servi (`build` de `<miaou_dist>/version.json`),
    relue à chaque appel : un fast-forward « MIAOU seul » la change sans
    redémarrage. None sans `miaou_dist`, ou si le fichier est absent ou
    illisible."""
    if miaou_dist is None:
        return None
    try:
        data = json.loads((miaou_dist / "version.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    build = data.get("build") if isinstance(data, dict) else None
    return build if isinstance(build, str) else None


class StatusParams(types.RequestParams):
    refresh: bool = False


class ApplyParams(types.RequestParams):
    target: str


class StatusResult(types.Result):
    status: dict[str, Any]


class ApplyResult(types.Result):
    applied: bool
    restart: bool
    kind: str
    before: str
    target: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class UpdateConfig:
    root: Path
    miaou_dist: Path | None
    supervised: bool
    state_file: Path | None


class SelfUpdater:
    """Cache de l'état, boucle de sonde, et les deux handlers.

    `request_restart` est appelé après un fast-forward qui exige un
    redémarrage, une fois la réponse prête : il demande l'arrêt au serveur
    uvicorn (`entry.py`), dont l'arrêt en douceur laisse partir la réponse.
    """

    def __init__(
        self,
        config: UpdateConfig,
        in_flight: InFlight,
        request_restart: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.in_flight = in_flight
        self.request_restart = request_restart
        self._status: selfupdate.Status | None = None
        self._checked_at: datetime | None = None
        self._probe_lock = asyncio.Lock()
        self._apply_lock = asyncio.Lock()

    # --- Sonde -------------------------------------------------------------

    async def refresh(self) -> None:
        """Une sonde, une seule à la fois : un appel concurrent attend la
        sonde en cours au lieu d'en lancer une seconde."""
        started = _now()
        async with self._probe_lock:
            if self._checked_at is not None and self._checked_at >= started:
                return
            status = await asyncio.to_thread(
                selfupdate.probe, self.config.root, self.config.miaou_dist
            )
            previous = self._status.state if self._status else None
            self._status, self._checked_at = status, _now()
        if status.state != previous:
            _log(f"Mise à jour du proxy : {status.state}"
                 + (f" ({status.detail})" if status.detail else ""))

    async def run(self) -> None:
        """Boucle du lifespan : sonde au démarrage, puis à intervalle fixe.
        Une exception est journalisée, jamais propagée (elle emporterait le
        task group, donc le proxy)."""
        while True:
            try:
                await self.refresh()
            except Exception as e:  # pragma: no cover - filet
                _log(f"Sonde de mise à jour interrompue ({type(e).__name__}: {e}).")
            await asyncio.sleep(PROBE_INTERVAL_S)

    def snapshot(self) -> dict[str, Any]:
        status = self._status.to_dict() if self._status else selfupdate.Status(CHECKING).to_dict()
        status.update(
            checked_at=self._checked_at.isoformat(timespec="seconds") if self._checked_at else None,
            supervised=self.config.supervised,
            applying=self._apply_lock.locked(),
            served_build=served_build(self.config.miaou_dist),
        )
        return status

    # --- Handlers ----------------------------------------------------------

    async def handle_status(self, ctx: Any, params: StatusParams | None) -> StatusResult:
        if params is not None and params.refresh:
            fresh = (
                self._checked_at is not None
                and (_now() - self._checked_at).total_seconds() < REFRESH_MIN_INTERVAL_S
            )
            if not fresh:
                await self.refresh()
        return StatusResult(status=self.snapshot())

    def _blocker(self, kind: str) -> str | None:
        # Une mise à jour MIAOU seul ne redémarre rien : aucun appel coupé.
        if kind == selfupdate.KIND_MIAOU:
            return None
        if self.in_flight.count:
            return f"{self.in_flight.count} appel(s) d'outil en cours"
        return None

    async def handle_apply(self, ctx: Any, params: ApplyParams) -> ApplyResult:
        if not self.config.supervised:
            raise _refusal(
                NOT_SUPERVISED,
                f"Le proxy n'est pas lancé par un superviseur ({SUPERVISED_ENV}=1 "
                f"absente) : il ne redémarrerait pas. Mettre à jour à la main.",
            )
        if not selfupdate.is_valid_target(params.target):
            raise _refusal(
                INVALID_TARGET, f"Cible invalide : {params.target!r} (sha complet attendu).",
                types.INVALID_PARAMS,
            )
        if self._apply_lock.locked():
            raise _refusal(IN_PROGRESS, "Une mise à jour est déjà en cours.")
        async with self._apply_lock:
            # Vérifié une première fois ici pour refuser vite, puis dans le
            # thread juste avant le merge, genre connu (`before_merge`).
            if self.in_flight.count and self._status and self._status.update \
                    and self._status.update.kind != selfupdate.KIND_MIAOU:
                raise _refusal(TOOLS_IN_FLIGHT, f"{self.in_flight.count} appel(s) d'outil en cours.")
            result = await asyncio.to_thread(
                selfupdate.apply_update,
                params.target,
                self.config.root,
                self.config.miaou_dist,
                self.config.state_file,
                self._blocker,
            )
            if result.outcome != selfupdate.APPLIED:
                code = _OUTCOME_CODES.get(result.outcome, REFUSED)
                level = types.INTERNAL_ERROR if code in (ROLLED_BACK, ROLLBACK_FAILED) \
                    else types.INVALID_REQUEST
                raise _refusal(code, result.detail or result.outcome, level,
                               remedy=result.remedy, before=result.before, target=result.target)
            restart = result.kind != selfupdate.KIND_MIAOU
            _log(f"Mise à jour appliquée : {result.before[:12]} → {result.target[:12]} "
                 f"({result.kind}){', redémarrage' if restart else ''}.")
            if not restart:
                # Rien ne redémarre : l'état publié doit dire « à jour » tout de
                # suite, pas à la prochaine sonde.
                self._checked_at = None
                await self.refresh()
            elif self.request_restart is not None:
                self.request_restart()
            return ApplyResult(
                applied=True, restart=restart, kind=result.kind,
                before=result.before, target=result.target,
            )

    def install(self, server: Server) -> None:
        server.add_request_handler(STATUS_METHOD, StatusParams, self.handle_status)
        server.add_request_handler(APPLY_METHOD, ApplyParams, self.handle_apply)
        server.extensions[EXTENSION_ID] = {}


def _refusal(
    code: str, message: str, level: int = types.INVALID_REQUEST, **extra: Any
) -> MCPError:
    data = {"code": code, **{k: v for k, v in extra.items() if v is not None}}
    return MCPError(level, message, data=data)
