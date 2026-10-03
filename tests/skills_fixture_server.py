"""Upstream inprocess de test qui sert des skills (non collecté : pas de préfixe
`test_`).

`build(config)` : `config["skills_dir"]` désigne le dossier de skills, et
`config["requires_skill"]` (facultatif) est passé tel quel à `finalize_tools`.
Sans `requires_skill`, ses skills sont FACULTATIVES : aucun outil ne les exige.

Lancé en script, il sert les mêmes skills sur stdio : c'est l'upstream stdio
moderne qui déclare l'extension (et, monté en app streamable-http par
`build()`, l'upstream http).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from mcp_base import MiaouMCPBase


class SkillsFixtureServer(MiaouMCPBase):
    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        super().__init__(
            "skills-fixture",
            default_port=0,
            config=config,
            instructions=config.get("instructions"),
            skills_dir=config.get("skills_dir"),
        )

        @self.mcp.tool()
        async def ping() -> str:
            """Répond pong."""
            return "pong"

        @self.mcp.tool()
        async def pong() -> str:
            """Répond ping."""
            return "ping"

        self.finalize_tools(requires_skill=config.get("requires_skill"))


def build(config: dict | None = None):
    return SkillsFixtureServer(config).mcp


if __name__ == "__main__":
    # Le même serveur sur stdio, lancé en subprocess par `StdioUpstream` :
    # `python skills_fixture_server.py <skills_dir> [<requires_skill>]`.
    build(
        {"skills_dir": sys.argv[1], **({"requires_skill": sys.argv[2]} if len(sys.argv) > 2 else {})}
    ).run("stdio")
