"""Upstream MODERNE de test qui sert des entrées de skills FABRIQUÉES (non
collecté : pas de préfixe `test_`).

`Skills` de `mcp_base` n'émet que des entrées valides, en une page. Ce serveur
fabrique les formes que la spec admet ou qu'un tiers pourrait produire, pour
éprouver le relais du proxy : pagination par `nextCursor` (curseur répété en
dernière page), `resources: "dynamic"`, URI hors `skill://`, élément de
`resources` sans `uri`, `description` ou `name` absents, description trop
longue, `resources` vide, entrée qui n'est pas un objet, et une skill servie
mais NON listée (lisible par `skills/get` et `resources/read`).

`build(listing)` : `"fabricated"` (défaut) ou `"empty"` (extension déclarée,
listage vide). Monté en app streamable-http par les tests.
"""

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

import mcp.types as types
from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from mcp_base import SKILLS_EXTENSION_ID, GetSkillParams, GetSkillResult


class ListSkillsResult(types.PaginatedResult, types.CacheableResult):
    """Celui de `mcp_base` exige des objets : une entrée fabriquée qui n'en est
    pas un ferait échouer la réponse côté serveur."""

    result_type: types.ResultType = "complete"
    skills: list[Any]

LONG_DESCRIPTION = "x" * 2000
PAGE_TWO_TTL_MS = 1000


def _entry(name: str, description: str | None = None, resources: Any = None) -> dict[str, Any]:
    uri = f"skill://{name}/SKILL.md"
    frontmatter: dict[str, Any] = {"name": name}
    if description is not None:
        frontmatter["description"] = description
    return {
        "uri": uri,
        "frontmatter": frontmatter,
        "resources": resources if resources is not None else [{"uri": uri, "digest": "sha256:00", "size": 5}],
    }


ALPHA = _entry("alpha", "Alpha")
DYNAMIC = _entry("dyn", "Dynamique", resources="dynamic")
NO_DESCRIPTION = _entry("nodesc", resources=[])
ANONYMOUS = {"uri": "skill://anon/SKILL.md", "frontmatter": {"description": LONG_DESCRIPTION}, "resources": []}
FOREIGN = {**_entry("gh", "Ailleurs"), "uri": "github://owner/repo/SKILL.md"}
NO_RESOURCE_URI = _entry("bad", "Ressource sans uri", resources=[{"digest": "sha256:00"}])
BETA = _entry("beta", "Bêta")
HIDDEN = _entry("hidden", "Servie, non listée")

PAGE_ONE = [ALPHA, DYNAMIC, NO_DESCRIPTION, ANONYMOUS, FOREIGN, NO_RESOURCE_URI, "pas un objet"]
PAGE_TWO = [BETA]
SERVED = {e["uri"]: e for e in (ALPHA, DYNAMIC, NO_DESCRIPTION, ANONYMOUS, BETA, HIDDEN)}


class _FabricatedSkills(Extension):
    identifier = SKILLS_EXTENSION_ID

    def __init__(self, listing: str) -> None:
        self._listing = listing

    def methods(self) -> list[MethodBinding]:
        async def skills_list(ctx: Any, params: Any) -> ListSkillsResult:
            if self._listing == "empty":
                return ListSkillsResult(skills=[])
            cursor = getattr(params, "cursor", None) if params is not None else None
            if cursor is None:
                return ListSkillsResult(skills=PAGE_ONE, next_cursor="p2", ttl_ms=300_000, cache_scope="public")
            # Dernière page, curseur RÉPÉTÉ : le proxy doit s'arrêter là.
            return ListSkillsResult(
                skills=PAGE_TWO, next_cursor="p2", ttl_ms=PAGE_TWO_TTL_MS, cache_scope="private"
            )

        async def skills_get(ctx: Any, params: GetSkillParams) -> GetSkillResult:
            entry = SERVED.get(params.uri)
            if entry is None:
                raise MCPError(types.INVALID_PARAMS, f"No skill is served at {params.uri}")
            return GetSkillResult(skill=entry)

        return [
            MethodBinding("skills/list", types.PaginatedRequestParams, skills_list),
            MethodBinding("skills/get", GetSkillParams, skills_get),
        ]


def build(listing: str = "fabricated") -> MCPServer:
    mcp = MCPServer("fabricated-skills", extensions=[_FabricatedSkills(listing)])

    @mcp.tool()
    def ping() -> str:
        return "pong"

    @mcp.resource("skill://alpha/SKILL.md", mime_type="text/markdown")
    def alpha_md() -> str:
        return "alpha"

    @mcp.resource("skill://hidden/SKILL.md", mime_type="text/markdown")
    def hidden_md() -> str:
        return "---\nname: hidden\ndescription: Servie, non listée\n---\nhidden\n"

    return mcp
