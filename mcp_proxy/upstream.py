"""Les trois types d'upstream que le proxy sait piloter.

`InProcessUpstream` importe un module serveur et lui parle sans subprocess ;
`StdioUpstream` lance un subprocess ; `HttpUpstream` parle streamable-http à un
serveur tiers. Tous exposent la même surface `Upstream`.
"""

from __future__ import annotations

import importlib
import os
import sys
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack
from typing import Any

import mcp.types as types

from .logging import _log


class Upstream(ABC):
    # Consigne de portée serveur publiée par l'upstream (champ `instructions`
    # de l'InitializeResult MCP), ou None s'il n'en déclare pas. Renseigné par
    # start() — avant, aucun upstream n'a été interrogé et la valeur est None
    # pour tout le monde. Agrégée par aggregate_instructions().
    instructions: str | None = None

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def list_tools(self) -> list[types.Tool]: ...

    @abstractmethod
    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> list[Any] | types.CallToolResult: ...


def relay_call_result(call_result: types.CallToolResult) -> types.CallToolResult:
    """Résultat d'un upstream stdio/http tel que le proxy le rend à son client.

    Relaie `content`, `isError` et `_meta`. Jusqu'au lot AI, seul `content`
    passait : le SDK ré-enveloppait la liste en `isError=False`, si bien qu'un
    échec signalé par l'upstream arrivait au client comme un succès, et que le
    `_meta` d'un résultat (`miaou/web` de mcp_web) disparaissait.

    `structuredContent` n'est PAS relayé, par cohérence avec `tools/list` : le
    proxy ne publie pas l'`outputSchema` des upstreams. Un CallToolResult rendu
    par le handler traverse le SDK sans validation de sortie.

    `**{"_meta": ...}` : pydantic ne sérialise sous l'alias que si le champ a
    été peuplé par l'alias (même remarque que pour `tools/list`)."""
    fields: dict[str, Any] = {
        "content": list(call_result.content),
        "is_error": bool(call_result.is_error),
    }
    if call_result.meta:
        fields["_meta"] = dict(call_result.meta)
    return types.CallToolResult(**fields)


class InProcessUpstream(Upstream):
    """Appelle un MCPServer dans le même processus Python, sans subprocess."""

    def __init__(
        self,
        module_name: str,
        env: dict[str, str] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._module_name = module_name
        self._env = env
        self._config = config
        self._server: Any = None

    async def start(self) -> None:
        if self._env:
            import os
            for key, value in self._env.items():
                os.environ.setdefault(key, value)
        already_imported = self._module_name in sys.modules
        module = importlib.import_module(self._module_name)
        # Un module qui expose build(config) -> MCPServer supporte le
        # multi-instance (plusieurs entrées config.json du même module, chacune
        # avec sa propre config) : importlib.import_module ne recharge un module
        # qu'une fois par process, donc tout état lu au niveau module (ou via
        # env) serait figé à la première instanciation. Fallback sur le
        # singleton module.mcp pour les serveurs qui n'ont pas besoin de
        # multi-instance.
        build_fn = getattr(module, "build", None)
        if build_fn is not None:
            server = build_fn(self._config)
        else:
            if already_imported:
                print(
                    f"Attention : module '{self._module_name}' réutilisé par plusieurs "
                    f"entrées inprocess sans build(config) — même instance MCPServer "
                    f"partagée (env figé au premier import).",
                    file=sys.stderr,
                )
            server = getattr(module, "mcp", None)
        if server is None:
            raise RuntimeError(
                f"Le module '{self._module_name}' n'expose ni 'build(config)' ni 'mcp' (MCPServer)."
            )
        self._server = server
        # Pas d'`initialize` sur ce chemin (on parle au MCPServer en direct,
        # sans transport) : les instructions se lisent sur l'objet, là où les
        # deux autres upstreams les reçoivent dans leur InitializeResult.
        self.instructions = server.instructions

    async def stop(self) -> None:
        pass

    async def list_tools(self) -> list[types.Tool]:
        # Les trois champs que le proxy republie, et eux seuls : c'est la forme
        # que les upstreams ont toujours eue ici.
        return [
            types.Tool(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
            )
            for t in await self._server.list_tools()
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        # API publique : rend un CallToolResult (contenu converti, `_meta` d'un
        # outil qui le pose — fetch_url de mcp_web —, données structurées),
        # que le proxy laisse traverser tel quel. Une ToolError de l'outil
        # remonte en exception, que handle_call_tool rend en isError ; une
        # MCPError (REF_UNKNOWN) remonte telle quelle, et traverse.
        return await self._server.call_tool(name, arguments)


_STDIO_HANDSHAKE_TIMEOUT_S = 15


class StdioUpstream(Upstream):
    """Lance un serveur MCP externe en subprocess et communique via stdio."""

    def __init__(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._command = command
        self._args = args
        self._env = env
        self._cwd = cwd
        self._exit_stack = AsyncExitStack()
        self._session: Any = None

    async def start(self) -> None:
        import asyncio

        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env,
            cwd=self._cwd,
        )
        async def _handshake() -> None:
            read, write = await self._exit_stack.enter_async_context(stdio_client(params))
            session = ClientSession(read, write)
            self._session = await self._exit_stack.enter_async_context(session)
            result = await self._session.initialize()
            self.instructions = result.instructions

        try:
            # wait_for (pas asyncio.timeout, réservé à Python 3.11+) — le PEP 723
            # de ce fichier déclare requires-python >= 3.10.
            await asyncio.wait_for(_handshake(), timeout=_STDIO_HANDSHAKE_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"Subprocess '{self._command}' n'a pas répondu au handshake MCP "
                f"initialize sous {_STDIO_HANDSHAKE_TIMEOUT_S}s."
            ) from e

    async def stop(self) -> None:
        await self._exit_stack.aclose()

    async def list_tools(self) -> list[types.Tool]:
        result = await self._session.list_tools()
        return result.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        result = await self._session.call_tool(name, arguments)
        return relay_call_result(result)


# Borne du handshake d'un upstream HTTP. Constante distincte de celle des
# subprocess stdio, et non un partage : les deux mesurent des choses différentes
# (un subprocess qui ne démarre pas vs un serveur distant injoignable), et un
# jour l'une bougera sans l'autre. Un nom qui mentirait sur ce qu'il borne vaut
# un commentaire faux.
_HTTP_HANDSHAKE_TIMEOUT_S = 30


def _unwrap_exception_group(exc: BaseException) -> BaseException:
    """Rend la cause réelle d'un ExceptionGroup à cause unique.

    anyio enveloppe systématiquement ce qui sort d'un task group. Laisser
    l'enveloppe remonter donnerait "unhandled errors in a TaskGroup" en guise
    de diagnostic — même effacement de la cause que celui déjà payé sur les
    cancel scopes (cf. HttpUpstream). Une enveloppe à causes MULTIPLES est
    rendue telle quelle : il n'y a alors rien à choisir.
    """
    subs = getattr(exc, "exceptions", None)
    while subs is not None and len(subs) == 1:
        exc = subs[0]
        subs = getattr(exc, "exceptions", None)
    return exc


class HttpUpstream(Upstream):
    """Serveur MCP distant, transport streamable-http.

    `auth` est un httpx2.Auth (None = aucune authentification) : c'est par ce
    seul paramètre que le client OAuth se branche, sans que cette classe ait à
    connaître OAuth.

    Le proxy réseau (--proxy/--noproxy) est honoré sans code ici : le client
    httpx2 est construit avec le défaut trust_env=True, donc il lit les
    variables d'environnement du process, que main() a déjà posées avant
    build_upstreams(). Attention, httpx2 lit AUSSI ALL_PROXY et NO_PROXY, que
    _PROXY_ENV_KEYS ne gère pas — limite documentée dans docs/proxy.md.

    **Le transport vit dans SA propre tâche, du début à la fin.** Les contextes
    asynchrones du SDK (streamable_http_client, ClientSession) portent des cancel
    scopes anyio, qu'anyio interdit d'ouvrir dans une tâche et de refermer dans
    une autre. Une AsyncExitStack ouverte par start() et refermée par stop()
    fait exactement ce croisement dès que les deux ne tournent pas dans la même
    tâche — ce qui est le cas ici (démarrage dans le lifespan, arrêt ailleurs,
    ré-autorisation dans une tâche de fond). Le symptôme est un
    "Attempted to exit cancel scope in a different task than it was entered in"
    qui REMPLACE la cause réelle : une autorisation manquante arrivait ainsi
    illisible jusqu'au log. D'où ce patron — une tâche de service dédiée, qui
    ouvre, signale, attend l'ordre d'arrêt, puis referme au même endroit.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        auth: Any = None,
        timeout: float = _HTTP_HANDSHAKE_TIMEOUT_S,
    ) -> None:
        self._url = url
        self._headers = headers
        self._auth = auth
        self._timeout = timeout
        self._session: Any = None
        self._host_task_group: Any = None
        self._stop_event: Any = None
        self._ready: Any = None
        self._stopped: Any = None
        self._serving = False
        self._failure: BaseException | None = None

    def _build_http_client(self) -> Any:
        """Le client HTTP de la session amont — le SEUL que l'upstream emploie.

        En-têtes, délais et auth se posent ici, sur le client, que le transport
        du SDK 2.x ne construit plus lui-même dès qu'on lui en passe un. Le délai
        de LECTURE est explicite : sans lui httpx2 retombe sur 5 s à plat, trop
        court pour le flux GET long du transport — 300 s est la valeur que
        l'ancien transport appliquait d'office.

        `trust_env` reste au défaut (vrai) : c'est ce qui fait honorer
        --proxy/--noproxy, posés dans os.environ par main(). Les tests portent
        sur CE client, pas sur celui que le SDK construirait à notre place.
        """
        import httpx2

        return httpx2.AsyncClient(
            headers=self._headers,
            timeout=httpx2.Timeout(self._timeout, read=300),
            auth=self._auth,
        )

    async def _serve(self) -> None:
        """Tourne pour toute la vie de l'upstream, dans une tâche à elle."""
        import anyio

        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        try:
            http_client = self._build_http_client()
            async with http_client, streamable_http_client(
                self._url, http_client=http_client
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    result = await session.initialize()
                    self.instructions = result.instructions
                    self._session = session
                    self._ready.set()
                    # Reste ouvert jusqu'à stop() : c'est ce maintien qui garde
                    # la session utilisable entre deux appels d'outil.
                    await self._stop_event.wait()
        except Exception as e:
            self._failure = _unwrap_exception_group(e)
        finally:
            self._session = None
            # Toujours réveiller start(), y compris en échec : sinon il
            # attendrait sa borne entière pour une erreur déjà connue.
            self._ready.set()
            self._stopped.set()

    def host_tasks_in(self, task_group: Any) -> None:
        """Désigne le task group qui HÉBERGE la tâche de service.

        Il doit vivre au moins aussi longtemps que l'upstream : celui du
        lifespan. Le laisser créer son propre task group serait tentant, mais
        le scope appartiendrait alors à la tâche qui a appelé start() — or
        celle-ci peut être éphémère (une requête /authorize/{name}, par
        exemple), et le scope resterait ouvert dans une tâche morte. anyio le
        signale par "Attempted to exit a cancel scope that isn't the current
        task's current cancel scope", encore un message qui remplace la cause.
        """
        self._host_task_group = task_group

    async def start(self) -> None:
        import anyio

        if self._host_task_group is None:
            raise RuntimeError(
                "HttpUpstream.start() sans task group hôte : appeler "
                "host_tasks_in() d'abord (build_app le fait au lifespan)."
            )

        await self.stop()  # relance propre : jamais deux tâches de service

        # Les trois événements sont créés AVANT le start_soon : stop() peut
        # être appelé sur-le-champ, y compris avant que la tâche ait tourné.
        self._stop_event = anyio.Event()
        self._ready = anyio.Event()
        self._stopped = anyio.Event()
        self._failure = None
        self._serving = True
        self._host_task_group.start_soon(self._serve)

        with anyio.move_on_after(self._timeout):
            await self._ready.wait()

        if self._session is None:
            failure = self._failure
            # Un serveur qui ne répond jamais laisse la tâche bloquée dans le
            # handshake, où l'événement d'arrêt n'est pas encore attendu : on
            # la réveille par l'événement, la borne du serve() fera le reste.
            await self.stop()
            if failure is not None:
                raise failure
            raise RuntimeError(
                f"Le serveur MCP distant '{self._url}' n'a pas répondu au "
                f"handshake MCP initialize sous {self._timeout}s."
            )

    async def stop(self) -> None:
        """Demande l'arrêt à la tâche de service et attend qu'elle ait rendu.

        On ne ferme aucun scope ici : c'est _serve(), dans SA tâche, qui sort
        de ses propres contextes. C'est toute la raison d'être de ce patron.
        """
        if not self._serving:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        if self._stopped is not None:
            import anyio

            # Borné : un transport qui refuse de se fermer ne doit pas retenir
            # l'extinction du proxy.
            with anyio.move_on_after(self._timeout):
                await self._stopped.wait()
        self._serving = False
        self._session = None

    async def list_tools(self) -> list[types.Tool]:
        result = await self._session.list_tools()
        return result.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """Appelle l'outil, en surveillant la MORT DE LA TÂCHE DE SERVICE.

        La session vit dans `_serve()`, une autre tâche (patron des cancel
        scopes anyio). Une exception levée par le transport de CETTE tâche —
        typiquement `AuthorizationRequired`, quand l'AS ne réclame son jeton
        qu'au premier appel réel — y est capturée, rangée dans `_failure`, et
        `_serve` sort de ses contextes. Le stream se ferme alors sous les pieds
        de l'appelant, **sans réponse ni erreur pour lui** : `session.call_tool`
        attend une réponse qui n'arrivera jamais, jusqu'à son propre timeout.
        Observé en production le 2026-09-07 (client suspendu, refus jamais
        rendu) et reproduit en banc.

        On attend donc l'appel ET la fin de la tâche de service, la première des
        deux qui vient l'emportant. Si le service meurt d'abord, on relève SA
        cause : c'est elle qui explique l'échec, et c'est elle que le site
        d'appel sait convertir en refus d'autorisation.
        """
        import anyio

        session = self._session
        if session is None:
            failure = self._failure
            if failure is not None:
                raise failure
            raise RuntimeError(
                f"Le serveur MCP distant '{self._url}' n'a pas de session ouverte."
            )

        result: types.CallToolResult | None = None
        done = False

        async def _call() -> None:
            nonlocal result, done
            call_result = await session.call_tool(name, arguments)
            result = relay_call_result(call_result)
            done = True
            task_group.cancel_scope.cancel()

        async def _watch_service_death() -> None:
            # `_stopped` est signalé par le `finally` de `_serve`, donc dans
            # TOUS les cas où la session cesse d'être servie — échec compris.
            if self._stopped is None:  # pragma: no cover
                return
            await self._stopped.wait()
            task_group.cancel_scope.cancel()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_call)
            task_group.start_soon(_watch_service_death)

        if done:
            return result

        # La tâche de service est morte avant la réponse : sa cause est la
        # vraie explication, et `_failure` la porte déjà (posée AVANT le
        # `finally` qui signale `_stopped`, donc lisible ici).
        failure = self._failure
        if failure is not None:
            raise failure
        raise RuntimeError(
            f"Le serveur MCP distant '{self._url}' a fermé sa session pendant "
            f"l'appel de '{name}'."
        )
