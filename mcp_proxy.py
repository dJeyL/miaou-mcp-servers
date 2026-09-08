#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.28.1,<2", "uvicorn", "starlette", "anyio", "pyjwt[crypto]", "html2text", "pymupdf", "python-docx", "openpyxl", "python-pptx", "truststore"]
# ///
"""
Serveur MCP proxy pour MIAOU — agrège plusieurs serveurs MCP upstream.

Transport streamable-http uniquement (HTTP ↔ MIAOU). Les upstreams peuvent être
in-process (import Python direct, pas de subprocess) ou stdio (subprocess externe).

Tous les outils upstream sont exposés préfixés du nom de serveur suivi de "__" :
    bench__echo, bench__get_image, weather__get_weather, …

Configuration : config.json (non versionné, copier config.sample.json).

Lancement :
    uv run mcp_proxy.py                            # lit config.json, port dedans
    uv run mcp_proxy.py --config mon_config.json   # config alternative
    uv run mcp_proxy.py --host 0.0.0.0             # override host
    uv run mcp_proxy.py --port 8765                # override port

Dans MIAOU → Paramètres → Serveurs MCP → Ajouter :
    Nom       : proxy
    URL       : http://127.0.0.1:<port>/mcp
    Transport : streamable-http
    Activé    : oui
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path

# Rend les modules servers/ importables directement ("mcp_bench", "mcp_weather")
# quand le proxy est lancé depuis la racine du projet.
_SERVERS_DIR = Path(__file__).parent / "servers"
if _SERVERS_DIR.exists() and str(_SERVERS_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVERS_DIR))

from contextlib import AsyncExitStack
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount, Route

# Importé après l'insertion de servers/ dans sys.path (plus haut) : c'est ce qui
# rend `mcp_base` atteignable. Le proxy ne réimplémente pas l'activation du
# magasin de confiance système — même code que les serveurs standalone, pour que
# les deux modes de lancement se comportent identiquement face à une AC interne.
from mcp_base import enable_system_trust_store


# ---------------------------------------------------------------------------
# Upstream abstraction
# ---------------------------------------------------------------------------

class Upstream(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def list_tools(self) -> list[types.Tool]: ...

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[Any]: ...


class InProcessUpstream(Upstream):
    """Appelle un FastMCP dans le même processus Python, sans subprocess."""

    def __init__(
        self,
        module_name: str,
        env: dict[str, str] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._module_name = module_name
        self._env = env
        self._config = config
        self._tool_manager: Any = None
        # Renseigné au start() si le module upstream expose le contrat
        # REF_UNKNOWN (cf. _ref_unknown_contract / _wrap_ref_unknown_sentinel).
        # Aucun import de mcp_docs ici : c'est le module réellement chargé, quel
        # qu'il soit, qui déclare son sentinel — le proxy n'en connaît aucun.
        self.ref_unknown_contract: tuple[str, Any] | None = None

    async def start(self) -> None:
        if self._env:
            import os
            for key, value in self._env.items():
                os.environ.setdefault(key, value)
        already_imported = self._module_name in sys.modules
        module = importlib.import_module(self._module_name)
        # Un module qui expose build(config) -> FastMCP supporte le multi-instance
        # (plusieurs entrées config.json du même module, chacune avec sa propre
        # config) : importlib.import_module ne recharge un module qu'une fois par
        # process, donc tout état lu au niveau module (ou via env) serait figé à
        # la première instanciation. Fallback sur le singleton module.mcp pour les
        # serveurs existants, qui n'ont pas besoin de multi-instance.
        build_fn = getattr(module, "build", None)
        if build_fn is not None:
            fastmcp = build_fn(self._config)
        else:
            if already_imported:
                print(
                    f"Attention : module '{self._module_name}' réutilisé par plusieurs "
                    f"entrées inprocess sans build(config) — même instance FastMCP "
                    f"partagée (env figé au premier import).",
                    file=sys.stderr,
                )
            fastmcp = getattr(module, "mcp", None)
        if fastmcp is None:
            raise RuntimeError(
                f"Le module '{self._module_name}' n'expose ni 'build(config)' ni 'mcp' (FastMCP)."
            )
        self._tool_manager = fastmcp._tool_manager

        # PRX2 : lecture opportuniste du contrat REF_UNKNOWN sur le module qui
        # vient d'être importé, au lieu d'un `from mcp_docs import ...` au niveau
        # du wrapper — celui-ci forçait l'import de mcp_docs (et de pymupdf,
        # python-docx, openpyxl, python-pptx, plus l'instanciation de
        # DocsServer()) même sur un proxy configuré sans l'entrée docs.
        sentinel = getattr(module, "REF_UNKNOWN_SENTINEL", None)
        error_code = getattr(module, "REF_UNKNOWN_ERROR_CODE", None)
        if sentinel is not None and error_code is not None:
            self.ref_unknown_contract = (sentinel, error_code)

    async def stop(self) -> None:
        pass

    async def list_tools(self) -> list[types.Tool]:
        tools = self._tool_manager.list_tools()
        return [
            types.Tool(
                name=t.name,
                description=t.description,
                inputSchema=t.parameters,
            )
            for t in tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[Any]:
        return await self._tool_manager.call_tool(name, arguments, convert_result=True)


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
            await self._session.initialize()

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

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[Any]:
        result = await self._session.call_tool(name, arguments)
        return result.content


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

    `auth` est un httpx.Auth (None = aucune authentification) : c'est par ce
    seul paramètre que le client OAuth se branche, sans que cette classe ait à
    connaître OAuth.

    Le proxy réseau (--proxy/--noproxy) est honoré sans code ici : le client
    httpx du SDK est construit avec le défaut trust_env=True, donc il lit les
    variables d'environnement du process, que main() a déjà posées avant
    build_upstreams(). Attention, httpx lit AUSSI ALL_PROXY et NO_PROXY, que
    _PROXY_ENV_KEYS ne gère pas — limite documentée dans docs/proxy.md.

    **Le transport vit dans SA propre tâche, du début à la fin.** Les contextes
    asynchrones du SDK (streamablehttp_client, ClientSession) portent des cancel
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

    async def _serve(self) -> None:
        """Tourne pour toute la vie de l'upstream, dans une tâche à elle."""
        import anyio

        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        try:
            async with streamablehttp_client(
                self._url,
                headers=self._headers,
                timeout=self._timeout,
                auth=self._auth,
            ) as streams:
                # Triplet (read, write, get_session_id) ; le troisième ne sert
                # pas ici, le proxy ne gère pas la session HTTP amont.
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
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

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[Any]:
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

        result: list[Any] = []
        done = False

        async def _call() -> None:
            nonlocal result, done
            call_result = await session.call_tool(name, arguments)
            result = call_result.content
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


# ---------------------------------------------------------------------------
# Override CLI du proxy réseau (--proxy / --noproxy)
# ---------------------------------------------------------------------------

# Les 4 variantes de casse lues par urllib (make_opener) comme par la plupart
# des clients HTTP — on les pose/efface toutes pour ne pas laisser une variante
# existante contredire l'override CLI.
_PROXY_ENV_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")


def resolve_proxy_url(raw: str) -> str:
    """Ajoute "http://" si l'argument --proxy ne porte aucun schéma."""
    return raw if "://" in raw else f"http://{raw}"


def compute_proxy_env_overrides(
    proxy: str | None, noproxy: bool
) -> dict[str, str | None] | None:
    """Calcule les overrides d'environnement proxy à appliquer partout (inprocess
    via os.environ du process, stdio via le dict env du subprocess).

    None → aucun override (comportement inchangé, ni --proxy ni --noproxy).
    Une valeur str → variable posée à cette valeur (ex. "http://host:port").
    Une valeur None → variable à supprimer/ne pas transmettre (--noproxy).

    --proxy et --noproxy sont absolus : ils priment sur tout http_proxy/https_proxy
    déjà défini dans le `env` d'une entrée config.json (choix explicite : la CLI
    est la garantie ultime de contrôle du proxy réseau vu par les upstreams).
    """
    if noproxy:
        return {key: None for key in _PROXY_ENV_KEYS}
    if proxy:
        url = resolve_proxy_url(proxy)
        return {key: url for key in _PROXY_ENV_KEYS}
    return None


def apply_proxy_env_overrides_to_process(overrides: dict[str, str | None]) -> None:
    """Applique les overrides à os.environ du process proxy lui-même — couvre tous
    les upstreams inprocess, qui partagent ce process (make_opener() dans
    mcp_base.py relit os.environ à chaque requête via ProxyHandler())."""
    for key, value in overrides.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def merge_proxy_env_overrides(
    env: dict[str, str] | None, overrides: dict[str, str | None] | None
) -> dict[str, str] | None:
    """Fusionne les overrides proxy dans le `env` d'un upstream stdio (config.json
    prioritaire par défaut, mais CLI écrase toujours — cf. compute_proxy_env_overrides).
    Ignoré si overrides est None (ni --proxy ni --noproxy) : env repart inchangé,
    y compris None (StdioServerParameters distingue env=None de env={})."""
    if overrides is None:
        return env
    merged = dict(env) if env else {}
    for key, value in overrides.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: str | Path) -> dict[str, Any]:
    try:
        cfg = json.loads(Path(path).read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"La config '{path}' n'est pas un JSON valide : {e}") from e
    if "port" not in cfg:
        raise ValueError(f"La config '{path}' doit contenir la clé 'port'.")
    return cfg


def build_upstreams(
    cfg: dict[str, Any],
    proxy_env_overrides: dict[str, str | None] | None = None,
) -> dict[str, Upstream]:
    """`proxy_env_overrides` (cf. compute_proxy_env_overrides) : appliqué au `env`
    de chaque upstream stdio. Les inprocess n'en ont pas besoin ici — ils partagent
    l'environnement du process proxy, déjà modifié directement par main() via
    apply_proxy_env_overrides_to_process() avant l'import des modules. Les http
    non plus, et pour la même raison : leur client httpx lit os.environ du
    process (trust_env=True par défaut), déjà modifié au même endroit."""
    upstreams: dict[str, Upstream] = {}
    for name, srv in cfg.get("mcpServers", {}).items():
        if srv.get("_disabled"):
            continue
        srv_type = srv.get("type", "stdio")
        if srv_type == "inprocess":
            module = srv.get("module")
            if not module:
                raise ValueError(f"Serveur '{name}' inprocess sans clé 'module'.")
            upstreams[name] = InProcessUpstream(
                module, env=srv.get("env"), config=srv.get("config")
            )
        elif srv_type == "stdio":
            command = srv.get("command")
            if not command:
                raise ValueError(f"Serveur '{name}' stdio sans clé 'command'.")
            env = merge_proxy_env_overrides(srv.get("env"), proxy_env_overrides)
            upstreams[name] = StdioUpstream(
                command=command,
                args=srv.get("args", []),
                env=env,
                cwd=srv.get("cwd"),
            )
        elif srv_type == "http":
            url = srv.get("url")
            if not url:
                raise ValueError(f"Serveur '{name}' http sans clé 'url'.")
            upstreams[name] = HttpUpstream(
                url=url,
                headers=srv.get("headers"),
                timeout=srv.get("timeout", _HTTP_HANDSHAKE_TIMEOUT_S),
            )
        else:
            raise ValueError(f"Type de serveur inconnu pour '{name}': '{srv_type}'.")
    return upstreams


# ---------------------------------------------------------------------------
# Proxy MCP Server (low-level mcp.server.Server pour l'enregistrement dynamique)
# ---------------------------------------------------------------------------

AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
"""Code applicatif du refus d'un outil dont l'upstream n'est pas autorisé.

Contrat avec le client (MIAOU, lot AB-3), sur le motif déjà éprouvé de
REF_UNKNOWN : le code voyage dans `error.data.code` d'une vraie erreur JSON-RPC
— `code` au niveau de l'erreur reste l'entier protocolaire, `data` est le slot
applicatif — et le client le teste par ÉGALITÉ de cette constante, jamais par
sous-chaîne dans le message. Un message est de la prose : il se traduit, se
reformule, et un test par sous-chaîne casse sans prévenir.
"""


def authorize_path(upstream_name: str) -> str:
    """Chemin à ouvrir pour (ré)autoriser un upstream. Source UNIQUE.

    Ses trois consommateurs — le contrat d'erreur, le rapport `status` et le
    `_meta` de `tools/list` — passent tous par ici : recomposer la chaîne
    ailleurs laisserait deux natures de lien pour une même action.

    **Relatif, jamais absolu.** Le proxy ne connaît que son adresse d'écoute
    (`build_callback_url` replie même `0.0.0.0` sur `127.0.0.1`) : un proxy
    atteint derrière un reverse proxy donnerait une URL injoignable. C'est au
    client de composer l'origine depuis l'URL qu'il a lui-même configurée — la
    seule valeur qui décrive comment il joint réellement le proxy.

    À ne pas confondre avec `UpstreamAuthorizer.last_authorization_url`, qui
    porte l'URL de l'AS pour une transaction ABANDONNÉE (le `state` et le PKCE
    challenge d'un parcours non interactif interrompu au démarrage) : bonne à
    afficher en diagnostic, mauvaise à suivre — la suivre mène à `/callback`
    sans `pending`, donc à une erreur. Le chemin rendu ici, lui, lance un
    parcours frais.
    """
    return f"/authorize/{upstream_name}"


UNAUTHORIZED_UPSTREAMS_META_KEY = "miaou/unauthorized_upstreams"
"""Clé du `_meta` de `tools/list` énumérant les upstreams non autorisés.

Surface adressée au CLIENT, là où la description marquée
(`format_stale_description`) et le rapport `status` s'adressent au modèle : sans
elle, un client ne peut pas savoir qu'un upstream est dégradé sans faire appeler
un outil par un modèle, ce qui condamne l'utilisateur à découvrir le besoin
d'autorisation par un échec.

Valeur : une liste d'objets `{"name": …, "authorize_path": …}`. Une liste dès la
première version — N upstreams d'un même proxy peuvent être non autorisés
simultanément, et un objet singulier serait à refaire. Clé absente, jamais liste
vide, quand il n'y a rien à signaler.

Le préfixe `miaou/` est délibéré : `_meta` est un espace partagé, une clé nue
collisionnerait avec une extension future du SDK ou d'un autre agrégateur.
"""


class ToolCatalogCache:
    """Se souvient des outils d'un upstream, pour les servir quand il ne répond
    plus.

    Sans ce cache, un upstream non autorisé serait muet : `tools/list` répond
    401 avant de rien dire, donc on ne saurait pas quels outils annoncer, et le
    troisième état (« connu mais pas autorisé ») n'aurait rien à montrer. Il
    couvre du même geste le redémarrage du proxy, où rien n'est encore connu.

    Sur disque, à côté du fichier de jetons : ce n'est pas un secret, mais ça
    partage sa durée de vie.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            _log(f"Cache d'outils illisible ({e}) — ignoré.")
            return {}
        return data if isinstance(data, dict) else {}

    def remember(self, upstream_name: str, tools: list[types.Tool]) -> None:
        data = self._read()
        data[upstream_name] = {
            "known_at": time.time(),
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.inputSchema,
                }
                for t in tools
            ],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, indent=2, sort_keys=True))
        except OSError as e:
            # Un cache non écrit dégrade, il ne casse pas : le proxy sert
            # toujours l'upstream, il l'oubliera seulement au redémarrage.
            _log(f"Cache d'outils non écrit ({e}).")

    def recall(self, upstream_name: str) -> tuple[list[types.Tool], float | None]:
        entry = self._read().get(upstream_name)
        if not isinstance(entry, dict):
            return [], None
        tools = []
        for raw in entry.get("tools", []):
            try:
                tools.append(
                    types.Tool(
                        name=raw["name"],
                        description=raw.get("description"),
                        inputSchema=raw.get("inputSchema") or {},
                    )
                )
            except Exception:
                continue
        return tools, entry.get("known_at")


def format_stale_description(description: str | None, known_at: float | None) -> str:
    """Marque une description d'outil resservie depuis le cache.

    Le modèle appelant doit pouvoir distinguer un outil vivant d'un outil dont
    on se souvient : présenter une liste périmée comme vivante serait lui
    mentir, et il n'a aucun autre moyen de le savoir. La date est absolue (pas
    « il y a 3 h ») — un texte qui change à chaque tour casserait le cache KV
    du modèle.
    """
    base = (description or "").strip()
    when = ""
    if known_at:
        when = " (dernière liste connue : " + time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(known_at)
        ) + ")"
    notice = (
        f"[Serveur non autorisé — cet outil n'est PAS appelable pour l'instant{when}. "
        f"L'appeler renvoie une erreur {AUTHORIZATION_REQUIRED} indiquant comment "
        f"l'utilisateur peut l'autoriser.]"
    )
    return f"{notice} {base}".strip()


def _resolve_via_prefix(
    name: str, upstreams: dict[str, Upstream]
) -> tuple[str, str] | None:
    """Fallback quand `name` n'est pas (encore) dans tool_map : un client qui
    rappelle tools/call après reconnexion (cache local, sans repasser par
    tools/list) sinon reçoit "Outil inconnu" à tort. tool_map reste l'autorité
    pour tout nom qui contient lui-même "__" au-delà du premier segment."""
    if "__" not in name:
        return None
    prefix, orig_name = name.split("__", 1)
    if prefix in upstreams:
        return prefix, orig_name
    return None


_AUTHORIZATION_SENTINEL = "__MIAOU_AUTHORIZATION_REQUIRED__"
"""Marqueur interne, jamais vu du client.

Il traverse le `except Exception` que le SDK pose autour de tout handler
d'outil, seul chemin par lequel un refus levé côté proxy peut ressortir en
erreur JSON-RPC plutôt qu'en `isError` textuel. Distinct de la constante de
contrat AUTHORIZATION_REQUIRED, qui, elle, est publique et voyage dans
`error.data.code`.
"""


class UpstreamNotAuthorized(Exception):
    """Appel d'un outil dont l'upstream n'est pas (encore) autorisé.

    Interne au proxy : jamais vue du client, qui reçoit l'erreur JSON-RPC
    produite par _wrap_authorization_required.
    """

    def __init__(self, upstream_name: str) -> None:
        # Le sentinel voyage DANS le message : c'est la seule voie qui traverse
        # le `except Exception` du SDK (cf. _wrap_authorization_required). Il
        # est retiré du message avant que celui-ci n'atteigne le client.
        #
        # Le message nomme QUI peut agir, et ne donne pas d'adresse à suivre :
        # il est lu par un modèle, qui ne peut ni ouvrir un lien ni résoudre un
        # chemin relatif contre l'origine du proxy. Le chemin y figure en
        # diagnostic — pour que le modèle puisse le citer à l'utilisateur, seul
        # capable de l'ouvrir. L'affordance cliquable, elle, passe par le
        # `_meta` de `tools/list`, adressé au client.
        message = (
            f"{_AUTHORIZATION_SENTINEL} Le serveur '{upstream_name}' exige une "
            f"autorisation OAuth qui n'a pas encore été accordée. Seul "
            f"l'utilisateur peut l'accorder, depuis son client MCP "
            f"(chemin {authorize_path(upstream_name)} sur ce proxy)."
        )
        super().__init__(message)
        self.upstream_name = upstream_name
        self.authorization_path = authorize_path(upstream_name)


def _wrap_authorization_required(
    server: Server,
    upstreams: dict[str, Upstream],
    authorizers: dict[str, Any],
) -> None:
    """Relève UpstreamNotAuthorized en vraie erreur JSON-RPC.

    Même contrainte que pour REF_UNKNOWN, et pour la même raison : le SDK
    (@server.call_tool()) attrape TOUTE exception de l'outil appelé et la
    transforme en CallToolResult(isError=True) — un `isError` textuel, que le
    client ne peut distinguer d'un échec métier que par de la sous-chaîne.
    On remplace donc le handler déjà enregistré et on relève l'exception une
    fois hors de sa portée, où _handle_request la convertit.

    Wrapper SÉPARÉ de _wrap_ref_unknown_sentinel, et non une généralisation des
    deux : ils n'observent pas la même chose au même moment. REF_UNKNOWN
    inspecte un résultat APRÈS exécution (le sentinel n'existe qu'une fois
    l'outil appelé) ; celui-ci intercepte un refus levé AVANT tout appel. Les
    fondre imposerait un mécanisme qui fait les deux mal.
    """
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData, INVALID_REQUEST

    original_handler = server.request_handlers[types.CallToolRequest]

    async def handler(req: types.CallToolRequest):
        result = await original_handler(req)
        # L'exception a déjà été avalée par le SDK : on la reconnaît à son
        # sentinel dans le texte du résultat isError, exactement comme le fait
        # _wrap_ref_unknown_sentinel. Attraper l'exception elle-même serait plus
        # direct, mais impossible — le `except Exception` du SDK est À
        # L'INTÉRIEUR du handler qu'on enveloppe, donc aucun wrapper externe ne
        # peut la voir passer. Vérifié à l'exécution, pas déduit.
        call_result = result.root
        if not (call_result.isError and call_result.content):
            return result
        text = getattr(call_result.content[0], "text", "") or ""
        if _AUTHORIZATION_SENTINEL not in text:
            return result

        name = req.params.name
        prefix = name.split("__", 1)[0] if "__" in name else None
        raise McpError(
            ErrorData(
                code=INVALID_REQUEST,
                message=text.replace(_AUTHORIZATION_SENTINEL, "").strip(),
                # Slot applicatif : `code` au niveau de l'erreur reste l'entier
                # protocolaire. Le client teste data.code par ÉGALITÉ de
                # constante, jamais par sous-chaîne du message.
                #
                # `authorization_url` porte désormais un CHEMIN RELATIF
                # (cf. authorize_path) là où il portait une URL absolue : celle
                # d'un parcours avorté, qui menait à un callback orphelin. Le
                # nom du champ est conservé — c'est le contrat publié à MIAOU,
                # le renommer casserait davantage que le changement de forme.
                data={
                    "code": AUTHORIZATION_REQUIRED,
                    "upstream": prefix,
                    "authorization_url": (
                        authorize_path(prefix) if prefix else None
                    ),
                },
            )
        )

    server.request_handlers[types.CallToolRequest] = handler


STATUS_TOOL_NAME = "status"
"""Nom NU, sans préfixe de serveur.

MIAOU préfixe déjà par le nom de la carte serveur : `proxy__status` deviendrait
`miaou-proxy__proxy__status`. Conséquence à ne pas rater — la table de routage
résout tout par préfixe (`_resolve_via_prefix`), donc un nom sans `__` est un
cas particulier explicite dans handle_call_tool, sinon l'appel part chercher un
upstream nommé « status ».
"""


def _status_tool() -> types.Tool:
    return types.Tool(
        name=STATUS_TOOL_NAME,
        description=(
            "État des serveurs agrégés par ce proxy : type, disponibilité, "
            "nombre d'outils, et — pour un serveur exigeant une autorisation "
            "OAuth non encore accordée — le lien à ouvrir pour l'accorder."
        ),
        inputSchema={"type": "object", "properties": {}},
    )


def build_status_report(
    upstreams: dict[str, Upstream],
    authorizers: dict[str, Any] | None = None,
    catalog: Any = None,
) -> str:
    """Rapport lisible par le modèle. Pur : ni réseau, ni I/O."""
    authorizers = authorizers or {}
    lines: list[str] = []
    for name, upstream in sorted(upstreams.items()):
        kind = type(upstream).__name__.replace("Upstream", "").lower()
        authorizer = authorizers.get(name)
        if authorizer is not None and not upstream_is_live(upstream, authorizer):
            lines.append(
                f"- {name} ({kind}) : NON AUTORISÉ. Ses outils sont listés mais "
                f"refusent à l'appel avec {AUTHORIZATION_REQUIRED}."
            )
            # Ce rapport est lu par un MODÈLE : il ne peut ouvrir aucun lien, et
            # un chemin relatif ne se résout pas sans l'origine du proxy, que le
            # proxy lui-même ne connaît pas. On nomme donc qui peut agir, et on
            # cite le chemin pour que le modèle puisse le transmettre.
            lines.append(
                f"  À autoriser par l'utilisateur depuis son client MCP "
                f"(chemin {authorize_path(name)} sur ce proxy)."
            )
            failure = getattr(authorizer, "last_error", None)
            if failure:
                lines.append(f"  Dernière tentative en échec : {failure}")
                if "403" in failure:
                    # Un 403 n'est PAS une autorisation manquante : le jeton a
                    # été obtenu, le serveur le refuse pour scope insuffisant.
                    # Relancer le parcours ne répare rien — c'est la config qui
                    # est en cause, et le dire évite un cycle de reclics.
                    lines.append(
                        "  (403 : le jeton a bien été obtenu mais ses scopes sont "
                        "insuffisants. Relancer l'autorisation n'y changera rien — "
                        "vérifier 'required_scopes' côté serveur et les scopes que "
                        "son émetteur sait accorder.)"
                    )
            if catalog is not None:
                known, known_at = catalog.recall(name)
                if known:
                    when = (
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(known_at))
                        if known_at
                        else "date inconnue"
                    )
                    lines.append(
                        f"  {len(known)} outil(s) connus, liste du {when}."
                    )
            continue
        lines.append(f"- {name} ({kind}) : disponible.")
    if not lines:
        return "Aucun serveur agrégé."
    return "Serveurs agrégés par ce proxy :\n" + "\n".join(lines)


def upstream_is_live(upstream: Upstream, authorizer: Any = None) -> bool:
    """« Cet upstream honorerait-il un appel d'outil maintenant ? »

    Prédicat UNIQUE, partagé par la liste, le refus d'appel et le rapport de
    status : trois endroits qui doivent répondre la même chose, sous peine
    d'annoncer un outil qu'on refuse ensuite pour une raison qu'on ne rapporte
    pas.

    DEUX conditions, et il faut les deux — la seconde a manqué jusqu'au
    2026-09-07. « Transport ouvert » ne vaut pas « autorisé » : un upstream
    OAuth peut accepter `initialize` ET `tools/list` sans jeton, et n'exiger
    l'autorisation qu'au premier `tools/call` (cas d'un Jira d'entreprise,
    observé en production). Le juger sur la seule session le déclarait vivant,
    donc ses outils listés sans réserve, son `_meta` vide, et son premier appel
    parti pour de bon vers un 401 — au lieu d'être refusé avant émission.

    L'`authorizer` est facultatif : les appelants qui n'en ont pas (un upstream
    sans OAuth, un test) obtiennent le comportement d'avant, à l'octet près.
    """
    if authorizer is not None and getattr(authorizer, "authorization_pending", False):
        return False
    if isinstance(upstream, HttpUpstream):
        return upstream._session is not None
    return True


def build_proxy_server(
    upstreams: dict[str, Upstream],
    tool_map: dict[str, tuple[str, str]],
    authorizers: dict[str, Any] | None = None,
    catalog: Any = None,
) -> Server:
    """Construit le Server MCP avec les handlers list_tools / call_tool.

    `authorizers`/`catalog` (lot AB-2.5) : non fournis → comportement d'avant le
    lot, à l'octet près. Fournis, ils ouvrent le troisième état d'upstream
    (« connu mais pas autorisé ») et l'outil `status`.
    """
    server: Server = Server("miaou-proxy")
    authorizers = authorizers or {}

    @server.list_tools()
    async def handle_list_tools() -> types.ListToolsResult:
        # Style NOUVEAU (retour ListToolsResult) et non `list[types.Tool]` :
        # le SDK enveloppe un retour de style ancien en
        # `ListToolsResult(tools=result)`, SANS `_meta`, donc il n'existe aucun
        # moyen d'en porter un sans migrer. Le dispatch du SDK se fait sur la
        # SIGNATURE du handler (`create_call_wrapper`), pas sur son type de
        # retour : l'appelant est inchangé.
        tools: list[types.Tool] = []
        unauthorized: list[dict[str, str]] = []
        for prefix, upstream in upstreams.items():
            live = upstream_is_live(upstream, authorizers.get(prefix))
            if not live and prefix in authorizers:
                # Un upstream non vivant SANS authorizer est injoignable, pas
                # non autorisé : il n'a aucun parcours à proposer, et le
                # publier enverrait le client sur un /authorize/{name} qui
                # répond 404. Même prédicat d'appartenance que
                # build_status_report, et il passe par upstream_is_live, seul
                # juge de « cet upstream répond-il ? ».
                unauthorized.append(
                    {"name": prefix, "authorize_path": authorize_path(prefix)}
                )
            if live:
                upstream_tools = await upstream.list_tools()
                if catalog is not None:
                    catalog.remember(prefix, upstream_tools)
                stale_since = None
            elif catalog is not None:
                # Non autorisé : tools/list répondrait 401 avant de rien dire.
                # On ressert ce qu'on sait, marqué comme tel.
                upstream_tools, stale_since = catalog.recall(prefix)
            else:
                upstream_tools, stale_since = [], None

            for tool in upstream_tools:
                prefixed = f"{prefix}__{tool.name}"
                tool_map[prefixed] = (prefix, tool.name)
                description = tool.description
                if not live:
                    description = format_stale_description(description, stale_since)
                tools.append(
                    types.Tool(
                        name=prefixed,
                        description=description,
                        inputSchema=tool.inputSchema,
                    )
                )
        if authorizers:
            tools.append(_status_tool())

        # `**{"_meta": ...}` et non `meta=...` : pydantic ne sérialise sous
        # l'alias que si le champ a été peuplé PAR l'alias. La version du SDK
        # installée refuse `meta=` d'un TypeError, mais ce n'était pas le cas
        # partout, et la propriété qui compte n'est pas ce refus : c'est que la
        # clé arrive sur le fil en `_meta`. Un test le vérifie sur la CHAÎNE
        # JSON émise, pas sur l'objet Python — `result.meta` rend la même chose
        # quelle que soit la clé sérialisée, donc un test sur l'objet passerait
        # aussi bien sur une sortie invalide.
        #
        # Clé ABSENTE quand il n'y a rien à signaler, plutôt qu'un tableau
        # vide : un client lit pareil dans les deux cas, et un proxy sain n'a
        # pas à publier un `_meta` à chaque tools/list.
        if not unauthorized:
            return types.ListToolsResult(tools=tools)
        return types.ListToolsResult(
            tools=tools,
            **{"_meta": {UNAUTHORIZED_UPSTREAMS_META_KEY: unauthorized}},
        )

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[Any]:
        # Nom NU : traité AVANT la résolution par préfixe, qui partirait sinon
        # chercher un upstream appelé « status ».
        if name == STATUS_TOOL_NAME and authorizers:
            return [
                types.TextContent(
                    type="text",
                    text=build_status_report(upstreams, authorizers, catalog),
                )
            ]

        if name in tool_map:
            upstream_name, orig_name = tool_map[name]
        else:
            resolved = _resolve_via_prefix(name, upstreams)
            if resolved is None:
                from mcp.shared.exceptions import McpError
                from mcp.types import INVALID_PARAMS, ErrorData
                raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Outil inconnu : '{name}'"))
            upstream_name, orig_name = resolved

        upstream = upstreams[upstream_name]
        if not upstream_is_live(upstream, authorizers.get(upstream_name)):
            # Refus AVANT l'appel, et non conversion d'un résultat après coup :
            # c'est ce qui distingue ce contrat de REF_UNKNOWN, dont le sentinel
            # ne peut être reconnu qu'une fois l'outil exécuté. Levée ici, elle
            # serait avalée par le SDK en isError — d'où _wrap_authorization_
            # required, qui la relève en vraie erreur JSON-RPC.
            raise UpstreamNotAuthorized(upstream_name)
        try:
            return await upstream.call_tool(orig_name, arguments or {})
        except Exception as e:
            # L'AS peut ne réclamer l'autorisation qu'ICI : un upstream qui
            # accepte `initialize` et `tools/list` sans jeton n'a encore rien
            # révélé, et le refus n'a donc pas pu être posé plus tôt. Le
            # parcours OAuth du SDK client démarre alors au milieu de CET
            # appel, `_on_redirect` le trouve non interactif et lève.
            #
            # Sans cette conversion, l'exception traverse le transport sans
            # être reconnue et l'appel reste suspendu jusqu'à son timeout ;
            # le refus n'arrive qu'au tour SUIVANT, une fois l'état posé.
            # C'est ce tour perdu qu'on supprime — le premier appel doit
            # refuser aussi nettement que les suivants.
            #
            # `_unwrap_exception_group` : anyio empaquette ce qui traverse un
            # task group, la cause réelle n'est pas toujours au premier plan.
            if isinstance(_unwrap_exception_group(e), AuthorizationRequired):
                raise UpstreamNotAuthorized(upstream_name) from e
            raise

    # Deux wrappers indépendants, chacun sur son propre sentinel : ils
    # inspectent le même résultat mais ne se marchent pas dessus (un texte
    # d'erreur ne peut pas porter les deux marqueurs). L'ordre est donc
    # indifférent — ce qui n'allait PAS de soi : la première version levait
    # l'exception au lieu de la marquer, et se faisait avaler par le
    # `except Exception` que le SDK pose à l'intérieur du handler d'outil.
    _wrap_ref_unknown_sentinel(server, upstreams)
    _wrap_authorization_required(server, upstreams, authorizers)
    return server


def _wrap_ref_unknown_sentinel(server: Server, upstreams: dict[str, Upstream]) -> None:
    """Convertit le sentinel REF_UNKNOWN (texte isError) en erreur JSON-RPC.

    Le SDK MCP (@server.call_tool(), voir mcp/server/lowlevel/server.py) avale
    toute exception levée par l'outil appelé — y compris McpError — et la
    transforme en CallToolResult(isError=True). C'est incompatible avec le
    contrat client (brief A, D6) qui attend une vraie erreur JSON-RPC
    data.code == 'REF_UNKNOWN' pour déclencher le rejeu avec contenu inliné.

    Seule voie compatible SDK : remplacer le handler déjà enregistré sous
    types.CallToolRequest (server.request_handlers), inspecter son résultat, et
    lever McpError quand le sentinel est détecté — _handle_request (L777)
    convertit alors l'exception en erreur JSON-RPC (`response = err.error`).

    Portée (PRX1) : la conversion ne s'applique qu'aux outils routés vers un
    upstream inprocess dont le module expose lui-même REF_UNKNOWN_SENTINEL et
    REF_UNKNOWN_ERROR_CODE (lus au start(), cf. InProcessUpstream — le proxy ne
    connaît ni mcp_docs ni aucun sentinel en propre). Le sentinel étant cherché
    par sous-chaîne (FastMCP
    préfixe le message avant qu'il n'atteigne isError, le match ne peut pas être
    ancré en tête), sans ce scoping un message d'erreur quelconque contenant
    « REF_UNKNOWN » — autre serveur, outil citant la constante — déclencherait un
    rejeu client inutile. Les upstreams stdio sont hors périmètre : le proxy ne
    peut pas lire de constante dans un subprocess, un serveur stdio qui voudrait
    ce contrat devrait lever l'erreur JSON-RPC lui-même.
    """
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    original_handler = server.request_handlers[types.CallToolRequest]

    async def handler(req: types.CallToolRequest):
        result = await original_handler(req)
        name = req.params.name
        prefix = name.split("__", 1)[0] if "__" in name else None
        upstream = upstreams.get(prefix) if prefix is not None else None
        # Contrat résolu à l'appel, pas à la construction : build_proxy_server()
        # s'exécute avant le lifespan qui démarre les upstreams, or
        # ref_unknown_contract n'est renseigné que par start(). Un instantané pris
        # ici capturerait un dict vide et désactiverait REF_UNKNOWN en silence.
        contract = getattr(upstream, "ref_unknown_contract", None)
        # Forme validée, pas dépaquetée à l'aveugle : `ref_unknown_contract` est
        # un attribut d'upstream (déclaratif, renseigné hors de ce module) — une
        # valeur mal formée doit laisser passer le résultat tel quel, pas faire
        # planter chaque tools/call sur un ValueError d'unpacking.
        if not (isinstance(contract, tuple) and len(contract) == 2):
            return result
        sentinel, error_code = contract
        if not isinstance(sentinel, str) or not isinstance(error_code, int):
            return result
        call_result = result.root
        if call_result.isError and call_result.content:
            first = call_result.content[0]
            text = getattr(first, "text", "")
            # FastMCP préfixe le message d'exception ("Error executing tool
            # <name>: ...") avant qu'il n'atteigne isError — le sentinel n'est
            # donc pas forcément en tête du texte final, juste présent dedans.
            if sentinel in text:
                raise McpError(
                    ErrorData(
                        code=error_code,
                        message=text,
                        data={"code": "REF_UNKNOWN"},
                    )
                )
        return result

    server.request_handlers[types.CallToolRequest] = handler


# ---------------------------------------------------------------------------
# Auth entrante (OAuth 2.1 Resource Server, lot AB-1)
# ---------------------------------------------------------------------------

# Le proxy est un *Resource Server* : il VÉRIFIE des jetons, il n'en émet
# jamais. L'émission appartient à un Authorization Server distinct (cf.
# dev_auth_server.py pour celui de développement). La révision 2025-03-26 de la
# spec MCP faisait du serveur MCP son propre AS ; c'est abandonné depuis, ne pas
# le réintroduire par commodité.
#
# Auth DÉSACTIVÉE par défaut : sans clé "auth" dans la config, le proxy se
# comporte exactement comme avant ce lot. La boucle de développement locale
# (MIAOU ↔ proxy sans jeton) ne doit pas se mettre à exiger une autorisation.


class AuthConfigError(ValueError):
    """Config d'auth présente mais inexploitable (clé manquante, URL invalide)."""


def resolve_auth_config(
    cfg: dict[str, Any],
    cli_auth: bool | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> dict[str, Any] | None:
    """Normalise la clé `auth` de la config en un dict prêt à consommer, ou None
    si l'auth est désactivée.

    `cli_auth` (--auth/--no-auth) l'emporte sur la config :
      - None  : la config décide (absente → désactivé) ;
      - False : désactivé quoi qu'en dise la config ;
      - True  : activé — exige que la config porte de quoi le faire.

    `host`/`port` servent à dériver `resource_url` quand il n'est pas donné :
    c'est l'URL publique de CE serveur, et elle doit désigner l'endpoint MCP
    (`/mcp`), pas la racine — le client la renvoie en paramètre `resource`
    (RFC 8707) et c'est elle qu'on comparera à l'audience du jeton.

    Fonction PURE : aucun accès réseau, aucune lecture de fichier. Testable
    directement.
    """
    if cli_auth is False:
        return None
    raw = cfg.get("auth")
    if raw is None:
        if cli_auth is True:
            raise AuthConfigError(
                "--auth demandé mais la config ne contient pas de clé 'auth'."
            )
        return None
    if not isinstance(raw, dict):
        raise AuthConfigError("La clé 'auth' de la config doit être un objet.")
    if raw.get("_disabled") and cli_auth is not True:
        return None

    issuer_url = raw.get("issuer_url")
    if not issuer_url:
        raise AuthConfigError(
            "La clé 'auth' doit contenir 'issuer_url' (l'Authorization Server "
            "dont on accepte les jetons)."
        )

    # Défaut dérivé de l'écoute réelle. Un host d'écoute 0.0.0.0 ne peut pas
    # servir d'identité publique (ce n'est l'adresse de personne) : on retombe
    # sur 127.0.0.1, qui est ce que le client atteint en développement local.
    advertised_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    resource_url = raw.get("resource_url") or f"http://{advertised_host}:{port}/mcp"

    authorization_servers = raw.get("authorization_servers") or [issuer_url]
    if not isinstance(authorization_servers, list) or not authorization_servers:
        raise AuthConfigError(
            "'authorization_servers' doit être une liste non vide d'URLs."
        )

    required_scopes = raw.get("required_scopes") or []
    if not isinstance(required_scopes, list):
        raise AuthConfigError("'required_scopes' doit être une liste.")

    return {
        "issuer_url": issuer_url,
        "resource_url": resource_url,
        "authorization_servers": authorization_servers,
        "required_scopes": required_scopes,
        "scopes_supported": raw.get("scopes_supported"),
        "jwks_uri": raw.get("jwks_uri"),
        "algorithms": raw.get("algorithms") or ["RS256"],
    }


class JwtAudienceError(Exception):
    """Jeton refusé par le verifier. Interne : jamais propagée hors de
    verify_token(), qui renvoie None (un jeton invalide est un 401, pas un 500)."""


def _discover_jwks_uri(issuer_url: str, timeout: float = 10.0) -> str:
    """Trouve le `jwks_uri` d'un Authorization Server par sa métadonnée.

    Sonde les deux chemins bien connus, dans l'ordre où la spec les impose :
    RFC 8414 (`/.well-known/oauth-authorization-server`) puis OpenID Connect
    Discovery (`/.well-known/openid-configuration`) — un AS OIDC ne sert
    souvent que le second, et un client MCP conforme sonde les deux.

    Bloquant (urllib) : appeler via asyncio.to_thread, jamais directement dans
    un chemin async.
    """
    import urllib.error
    import urllib.request

    base = issuer_url.rstrip("/")
    candidates = (
        f"{base}/.well-known/oauth-authorization-server",
        f"{base}/.well-known/openid-configuration",
    )
    errors: list[str] = []
    for url in candidates:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                meta = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            errors.append(f"{url}: {e}")
            continue
        jwks_uri = meta.get("jwks_uri")
        if jwks_uri:
            return str(jwks_uri)
        errors.append(f"{url}: métadonnée sans 'jwks_uri'")
    raise JwtAudienceError(
        "Impossible de découvrir le jwks_uri de l'émetteur "
        f"{issuer_url} — " + " ; ".join(errors)
    )


def _audience_matches(claim: Any, expected: str) -> bool:
    """`aud` est soit une chaîne, soit une liste de chaînes (RFC 7519 §4.1.3).

    Comparaison EXACTE, jamais par préfixe ni par sous-chaîne : une audience
    `https://evil.example/mcp-attacker` ne doit pas passer pour
    `https://evil.example/mcp`. Le slash final est la seule normalisation
    tolérée — les AS ne s'accordent pas dessus.
    """
    if claim is None:
        return False
    values = [claim] if isinstance(claim, str) else list(claim)
    wanted = expected.rstrip("/")
    return any(isinstance(v, str) and v.rstrip("/") == wanted for v in values)


class JwtTokenVerifier:
    """TokenVerifier qui valide un JWT signé par l'AS déclaré, et surtout son
    AUDIENCE (RFC 8707).

    C'est le point que le SDK ne couvre pas : `BearerAuthBackend` appelle ce
    verifier puis re-vérifie seulement `expires_at`. Il ne regarde JAMAIS
    `AccessToken.resource`. Sans la vérification faite ici, un jeton parfaitement
    valide émis pour un AUTRE Resource Server serait accepté — c'est la
    confused deputy que RFC 8707 existe pour empêcher, et le défaut le plus
    fréquent des implémentations MCP.

    La clef publique vient du JWKS de l'émetteur : `jwks_uri` s'il est en config,
    sinon découvert depuis les métadonnées de l'issuer. `PyJWKClient` met le jeu
    de clefs en cache — pas un appel réseau par requête.
    """

    def __init__(
        self,
        issuer_url: str,
        resource_url: str,
        algorithms: list[str] | None = None,
        jwks_uri: str | None = None,
        required_scopes: list[str] | None = None,
    ):
        self.issuer_url = issuer_url
        self.resource_url = resource_url
        self.algorithms = algorithms or ["RS256"]
        self._configured_jwks_uri = jwks_uri
        self.required_scopes = required_scopes or []
        self._jwk_client: Any = None

    def _client(self) -> Any:
        """Construit (une fois) le PyJWKClient. Découvre le jwks_uri au premier
        appel si la config ne le donne pas : au démarrage l'AS peut ne pas être
        joignable encore, et un proxy qui refuse de démarrer parce que son AS
        dort est un mauvais compromis."""
        if self._jwk_client is None:
            from jwt import PyJWKClient

            uri = self._configured_jwks_uri or _discover_jwks_uri(self.issuer_url)
            self._jwk_client = PyJWKClient(uri, cache_keys=True)
        return self._jwk_client

    def _verify_sync(self, token: str) -> Any:
        """Tout le travail bloquant (récupération JWKS + vérification crypto),
        isolé pour asyncio.to_thread — pattern du dépôt pour l'I/O en contexte
        async."""
        import jwt

        signing_key = self._client().get_signing_key_from_jwt(token).key
        # `verify_aud=False` : pyjwt sait comparer une audience, mais son échec
        # ne se distingue pas d'un autre. On veut refuser l'audience étrangère
        # explicitement, avec un message propre — et surtout peupler
        # AccessToken.resource depuis ce qu'on a réellement vérifié.
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=self.algorithms,
            issuer=self.issuer_url,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": False,
                "require": ["exp"],
            },
        )
        if not _audience_matches(claims.get("aud"), self.resource_url):
            raise JwtAudienceError(
                f"audience {claims.get('aud')!r} ne désigne pas ce Resource "
                f"Server ({self.resource_url})"
            )
        return claims

    async def verify_token(self, token: str) -> Any:
        """Renvoie un AccessToken, ou None sur TOUT échec.

        Jamais d'exception : elle remonterait en 500 alors qu'un jeton invalide
        est un 401. Le refus est journalisé — sans trace, un jeton rejeté à
        tort est indébuggable côté client, qui ne voit qu'un 401 nu.
        """
        import asyncio

        from mcp.server.auth.provider import AccessToken

        try:
            claims = await asyncio.to_thread(self._verify_sync, token)
        except Exception as e:  # jeton invalide, JWKS injoignable, algo refusé…
            _log(f"Jeton refusé : {type(e).__name__}: {e}")
            return None

        scopes = claims.get("scope") or ""
        if isinstance(scopes, str):
            scope_list = scopes.split()
        else:
            scope_list = list(scopes)

        expires_at = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or claims.get("azp") or claims.get("sub") or ""),
            scopes=scope_list,
            expires_at=int(expires_at) if expires_at is not None else None,
            resource=self.resource_url,
            subject=claims.get("sub"),
            claims=claims,
        )


def build_token_verifier(auth: dict[str, Any]) -> Any:
    """Construit le TokenVerifier depuis la config d'auth résolue.

    Voir JwtTokenVerifier : signature, expiration et surtout AUDIENCE. La
    validation d'audience n'est pas optionnelle — c'est la raison d'être du
    mode auth, et un serveur qui l'omet accepte les jetons destinés à autrui.
    """
    return JwtTokenVerifier(
        issuer_url=auth["issuer_url"],
        resource_url=auth["resource_url"],
        algorithms=auth.get("algorithms"),
        jwks_uri=auth.get("jwks_uri"),
        required_scopes=auth.get("required_scopes"),
    )


# ---------------------------------------------------------------------------
# Auth sortante (OAuth 2.1 client d'upstreams tiers, lot AB-2)
# ---------------------------------------------------------------------------

# Symétrique de l'auth entrante ci-dessus, et sans rapport avec elle : là, le
# proxy VÉRIFIE les jetons de ses clients ; ici, il en OBTIENT auprès de
# serveurs tiers, et les détient à la place de MIAOU. Les deux cohabitent sans
# se connaître — un proxy peut faire l'une, l'autre, les deux ou aucune.
#
# Le parcours OAuth lui-même vient du SDK (OAuthClientProvider, un httpx.Auth) :
# découverte, DCR, PKCE S256, échange de code, refresh. Ce qui est à nous est le
# stockage des jetons, parce qu'il touche au disque et aux secrets.

_TOKENS_FILE_MODE = 0o600


def _default_tokens_path(config_path: str | Path) -> Path:
    """À côté de config.json, suffixé — pas dedans.

    config.json est ouvert et édité à la main ; un refresh token n'a rien à y
    faire. Fichier distinct, donc, et à ajouter au .gitignore.
    """
    cfg = Path(config_path)
    return cfg.with_name(f"{cfg.stem}-tokens.json")


def _write_secret_file(path: Path, payload: dict[str, Any]) -> None:
    """Écriture atomique, permissions restreintes posées À LA CRÉATION.

    Deux gardes, chacune pour une raison distincte :

    - `os.open(..., mode=0o600)` plutôt qu'un `chmod` après coup : entre le
      write et le chmod il existe une fenêtre pendant laquelle le refresh token
      est lisible par tout le monde. La fenêtre est courte, le fichier est
      durable.
    - Fichier temporaire dans le MÊME répertoire puis `os.replace` (atomique sur
      POSIX) : une écriture interrompue au milieu laisserait sinon un fichier de
      jetons tronqué, ce qui coûterait une ré-autorisation manuelle de tous les
      upstreams. Le même répertoire est nécessaire — `os.replace` n'est atomique
      qu'à l'intérieur d'un système de fichiers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _TOKENS_FILE_MODE)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class UpstreamTokenStorage:
    """Implémente le protocol mcp.client.auth.TokenStorage pour UN upstream.

    Un seul fichier pour tous les upstreams, une entrée par nom : le fichier est
    relu à chaque écriture pour ne pas écraser l'entrée d'un voisin.

    `client_info_override` porte les credentials pré-provisionnés de la config.
    Les rendre depuis get_client_info() suffit à court-circuiter la DCR côté SDK
    (`if not self.context.client_info:`), sans branche à ajouter nulle part :
    c'est le chemin prévu pour un AS qui n'enregistre pas dynamiquement.
    """

    def __init__(
        self,
        path: str | Path,
        upstream_name: str,
        client_info_override: Any = None,
    ) -> None:
        self._path = Path(path)
        self._name = upstream_name
        self._client_info_override = client_info_override

    # -- fichier ------------------------------------------------------------

    def _read_all(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            # Un fichier de jetons illisible ne doit pas empêcher le proxy de
            # démarrer : on repart d'une ardoise vide, ce qui coûte une
            # ré-autorisation — pas un crash au boot.
            _log(f"Fichier de jetons illisible ({e}) — ignoré.")
            return {}
        return data if isinstance(data, dict) else {}

    def _read_entry(self) -> dict[str, Any]:
        entry = self._read_all().get(self._name)
        return entry if isinstance(entry, dict) else {}

    def _update_entry(self, **fields: Any) -> None:
        data = self._read_all()
        entry = data.get(self._name)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry.update(fields)
        data[self._name] = entry
        _write_secret_file(self._path, data)

    # -- protocol TokenStorage ----------------------------------------------

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken

        entry = self._read_entry()
        raw = entry.get("tokens")
        if not raw:
            return None
        try:
            token = OAuthToken.model_validate(raw)
        except Exception as e:
            _log(f"Jetons stockés pour '{self._name}' illisibles ({e}) — ignorés.")
            return None

        # `expires_in` est une DURÉE, relative à l'instant d'émission : la
        # relire telle quelle après un redémarrage la ferait courir à nouveau
        # depuis maintenant. On persiste donc l'instant absolu d'expiration et
        # on recalcule la durée restante au chargement.
        #
        # Ce recalcul n'est pas un raffinement : le SDK charge les jetons dans
        # _initialize() SANS repasser par update_token_expiry(), donc
        # token_expiry_time reste None et is_token_valid() rend True pour un
        # jeton expiré depuis des heures. Le proxy l'enverrait, prendrait un
        # 401, et repartirait dans un parcours interactif au lieu de rafraîchir.
        expires_at = entry.get("expires_at")
        if expires_at is not None:
            remaining = int(expires_at - time.time())
            token.expires_in = max(remaining, 0)
        return token

    async def has_usable_token(self) -> bool:
        """« Un appel partirait-il authentifié, là, maintenant ? »

        PUREMENT LOCAL : lit le fichier de jetons, n'émet aucune requête. C'est
        ce qui permet de savoir qu'une autorisation manque AVANT le premier
        appel d'outil, sans sonder l'upstream ni retarder le démarrage.

        Un jeton expiré mais porteur d'un `refresh_token` compte comme
        utilisable : le SDK le rafraîchira tout seul au premier appel, sans
        parcours interactif. Le dire « à autoriser » enverrait l'utilisateur
        cliquer pour un problème qui se règle sans lui.
        """
        token = await self.get_tokens()
        if token is None or not token.access_token:
            return False
        if token.expires_in is not None and token.expires_in <= 0:
            return bool(token.refresh_token)
        return True

    def observed_lifetime(self) -> float | None:
        """Durée de vie à l'émission du dernier jeton écrit, ou None.

        Sert à calibrer la marge de renouvellement sur ce que l'AS émet
        réellement, plutôt que sur une constante qui suppose des jetons d'une
        heure. Lecture de fichier, aucun réseau.
        """
        value = self._read_entry().get("lifetime")
        return float(value) if isinstance(value, (int, float)) else None

    async def set_tokens(self, tokens: Any) -> None:
        payload = tokens.model_dump(exclude_none=True, mode="json")
        fields: dict[str, Any] = {"tokens": payload}
        if tokens.expires_in is not None:
            fields["expires_at"] = time.time() + tokens.expires_in
            # DURÉE DE VIE À L'ÉMISSION, mémorisée parce que c'est le seul
            # endroit où on la voit : relu plus tard, `expires_in` est ce qu'il
            # RESTE. C'est elle qui calibre la marge de renouvellement, laquelle
            # ne peut pas être une constante — un AS qui émet des jetons de 5
            # minutes rendrait « bientôt expiré » tout jeton dès son émission.
            #
            # MAIS ce qui arrive ici n'est pas toujours un jeton frais. Le SDK
            # garde en mémoire l'objet rendu par `get_tokens()`, dont on a
            # justement écrasé `expires_in` par le RESTANT, et il lui arrive de
            # le réécrire tel quel. Prendre cette valeur pour une durée de vie
            # donnait `lifetime: 0` sur un jeton relu périmé, donc un
            # `expires_at` dans le passé : le jeton se retrouvait marqué expiré
            # à l'instant même où il venait d'être renouvelé, et la trace
            # annonçait « valide 0s ». Mesuré le 2026-09-08.
            #
            # Une durée de vie ne RÉTRÉCIT jamais : on ne retient donc que la
            # plus longue vue, ce qui ignore les réécritures dégradées sans
            # avoir à deviner d'où vient l'objet.
            known = self.observed_lifetime() or 0
            fields["lifetime"] = max(known, tokens.expires_in)

            # Même cause, autre dégât : réécrit depuis un objet relu, cet
            # `expires_at` RECULE l'échéance au lieu de la porter. On ne la
            # laisse jamais reculer pour un access token inchangé — le seul
            # cas où une échéance doit se rapprocher est une révocation, qui
            # ne passe pas par ici. Un jeton VRAIMENT différent repart, lui,
            # de l'échéance qu'il annonce.
            previous = self._read_entry()
            same_token = (
                (previous.get("tokens") or {}).get("access_token")
                == tokens.access_token
            )
            if same_token and previous.get("expires_at") is not None:
                fields["expires_at"] = max(
                    float(previous["expires_at"]), fields["expires_at"]
                )
        else:
            fields["expires_at"] = None
        self._update_entry(**fields)

        # PASSAGE OBLIGÉ de tout jeton obtenu — échange initial comme
        # rafraîchissement. Le tracer ici, et pas dans le parcours, est ce qui
        # rend le cycle de vie observable sans rejouer une autorisation.
        #
        # L'absence de `refresh_token` est dite à voix haute : sans lui, le
        # jeton expirera sans que rien ne puisse le renouveler, et l'utilisateur
        # se reverra proposer « Autoriser » sans explication. Le savoir à
        # l'obtention plutôt qu'à l'expiration, c'est la différence entre un
        # réglage à corriger côté AS (grant `refresh_token`, scope
        # `offline_access`) et une panne subie des heures plus tard.
        if not _auth_debug_enabled():
            return
        if tokens.refresh_token:
            horizon = (f"expire dans {tokens.expires_in}s"
                       if tokens.expires_in is not None
                       else "sans expiration annoncée")
            _log(f"  [auth] {self._name} jeton enregistré ({horizon}), "
                 f"refresh_token présent — renouvellement automatique possible.")
        else:
            _log(f"  [auth] {self._name} jeton enregistré SANS refresh_token : "
                 f"il ne pourra pas être renouvelé, et l'autorisation devra "
                 f"être refaite à la main à son expiration. Vérifier le grant "
                 f"'refresh_token' du client et le scope 'offline_access'.")

    async def get_client_info(self) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull

        if self._client_info_override is not None:
            return self._client_info_override
        raw = self._read_entry().get("client_info")
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception as e:
            _log(f"Client info stockée pour '{self._name}' illisible ({e}) — ignorée.")
            return None

    async def set_client_info(self, client_info: Any) -> None:
        # Enregistrement dynamique mémorisé même quand la config fournit des
        # credentials : si l'override disparaît de la config, on retombe sur
        # l'enregistrement plutôt que d'en refaire un.
        self._update_entry(
            client_info=client_info.model_dump(exclude_none=True, mode="json")
        )


def build_client_info_override(auth: dict[str, Any] | None) -> Any:
    """Credentials pré-provisionnés → OAuthClientInformationFull, ou None.

    Pour un AS qui ne fait pas d'enregistrement dynamique (GitHub, notamment),
    où client_id/client_secret sont créés à la main.
    """
    if not auth:
        return None
    client_id = auth.get("client_id")
    if not client_id:
        return None

    from mcp.shared.auth import OAuthClientInformationFull

    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=auth.get("client_secret"),
        redirect_uris=[auth["redirect_uri"]] if auth.get("redirect_uri") else None,
        scope=auth.get("scope"),
        token_endpoint_auth_method=auth.get(
            "token_endpoint_auth_method",
            "client_secret_post" if auth.get("client_secret") else "none",
        ),
    )


def build_oauth_metadata_override(auth: dict[str, Any] | None) -> Any:
    """Endpoints de l'AS déclarés en config → OAuthMetadata, ou None.

    POURQUOI CE N'EST PAS UN CONFORT. Le SDK ne connaît le token endpoint que
    par `context.oauth_metadata`, peuplé UNIQUEMENT par la découverte
    `/.well-known/...` qui a lieu dans la branche 401 d'`async_auth_flow`. Deux
    conséquences, toutes deux mesurées le 2026-09-07 sur un WSO2 :

    - cette découverte ÉCHOUE quand l'AS ne sert pas ses métadonnées aux trois
      chemins que le SDK essaie (ici : realms/wso2, où seuls les endpoints
      `protocol/openid-connect/*` existent) ;
    - même réussie, elle n'est JAMAIS persistée : au redémarrage du proxy,
      `_initialize()` recharge les jetons et le client_info depuis le fichier,
      mais `oauth_metadata` repart à None.

    Or `_refresh_token()` du SDK se replie alors sur
    `urljoin(base_url_du_SERVEUR_MCP, "/token")` — l'hôte du Jira, pas celui de
    l'AS. Le POST de rafraîchissement part vers une URL inexistante, échoue, et
    `_handle_refresh_response` appelle `clear_tokens()` : le refresh token est
    jeté, un parcours interactif s'ouvre, et l'utilisateur reclique. Le tout
    sans un mot, un `logger.warning` en DEBUG étant la seule trace.

    Déclarer les endpoints rend donc le rafraîchissement possible là où la
    découverte ne peut pas aboutir. `issuer` n'a pas d'usage propre ici — le
    SDK ne le vérifie pas — mais le modèle l'exige : on le dérive de
    l'authorization endpoint quand il n'est pas donné.
    """
    if not auth:
        return None
    token_endpoint = auth.get("token_endpoint")
    authorization_endpoint = auth.get("authorization_endpoint")
    if not token_endpoint or not authorization_endpoint:
        # Les deux ou rien : un token endpoint seul laisserait le parcours
        # initial rediriger vers une URL découverte, donc potentiellement vers
        # un autre AS que celui qui rafraîchit. Deux AS pour une même identité
        # est un mode de panne bien pire que l'absence de configuration.
        return None

    from mcp.shared.auth import OAuthMetadata

    issuer = auth.get("issuer")
    if not issuer:
        from urllib.parse import urlparse

        parsed = urlparse(authorization_endpoint)
        issuer = f"{parsed.scheme}://{parsed.netloc}"

    return OAuthMetadata(
        issuer=issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        registration_endpoint=auth.get("registration_endpoint"),
        scopes_supported=auth["scope"].split() if auth.get("scope") else None,
    )


class PendingAuthorization:
    """Rendez-vous entre le navigateur (route /callback) et le parcours OAuth.

    Le SDK attend un `callback_handler` qui BLOQUE puis rend `(code, state)`.
    L'attente est bornée : sans borne, un upstream jamais autorisé retiendrait
    indéfiniment la tâche qui l'attend. Le `timeout` d'OAuthContext ne couvre
    pas ce handler — il est passé au client httpx, pas à notre attente — donc
    la borne est ici, explicitement.

    Le `state` n'est pas vérifié ici : le SDK le compare lui-même en
    `secrets.compare_digest`. En rajouter une deuxième vérification donnerait
    deux prédicats qui peuvent diverger.
    """

    def __init__(self, upstream_name: str, timeout: float) -> None:
        import anyio

        self.upstream_name = upstream_name
        self.timeout = timeout
        self.authorization_url: str | None = None
        # `state` attendu, extrait de l'URL d'autorisation : sert uniquement à
        # router le callback vers le bon upstream (cf. build_callback_route).
        self.state: str | None = None
        self._event = anyio.Event()
        self._code: str | None = None
        self._state: str | None = None
        self._error: str | None = None

    def resolve(self, code: str | None, state: str | None, error: str | None = None) -> None:
        """Appelée depuis la route /callback. Idempotente : un rechargement de
        l'onglet ne doit pas écraser un résultat déjà reçu."""
        if self._event.is_set():
            return
        self._code, self._state, self._error = code, state, error
        self._event.set()

    async def wait(self) -> tuple[str, str | None]:
        import anyio

        with anyio.move_on_after(self.timeout) as scope:
            await self._event.wait()
        if scope.cancelled_caught:
            raise TimeoutError(
                f"Aucune autorisation reçue pour '{self.upstream_name}' sous "
                f"{self.timeout:.0f}s. Relancer l'autorisation quand tu es prêt."
            )
        if self._error:
            raise RuntimeError(
                f"Autorisation refusée pour '{self.upstream_name}' : {self._error}"
            )
        if not self._code:
            raise RuntimeError(
                f"Callback sans code d'autorisation pour '{self.upstream_name}'."
            )
        return self._code, self._state


# Bornes du parcours interactif. L'attente d'un humain qui clique n'est pas du
# réseau : cinq minutes est un ordre de grandeur d'attention, pas un timeout
# technique.
# Paramètres de requête qui ne doivent JAMAIS atteindre un log : le mode debug
# sert à diagnostiquer un parcours, pas à exposer ce qu'il transporte. Masqué
# par NOM plutôt que par forme — une valeur opaque ne se reconnaît pas.
_SENSITIVE_QUERY_KEYS = (
    "code", "access_token", "refresh_token", "id_token", "client_secret",
    "code_verifier", "assertion", "password",
)


def _redact_url(url: Any) -> str:
    """URL lisible, valeurs sensibles remplacées.

    `state` et `code_challenge` sont CONSERVÉS : ils ne donnent aucun accès et
    ce sont eux qu'on lit pour comprendre un parcours (corrélation d'un
    callback, présence de PKCE).
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parts = urlsplit(str(url))
    except Exception:  # pragma: no cover - une URL illisible se log telle quelle
        return str(url)
    if not parts.query:
        return str(url)
    pairs = [
        (k, "***" if k.lower() in _SENSITIVE_QUERY_KEYS else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    # `safe="*"` : sans lui le masque ressort en %2A%2A%2A, illisible pour qui
    # lit un log en diagonale — et un log qu'on ne lit pas ne sert à rien.
    return urlunsplit(parts._replace(query=urlencode(pairs, safe="*")))


_AUTH_DEBUG = False


def _auth_debug_enabled() -> bool:
    return _AUTH_DEBUG


def enable_auth_debug() -> None:
    """Rend le parcours OAuth sortant observable (option `--debug-auth`).

    Un parcours qui n'aboutit pas est SILENCIEUX par construction : le SDK
    avale ses propres erreurs de découverte, et le proxy ne voit qu'une absence
    de jeton. Impossible alors de distinguer « l'upstream n'a rien demandé » de
    « l'AS a refusé l'enregistrement » ou de « la découverte a échoué » — trois
    causes, un seul symptôme, et le diagnostic se fait au jugé. Ce mode expose
    les requêtes réellement émises, seule information qui les sépare.

    Branché sur les loggers du SDK MCP et de httpx plutôt que sur un traçage
    maison : ce sont eux qui voient les requêtes, y compris celles que le proxy
    n'émet pas lui-même (découverte, enregistrement, échange de jeton).
    """
    import logging

    class _RedactingFilter(logging.Filter):
        """Masque les valeurs sensibles des URL présentes dans un message."""

        def filter(self, record: logging.LogRecord) -> bool:
            try:
                message = record.getMessage()
            except Exception:  # pragma: no cover
                return True
            if "?" in message and any(
                k in message for k in _SENSITIVE_QUERY_KEYS
            ):
                import re

                record.msg = re.sub(
                    r"(https?://\S+)",
                    lambda m: _redact_url(m.group(1)),
                    message,
                )
                record.args = ()
            return True

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("DEBUG:    %(name)s %(message)s"))
    handler.addFilter(_RedactingFilter())

    # `httpcore` est volontairement ABSENT : ses lignes ne portent ni URL ni
    # en-tête (`send_request_headers.started request=<Request [b'POST']>`), donc
    # elles noient le journal sans rien apprendre — mesuré, pas supposé. Seul
    # `mcp.client.auth` dit quelque chose d'exploitable ; le reste du diagnostic
    # vient des traces explicites du proxy, qui, elles, nomment ce qu'elles
    # observent.
    for name in ("mcp.client.auth", "httpx"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False

    global _AUTH_DEBUG
    _AUTH_DEBUG = True

    _log("Mode debug du parcours OAuth actif (--debug-auth).")
    _log("  Les jetons et codes d'autorisation sont masqués dans ces traces.")


_MCP_PROTOCOL_VERSION = "2025-06-18"
"""Version annoncée par la requête d'amorçage d'`UpstreamAuthorizer`.

Ne concerne QUE cette poignée de requêtes : les sessions réelles sont ouvertes
par le SDK, qui négocie sa propre version. Un serveur qui n'accepterait pas
celle-ci répondrait une erreur de protocole — laquelle n'empêche pas le 401 de
`tools/call`, seul événement recherché ici.
"""

_AUTH_PROBE_TOOL = "__miaou_authorization_probe__"
"""Nom d'outil de repli, quand `tools/list` n'apprend rien.

**Ce n'est PAS le choix par défaut, et l'histoire vaut d'être connue.** Il l'a
été, sur la foi d'une mesure mal lue : un 401 obtenu sur `tools/call` semblait
prouver que le refus d'autorisation précédait la résolution du nom. Il ne le
prouvait pas — cette mesure portait sur un outil qui EXISTE. Reprise avec ce
nom-ci, elle rend `403 No matching resource found in the API` : la passerelle
(WSO2) route par ressource et rejette un nom inconnu **avant** toute question
d'autorisation. Un nom inventé ne peut donc jamais amorcer le parcours là-bas.

On préfère un outil réel, choisi en lecture seule (cf. `_pick_probe_tool`). Ce
repli ne sert que si aucun n'est trouvable, où il vaut mieux qu'une requête non
émise : sur un serveur qui, lui, refuse avant de résoudre, il fonctionne.
"""

# Verbes trahissant un outil à EFFET DE BORD. La sonde d'autorisation en appelle
# un pour se faire refuser : ce doit être une LECTURE. Un faux positif ne coûte
# qu'un candidat écarté ; un faux négatif crée un ticket ou envoie un message.
#
# Liste dupliquée dans `tests/live_auth_probe.py`, qui est un script autonome
# (bloc PEP 723) : l'importer d'ici y tirerait tout le proxy. Même arbitrage que
# le helper TLS de `live_call.py` — toute évolution est à répercuter.
_MUTATING_HINTS = (
    "add", "append", "archive", "assign", "cancel", "clear", "close", "comment",
    "create", "delete", "destroy", "edit", "insert", "move", "patch", "post",
    "publish", "purge", "push", "put", "remove", "rename", "replace", "reset",
    "restore", "run", "send", "set", "start", "stop", "submit", "transition",
    "trigger", "update", "upload", "write",
)


def _looks_mutating(name: str) -> bool:
    """Le nom d'outil évoque-t-il une écriture ?

    Découpage sur les séparateurs usuels (`__`, `_`, `-`, `.`) plus les
    frontières de casse, pour attraper `createIssue` comme `create_issue` ou
    `jira__add-comment`. On compare des SEGMENTS, jamais des sous-chaînes :
    `update` doit écarter `update_issue` sans écarter `list_updates`.
    """
    import re

    segments = re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", name)
    return any(seg.lower() in _MUTATING_HINTS for seg in segments if seg)


def _pick_probe_tool(listed_body: str) -> str | None:
    """Un outil RÉEL et en lecture seule dans une réponse `tools/list`.

    Réel parce qu'une passerelle qui route par ressource rejette un nom inconnu
    avant d'évaluer l'autorisation ; en lecture seule parce qu'obtenir un jeton
    ne doit pas exécuter une action que personne n'a demandée. Aucun candidat
    sûr → `None`, et l'appelant garde son repli : mieux vaut une sonde qui
    échoue qu'une sonde qui écrit.

    Le corps est lu en texte plutôt que parsé : `tools/list` arrive en SSE
    (`event: message\ndata: {...}`) sur ce transport, et on ne cherche qu'une
    liste de noms.
    """
    import re

    if not listed_body:
        return None
    for name in re.findall(r'"name"\s*:\s*"([^"]+)"', listed_body):
        if not _looks_mutating(name):
            return name
    return None

_AUTHORIZATION_WAIT_S = 300.0

_AUTHORIZE_ROUTE_WAIT_S = 20.0

# Rafraîchissement PROACTIF : intervalle de réveil, et marge avant expiration
# en deçà de laquelle on renouvelle sans attendre un appel d'outil.
#
# Le refresh du SDK est PASSIF — il n'a lieu qu'au passage d'une requête. Sur un
# upstream peu sollicité, l'access token peut donc expirer, puis le refresh
# token lui-même, sans qu'aucune requête ne les ait jamais renouvelés : au
# prochain appel, des semaines plus tard, l'utilisateur se retrouve devant un
# parcours d'autorisation complet. Ce que la boucle achète est exactement ça —
# elle ne remplace pas le refresh passif, elle couvre l'INACTIVITÉ.
#
# INVARIANT : la marge doit rester large devant l'intervalle, pour que
# plusieurs réveils tombent dans la fenêtre « bientôt expiré » avant
# l'échéance — sinon un réveil manqué (proxy suspendu, machine en veille)
# laisse passer l'expiration.
#
# Deux constantes fixes ne peuvent pas tenir cet invariant face à un AS
# quelconque. La marge est plafonnée à la moitié de la durée de vie réellement
# émise (`_refresh_margin`), donc sur des jetons de 5 minutes — WSO2, mesuré le
# 2026-09-08 — elle tombe à 150 s, sous un intervalle de 300 s : la fenêtre
# serait sautée en entier. L'intervalle se dérive donc de la marge la plus
# courte en vigueur (`_refresh_poll_interval`), et ces valeurs ne sont que des
# plafonds, pour un AS qui émet des jetons de longue durée.
_REFRESH_POLL_INTERVAL_S = 300.0
_REFRESH_MARGIN_S = 900.0


def _refresh_poll_interval(authorizers: dict[str, Any] | None) -> float:
    """Période de réveil : assez courte pour qu'aucune fenêtre ne soit sautée.

    Un tiers de la marge la plus courte parmi les upstreams, pour que trois
    réveils au moins tombent dans la fenêtre de chacun. Plafonné à
    `_REFRESH_POLL_INTERVAL_S` — un AS généreux n'a pas à être sondé plus
    souvent — et borné en bas pour ne pas tourner en boucle serrée sur un AS
    aux jetons très courts.
    """
    margins = []
    for authorizer in (authorizers or {}).values():
        storage = getattr(authorizer, "_storage", None)
        observed = getattr(storage, "observed_lifetime", None)
        lifetime = observed() if callable(observed) else None
        if lifetime:
            margins.append(min(_REFRESH_MARGIN_S, lifetime / 2))
    if not margins:
        return _REFRESH_POLL_INTERVAL_S
    return max(30.0, min(_REFRESH_POLL_INTERVAL_S, min(margins) / 3))
"""Ce que /authorize/{name} attend avant de rendre la main au navigateur.

Borne la DÉCOUVERTE de l'AS (métadonnées, éventuel enregistrement de client),
pas le parcours entier — celui-ci se poursuit en tâche de fond et l'utilisateur
y entre par la redirection. Généreux à dessein : derrière un portail
d'entreprise, la découverte prend des secondes, et conclure trop tôt affiche
une panne là où il n'y en a pas. Le coût d'une borne trop large est une page
qui tarde ; celui d'une borne trop courte est un diagnostic faux.
"""


def format_authorization_notice(upstream_name: str, url: str) -> list[str]:
    """Le lien, en évidence, sur stderr.

    Le lien copiable est le mécanisme de référence, pas un repli : mcp_proxy est
    une CLI, et confier l'ouverture à l'OS ne garantit ni le bon navigateur ni
    le bon profil (celui où la session tierce est ouverte). --open reste un
    confort explicitement demandé.
    """
    return [
        "",
        f"  Autorisation requise pour l'upstream '{upstream_name}'.",
        "  Ouvrir ce lien dans le navigateur où tu es connecté :",
        "",
        f"    {url}",
        "",
    ]


class AuthorizationRequired(Exception):
    """Un upstream a besoin d'une autorisation qu'on ne peut pas demander ici.

    Levée quand le parcours interactif est INHIBÉ — au démarrage, notamment.
    Distincte d'une panne : un module introuvable ou un subprocess mort ne se
    répare pas, celui-ci se répare par un clic.
    """

    def __init__(self, upstream_name: str) -> None:
        super().__init__(
            f"L'upstream '{upstream_name}' exige une autorisation OAuth."
        )
        self.upstream_name = upstream_name


class UpstreamAuthorizer:
    """Porte l'état d'autorisation d'UN upstream, et fabrique son httpx.Auth.

    Un seul OAuthClientProvider par upstream, construit une fois et réutilisé :
    c'est ce qui rend effectif le verrou d'OAuthContext (un anyio.Lock pris pour
    tout async_auth_flow), donc l'écrivain unique du refresh. En construire un
    par requête rendrait le verrou inopérant et ferait voir un rejeu à un AS à
    rotation, qui révoquerait toute la famille de jetons.
    """

    def __init__(
        self,
        name: str,
        server_url: str,
        storage: Any,
        callback_url: str,
        scope: str | None = None,
        open_browser: bool = False,
        wait_timeout: float = _AUTHORIZATION_WAIT_S,
        oauth_metadata: Any = None,
    ) -> None:
        self.name = name
        self.server_url = server_url
        self.callback_url = callback_url
        self.scope = scope
        # Endpoints déclarés en config, s'il y en a. Posés sur le contexte du
        # provider à sa construction : le SDK ne les découvre qu'en branche
        # 401 et ne les persiste jamais, donc sans ça le refresh d'un proxy
        # redémarré vise `<hôte-du-serveur-MCP>/token`. Cf.
        # build_oauth_metadata_override.
        self.oauth_metadata = oauth_metadata
        self.open_browser = open_browser
        self.wait_timeout = wait_timeout
        self._storage = storage
        self._provider: Any = None
        self.pending: PendingAuthorization | None = None
        # Dernière URL d'autorisation connue, pour la rendre à qui la demande
        # sans relancer un parcours.
        self.last_authorization_url: str | None = None
        # Une autorisation a été RÉCLAMÉE par l'AS et pas encore accordée.
        #
        # Distinct de « pas de session » : un upstream peut très bien accepter
        # `initialize` ET `tools/list` sans jeton, et n'exiger l'autorisation
        # qu'au premier `tools/call` — c'est le cas d'un Jira derrière un
        # portail d'entreprise, observé en production. `_session` est alors POSÉE et
        # `upstream_is_live` rend True, alors que l'upstream refusera tout
        # appel. Sans ce drapeau, les trois surfaces (listing `_meta`, refus
        # d'appel, rapport `status`) concluent toutes « il va bien ».
        #
        # Il est posé par `_on_redirect` quand le parcours est inhibé, c'est-à-
        # dire au seul moment où l'on apprend de l'AS lui-même qu'un jeton
        # manque, et levé par un parcours mené à son terme.
        self.authorization_pending = False
        # Dernier échec de parcours, pour que `status` dise POURQUOI. Sans lui,
        # un scope insuffisant se présente comme une autorisation manquante, et
        # l'exploitant reclique indéfiniment sur un lien qui ne peut pas
        # réparer sa configuration.
        self.last_error: str | None = None
        # Signalé dès qu'un parcours interactif a produit son URL de
        # redirection, OU qu'il s'est terminé sans en produire. C'est ce que
        # /authorize/{name} attend pour répondre : une attente sur ÉVÉNEMENT,
        # jamais un délai fixe (cf. la route). None hors parcours — la route
        # l'arme elle-même avant de lancer sa tâche.
        self.redirect_ready: Any = None
        # Le parcours interactif est INHIBÉ par défaut, et c'est le point
        # important : le start() d'un upstream tourne dans le lifespan, AVANT
        # qu'uvicorn n'ouvre le port. Y attendre un clic sur /callback serait un
        # interblocage — le proxy attendrait une redirection vers une route
        # qu'il ne sert pas encore. On refuse donc au démarrage, et le parcours
        # ne s'ouvre que sur demande explicite, port déjà ouvert.
        self.interactive = False

    async def _on_redirect(self, url: str) -> None:
        from urllib.parse import parse_qs, urlparse

        if _auth_debug_enabled():
            # CE QUE L'AS REÇOIT, et non ce qu'on croit lui envoyer. Un refus de
            # `redirect_uri` se règle en comparant CETTE valeur, caractère par
            # caractère, à celle enregistrée dans le client OAuth — schéma,
            # hôte, port et chemin compris. La lire ailleurs (config, log de
            # démarrage) ne prouve rien : seule celle-ci part réellement.
            query = parse_qs(urlparse(url).query)
            _log(f"  [auth] {self.name} redirection vers l'AS :")
            _log(f"  [auth]   endpoint     : {urlparse(url)._replace(query='').geturl()}")
            for key in ("redirect_uri", "client_id", "scope", "response_type"):
                value = (query.get(key) or [None])[0]
                if value is not None:
                    _log(f"  [auth]   {key:<12} : {value}")
            _log(f"  [auth]   Comparer redirect_uri à celle enregistrée dans "
                 f"le client OAuth de l'AS — l'égalité doit être EXACTE.")

        if not self.interactive:
            # Mémorisé pour que `status` puisse le rendre, mais on ne bloque
            # pas : le parcours s'arrête ici et l'upstream reste « connu mais
            # non autorisé ».
            self.last_authorization_url = url
            # C'est ICI, et nulle part ailleurs, qu'on apprend de l'AS qu'un
            # jeton manque. Le noter durablement est ce qui permet aux surfaces
            # de le dire sans avoir à re-provoquer l'échec.
            self.authorization_pending = True
            if self.redirect_ready is not None:
                self.redirect_ready.set()
            raise AuthorizationRequired(self.name)

        pending = PendingAuthorization(self.name, self.wait_timeout)
        pending.authorization_url = url
        pending.state = (parse_qs(urlparse(url).query).get("state") or [None])[0]
        self.pending = pending
        # Signalé ICI, pas au retour d'`authorize()` : celle-ci bloque juste
        # après, en attente du retour du navigateur sur /callback. Attendre sa
        # fin ferait tenir la route jusqu'à sa borne alors que l'URL vers
        # laquelle rediriger est déjà connue.
        if self.redirect_ready is not None:
            self.redirect_ready.set()
        for line in format_authorization_notice(self.name, url):
            print(line, file=sys.stderr, flush=True)
        if self.open_browser:
            import webbrowser

            # Confort seulement : un échec d'ouverture ne doit pas casser le
            # parcours, le lien reste affiché.
            try:
                webbrowser.open(url)
            except Exception as e:  # pragma: no cover
                _log(f"Ouverture du navigateur impossible ({e}) — utiliser le lien.")

    async def _on_callback(self) -> tuple[str, str | None]:
        if self.pending is None:  # pragma: no cover
            raise RuntimeError("Callback attendu sans autorisation en cours.")
        try:
            return await self.pending.wait()
        finally:
            self.pending = None

    async def _provoke_refusal(self, client: Any) -> None:
        """Émet la séquence MCP jusqu'à obtenir le refus qui amorce l'OAuth.

        `initialize` NE SUFFIT PAS, et c'est tout l'objet de cette méthode : sur
        un déploiement d'entreprise (passerelle devant un Jira, mesuré le
        2026-09-07), `initialize` répond 200 et seul `tools/call` renvoie le 401
        porteur du `WWW-Authenticate`. Une requête d'amorçage arbitraire — un
        `ping`, la version précédente — n'était donc jamais refusée, et aucun
        parcours ne démarrait.

        Or `tools/call` ne s'envoie pas nu : le transport streamable-http exige
        un `Mcp-Session-Id` obtenu à `initialize` et rejoué ensuite. On déroule
        donc la vraie séquence. Les réponses ne sont pas lues — httpx exécute le
        flow d'authentification AVANT de nous rendre la main, et c'est ce
        passage, pas le résultat, qui nous intéresse.

        Le nom d'outil est sans importance : le refus d'autorisation précède la
        résolution du nom. En inventer un est même préférable — appeler un outil
        réel pour obtenir un jeton exécuterait une action non demandée, et rien
        ne garantit qu'elle soit sans effet de bord.
        """
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

        def _trace(step: str, response: Any) -> None:
            """Ce que l'étape a VRAIMENT envoyé et obtenu.

            Les en-têtes de la REQUÊTE en font partie, et c'est le point : les
            loggers de `httpcore` ne les montrent pas (`send_request_headers.
            started request=<Request [b'POST']>`, mesuré), donc ils sont
            invisibles sans ça. Or la seule différence possible entre une
            requête du proxy et celle d'un banc externe qui obtient, lui, un
            résultat différent, se trouve là — au premier rang, l'en-tête
            `Authorization` que le provider OAuth ajoute quand il croit détenir
            un jeton, et qui fait répondre 403 là où l'absence de jeton donne
            401.

            Les valeurs ne sont JAMAIS journalisées : seuls les noms d'en-tête,
            plus la forme du jeton (son schéma et sa longueur) quand il y en a
            un. C'est assez pour conclure, et ça ne fuite rien.
            """
            if not _auth_debug_enabled():
                return
            sent = response.request.headers
            names = ", ".join(sorted(k.lower() for k in sent.keys()))
            _log(f"  [auth] {self.name} {step} -> HTTP {response.status_code}")
            _log(f"  [auth]   URL demandée : {_redact_url(response.request.url)}")
            _log(f"  [auth]   en-têtes envoyés : {names}")
            authorization = sent.get("authorization")
            if authorization:
                scheme, _, value = authorization.partition(" ")
                _log(f"  [auth]   ATTENTION : Authorization envoyé "
                     f"({scheme}, {len(value)} caractères) — un jeton refusé "
                     f"donne 403 là où l'absence de jeton donne 401.")
            challenge = response.headers.get("www-authenticate")
            if challenge:
                _log(f"  [auth]   www-authenticate : {challenge}")
            body = (response.text or "").strip()
            if response.status_code >= 400 and body:
                _log(f"  [auth]   corps : {body[:200]}")

        response = await client.post(
            self.server_url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "miaou-proxy", "version": "1"},
                },
            },
        )
        _trace("initialize", response)

        session_id = response.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
            # Le serveur attend la notification avant de servir la suite.
            await client.post(
                self.server_url,
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
        elif _auth_debug_enabled():
            _log(f"  [auth] {self.name} initialize n'a renvoyé aucun "
                 f"Mcp-Session-Id — tools/call partira sans session.")

        # `tools/list` d'abord : il faut le NOM d'un outil réel. Une passerelle
        # qui route par ressource (WSO2, mesuré) rejette un nom inconnu avant
        # d'évaluer l'autorisation, donc un nom inventé n'obtient jamais le 401
        # qu'on cherche — il obtient un 403 de routage.
        listed = await client.post(
            self.server_url,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        _trace("tools/list", listed)

        probe_tool = _pick_probe_tool(listed.text or "") or _AUTH_PROBE_TOOL
        if _auth_debug_enabled():
            origin = ("lu dans tools/list" if probe_tool != _AUTH_PROBE_TOOL
                      else "REPLI — aucun outil de lecture trouvé")
            _log(f"  [auth]   outil de sonde : {probe_tool} ({origin})")

        response = await client.post(
            self.server_url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": probe_tool, "arguments": {}},
            },
        )
        _trace("tools/call", response)
        if _auth_debug_enabled():
            self._explain_probe_outcome(response)

    def _explain_probe_outcome(self, response: Any) -> None:
        """Conclut, plutôt que de laisser conclure.

        Ce mode s'utilise sur un poste distant, souvent sans copier-coller
        possible : un journal qu'il faut recopier à la main pour être interprété
        ailleurs n'est pas un outil de diagnostic. On nomme donc le cas observé
        ET l'action qui en découle.
        """
        status = response.status_code
        if status == 401:
            if response.headers.get("www-authenticate"):
                _log(f"  [auth] {self.name} : VERDICT — refus exploitable, le "
                     f"parcours OAuth peut s'amorcer.")
            else:
                _log(f"  [auth] {self.name} : VERDICT — 401 SANS "
                     f"www-authenticate. Le client n'a aucun serveur "
                     f"d'autorisation à découvrir ; à corriger côté serveur.")
            return

        if status == 403 and response.request.headers.get("authorization"):
            _log(f"  [auth] {self.name} : VERDICT — 403 alors qu'un jeton a été "
                 f"envoyé. Ce jeton est refusé (expiré, scopes insuffisants, "
                 f"mauvais public). Le supprimer du fichier de jetons force un "
                 f"parcours neuf ; s'il revient, le refus est côté droits.")
            return

        if status == 403:
            _log(f"  [auth] {self.name} : VERDICT — 403 sans jeton envoyé. Ce "
                 f"n'est pas une autorisation manquante au sens OAuth : une "
                 f"passerelle ou le serveur refuse la requête elle-même. "
                 f"Comparer avec `tests/live_auth_probe.py`, qui pose la même "
                 f"question depuis un autre poste.")
            return

        _log(f"  [auth] {self.name} : VERDICT — ni 401 ni 403 (HTTP {status}). "
             f"Rien à quoi accrocher un parcours OAuth par ce chemin.")

    async def authorize(self, upstream: Upstream) -> None:
        """Déroule le parcours interactif, puis (re)démarre l'upstream.

        À n'appeler qu'une fois le port ouvert : c'est la condition qui rend le
        /callback atteignable. Le drapeau `interactive` n'est levé que pour la
        durée du parcours, pour qu'un échec ultérieur au démarrage ne rouvre
        pas un parcours à l'insu de tout le monde.

        **Le parcours est PROVOQUÉ, jamais espéré comme effet de bord d'un
        `start()`.** C'est la correction du 2026-09-07 : `start()` ouvre une
        session, et sur un upstream dont `initialize` (voire `tools/list`)
        passe sans jeton, il réussit sans qu'aucune requête ne soit refusée —
        donc sans déclencher l'OAuth. `authorize()` retournait alors sans rien
        avoir fait, on annonçait « autorisation accordée », et aucun jeton
        n'était jamais écrit : sur le terrain, ni fichier de jetons ni le
        moindre appel à l'AS, pour une page qui disait le contraire.

        On émet donc une requête à nous sur l'URL de l'upstream, à travers un
        client httpx portant le provider en `auth`. Le 401 attendu fait dérouler
        au SDK son chemin NOMINAL — découverte des métadonnées, enregistrement
        si besoin, redirection, échange du code, écriture du jeton. Rien n'est
        réimplémenté ici : mener le flow à la main dupliquerait la moitié du
        SDK, et deux chemins d'autorisation finiraient par diverger.

        Si l'upstream répond sans exiger d'autorisation, il n'y avait rien à
        accorder — pas une erreur, mais pas non plus un jeton : c'est
        `has_usable_token` qui tranche ensuite, jamais le seul fait d'être
        arrivé ici sans exception.
        """
        import httpx

        self.interactive = True
        try:
            async with httpx.AsyncClient(
                auth=self.provider(), timeout=self.wait_timeout, follow_redirects=False
            ) as client:
                await self._provoke_refusal(client)
        except AuthorizationRequired:
            # Ne peut pas arriver ici (`interactive` est vrai), mais si le
            # parcours est inhibé pour une raison qu'on n'a pas prévue, ne
            # surtout pas le présenter comme un succès.
            raise
        except Exception:
            # L'autorisation reste due : un parcours qui échoue ne doit pas
            # faire croire aux surfaces que le problème est réglé.
            raise
        finally:
            self.interactive = False
            self.pending = None

        # Le témoin est le JETON, jamais l'absence d'exception. Un upstream qui
        # répond 200 sans rien exiger n'a pas « accordé » quoi que ce soit.
        if await self._storage.has_usable_token():
            self.authorization_pending = False
            # La session courante a été ouverte SANS jeton : la rouvrir est ce
            # qui la fait porter l'en-tête Authorization.
            await upstream.stop()
            await upstream.start()

    async def refresh_if_due(self) -> bool:
        """Renouvelle le jeton s'il expire bientôt. Rend True s'il l'a fait.

        POURQUOI CETTE MÉTHODE EXISTE. Le refresh du SDK est passif : il se
        déclenche dans `async_auth_flow`, donc uniquement quand une requête
        traverse le provider. Un upstream qu'on n'appelle pas pendant une
        semaine voit son access token expirer, puis son refresh token, sans
        qu'aucun des deux n'ait servi — et le prochain appel exige une
        ré-autorisation manuelle. Rafraîchir d'avance transforme l'inactivité
        en non-événement, et sous un AS à rotation (WSO2 le fait, c'est
        configurable) cela repousse indéfiniment l'échéance du refresh token.

        ÉCRITURE PAR LE PROVIDER PARTAGÉ, jamais en direct. Le renouvellement
        passe par `async_auth_flow`, sous le `anyio.Lock` d'OAuthContext, comme
        le ferait un appel d'outil : c'est ce qui garde UN SEUL écrivain sur le
        fichier de jetons. Émettre le POST nous-mêmes doublerait le chemin de
        refresh, et un AS à rotation verrait deux usages du même refresh token
        — il révoquerait alors toute la famille, ce que cette boucle est
        justement censée éviter.

        Le déclencheur est l'échéance lue en STOCKAGE, pas
        `context.is_token_valid()` : le contexte peut très bien ne rien savoir
        (provider jamais sollicité depuis le démarrage), auquel cas son verdict
        serait « valide » sur un jeton périmé.
        """
        token = await self._storage.get_tokens()
        if token is None or not token.refresh_token:
            # Rien à renouveler : soit aucun jeton, soit un jeton sans moyen de
            # l'être. Dans les deux cas c'est à l'utilisateur d'autoriser, et
            # les surfaces le disent déjà.
            return False
        if token.expires_in is None:
            # Sans échéance annoncée, rien ne permet de dire « bientôt » : on
            # laisse le refresh passif faire son travail au premier 401.
            return False
        if token.expires_in > self._refresh_margin(token):
            return False

        # L'instant d'expiration AVANT la tentative : c'est son avancement qui
        # dira si un renouvellement a réellement eu lieu (cf. plus bas).
        deadline_before = time.time() + token.expires_in

        import httpx

        # Une requête quelconque suffit : c'est son PASSAGE par le provider qui
        # déclenche le refresh, pas ce qu'elle demande. On vise donc l'upstream
        # avec la plus inoffensive qui soit — et son résultat ne nous intéresse
        # pas, seulement l'effet de bord sur le stockage.
        #
        # `interactive` reste FAUX : si le renouvellement échoue (refresh token
        # révoqué ou expiré), le SDK enchaîne sur un parcours complet, que
        # `_on_redirect` inhibe en levant AuthorizationRequired. C'est le
        # comportement voulu — une boucle de fond ne doit jamais ouvrir un
        # parcours interactif que personne n'a demandé ; elle marque
        # l'autorisation comme due et s'arrête là.
        try:
            async with httpx.AsyncClient(
                auth=self.provider(), timeout=self.wait_timeout,
                follow_redirects=False,
            ) as client:
                await client.post(
                    self.server_url,
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": _MCP_PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {"name": "miaou-proxy", "version": "1"},
                        },
                    },
                )
        except AuthorizationRequired:
            _log(f"Renouvellement du jeton de '{self.name}' impossible — "
                 f"autorisation à refaire : {authorize_path(self.name)}")
            self.authorization_pending = True
            return False
        except Exception as e:
            # Un AS injoignable n'est pas une autorisation perdue : le jeton
            # courant reste valable jusqu'à son terme, et le prochain réveil
            # réessaiera. Ne PAS marquer l'autorisation comme due ici, ce
            # serait envoyer cliquer pour une panne réseau passagère.
            _log(f"Renouvellement du jeton de '{self.name}' échoué ({e}) — "
                 f"nouvelle tentative au prochain réveil.")
            return False

        # Le témoin est le STOCKAGE, pas l'absence d'exception : la requête
        # peut très bien avoir abouti sans qu'aucun refresh n'ait eu lieu.
        #
        # Et le critère est que l'ÉCHÉANCE AIT AVANCÉ, jamais qu'elle dépasse
        # une marge. Comparer à `_REFRESH_MARGIN_S` (première version) exigeait
        # d'un jeton frais qu'il vive plus de 15 minutes : sur un AS qui en
        # émet de 5 minutes — WSO2, mesuré le 2026-09-08 — un renouvellement
        # parfaitement réussi était classé en échec. Rien n'était journalisé,
        # et la boucle recommençait à chaque réveil sur un jeton qu'elle venait
        # de renouveler, ce qui est exactement le rejeu qu'on veut éviter
        # devant un AS à rotation.
        renewed = await self._storage.get_tokens()
        if renewed is None or renewed.expires_in is None:
            return False
        deadline_after = time.time() + renewed.expires_in
        if deadline_after <= deadline_before:
            return False
        _log(f"Jeton de '{self.name}' renouvelé "
             f"(valide {int(renewed.expires_in)}s).")
        self.authorization_pending = False
        return True

    def _refresh_margin(self, token: Any) -> float:
        """Combien de temps AVANT l'échéance on renouvelle.

        Plafonnée à une FRACTION de la durée de vie du jeton, sans quoi une
        marge fixe plus large que cette durée rend tout jeton « bientôt
        expiré » dès son émission : la boucle renouvellerait à chaque réveil,
        indéfiniment. Un AS d'entreprise émet couramment des jetons de 5
        minutes (WSO2, mesuré le 2026-09-08) là où la marge vaut 15 minutes.

        La durée de vie n'est pas lisible sur un jeton déjà entamé —
        `expires_in` est ce qu'il en RESTE. On retient donc la plus longue
        échéance vue pour cet upstream, mémorisée à l'écriture par le stockage,
        et on retombe sur `expires_in` tant qu'on n'a rien vu de mieux.
        """
        lifetime = getattr(self._storage, "observed_lifetime", None)
        if callable(lifetime):
            lifetime = lifetime()
        if not lifetime:
            lifetime = token.expires_in or 0
        # La moitié de la durée de vie laisse un réveil entier pour réessayer
        # après un échec, sans renouveler dès l'émission.
        return min(_REFRESH_MARGIN_S, lifetime / 2)

    def provider(self) -> Any:
        """Construit le provider AU PREMIER APPEL, puis le rend tel quel."""
        if self._provider is not None:
            return self._provider

        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata

        metadata = OAuthClientMetadata(
            client_name="miaou-proxy",
            redirect_uris=[self.callback_url],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=self.scope,
        )
        self._provider = OAuthClientProvider(
            server_url=self.server_url,
            client_metadata=metadata,
            storage=self._storage,
            redirect_handler=self._on_redirect,
            callback_handler=self._on_callback,
        )
        if self.oauth_metadata is not None:
            # Posé sur le CONTEXTE, pas passé au constructeur : le SDK n'a pas
            # de paramètre pour ça, il n'attend ces métadonnées que de sa
            # découverte. Les y installer d'avance fait exactement ce que la
            # découverte aurait fait si elle avait abouti — `_refresh_token()`
            # et `_perform_authorization()` lisent tous deux
            # `context.oauth_metadata` sans se soucier de son origine.
            #
            # Ne PAS mettre `auth_server_url` à jour au passage : il ne sert
            # qu'à construire les URLs de découverte, laquelle n'aura plus
            # lieu d'être pour les endpoints qu'on vient de fournir.
            self._provider.context.oauth_metadata = self.oauth_metadata
        return self._provider


def build_callback_url(host: str, port: int, path: str = "/callback") -> str:
    """URL de redirection annoncée à l'AS.

    Servie par le proxy en fonctionnement normal, pas par un listener éphémère :
    c'est la seule forme compatible avec un parcours déclenché depuis MIAOU.
    Port FIXE — RFC 8252 §7.3 demande à l'AS d'ignorer le port d'un redirect
    loopback, mais en pratique certains le comparent strictement.
    """
    advertised = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return f"http://{advertised}:{port}{path}"


_CALLBACK_PAGE = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 34rem; margin: 4rem auto;
        padding: 0 1rem; line-height: 1.5; }}
.ok {{ color: #16794a; }} .ko {{ color: #a4302a; }}
code {{ background: #f0f0f0; padding: .1rem .3rem; border-radius: .2rem; }}
</style></head>
<body><h1 class="{cls}">{title}</h1><p>{message}</p></body></html>"""


def render_callback_page(upstream_name: str | None, error: str | None) -> str:
    """Page rendue au navigateur au retour de l'AS.

    Rendue AUSSI en cas d'erreur : un onglet blanc après un refus laisserait
    croire à une panne du proxy, alors que le refus vient de l'utilisateur.
    """
    who = f" pour <code>{upstream_name}</code>" if upstream_name else ""
    if error:
        return _CALLBACK_PAGE.format(
            cls="ko",
            title="Autorisation refusée",
            message=f"Le serveur d'autorisation a répondu <code>{error}</code>{who}. "
            "Le proxy n'a reçu aucun jeton — tu peux fermer cet onglet et relancer.",
        )
    return _CALLBACK_PAGE.format(
        cls="ok",
        title="Autorisation reçue",
        message=f"Le proxy a reçu le code d'autorisation{who}. "
        "Tu peux fermer cet onglet.",
    )


def build_callback_route(authorizers: dict[str, UpstreamAuthorizer]) -> Any:
    """Route /callback, PUBLIQUE — et c'est voulu.

    Seul /mcp est enveloppé par RequireAuthMiddleware : le navigateur qui revient
    de l'AS tiers ne porte aucun jeton du proxy, exiger le nôtre ici fermerait la
    boucle, exactement comme l'envelopper les routes /.well-known l'aurait fait
    pour l'auth entrante (piège déjà payé en AB-1).
    """
    from starlette.responses import HTMLResponse

    async def handle_callback(request: Any) -> Any:
        params = request.query_params
        code = params.get("code")
        state = params.get("state")
        error = params.get("error")

        # Le state ne sert PAS à valider ici (le SDK le compare lui-même en
        # compare_digest) : il sert à savoir QUEL upstream attend, quand
        # plusieurs partagent la route. Comparaison sur le paramètre `state`
        # extrait de l'URL d'autorisation, jamais par sous-chaîne — un state
        # est du texte arbitraire, il peut apparaître ailleurs dans l'URL.
        waiting = [a for a in authorizers.values() if a.pending is not None]
        target = None
        if state:
            for auth in waiting:
                if auth.pending.state == state:
                    target = auth
                    break
        # Un seul upstream en attente : pas d'ambiguïté à lever, on lui remet le
        # callback même sans state (un AS qui ne le renvoie pas fera échouer la
        # comparaison du SDK, ce qui est le bon endroit pour ce refus).
        if target is None and len(waiting) == 1:
            target = waiting[0]

        if target is None:
            return HTMLResponse(
                render_callback_page(None, "aucune autorisation en attente"),
                status_code=400,
            )

        target.pending.resolve(code, state, error)
        return HTMLResponse(
            render_callback_page(target.name, error),
            status_code=400 if error else 200,
        )

    return Route("/callback", handle_callback, methods=["GET"])


def build_authorize_route(
    authorizers: dict[str, UpstreamAuthorizer],
    upstreams: dict[str, Upstream],
) -> Any:
    """Route `/authorize/{name}` — déclenche le parcours, port déjà ouvert.

    PUBLIQUE, comme /callback et pour la même raison (cf. build_callback_route).
    C'est ici, et pas dans le lifespan, que le parcours interactif peut vivre :
    le port écoute, donc la redirection vers /callback est atteignable.

    Le parcours est lancé en tâche de fond et la réponse part tout de suite : le
    navigateur ne doit pas rester suspendu pendant qu'on attend son propre
    retour sur /callback.
    """
    import anyio
    from html import escape
    from starlette.responses import HTMLResponse

    async def handle_authorize(request: Any) -> Any:
        name = request.path_params["name"]
        authorizer = authorizers.get(name)
        if authorizer is None or name not in upstreams:
            return HTMLResponse(
                _CALLBACK_PAGE.format(
                    cls="ko",
                    title="Upstream inconnu",
                    # `name` vient du chemin d'URL, donc d'un tiers : la route
                    # est PUBLIQUE. Échappé, sans exception.
                    message=f"Aucun upstream OAuth nommé <code>{escape(name)}</code>.",
                ),
                status_code=404,
            )

        if authorizer.pending is not None:
            url = authorizer.pending.authorization_url or ""
            return HTMLResponse(
                _CALLBACK_PAGE.format(
                    cls="ok",
                    title="Autorisation déjà en cours",
                    message=(
                        f'Suivre <a href="{escape(url, quote=True)}">ce lien</a> '
                        f"pour la terminer."
                    ),
                )
            )

        # Armé AVANT de lancer la tâche : le parcours peut produire son URL
        # immédiatement, et un événement créé après coup manquerait le signal.
        authorizer.redirect_ready = anyio.Event()
        authorizer.last_error = None

        async def _run() -> None:
            try:
                await authorizer.authorize(upstreams[name])
                authorizer.last_error = None
                _log(f"Upstream '{name}' autorisé.")
            except Exception as e:
                authorizer.last_error = str(e)
                _log(f"Autorisation de '{name}' échouée : {e}")
            finally:
                # Toujours réveiller la route, y compris en échec : sinon elle
                # attendrait sa borne entière pour une erreur déjà connue.
                authorizer.redirect_ready.set()

        # Tâche détachée : la réponse doit partir avant que le parcours
        # n'attende le retour du navigateur sur /callback.
        request.app.state.task_group.start_soon(_run)

        # Attente sur ÉVÉNEMENT, jamais un délai fixe. Le parcours doit d'abord
        # découvrir l'AS (`/.well-known/...`), et éventuellement enregistrer un
        # client : derrière un portail d'entreprise, c'est un aller-retour
        # réseau que 100 ms ne couvrent pas. Le chronomètre concluait alors
        # « serveur d'autorisation injoignable » alors qu'il répondait très
        # bien, simplement plus lentement que la borne — diagnostic faux, et
        # faux dans le sens qui décourage de réessayer.
        with anyio.move_on_after(_AUTHORIZE_ROUTE_WAIT_S):
            await authorizer.redirect_ready.wait()

        url = authorizer.pending.authorization_url if authorizer.pending else None
        if url:
            from starlette.responses import RedirectResponse

            return RedirectResponse(url, status_code=302)

        # TROISIÈME issue, et il faut la traiter AVANT de conclure à l'échec :
        # le parcours peut ABOUTIR SANS jamais rediriger. Un `refresh_token`
        # encore valide en stockage, ou un AS qui accorde sans interaction, et
        # le SDK obtient son jeton sans passer par `_on_redirect` — donc sans
        # `pending`. Tester la seule URL de redirection faisait alors répondre
        # « le serveur d'autorisation n'a pas répondu à temps » une seconde
        # après un « Upstream autorisé » dans le log : le contraire de ce qui
        # venait de se passer. Payé en production le 2026-09-07.
        #
        # Le témoin est l'état de l'upstream, pas le chemin qu'il a emprunté
        # pour y arriver : `authorize()` réussi pose `authorization_pending` à
        # faux. Noter que `pending` est de toute façon remis à None par le
        # `finally` d'`authorize()`, donc il ne pouvait rien dire d'un parcours
        # terminé — même abouti par redirection.
        if not authorizer.authorization_pending and authorizer.last_error is None:
            return HTMLResponse(
                _CALLBACK_PAGE.format(
                    cls="ok",
                    title="Autorisation accordée",
                    message=(
                        f"L'upstream <code>{escape(name)}</code> est autorisé. "
                        f"Retourner à MIAOU et relancer la demande."
                    ),
                )
            )

        # Ni redirection, ni jeton, ni erreur : l'upstream a répondu sans rien
        # exiger. Le dire tel quel — annoncer une autorisation accordée serait
        # le faux positif payé le 2026-09-07, où la page confirmait un succès
        # pendant qu'aucun jeton n'était écrit et qu'aucun appel ne partait
        # vers l'AS.
        if authorizer.last_error is None:
            return HTMLResponse(
                _CALLBACK_PAGE.format(
                    cls="ko",
                    title="Rien à autoriser",
                    message=(
                        f"L'upstream <code>{escape(name)}</code> a répondu sans "
                        f"demander d'autorisation, et aucun jeton n'a été "
                        f"obtenu. Si ses outils refusent malgré tout, "
                        f"l'autorisation se joue ailleurs que sur ce parcours "
                        f"— voir la sortie du proxy."
                    ),
                ),
                status_code=409,
            )

        # `last_error` dit POURQUOI quand le parcours a échoué (scope refusé,
        # enregistrement rejeté). Sans elle, une erreur de configuration se
        # présentait comme une panne réseau, et l'exploitant recliquait sur un
        # lien qui ne pouvait pas la réparer.
        detail = authorizer.last_error
        message = (
            f"Le parcours d'autorisation a échoué : <code>{escape(str(detail))}</code>"
            if detail
            else "Le serveur d'autorisation n'a pas répondu à temps. "
            "Voir la sortie du proxy."
        )
        return HTMLResponse(
            _CALLBACK_PAGE.format(
                cls="ko",
                title="Autorisation impossible",
                message=message,
            ),
            status_code=502,
        )

    return Route("/authorize/{name}", handle_authorize, methods=["GET"])


def build_upstream_authorizers(
    cfg: dict[str, Any],
    upstreams: dict[str, Upstream],
    tokens_path: str | Path,
    callback_url: str,
    open_browser: bool = False,
) -> dict[str, UpstreamAuthorizer]:
    """Un UpstreamAuthorizer par upstream http portant une clé `auth`.

    Câble aussi le provider dans l'HttpUpstream correspondant : c'est le seul
    endroit où l'auth entre dans le transport, par le paramètre httpx.Auth.
    """
    authorizers: dict[str, UpstreamAuthorizer] = {}
    for name, srv in cfg.get("mcpServers", {}).items():
        if srv.get("_disabled") or name not in upstreams:
            continue
        auth = srv.get("auth")
        if not isinstance(auth, dict) or auth.get("_disabled"):
            continue
        upstream = upstreams[name]
        if not isinstance(upstream, HttpUpstream):
            raise ValueError(
                f"Serveur '{name}' : la clé 'auth' n'a de sens que sur un "
                f"upstream de type 'http'."
            )
        storage = UpstreamTokenStorage(
            tokens_path, name, client_info_override=build_client_info_override(auth)
        )
        # `auth.redirect_uri` gouverne l'URL RÉELLEMENT annoncée à l'AS, pas
        # seulement celle déclarée au client pré-provisionné : les deux
        # devaient déjà coïncider, et rien ne l'imposait. L'URL dérivée de
        # l'adresse d'écoute (`http://127.0.0.1:port/callback`) reste le défaut,
        # mais elle est refusée par les AS d'entreprise qui n'admettent ni le
        # loopback ni le `http` en clair, ou qui exigent une URL déclarée
        # d'avance — cas rencontré en production (WSO2) le 2026-09-07. C'est
        # alors au déploiement de router cette URL vers `/callback` du proxy.
        upstream_callback = auth.get("redirect_uri") or callback_url
        oauth_metadata = build_oauth_metadata_override(auth)
        authorizer = UpstreamAuthorizer(
            name=name,
            server_url=srv["url"],
            storage=storage,
            callback_url=upstream_callback,
            scope=auth.get("scope"),
            open_browser=open_browser,
            oauth_metadata=oauth_metadata,
        )
        if oauth_metadata is not None:
            _log(f"  {name:<12} endpoints OAuth déclarés : "
                 f"token={oauth_metadata.token_endpoint}")
        if auth.get("redirect_uri"):
            _log(f"  {name:<12} redirection OAuth : {upstream_callback} "
                 f"(depuis la config)")
        authorizers[name] = authorizer
        upstream._auth = authorizer.provider()
    return authorizers


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

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


def build_app(
    mcp_server: Server,
    upstreams: dict[str, Upstream],
    auth: dict[str, Any] | None = None,
    token_verifier: Any = None,
    authorizers: dict[str, UpstreamAuthorizer] | None = None,
) -> Any:
    """`auth` : sortie de resolve_auth_config(), ou None (auth désactivée —
    comportement d'avant le lot AB-1, à l'octet près).

    `token_verifier` : implémentation du protocol mcp.server.auth.provider.
    TokenVerifier. Injectable pour les tests ; None → construit depuis `auth`.

    `authorizers` : sortie de build_upstream_authorizers() (auth SORTANTE, lot
    AB-2). Non vide → la route /callback est servie. Rien à voir avec `auth`,
    qui gouverne l'auth entrante : un proxy peut faire l'une, l'autre, les deux
    ou aucune.
    """
    from contextlib import asynccontextmanager

    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        event_store=None,
        json_response=False,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        import anyio

        async def _start_upstreams() -> list[Upstream]:
            _log(f"Upstream servers ({len(upstreams)}):")
            # Un upstream qui refuse de démarrer (clef d'API absente, module
            # introuvable, subprocess qui ne répond pas) ne doit pas empêcher le
            # proxy de servir les autres : on le signale, on le retire de la table
            # de routage, et on continue. Retrait indispensable — un upstream resté
            # dans la table serait visible de _resolve_via_prefix et un appel
            # d'outil échouerait de façon obscure au lieu d'être simplement absent
            # de tools/list.
            #
            # UNE exception, et une seule : « pas encore autorisé » n'est pas une
            # panne. Ça ne se répare pas par un redémarrage mais par un clic, et
            # le retirer rendrait son propre parcours d'autorisation
            # inatteignable (/authorize/{name} ne le trouverait plus). Il reste
            # donc dans la table, sans session — cf. AuthorizationRequired.
            # Ce qu'on sait AVANT d'appeler quoi que ce soit : un upstream
            # OAuth dont le stockage ne porte pas de jeton utilisable exigera
            # une autorisation. Purement local (lecture de fichier), donc
            # gratuit et sans requête sortante — le boot n'est pas retardé.
            #
            # Sans cet amorçage, le seul événement qui révèle l'autorisation
            # manquante est un `_on_redirect`, lequel n'a lieu qu'au premier
            # appel réellement émis : un upstream qui accepte `initialize` ET
            # `tools/list` sans jeton (Jira d'entreprise, observé en
            # production) resterait donc annoncé sans réserve jusqu'à ce que
            # l'utilisateur se prenne l'échec. C'est précisément ce que la
            # surface `_meta` existe pour éviter.
            for name, authorizer in (authorizers or {}).items():
                storage = getattr(authorizer, "_storage", None)
                probe = getattr(storage, "has_usable_token", None)
                if probe is None:
                    continue
                try:
                    if not await probe():
                        authorizer.authorization_pending = True
                        continue
                    # Le stockage dit « utilisable », ce qui inclut UN JETON
                    # EXPIRÉ porteur d'un refresh token : utilisable au sens
                    # où il se renouvelle sans l'utilisateur, pas au sens où
                    # il partirait tel quel. Tenter le renouvellement ICI
                    # plutôt que d'attendre le premier appel ou le premier
                    # réveil de la boucle (plusieurs minutes), sans quoi le
                    # proxy démarre en annonçant un upstream disponible dont
                    # le tout premier appel d'outil échouera — mesuré sur le
                    # terrain le 2026-09-08.
                    #
                    # Ne renouvelle QUE si l'échéance est proche
                    # (`refresh_if_due` sort sans requête sinon) : un
                    # démarrage avec des jetons frais ne coûte rien.
                    refresher = getattr(authorizer, "refresh_if_due", None)
                    if refresher is not None:
                        await refresher()
                except Exception as e:
                    # Une surface facultative ne doit jamais empêcher un
                    # démarrage : au pire on ne signale pas, et le refus à
                    # l'appel reste le filet.
                    _log(f"  {name:<12} état d'autorisation indéterminé ({e})")

            started: list[Upstream] = []
            failed: list[str] = []
            for name, upstream in list(upstreams.items()):
                try:
                    await upstream.start()
                except AuthorizationRequired as e:
                    _log(f"  {name:<12} unauthorized — {e}")
                    _log(f"  {'':<12} autoriser : {authorize_path(name)}")
                    continue
                except Exception as e:
                    failed.append(name)
                    _log(f"  {name:<12} unavailable — {e}")
                    continue
                started.append(upstream)
                # Le nombre d'outils n'est connu qu'après start() : un upstream
                # inprocess n'a pas encore son _tool_manager avant, et un stdio
                # n'a pas fait son handshake. Un upstream qui démarre mais dont
                # list_tools() échoue ne doit pas empêcher le proxy de servir les
                # autres — on le signale sans propager.
                try:
                    count = len(await upstream.list_tools())
                    detail = f"{count} tool{'s' if count != 1 else ''}"
                except Exception as e:
                    detail = f"tools unavailable ({type(e).__name__})"
                _log(f"  {name:<12} {detail}")
            for name in failed:
                del upstreams[name]
            return started

        # UN task group, de la durée de vie du proxy, qui héberge deux sortes de
        # tâches : les tâches de service des upstreams HTTP (leurs contextes
        # anyio doivent être ouverts ET refermés dans la même tâche, cf.
        # HttpUpstream) et les parcours d'autorisation déclenchés par
        # /authorize/{name} (qui doivent survivre à la requête qui les lance,
        # sans quoi le navigateur resterait suspendu pendant qu'on attend son
        # propre retour sur /callback).
        async def _refresh_loop() -> None:
            """Renouvelle d'avance les jetons qui vont expirer.

            Le refresh du SDK étant passif (il n'a lieu qu'au passage d'une
            requête), un upstream inutilisé assez longtemps perd son access
            token PUIS son refresh token, et redemande une autorisation
            manuelle. Cette boucle couvre cette inactivité, et elle seule : le
            chemin normal reste le refresh passif, au fil des appels.

            Jamais bruyante quand tout va bien — `refresh_if_due` ne fait rien
            tant que l'échéance est loin, et ne log que lorsqu'il se passe
            quelque chose. Une exception ici ne doit pas emporter le task
            group, donc le proxy : on la signale et on continue.
            """
            while True:
                # Recalculé à CHAQUE tour : au premier réveil aucun jeton n'a
                # encore été observé, et la période se resserre d'elle-même dès
                # qu'on connaît la durée de vie réellement émise par l'AS.
                await anyio.sleep(_refresh_poll_interval(authorizers))
                for name, authorizer in (authorizers or {}).items():
                    try:
                        await authorizer.refresh_if_due()
                    except Exception as e:  # pragma: no cover - filet
                        _log(f"Renouvellement de '{name}' interrompu ({e}).")

        async with anyio.create_task_group() as tg:
            app.state.task_group = tg
            for upstream in upstreams.values():
                if isinstance(upstream, HttpUpstream):
                    upstream.host_tasks_in(tg)

            started: list[Upstream] = []
            try:
                started = await _start_upstreams()
                # Lancée APRÈS le démarrage : elle n'a rien à faire tant qu'un
                # upstream n'a pas de jeton, et le premier réveil est de toute
                # façon différé d'un intervalle. Annulée avec le task group à
                # l'extinction (cancel_scope.cancel() du finally).
                if authorizers:
                    tg.start_soon(_refresh_loop)
                # session_manager.run() initialise le task group interne requis pour
                # traiter les requêtes MCP (sans ça : RuntimeError "Task group is not initialized").
                async with session_manager.run():
                    yield
            finally:
                for upstream in started:
                    try:
                        await upstream.stop()
                    except Exception:
                        pass
                # Sans annulation explicite, les tâches de service encore
                # vivantes retiendraient le task group — donc l'extinction.
                tg.cancel_scope.cancel()

    async def handle_mcp(scope: Any, receive: Any, send: Any) -> None:
        await session_manager.handle_request(scope, receive, send)

    routes: list[Any] = []
    middleware: list[Any] = []
    mcp_endpoint: Any = handle_mcp

    if auth is not None:
        from starlette.middleware import Middleware
        from starlette.middleware.authentication import AuthenticationMiddleware
        from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
        from mcp.server.auth.middleware.bearer_auth import (
            BearerAuthBackend,
            RequireAuthMiddleware,
        )
        from mcp.server.auth.routes import (
            build_resource_metadata_url,
            create_protected_resource_routes,
        )
        from pydantic import AnyHttpUrl

        if token_verifier is None:
            token_verifier = build_token_verifier(auth)

        # Métadonnées de ressource protégée (RFC 9728). Servies à
        # /.well-known/oauth-protected-resource/mcp — le chemin de la ressource
        # est SUFFIXÉ au well-known (RFC 9728 §3.1), ce n'est pas la racine.
        # Ces routes restent PUBLIQUES : un client non authentifié doit pouvoir
        # les lire, c'est tout leur objet. Les envelopper dans l'auth ferme la
        # boucle — le client ne peut alors jamais apprendre où s'authentifier.
        routes.extend(
            create_protected_resource_routes(
                resource_url=AnyHttpUrl(auth["resource_url"]),
                authorization_servers=[
                    AnyHttpUrl(u) for u in auth["authorization_servers"]
                ],
                scopes_supported=auth.get("scopes_supported"),
                resource_name="miaou-proxy",
            )
        )

        # AuthenticationMiddleware peuple scope["user"] SANS exiger de jeton ;
        # c'est RequireAuthMiddleware, posé sur le seul endpoint MCP, qui exige.
        middleware = [
            Middleware(
                AuthenticationMiddleware,
                backend=BearerAuthBackend(token_verifier),
            ),
            Middleware(AuthContextMiddleware),
        ]

        # resource_metadata_url est ce qui fait porter au 401 le pointeur
        # `resource_metadata="…"` du header WWW-Authenticate — sans lui, le
        # client reçoit un 401 nu et ne sait pas où aller. La branche qui
        # l'ajoute porte un `# pragma: no cover` dans le SDK : non couverte en
        # amont, donc épinglée par un test de ce dépôt.
        mcp_endpoint = RequireAuthMiddleware(
            handle_mcp,
            auth["required_scopes"],
            build_resource_metadata_url(AnyHttpUrl(auth["resource_url"])),
        )

    if authorizers:
        # Posées AVANT le Mount("/mcp") : une route explicite doit être trouvée
        # avant un montage de préfixe (Starlette retient la première qui matche).
        # Publiques à dessein, cf. build_callback_route.
        routes.append(build_callback_route(authorizers))
        routes.append(build_authorize_route(authorizers, upstreams))

    routes.append(Mount("/mcp", app=mcp_endpoint))

    app = Starlette(
        routes=routes,
        middleware=middleware,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
        allow_headers=["*"],
        # WWW-Authenticate est exposé sinon un client navigateur en
        # cross-origin ne peut pas LIRE le header du 401 qu'on prend soin
        # d'émettre : il verrait un 401 nu. Invisible en test curl (pas de
        # politique CORS), fatal en usage réel.
        # allow_credentials reste ABSENT : le combo avec allow_origins=["*"]
        # est interdit par la spec CORS, et c'est ce qui fait accepter
        # l'Origin: null de MIAOU ouvert en file://.
        expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],
    )

    # Mount("/mcp", ...) traite /mcp comme un préfixe et redirige en 307 vers
    # /mcp/ (strict-slash Starlette). Beaucoup de clients MCP tapent /mcp sans
    # slash final et ne suivent pas la redirection sur les requêtes POST/DELETE.
    # On réécrit le path en amont du routeur pour éviter la redirection.
    inner_app = app

    async def strip_trailing_slash_redirect(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"
        await inner_app(scope, receive, send)

    return strip_trailing_slash_redirect


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def run_with_dev_auth(
    proxy_app: Any,
    host: str,
    port: int,
    dev_auth_port: int,
    issuer_url: str,
    auto_approve: bool = False,
) -> None:
    """Sert le proxy ET le serveur d'autorisation de développement dans CE
    process, sur deux ports distincts.

    Confort de banc d'essai : sans lui il faut deux terminaux. **Deux ports, pas
    un** — chacun garde une origine à part entière, donc l'`issuer_url` reste une
    identité propre et non une sous-route du proxy. C'est ce qui fait de ce mode
    une commodité d'exécution et non une fusion : le proxy reste un Resource
    Server qui vérifie des jetons, l'AS reste seul à en émettre. Ne pas glisser
    vers un montage de l'AS dans l'app du proxy, qui effacerait cette frontière.

    `dev_auth_server` est importé ICI, pas au chargement du module : le proxy
    doit rester utilisable si ce fichier est absent (déploiement qui ne garde que
    le proxy), et surtout ne jamais charger de code d'émission de jetons quand on
    ne l'a pas demandé.
    """
    import asyncio

    import uvicorn

    try:
        import dev_auth_server
    except ImportError as e:  # pragma: no cover - dépend du déploiement
        print(
            f"Erreur : --with-dev-auth exige dev_auth_server.py à côté de "
            f"mcp_proxy.py ({e}).",
            file=sys.stderr,
        )
        sys.exit(1)

    keys = dev_auth_server.DevKeyPair()
    provider = dev_auth_server.DevAuthProvider(
        issuer_url, keys, auto_approve=auto_approve
    )
    auth_app = dev_auth_server.build_app(provider, keys, issuer_url)

    for line in dev_auth_server.banner_lines(issuer_url, auto_approve):
        print(line, file=sys.stderr, flush=True)

    proxy_server = uvicorn.Server(
        uvicorn.Config(proxy_app, host=host, port=port, log_level="info")
    )
    auth_server = uvicorn.Server(
        uvicorn.Config(
            auth_app, host="127.0.0.1", port=dev_auth_port, log_level="warning"
        )
    )

    async def _serve_both() -> None:
        """Le proxy commande : quand il s'arrête (Ctrl-C, erreur), l'AS suit.

        Sans ce couplage, un proxy tombé laisserait un émetteur de jetons
        tourner seul — un serveur qui distribue des autorisations pour une
        ressource qui n'écoute plus.
        """
        auth_task = asyncio.create_task(auth_server.serve())
        try:
            await proxy_server.serve()
        finally:
            auth_server.should_exit = True
            try:
                await asyncio.wait_for(auth_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                auth_task.cancel()

    try:
        asyncio.run(_serve_both())
    except KeyboardInterrupt:
        # `uvicorn.run()` avale le Ctrl-C ; `asyncio.run()` non, et laisserait
        # une trace KeyboardInterrupt à chaque arrêt. Sortie silencieuse, pour
        # que les deux modes de lancement se terminent de la même façon.
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Serveur MCP proxy MIAOU")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Chemin vers le fichier de configuration (défaut: config.json)",
    )
    parser.add_argument("--host", default=None, help="Override de l'adresse d'écoute")
    parser.add_argument("--port", type=int, default=None, help="Override du port")
    parser.add_argument(
        "--proxy",
        default=None,
        metavar="[http://]host:port",
        help=(
            "Force http_proxy/https_proxy (et variantes majuscules) vus par tous "
            "les serveurs MCP servis (inprocess et stdio), même si ces variables "
            "sont déjà définies dans l'environnement ou dans config.json. "
            "'http://' est ajouté si absent."
        ),
    )
    parser.add_argument(
        "--noproxy",
        action="store_true",
        help=(
            "Force l'absence de proxy pour tous les serveurs MCP servis (inprocess "
            "et stdio), même si http_proxy/https_proxy sont définis dans "
            "l'environnement ou dans config.json. Incompatible avec --proxy."
        ),
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        help=(
            "Force l'activation de l'auth OAuth entrante (exige la clé 'auth' "
            "dans la config). Incompatible avec --no-auth."
        ),
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help=(
            "Force la désactivation de l'auth OAuth entrante, même si la config "
            "porte une clé 'auth'. Incompatible avec --auth."
        ),
    )
    parser.add_argument(
        "--with-dev-auth",
        nargs="?",
        const=8787,
        type=int,
        metavar="PORT",
        help=(
            "Lance AUSSI le serveur d'autorisation de développement "
            "(dev_auth_server.py) dans ce process, sur PORT (défaut 8787), et "
            "pointe l'auth dessus. Confort de banc d'essai — JAMAIS en production."
        ),
    )
    parser.add_argument(
        "--dev-auth-auto-approve",
        action="store_true",
        help="Avec --with-dev-auth : approuve sans écran de consentement.",
    )
    parser.add_argument(
        "--tokens-file",
        default=None,
        metavar="FICHIER",
        help=(
            "Fichier des jetons OAuth des upstreams (défaut : <config>-tokens.json, "
            "à côté de la config). Distinct de config.json à dessein."
        ),
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help=(
            "Ouvre l'URL d'autorisation dans le navigateur par défaut de l'OS. "
            "Confort : le lien reste affiché, et c'est lui le mécanisme de "
            "référence (l'OS ne garantit ni le bon navigateur ni le bon profil)."
        ),
    )
    parser.add_argument(
        "--debug-auth",
        action="store_true",
        help=(
            "Trace le parcours OAuth sortant : chaque requête HTTP émise vers "
            "un upstream ou son serveur d'autorisation, avec son code de "
            "réponse et l'en-tête WWW-Authenticate. Sans elle, un parcours qui "
            "n'aboutit pas ne laisse AUCUNE trace — c'est ce qui a coûté "
            "plusieurs correctifs posés à l'aveugle. Les valeurs sensibles "
            "(jetons, codes, secrets) sont masquées."
        ),
    )
    args = parser.parse_args()

    if args.debug_auth:
        enable_auth_debug()

    # Avant TOUT : avant build_upstreams (qui importe les modules de serveurs, et
    # donc peut construire un contexte SSL), et avant le premier handshake TLS
    # d'un upstream http. truststore remplace ssl.SSLContext lui-même, donc cet
    # unique appel couvre aussi bien httpx (HttpUpstream, client OAuth du SDK)
    # qu'urllib (make_opener, dans les serveurs inprocess) : un upstream HTTPS
    # dont le certificat est signé par une AC d'entreprise interne cesse
    # d'échouer en CERTIFICATE_VERIFY_FAILED. Un contexte déjà construit
    # garderait l'ancienne classe — d'où la position en tête de main().
    enable_system_trust_store()

    if args.proxy and args.noproxy:
        print("Erreur : --proxy et --noproxy sont mutuellement exclusifs.", file=sys.stderr)
        sys.exit(1)

    if args.auth and args.no_auth:
        print("Erreur : --auth et --no-auth sont mutuellement exclusifs.", file=sys.stderr)
        sys.exit(1)

    if args.with_dev_auth is not None and args.no_auth:
        print(
            "Erreur : --with-dev-auth et --no-auth sont contradictoires "
            "(lancer un serveur d'autorisation puis n'exiger aucun jeton).",
            file=sys.stderr,
        )
        sys.exit(1)

    config_path = Path(args.config)
    if not config_path.exists():
        print(
            f"Erreur : config introuvable '{config_path}'. "
            "Copier config.sample.json → config.json et l'adapter.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        cfg = load_config(config_path)
        host = args.host or cfg.get("host", "127.0.0.1")
        port = args.port or int(cfg["port"])
    except ValueError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        sys.exit(1)

    proxy_overrides = compute_proxy_env_overrides(args.proxy, args.noproxy)
    if proxy_overrides is not None:
        # Avant build_upstreams : les upstreams inprocess importent leur module
        # (donc appellent potentiellement make_opener()/ProxyHandler() dès le
        # premier tool call) en partageant l'environnement de ce process — poser
        # les overrides ici les couvre sans toucher InProcessUpstream.
        apply_proxy_env_overrides_to_process(proxy_overrides)

    cli_auth = True if args.auth else (False if args.no_auth else None)
    dev_auth_url: str | None = None
    if args.with_dev_auth is not None:
        # Le serveur de développement EST l'émetteur : on le pose dans la config
        # avant résolution, pour que --with-dev-auth suffise sans éditer
        # config.json. Une clé `auth` déjà présente n'est pas écrasée — la
        # config explicite de l'utilisateur prime toujours sur la commodité.
        dev_auth_url = f"http://127.0.0.1:{args.with_dev_auth}"
        if not isinstance(cfg.get("auth"), dict):
            cfg = {**cfg, "auth": {"issuer_url": dev_auth_url}}
        else:
            dev_auth_url = cfg["auth"].get("issuer_url", dev_auth_url)
        cli_auth = True

    try:
        auth = resolve_auth_config(cfg, cli_auth=cli_auth, host=host, port=port)
    except AuthConfigError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        sys.exit(1)

    upstreams = build_upstreams(cfg, proxy_env_overrides=proxy_overrides)

    tokens_path = args.tokens_file or _default_tokens_path(args.config)
    try:
        authorizers = build_upstream_authorizers(
            cfg,
            upstreams,
            tokens_path=tokens_path,
            callback_url=build_callback_url(host, port),
            open_browser=args.open,
        )
    except ValueError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        sys.exit(1)

    tool_map: dict[str, tuple[str, str]] = {}
    # Cache d'outils à côté du fichier de jetons : pas un secret, mais même
    # durée de vie. Sans lui, un upstream non autorisé n'aurait rien à lister.
    catalog = ToolCatalogCache(Path(tokens_path).with_name(
        Path(tokens_path).stem.replace("-tokens", "") + "-tools.json"
    ))
    mcp_server = build_proxy_server(
        upstreams, tool_map, authorizers=authorizers, catalog=catalog
    )
    app = build_app(mcp_server, upstreams, auth=auth, authorizers=authorizers)

    import uvicorn

    if auth is not None:
        _log(f"Auth OAuth entrante ACTIVE — émetteur accepté : {auth['issuer_url']}")
        _log(f"  ressource protégée : {auth['resource_url']}")
    if authorizers:
        _log(
            f"Auth OAuth sortante — {len(authorizers)} upstream(s) : "
            f"{', '.join(sorted(authorizers))}"
        )
        _log(f"  jetons : {tokens_path}")
        _log(f"  redirection : {build_callback_url(host, port)}")
    print(f"miaou-proxy → http://{host}:{port}/mcp  (Ctrl-C pour arrêter)")

    if args.with_dev_auth is None:
        uvicorn.run(app, host=host, port=port, log_level="info")
        return

    run_with_dev_auth(
        app,
        host=host,
        port=port,
        dev_auth_port=args.with_dev_auth,
        issuer_url=dev_auth_url or f"http://127.0.0.1:{args.with_dev_auth}",
        auto_approve=args.dev_auth_auto_approve,
    )


if __name__ == "__main__":
    main()
