"""
Base class shared by MIAOU MCP servers.

Not a PEP 723 script — imported by mcp_bench.py and mcp_weather.py,
whose own PEP 723 blocks declare the shared dependencies.
"""

import argparse
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware


class MiaouMCPBase:
    """Base for MIAOU MCP servers.

    Usage:
        class MyServer(MiaouMCPBase):
            def __init__(self):
                super().__init__("my-server", default_port=9000)

                @self.mcp.tool()
                async def my_tool(...): ...

        server = MyServer()
        mcp = server.mcp  # expose for in-process proxy use

        if __name__ == "__main__":
            server.main()
    """

    def __init__(self, name: str, default_port: int) -> None:
        self.default_port = default_port
        _security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        self.mcp = FastMCP(name, transport_security=_security)

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
            port = int(args_raw[1]) if len(args_raw) > 1 else self.default_port
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
