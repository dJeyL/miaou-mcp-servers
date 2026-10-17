"""Extension `miaou/volatile-secrets` : dépôt, validation et oubli des secrets
volatils (lot AU).

Trois méthodes JSON-RPC hors outils, sur le patron de `miaou/self-update`
(`updater.py`) : invisibles du modèle, absentes de `tools/list`. **Ne jamais en
faire des outils.** Ère 2026-07-28 seulement pour la découverte (en legacy,
`initialize` ne publie jamais `extensions`).

**Garde.** Toute l'extension, `status` compris, ne répond qu'à une requête dont
`Host` est `127.0.0.1:<port>` ou `localhost:<port>` (port d'écoute du proxy)
ET dont `Origin` est la même origine (`http://<Host>`) : MIAOU servi par ce
proxy sous `/app/`, et rien d'autre. C'est la protection DNS-rebinding que le
proxy désactive globalement pour l'`Origin: null` de MIAOU en `file://`,
rétablie ici : sans elle, n'importe quelle page ouverte dans le navigateur de
l'utilisateur pourrait déposer des valeurs, donc faire échouer des
authentifications jusqu'au verrouillage du compte, et lire les noms des clefs.

**La valeur** n'est jamais écrite sur disque, jamais journalisée, jamais rendue
par une méthode ; toute trace d'un échec de construction est masquée
(`volatile.mask`). Le proxy n'en garde que des empreintes (HMAC à sel tiré au
démarrage, propre au processus) : celles des valeurs refusées, jamais
retestées, et celle de la valeur acceptée par chaque consommateur, qu'une
nouvelle saisie identique ne reconstruit pas. La valeur elle-même ne vit plus
que dans l'instance construite par le serveur, qui en a besoin. L'effacement
de la mémoire Python reste au mieux (chaînes immuables) : ne pas le promettre.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import json
import os
import secrets as _secrets
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import mcp.types as types
from mcp.server import Server
from mcp.shared.exceptions import MCPError

from .logging import _log, _warn
from .volatile import cites, mask

EXTENSION_ID = "miaou/volatile-secrets"
STATUS_METHOD = "miaou/secrets/status"
SUBMIT_METHOD = "miaou/secrets/submit"
FORGET_METHOD = "miaou/secrets/forget"

# Refus d'une requête : `error.data.code`, testé par égalité de constante.
ORIGIN_REFUSED = "SECRET_ORIGIN_REFUSED"
UNKNOWN_KEY = "SECRET_UNKNOWN_KEY"
INVALID_PARAMS = "SECRET_INVALID_PARAMS"

# État d'un consommateur : construit avec une valeur acceptée, ou en attente.
PENDING = "pending"
READY = "ready"

# Issue de la dernière tentative sur un consommateur (`verdict`), None avant
# toute saisie et après « Oublier ».
ACCEPTED = "accepted"
REJECTED = "rejected"
INDETERMINATE = "indeterminate"
INCOMPLETE = "incomplete"
UNTESTED = "untested"

ALLOWED_HOSTS = ("127.0.0.1", "localhost")
MAX_VALUE_LENGTH = 4096
# Empreintes de valeurs refusées gardées par clef (les plus récentes) : la
# garde « jamais retestée » ne doit pas faire croître le processus sans borne.
MAX_REJECTED = 64


class StatusParams(types.RequestParams):
    pass


class KeyParams(types.RequestParams):
    # `Any` et non `str` : une erreur de validation Pydantic recopie
    # `input_value` dans le message rendu au client. Validé à la main, par des
    # messages qui ne citent jamais la valeur.
    key: Any = None
    value: Any = None


class StatusResult(types.Result):
    status: dict[str, Any]


class SecretResult(types.Result):
    secret: dict[str, Any]


def _refusal(code: str, message: str, level: int = types.INVALID_REQUEST) -> MCPError:
    return MCPError(level, message, data={"code": code})


def classify_failure(exc: BaseException) -> str:
    """Issue d'un `build` qui a levé. Reconnue par NOM de classe dans le MRO
    autant que par identité : un serveur qui importe la base sous un autre nom
    de module (`servers.mcp_base`) lève une AUTRE classe `CredentialsRejected`,
    qu'`isinstance` manquerait — le refus passerait pour une panne, et MIAOU ne
    dirait jamais à l'utilisateur qu'il a raté sa saisie."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    if "CredentialsRejected" in names:
        return REJECTED
    if "ConfigIncomplete" in names:
        return INCOMPLETE
    return INDETERMINATE


@dataclass
class _Consumer:
    name: str
    upstream: Any
    accepted: bytes | None = None
    verdict: str | None = None
    detail: str | None = None

    def entry(self) -> dict[str, Any]:
        return {
            "upstream": self.name,
            "state": PENDING if self.upstream.secret_pending else READY,
            "verdict": self.verdict,
            "detail": self.detail,
        }


@dataclass
class _Key:
    key: str
    consumers: list[_Consumer]
    # Ordre d'insertion = ancienneté : LRU de `MAX_REJECTED` empreintes.
    rejected: collections.OrderedDict[bytes, None] = field(default_factory=collections.OrderedDict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Dépôt en cours : tâche détachée de la requête (elle seule tient le verrou)
    # et empreinte de la valeur qu'elle valide.
    task: asyncio.Task[None] | None = None
    task_fingerprint: bytes | None = None

    def reject(self, fingerprint: bytes) -> None:
        self.rejected[fingerprint] = None
        self.rejected.move_to_end(fingerprint)
        while len(self.rejected) > MAX_REJECTED:
            self.rejected.popitem(last=False)

    def entry(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "held": any(not c.upstream.secret_pending for c in self.consumers),
            "consumers": [c.entry() for c in self.consumers],
        }


class VolatileSecrets:
    """L'état des secrets volatils d'un proxy, et les trois handlers.

    `upstreams` : la table du proxy ; les consommateurs sont ses upstreams à
    clef restés dans la table après le démarrage (`attach`). `catalog` : le
    `ToolCatalogCache`, où un upstream construit laisse ses outils et ses
    `instructions` pour son prochain démarrage en attente. `port` : port
    d'écoute, pour la garde `Host`. `publish_surface` : rappelé après chaque
    changement d'instance (instructions, extension Skills).
    """

    def __init__(
        self,
        upstreams: dict[str, Any],
        catalog: Any = None,
        port: int | None = None,
        instance: str | None = None,
    ) -> None:
        self.upstreams = upstreams
        self.catalog = catalog
        self.port = port
        self.publish_surface: Callable[[], Awaitable[None]] | None = None
        # L'identité du processus (`lifecycle.ProcessIdentity`), la même que
        # celle de `miaou/lifecycle/status` ; tirée ici seulement à défaut.
        self.instance = instance or _secrets.token_hex(8)
        self._salt = os.urandom(32)
        self._keys: dict[str, _Key] = {}

    # --- Consommateurs -----------------------------------------------------

    def attach(self) -> None:
        """Recense les consommateurs, après le démarrage : un upstream écarté
        (config incomplète, plusieurs clefs, module sans `build`) a quitté la
        table et n'en est pas un. Ordre de la config : c'est celui du test
        séquentiel."""
        self._keys = {}
        for name, upstream in self.upstreams.items():
            key = getattr(upstream, "volatile_key", None)
            if not isinstance(key, str) or getattr(upstream, "secret_pending", None) is not True:
                continue
            self._keys.setdefault(key, _Key(key, [])).consumers.append(_Consumer(name, upstream))

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    def install(self, server: Server) -> None:
        server.add_request_handler(STATUS_METHOD, StatusParams, self.handle_status)
        server.add_request_handler(SUBMIT_METHOD, KeyParams, self.handle_submit)
        server.add_request_handler(FORGET_METHOD, KeyParams, self.handle_forget)
        server.extensions[EXTENSION_ID] = {}

    # --- Garde -------------------------------------------------------------

    def _guard(self, ctx: Any) -> None:
        """Refuse tout ce qui n'est pas MIAOU servi par ce proxy, sur loopback.
        Sans requête HTTP (transport en mémoire), sans port connu : refus."""
        request = getattr(ctx, "request", None)
        headers = getattr(request, "headers", None)
        host = origin = ""
        if headers is not None:
            host = (headers.get("host") or "").strip().lower()
            origin = (headers.get("origin") or "").strip().lower()
        allowed = {f"{h}:{self.port}" for h in ALLOWED_HOSTS} if self.port else set()
        if host in allowed and origin == f"http://{host}":
            return
        where = f"http://127.0.0.1:{self.port}/app/" if self.port else "/app/ de ce proxy"
        raise _refusal(
            ORIGIN_REFUSED,
            f"Secrets volatils : seul MIAOU servi par ce proxy peut les gérer. Ouvrir "
            f"MIAOU à {where} (ou par localhost), avec une carte de serveur MCP qui "
            f"vise la même adresse.",
        )

    def _key(self, params: KeyParams) -> _Key:
        key = params.key
        if not isinstance(key, str) or not key:
            raise _refusal(INVALID_PARAMS, "Paramètre 'key' : chaîne non vide attendue.", types.INVALID_PARAMS)
        found = self._keys.get(key)
        if found is None:
            raise _refusal(UNKNOWN_KEY, f"Aucun upstream de ce proxy n'attend le secret « {key} ».",
                           types.INVALID_PARAMS)
        return found

    def _fingerprint(self, value: str) -> bytes:
        return hmac.new(self._salt, value.encode("utf-8"), hashlib.sha256).digest()

    # --- Handlers ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {"instance": self.instance, "secrets": [k.entry() for k in self._keys.values()]}

    async def handle_status(self, ctx: Any, params: StatusParams) -> StatusResult:
        self._guard(ctx)
        return StatusResult(status=self.snapshot())

    async def handle_submit(self, ctx: Any, params: KeyParams) -> SecretResult:
        self._guard(ctx)
        key = self._key(params)
        value = params.value
        if not isinstance(value, str) or not value or len(value) > MAX_VALUE_LENGTH:
            raise _refusal(
                INVALID_PARAMS,
                f"Paramètre 'value' : chaîne non vide de {MAX_VALUE_LENGTH} caractères au plus attendue.",
                types.INVALID_PARAMS,
            )
        # La validation tourne dans une tâche DÉTACHÉE de la requête, qui tient
        # elle-même le verrou par clef : un `submit` annulé (onglet fermé,
        # délai du client) laisse le thread de `build` courir, et la tâche
        # finir (verdict, empreinte, publication). Un `submit` de la MÊME valeur
        # pendant ce temps attend cette tâche et l'adopte au lieu de relancer
        # un `build` concurrent ; une autre valeur se met en file sur le verrou.
        fingerprint = self._fingerprint(value)
        task = key.task
        if task is None or task.done() or key.task_fingerprint != fingerprint:
            task = asyncio.create_task(self._submit_locked(key, value))
            task.add_done_callback(self._retrieve)
            key.task, key.task_fingerprint = task, fingerprint
        await asyncio.shield(task)
        return SecretResult(secret=key.entry())

    async def _submit_locked(self, key: _Key, value: str) -> None:
        async with key.lock:
            changed = await self._submit(key, value)
        if changed:
            await self._publish()

    @staticmethod
    def _retrieve(task: asyncio.Task[None]) -> None:
        """Personne n'attend plus une tâche dont le demandeur est parti : son
        échec se journalise (sans valeur : la tâche ne la porte pas) au lieu de
        finir en « exception never retrieved »."""
        if not task.cancelled() and task.exception() is not None:
            _warn(f"Dépôt d'un secret volatil en échec ({type(task.exception()).__name__}).")

    async def handle_forget(self, ctx: Any, params: KeyParams) -> SecretResult:
        self._guard(ctx)
        key = self._key(params)
        async with key.lock:
            changed = False
            for consumer in key.consumers:
                if not consumer.upstream.secret_pending:
                    consumer.upstream.discard()
                    changed = True
                consumer.accepted = None
                consumer.verdict = consumer.detail = None
        _log(f"Secret volatil « {key.key} » oublié : {', '.join(c.name for c in key.consumers)} en attente.")
        if changed:
            await self._publish()
        return SecretResult(secret=key.entry())

    # --- Validation --------------------------------------------------------

    async def _submit(self, key: _Key, value: str) -> bool:
        """Test SÉQUENTIEL, dans l'ordre de la config, arrêté au premier refus :
        une saisie fausse coûte un échec à l'annuaire, pas un par consommateur.
        Une issue indéterminée ou incomplète n'arrête pas : elle ne dit rien de
        la valeur. Rend vrai si une instance a changé."""
        fingerprint = self._fingerprint(value)
        changed = False
        stopped = False
        for consumer in key.consumers:
            if stopped:
                consumer.verdict = UNTESTED
                consumer.detail = "non testé : un consommateur précédent a refusé cette valeur"
                continue
            if consumer.accepted == fingerprint and not consumer.upstream.secret_pending:
                consumer.verdict, consumer.detail = ACCEPTED, None
                continue
            if fingerprint in key.rejected:
                consumer.verdict = REJECTED
                consumer.detail = "valeur déjà refusée depuis le démarrage du proxy : non retestée"
                stopped = True
                continue
            try:
                server = await asyncio.to_thread(consumer.upstream.build_with, value)
            except (Exception, SystemExit) as e:
                verdict = classify_failure(e)
                consumer.verdict = verdict
                consumer.detail = mask(f"{type(e).__name__}: {e}", value)
                if verdict == REJECTED:
                    key.reject(fingerprint)
                    stopped = True
                kept = "" if consumer.upstream.secret_pending else ", instance précédente gardée"
                _log(f"Secret volatil « {key.key} » : {consumer.name} {verdict}{kept} ({consumer.detail})")
                continue
            consumer.upstream.adopt(server)
            consumer.accepted = fingerprint
            consumer.verdict, consumer.detail = ACCEPTED, None
            changed = True
            _log(f"Secret volatil « {key.key} » : {consumer.name} accepted.")
            await self._remember(consumer, value)
        return changed

    async def _remember(self, consumer: _Consumer, value: str) -> None:
        """Laisse dans le cache ce que le prochain démarrage en attente
        resservira : outils, `instructions` et entrées de skills. Jamais si la
        valeur y figure (un serveur qui interpolerait sa config dans une
        description, ou qui tirerait ses skills de sa config) : le cache est
        sur disque."""
        upstream = consumer.upstream
        if self.catalog is None:
            return
        # Tant que le contrôle n'a pas passé, rien de cette instance ne part en
        # cache : ni ici, ni par le listage de `tools/list` (server.py), s'il
        # échoue ou rend avant (le côté sûr : interdit jusqu'à preuve).
        upstream.cache_forbidden = True
        try:
            tools = await upstream.list_tools()
            skills = await upstream.list_skills()
        except Exception as e:
            _log(f"Outils de '{consumer.name}' non listés ({mask(str(e), value)}).")
            return
        dumped = "".join(t.model_dump_json(by_alias=True) for t in tools)
        # `ensure_ascii=False` : `cites` cherche aussi la forme JSON brute.
        dumped += json.dumps(skills, ensure_ascii=False, default=str)
        if cites(dumped, value) or cites(upstream.instructions or "", value):
            upstream.cache_forbidden = True
            _warn(
                f"'{consumer.name}' cite la valeur de son secret volatil dans ses outils, ses "
                f"instructions ou ses skills : rien de lui n'est écrit dans le cache d'outils."
            )
            return
        # Contrôle passé pour CETTE instance : le listage de `tools/list` peut
        # de nouveau écrire le catalogue.
        upstream.cache_forbidden = False
        self.catalog.remember(consumer.name, tools)
        self.catalog.remember_instructions(consumer.name, upstream.instructions)
        self.catalog.remember_skills(consumer.name, skills)

    async def _publish(self) -> None:
        if self.publish_surface is not None:
            await self.publish_surface()
