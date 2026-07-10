"""
Base class shared by MIAOU MCP servers.

Not a PEP 723 script — imported by mcp_bench.py and mcp_weather.py,
whose own PEP 723 blocks declare the shared dependencies.
"""

import argparse
import inspect
import os
import sys
import urllib.request

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware

# Par défaut, ArgModelBase (mcp>=1.28.1) laisse Pydantic ignorer silencieusement
# tout argument d'outil non déclaré dans la signature de la fonction (extra="ignore"
# implicite) : un appelant qui hallucine un nom de paramètre (ex. `page` au lieu de
# `selector`) voit son argument avalé sans erreur, et l'outil retombe sur son défaut
# sans jamais signaler l'anomalie. Passage en extra="forbid" pour transformer ça en
# erreur de validation explicite, avant même l'exécution de l'outil.
ArgModelBase.model_config["extra"] = "forbid"


def _strip_schema_titles(schema: object) -> None:
    """Supprime récursivement les clés "title" auto-générées par Pydantic dans un
    schéma JSON de paramètres ("Char Start", "readArguments", ...) : elles ne portent
    aucune information que le nom du paramètre ne donne déjà, et gonflent le payload
    tools/list envoyé au modèle à chaque requête. Les dicts sous "properties" et
    "$defs" sont des maps nom→sous-schéma : leurs clés sont des noms (un paramètre
    peut s'appeler "title"), seules leurs valeurs sont des schémas à nettoyer."""
    if isinstance(schema, list):
        for item in schema:
            _strip_schema_titles(item)
        return
    if not isinstance(schema, dict):
        return
    schema.pop("title", None)
    for key, value in schema.items():
        if key in ("properties", "$defs") and isinstance(value, dict):
            for sub_schema in value.values():
                _strip_schema_titles(sub_schema)
        else:
            _strip_schema_titles(value)


def make_opener() -> urllib.request.OpenerDirector:
    """Construit un opener urllib proxy-aware (lit http_proxy/https_proxy, en
    majuscules ou minuscules, chaque variable mappée sur son propre scheme)."""
    http_proxy = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
    https_proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    proxies = {}
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    if proxies:
        return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    return urllib.request.build_opener()


class MiaouMCPBase:
    """Base for MIAOU MCP servers.

    Usage:
        class MyServer(MiaouMCPBase):
            def __init__(self):
                super().__init__("my-server", default_port=9000)

                @self.mcp.tool()
                async def my_tool(...): ...

                self.finalize_tools()  # dernier appel du __init__

        server = MyServer()
        mcp = server.mcp  # expose for in-process proxy use

        if __name__ == "__main__":
            server.main()
    """

    def __init__(self, name: str, default_port: int) -> None:
        self.default_port = default_port
        _security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        self.mcp = FastMCP(name, transport_security=_security)

    def finalize_tools(self) -> None:
        """Normalise ce que tools/list expose, pour réduire le payload envoyé au
        modèle à chaque requête. À appeler en dernière ligne du __init__ de chaque
        serveur, après l'enregistrement de tous les outils. Idempotent.

        - Descriptions : inspect.cleandoc — une docstring assignée à la main
          (`func.__doc__ = f\"\"\"...\"\"\"`, pattern des caps interpolés) part sinon
          sur le wire avec l'indentation source de chaque ligne de continuation.
        - Schémas de paramètres : suppression des "title" auto-générés par Pydantic
          (voir _strip_schema_titles).
        """
        for tool in self.mcp._tool_manager._tools.values():
            if tool.description:
                tool.description = inspect.cleandoc(tool.description)
            _strip_schema_titles(tool.parameters)

    def _make_app(self):
        """Build the Starlette ASGI app with CORS middleware."""
        app = self.mcp.streamable_http_app()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
            allow_headers=["*"],
            expose_headers=["Mcp-Session-Id"],
        )
        return app

    def run_http(self, host: str = "127.0.0.1", port: int | None = None) -> None:
        import uvicorn

        port = port or self.default_port
        print(f"{self.mcp.name} → http://{host}:{port}/mcp  (Ctrl-C pour arrêter)")
        uvicorn.run(self._make_app(), host=host, port=port, log_level="info")

    def run_stdio(self) -> None:
        self.mcp.run(transport="stdio")

    def main(self) -> None:
        """CLI entrypoint.

        Supports two syntaxes for backward compat:
            server.py [host] [port]                  (legacy positional)
            server.py [--transport http|stdio] [--host H] [--port P]
        """
        # Detect legacy positional syntax: first arg looks like a host (no "--")
        args_raw = sys.argv[1:]
        positional = args_raw and not args_raw[0].startswith("--")

        if positional:
            host = args_raw[0] if len(args_raw) > 0 else "127.0.0.1"
            if len(args_raw) > 1:
                try:
                    port = int(args_raw[1])
                except ValueError:
                    print(f"Erreur : port invalide '{args_raw[1]}' (entier attendu)", file=sys.stderr)
                    sys.exit(1)
            else:
                port = self.default_port
            self.run_http(host, port)
            return

        parser = argparse.ArgumentParser(description=f"Serveur MCP {self.mcp.name}")
        parser.add_argument(
            "--transport",
            choices=["http", "stdio"],
            default="http",
            help="Transport à utiliser (défaut: http)",
        )
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=self.default_port)
        parsed = parser.parse_args()

        if parsed.transport == "stdio":
            self.run_stdio()
        else:
            self.run_http(parsed.host, parsed.port)
