"""Upstream MODERNE de test pour la négociation d'ère du proxy (non collecté : pas
de préfixe `test_`).

Un `MCPServer` du SDK 2.x, qui parle donc les deux ères : instructions, une
extension déclarée (visible seulement dans `server/discover`), un outil simple
et un outil dont le paramètre porte une annotation `x-mcp-header`.

- `build()` : le `MCPServer`, pour une app streamable-http montée en test
  (`streamable_http_app`, servie par `httpx2.ASGITransport`) ;
- `python era_fixture_server.py` : le même serveur sur stdio, lancé en
  subprocess par `StdioUpstream`.
"""

from typing import Annotated

from pydantic import Field

from mcp.server.extension import Extension
from mcp.server.mcpserver import MCPServer

INSTRUCTIONS = "consigne de l'upstream moderne"
EXTENSION_ID = "com.example/era-fixture"


class _FixtureExtension(Extension):
    identifier = EXTENSION_ID

    def settings(self):
        return {"flag": True}

    def tools(self):
        return []


def build() -> MCPServer:
    mcp = MCPServer("era-fixture", instructions=INSTRUCTIONS, extensions=[_FixtureExtension()])

    @mcp.tool()
    def ping(text: str) -> str:
        return "pong " + text

    @mcp.tool()
    def regional(
        region: Annotated[str, Field(json_schema_extra={"x-mcp-header": "Region"})],
    ) -> str:
        return "region " + region

    return mcp


if __name__ == "__main__":
    build().run("stdio")
