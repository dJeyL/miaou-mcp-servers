"""Upstream inprocess de test qui sert des skills (non collecté : pas de préfixe
`test_`).

`build(config)` : `config["skills_dir"]` désigne le dossier de skills, et
`config["requires_skill"]` (facultatif) est passé tel quel à `finalize_tools`.
Sans `requires_skill`, ses skills sont FACULTATIVES : aucun outil ne les exige.
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
