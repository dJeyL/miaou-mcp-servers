#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.28.1", "uvicorn", "starlette", "anyio", "html2text", "pymupdf", "python-docx", "openpyxl", "python-pptx"]
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
    uv run mcp_proxy.py --port 8767                # override port

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
import sys
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
from starlette.routing import Mount


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


def build_upstreams(cfg: dict[str, Any]) -> dict[str, Upstream]:
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
            upstreams[name] = StdioUpstream(
                command=command,
                args=srv.get("args", []),
                env=srv.get("env"),
                cwd=srv.get("cwd"),
            )
        else:
            raise ValueError(f"Type de serveur inconnu pour '{name}': '{srv_type}'.")
    return upstreams


# ---------------------------------------------------------------------------
# Proxy MCP Server (low-level mcp.server.Server pour l'enregistrement dynamique)
# ---------------------------------------------------------------------------

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


def build_proxy_server(
    upstreams: dict[str, Upstream],
    tool_map: dict[str, tuple[str, str]],
) -> Server:
    """Construit le Server MCP avec les handlers list_tools / call_tool."""
    server: Server = Server("miaou-proxy")

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        tools: list[types.Tool] = []
        for prefix, upstream in upstreams.items():
            for tool in await upstream.list_tools():
                prefixed = f"{prefix}__{tool.name}"
                tool_map[prefixed] = (prefix, tool.name)
                tools.append(
                    types.Tool(
                        name=prefixed,
                        description=tool.description,
                        inputSchema=tool.inputSchema,
                    )
                )
        return tools

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[Any]:
        if name in tool_map:
            upstream_name, orig_name = tool_map[name]
        else:
            resolved = _resolve_via_prefix(name, upstreams)
            if resolved is None:
                from mcp.shared.exceptions import McpError
                from mcp.types import INVALID_PARAMS, ErrorData
                raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Outil inconnu : '{name}'"))
            upstream_name, orig_name = resolved
        return await upstreams[upstream_name].call_tool(orig_name, arguments or {})

    _wrap_ref_unknown_sentinel(server, upstreams)
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
# App factory
# ---------------------------------------------------------------------------

def build_app(
    mcp_server: Server,
    upstreams: dict[str, Upstream],
) -> Any:
    from contextlib import asynccontextmanager

    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        event_store=None,
        json_response=False,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        started: list[Upstream] = []
        try:
            for upstream in upstreams.values():
                await upstream.start()
                started.append(upstream)
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

    async def handle_mcp(scope: Any, receive: Any, send: Any) -> None:
        await session_manager.handle_request(scope, receive, send)

    app = Starlette(
        routes=[Mount("/mcp", app=handle_mcp)],
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
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

def main() -> None:
    parser = argparse.ArgumentParser(description="Serveur MCP proxy MIAOU")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Chemin vers le fichier de configuration (défaut: config.json)",
    )
    parser.add_argument("--host", default=None, help="Override de l'adresse d'écoute")
    parser.add_argument("--port", type=int, default=None, help="Override du port")
    args = parser.parse_args()

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

    upstreams = build_upstreams(cfg)
    tool_map: dict[str, tuple[str, str]] = {}
    mcp_server = build_proxy_server(upstreams, tool_map)
    app = build_app(mcp_server, upstreams)

    import uvicorn

    print(f"miaou-proxy → http://{host}:{port}/mcp  (Ctrl-C pour arrêter)")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
