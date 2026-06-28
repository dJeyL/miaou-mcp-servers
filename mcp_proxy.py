#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2", "uvicorn", "starlette", "anyio", "html2text"]
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

    def __init__(self, module_name: str, env: dict[str, str] | None = None) -> None:
        self._module_name = module_name
        self._env = env
        self._tool_manager: Any = None

    async def start(self) -> None:
        if self._env:
            import os
            for key, value in self._env.items():
                os.environ.setdefault(key, value)
        module = importlib.import_module(self._module_name)
        fastmcp = getattr(module, "mcp", None)
        if fastmcp is None:
            raise RuntimeError(
                f"Le module '{self._module_name}' n'expose pas d'attribut 'mcp' (FastMCP)."
            )
        self._tool_manager = fastmcp._tool_manager

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
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env,
            cwd=self._cwd,
        )
        read, write = await self._exit_stack.enter_async_context(stdio_client(params))
        session = ClientSession(read, write)
        self._session = await self._exit_stack.enter_async_context(session)
        await self._session.initialize()

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
    cfg = json.loads(Path(path).read_text())
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
            upstreams[name] = InProcessUpstream(module, env=srv.get("env"))
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

def build_proxy_server(
    upstreams: dict[str, Upstream],
    tool_map: dict[str, tuple[str, str]],
) -> Server:
    """Construit le Server MCP avec les handlers list_tools / call_tool."""
    server: Server = Server("miaou-proxy")

    @server.list_tools()  # type: ignore[arg-type]
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

    @server.call_tool()  # type: ignore[arg-type]
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[Any]:
        if name not in tool_map:
            from mcp.shared.exceptions import McpError
            from mcp.types import ErrorCode # type: ignore
            raise McpError(ErrorCode.InvalidParams, f"Outil inconnu : '{name}'") # type: ignore
        upstream_name, orig_name = tool_map[name]
        return await upstreams[upstream_name].call_tool(orig_name, arguments or {})

    return server


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def build_app(
    mcp_server: Server,
    upstreams: dict[str, Upstream],
) -> Starlette:
    from contextlib import asynccontextmanager

    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        event_store=None,
        json_response=False,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):  # type: ignore[type-arg]
        for upstream in upstreams.values():
            await upstream.start()
        # session_manager.run() initialise le task group interne requis pour
        # traiter les requêtes MCP (sans ça : RuntimeError "Task group is not initialized").
        async with session_manager.run():
            yield
        for upstream in upstreams.values():
            await upstream.stop()

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
    return app


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

    cfg = load_config(config_path)
    host = args.host or cfg.get("host", "127.0.0.1")
    port = args.port or int(cfg["port"])

    upstreams = build_upstreams(cfg)
    tool_map: dict[str, tuple[str, str]] = {}
    mcp_server = build_proxy_server(upstreams, tool_map)
    app = build_app(mcp_server, upstreams)

    import uvicorn

    print(f"miaou-proxy → http://{host}:{port}/mcp  (Ctrl-C pour arrêter)")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
