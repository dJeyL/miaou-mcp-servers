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
from typing import Any, Awaitable, Callable

import mcp.types as types

from .logging import _log


class Upstream(ABC):
    # Consigne de portée serveur publiée par l'upstream (champ `instructions`
    # de l'InitializeResult ou du DiscoverResult MCP, selon l'ère négociée), ou
    # None s'il n'en déclare pas. Renseigné par start() — avant, aucun upstream
    # n'a été interrogé et la valeur est None pour tout le monde. Agrégée par
    # aggregate_instructions().
    instructions: str | None = None

    # Ère et capacités négociées avec un upstream stdio ou http : version de
    # protocole retenue (`2026-07-28` pour l'ère moderne, `2025-11-25` pour un
    # serveur qui ne parle que `initialize`) et `ServerCapabilities` de
    # l'upstream, `extensions` comprises en ère moderne. Posées par start(),
    # recalculées à chaque démarrage, jamais mémorisées. None pour l'inprocess,
    # sans fil : ses extensions se lisent sur le MCPServer lui-même.
    protocol_version: str | None = None
    capabilities: types.ServerCapabilities | None = None

    # Ère DEMANDÉE par la config (clé `protocol`) pour un upstream stdio ou
    # http : "auto" (sonde `server/discover`, repli sur `initialize`) ou
    # "legacy" (`initialize` d'emblée), passée telle quelle en `mode` à
    # `Client`. None pour l'inprocess, qui n'a pas de fil.
    mode: str | None = None

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

    # Extension Skills. Par défaut : aucune skill, et aucune requête.
    @property
    def serves_skills(self) -> bool:
        """Le proxy a-t-il le droit de demander ses skills à cet upstream ?

        Condition UNIQUE de tout appel `skills/*` et `resources/*` vers
        l'upstream : faux, les trois méthodes rendent « aucune skill » sans
        émettre de requête."""
        return False

    async def list_skills(self) -> list[dict[str, Any]]:
        """Entrées `Skill` de la spec, URI d'ORIGINE (non préfixées)."""
        return []

    async def list_skills_with_hints(self) -> tuple[list[Any], int | None, str | None]:
        """`list_skills`, plus les indices de cache (`ttlMs`, `cacheScope`) que
        l'upstream a posés sur sa réponse, None s'il n'en a pas transmis."""
        return await self.list_skills(), None, None

    async def get_skill(self, uri: str) -> dict[str, Any]:
        """Entrée de la skill dont `uri` est le `SKILL.md`, ou `MCPError`
        `-32602` si l'upstream ne la sert pas."""
        raise _unserved(uri)

    async def get_skill_with_hints(self, uri: str) -> tuple[dict[str, Any], int | None, str | None]:
        """`get_skill`, plus les indices de cache de la réponse."""
        return await self.get_skill(uri), None, None

    async def read_skill_file(self, uri: str) -> list[types.ResourceContents]:
        """Contenu d'un fichier listé dans le `resources` d'une skill servie,
        URI d'origine ; `MCPError` `-32602` pour toute autre URI."""
        raise _unserved(uri)


def _unserved(uri: str) -> Exception:
    from mcp.shared.exceptions import MCPError

    return MCPError(types.INVALID_PARAMS, f"No skill resource is served at {uri}")


def _adopt_session(upstream: Upstream, session: Any) -> None:
    """Lit sur la session négociée ce que le proxy retient de l'upstream.

    `ClientSession` couvre les deux ères : après `server/discover` comme après
    `initialize`, `instructions`, `protocol_version` et `server_capabilities`
    sont posés."""
    upstream.instructions = session.instructions
    upstream.protocol_version = session.protocol_version
    upstream.capabilities = session.server_capabilities


async def _prime_tool_listing(session: Any) -> None:
    """Liste les outils d'un upstream moderne dès l'ouverture de la session.

    En ère moderne, le SDK n'émet les en-têtes `Mcp-Param-*` d'un `tools/call`
    que pour un outil connu du DERNIER `tools/list` de la session : sans
    listage préalable, l'appel d'un outil à paramètre annoté `x-mcp-header`
    part sans en-tête, et l'upstream le refuse (`-32020`). Le proxy liste à
    chaque `tools/list` de son client, mais pas après le redémarrage
    d'`authorize()`, ni quand il ressert son catalogue en cache : amorcer ici
    ferme la fenêtre par construction.

    Une erreur de l'upstream sur ce listage n'empêche pas le démarrage : seul
    l'appel d'un outil annoté en pâtirait, et `_start_upstreams` liste de
    nouveau et signale l'échec. Une panne du transport, elle, n'est pas
    rattrapée — elle explique l'échec de `start()`."""
    from mcp.shared.exceptions import MCPError
    from mcp_types.version import MODERN_PROTOCOL_VERSIONS

    if session.protocol_version not in MODERN_PROTOCOL_VERSIONS:
        return
    try:
        await session.list_tools()
    except MCPError:
        pass


def _strip_reserved_result_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """`_meta` d'un résultat d'upstream sans l'identité de l'upstream.

    Un upstream moderne signe ses résultats de son `serverInfo`
    (`io.modelcontextprotocol/serverInfo`), et le serveur du proxy ne pose le
    sien que si la clé est ABSENTE : relayée, elle ferait passer l'identité de
    l'upstream pour celle du proxy auprès de son client."""
    return {k: v for k, v in meta.items() if k != types.SERVER_INFO_META_KEY}


def relay_call_result(call_result: types.CallToolResult) -> types.CallToolResult:
    """Résultat d'un upstream stdio/http tel que le proxy le rend à son client.

    Relaie `content`, `isError` et `_meta` (sans le `serverInfo` de l'upstream,
    cf. `_strip_reserved_result_meta`). Jusqu'au lot AI, seul `content`
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
    meta = _strip_reserved_result_meta(dict(call_result.meta or {}))
    if meta:
        fields["_meta"] = meta
    return types.CallToolResult(**fields)


class InProcessUpstream(Upstream):
    """Appelle un MCPServer dans le même processus Python, sans subprocess."""

    # Sans transport : l'extension Skills se lit sur le MCPServer lui-même,
    # qui n'en sert aucune s'il ne l'a pas.
    @property
    def serves_skills(self) -> bool:
        return True

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
        # Les champs que le proxy republie, et eux seuls : nom, description,
        # schéma d'entrée, et `_meta` (déclaration `miaou/requiresSkill`).
        return [
            types.Tool(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
                **({"_meta": dict(t.meta)} if t.meta else {}),
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

    def _skills(self) -> Any:
        # Résolu à CHAQUE appel, jamais capturé : le MCPServer n'existe
        # qu'après start(), et build_proxy_server() s'exécute avant.
        from mcp_base import find_skills_extension

        if self._server is None:
            return None
        return find_skills_extension(self._server)

    async def list_skills(self) -> list[dict[str, Any]]:
        skills = self._skills()
        return skills.list_entries() if skills is not None else []

    async def get_skill(self, uri: str) -> dict[str, Any]:
        skills = self._skills()
        if skills is None:
            raise _unserved(uri)
        return skills.get_entry(uri)

    async def read_skill_file(self, uri: str) -> list[types.ResourceContents]:
        import base64

        # Liste blanche : seuls les fichiers déclarés par une entrée. Le
        # MCPServer peut servir d'autres ressources, que le proxy ne publie pas.
        listed = {
            resource["uri"]
            for entry in await self.list_skills()
            if isinstance(entry.get("resources"), list)
            for resource in entry["resources"]
        }
        if uri not in listed:
            raise _unserved(uri)
        contents: list[types.ResourceContents] = []
        for item in await self._server.read_resource(uri):
            if isinstance(item.content, bytes):
                contents.append(
                    types.BlobResourceContents(
                        uri=uri,
                        mime_type=item.mime_type,
                        blob=base64.b64encode(item.content).decode("ascii"),
                    )
                )
            else:
                contents.append(
                    types.TextResourceContents(uri=uri, mime_type=item.mime_type, text=item.content)
                )
        return contents


_SKILLS_MAX_PAGES = 100
"""Pages de `skills/list` suivies au plus pour un upstream distant : un curseur
qui ne s'épuise jamais (sans se répéter) ne retient pas le listage du proxy."""


def _strictest_cache_hints(results: list[dict[str, Any]]) -> tuple[int | None, str | None]:
    """Les indices de cache les plus restrictifs d'une suite de réponses :
    `ttlMs` minimal (entier positif ou nul seulement), `private` dès qu'une
    réponse le dit. None quand aucune n'en porte d'exploitable."""
    ttls = [
        r.get("ttlMs")
        for r in results
        if isinstance(r.get("ttlMs"), int) and not isinstance(r.get("ttlMs"), bool) and r["ttlMs"] >= 0
    ]
    scopes = {r.get("cacheScope") for r in results} & {"public", "private"}
    scope = "private" if "private" in scopes else ("public" if scopes else None)
    return (min(ttls) if ttls else None), scope


class _RemoteSkills:
    """Les skills d'un upstream stdio ou http, relayées par le fil MCP.

    Le SDK client n'a pas de verbe pour `skills/*` : la requête part par
    `send_request`, et le résultat est lu en `dict` brut, sans modèle — le proxy
    relaie, il ne vérifie pas (le client le fait, empreintes comprises). Pour
    `resources/read`, `session.read_resource`, qui pose `Mcp-Name` en ère
    moderne. Chaque requête est bornée par `_skills_timeout()` : un upstream
    muet ne doit pas retenir `skills/list` du proxy jusqu'au délai de lecture
    du transport.

    La condition d'appel (`serves_skills`) tient en deux points, et il faut les
    deux : l'ère moderne ET l'extension déclarée dans `server/discover`. Un
    serveur 2.x répond à `skills/*` même abordé en legacy (mesuré) : la réponse
    ne dit donc pas si l'on avait le droit de demander, et un upstream legacy,
    constaté ou forcé, ne doit recevoir aucune requête de plus.
    """

    protocol_version: str | None
    capabilities: types.ServerCapabilities | None

    @property
    def serves_skills(self) -> bool:
        from mcp_base import SKILLS_EXTENSION_ID
        from mcp_types.version import MODERN_PROTOCOL_VERSIONS

        if self.protocol_version not in MODERN_PROTOCOL_VERSIONS:
            return False
        extensions = self.capabilities.extensions if self.capabilities is not None else None
        return SKILLS_EXTENSION_ID in (extensions or {})

    def _skills_timeout(self) -> float:
        raise NotImplementedError

    async def _on_session(self, call: Callable[[Any], Awaitable[Any]], what: str) -> Any:
        raise NotImplementedError

    async def _bounded(self, call: Callable[[Any], Awaitable[Any]], what: str) -> Any:
        import anyio

        with anyio.fail_after(self._skills_timeout()):
            return await self._on_session(call, what)

    async def _skills_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        from pydantic import TypeAdapter

        request = types.Request[Any, str](method=method, params=params)
        result = await self._bounded(
            lambda session: session.send_request(request, TypeAdapter(dict[str, Any])),
            f"`{method}`",
        )
        return result if isinstance(result, dict) else {}

    async def list_skills(self) -> list[dict[str, Any]]:
        return (await self.list_skills_with_hints())[0]

    async def list_skills_with_hints(self) -> tuple[list[Any], int | None, str | None]:
        if not self.serves_skills:
            return [], None, None
        entries: list[Any] = []
        hints: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(_SKILLS_MAX_PAGES):
            result = await self._skills_request("skills/list", {"cursor": cursor} if cursor else {})
            hints.append(result)
            page = result.get("skills")
            if isinstance(page, list):
                entries.extend(page)
            cursor = result.get("nextCursor")
            # Arrêt sur curseur absent, vide, non textuel ou déjà vu : un
            # upstream qui rendrait toujours le même ne ferait pas boucler.
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                break
            seen.add(cursor)
        return (entries, *_strictest_cache_hints(hints))

    async def get_skill(self, uri: str) -> dict[str, Any]:
        return (await self.get_skill_with_hints(uri))[0]

    async def get_skill_with_hints(self, uri: str) -> tuple[dict[str, Any], int | None, str | None]:
        from mcp.shared.exceptions import MCPError

        if not self.serves_skills:
            raise _unserved(uri)
        result = await self._skills_request("skills/get", {"uri": uri})
        # La spec enveloppe l'entrée sous `skill` ; le proxy la rend nue,
        # comme l'inprocess, et le handler la ré-enveloppe.
        skill = result.get("skill")
        if not isinstance(skill, dict):
            raise MCPError(types.INTERNAL_ERROR, f"Réponse `skills/get` illisible pour {uri}")
        return (skill, *_strictest_cache_hints([result]))

    async def read_skill_file(self, uri: str) -> list[types.ResourceContents]:
        # Aucune liste blanche ici : c'est l'upstream qui juge si le fichier
        # est servi, et il répond `-32602` sinon. Une liste tirée de son
        # `skills/list` refuserait les fichiers d'une skill servie mais non
        # listée, que la spec exige de savoir lire depuis sa seule URI.
        if not self.serves_skills:
            raise _unserved(uri)
        result = await self._bounded(lambda session: session.read_resource(uri), "`resources/read`")
        return list(result.contents)


_STDIO_HANDSHAKE_TIMEOUT_S = 15

# Borne d'une requête de skills vers un upstream stdio. Même valeur que celle
# du handshake, mais pas le même objet : un subprocess déjà démarré qui ne
# répond pas, et non un subprocess qui ne démarre pas.
_STDIO_SKILLS_TIMEOUT_S = 15


class StdioUpstream(_RemoteSkills, Upstream):
    """Lance un serveur MCP externe en subprocess et communique via stdio."""

    def __init__(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        protocol: str = "auto",
    ) -> None:
        self._command = command
        self._args = args
        self._env = env
        self._cwd = cwd
        self.mode = protocol
        self._exit_stack = AsyncExitStack()
        self._session: Any = None

    async def start(self) -> None:
        import asyncio

        from mcp.client import Client
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env,
            cwd=self._cwd,
        )
        async def _handshake() -> None:
            # `Client` négocie l'ère : sonde `server/discover`, repli sur
            # `initialize` pour un serveur qui ne parle pas 2026-07-28 (et sur
            # délai dépassé, ce qui laisse sa chance à un subprocess lent). Le
            # transport est construit ici et passé tel quel ; `cache=None` : le
            # cache de réponses de `Client` ne sert pas les appels faits sur
            # `client.session`, autant l'écarter sans ambiguïté.
            client = Client(stdio_client(params), mode=self.mode, cache=None)
            await self._exit_stack.enter_async_context(client)
            _adopt_session(self, client.session)
            await _prime_tool_listing(client.session)
            self._session = client.session

        try:
            # wait_for (pas asyncio.timeout, réservé à Python 3.11+) — le PEP 723
            # de ce fichier déclare requires-python >= 3.10.
            await asyncio.wait_for(_handshake(), timeout=_STDIO_HANDSHAKE_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"Subprocess '{self._command}' n'a pas répondu à la négociation "
                f"MCP (handshake) sous {_STDIO_HANDSHAKE_TIMEOUT_S}s."
            ) from e

    async def stop(self) -> None:
        await self._exit_stack.aclose()

    async def list_tools(self) -> list[types.Tool]:
        result = await self._session.list_tools()
        return result.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        result = await self._session.call_tool(name, arguments)
        return relay_call_result(result)

    def _skills_timeout(self) -> float:
        return _STDIO_SKILLS_TIMEOUT_S

    async def _on_session(self, call: Callable[[Any], Awaitable[Any]], what: str) -> Any:
        # Pas de tâche de service : la session est appelée en direct, et la
        # mort du subprocess remonte en `MCPError` « Connection closed ».
        if self._session is None:
            raise RuntimeError(f"Subprocess '{self._command}' sans session ouverte.")
        return await call(self._session)


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


class HttpUpstream(_RemoteSkills, Upstream):
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
        protocol: str = "auto",
    ) -> None:
        self._url = url
        self._headers = headers
        self._auth = auth
        self._timeout = timeout
        self.mode = protocol
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
        from mcp.client import Client
        from mcp.client.streamable_http import streamable_http_client

        try:
            http_client = self._build_http_client()
            # Transport construit ici, pas l'URL passée à `Client` : le client
            # httpx2 de _build_http_client reste le seul employé (en-têtes,
            # délais, auth OAuth, proxy réseau). `Client` négocie l'ère comme
            # pour stdio, et s'ouvre et se referme dans CETTE tâche.
            transport = streamable_http_client(self._url, http_client=http_client)
            async with http_client, Client(transport, mode=self.mode, cache=None) as client:
                _adopt_session(self, client.session)
                await _prime_tool_listing(client.session)
                self._session = client.session
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
                f"Le serveur MCP distant '{self._url}' n'a pas répondu à la "
                f"négociation MCP (handshake) sous {self._timeout}s."
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
        result = await self._on_session(
            lambda session: session.call_tool(name, arguments), f"l'appel de '{name}'"
        )
        return relay_call_result(result)

    def _skills_timeout(self) -> float:
        return self._timeout

    async def _on_session(self, call: Callable[[Any], Awaitable[Any]], what: str) -> Any:
        """Appelle la session, en surveillant la MORT DE LA TÂCHE DE SERVICE.

        Garde UNIQUE de toute requête vers l'upstream émise hors de `_serve` :
        `call_tool` et les trois méthodes de skills passent par elle.

        La session vit dans `_serve()`, une autre tâche (patron des cancel
        scopes anyio). Une exception levée par le transport de CETTE tâche —
        typiquement `AuthorizationRequired`, quand l'AS ne réclame son jeton
        qu'au premier appel réel — y est capturée, rangée dans `_failure`, et
        `_serve` sort de ses contextes. Le stream se ferme alors sous les pieds
        de l'appelant, **sans réponse ni erreur pour lui** : la requête attend
        une réponse qui n'arrivera jamais, jusqu'à son propre timeout.
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

        result: Any = None
        done = False

        async def _call() -> None:
            nonlocal result, done
            result = await call(session)
            done = True
            task_group.cancel_scope.cancel()

        async def _watch_service_death() -> None:
            # `_stopped` est signalé par le `finally` de `_serve`, donc dans
            # TOUS les cas où la session cesse d'être servie — échec compris.
            if self._stopped is None:  # pragma: no cover
                return
            await self._stopped.wait()
            task_group.cancel_scope.cancel()

        # Déballé : anyio enveloppe dans un ExceptionGroup ce qui sort du task
        # group, y compris la MCPError par laquelle l'upstream refuse l'appel.
        # Enveloppée, elle échappait aux sites d'appel, et le client lisait
        # « unhandled errors in a TaskGroup » au lieu du motif de l'upstream.
        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(_call)
                task_group.start_soon(_watch_service_death)
        except Exception as e:
            cause = _unwrap_exception_group(e)
            if cause is e:
                raise
            raise cause from None

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
            f"{what}."
        )
