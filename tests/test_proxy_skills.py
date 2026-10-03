"""Le proxy sert l'extension Skills : skills de ses upstreams inprocess, URI
préfixées du nom d'upstream, capacités publiées seulement si une skill est
servie.

Chaque test reproduit l'ordre réel de `main()` : `build_proxy_server()` AVANT
`start()` des upstreams, puis `install_skills()` — ce que fait le lifespan.
"""

import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

import mcp.types as types
from mcp.client import Client
from mcp.shared.exceptions import MCPError

from mcp_base import SKILLS_EXTENSION_ID, GetSkillParams, skill_digest
from mcp_proxy.server import build_proxy_server
from mcp_proxy.skills import (
    install_skills,
    prefix_skill_entry,
    prefix_skill_uri,
    resolve_skill_uri,
)
from mcp_proxy.upstream import HttpUpstream, InProcessUpstream, StdioUpstream
from tests.proxy_client import _BaseExceptionGroup, _single_cause

FIXTURE = "tests.skills_fixture_server"

OPTIONAL_SKILL_MD = (
    "---\nname: tips\ndescription: Astuces facultatives pour ping et pong\n---\n\n"
    "# tips\n\nVoir `annexes/detail.md`.\n"
)
OPTIONAL_ANNEX = "# Détail\r\n\r\nLigne CRLF.\r\n"


@pytest.fixture
def optional_skills_dir(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "tips" / "annexes").mkdir(parents=True)
    (root / "tips" / "SKILL.md").write_bytes(OPTIONAL_SKILL_MD.encode("utf-8"))
    (root / "tips" / "annexes" / "detail.md").write_bytes(OPTIONAL_ANNEX.encode("utf-8"))
    return root


async def _proxy(upstreams: dict[str, Any]) -> tuple[Any, bool]:
    server = build_proxy_server(upstreams, {})
    for upstream in upstreams.values():
        await upstream.start()
    served = await install_skills(server, upstreams)
    return server, served


async def _unwrapped(coro_fn):
    try:
        return await coro_fn()
    except _BaseExceptionGroup as group:
        cause = _single_cause(group)
        if cause is None:
            raise
        raise cause from None


async def _request(server: Any, method: str, params: Any, mode: str = "2026-07-28") -> dict:
    async def run():
        async with Client(server, mode=mode) as client:
            request = types.Request[Any, str](method=method, params=params)
            return await client.session.send_request(request, TypeAdapter(dict[str, Any]))

    return await _unwrapped(run)


async def _read(server: Any, uri: str, mode: str = "2026-07-28") -> types.ReadResourceResult:
    async def run():
        async with Client(server, mode=mode) as client:
            return await client.read_resource(uri)

    return await _unwrapped(run)


# --- Réécriture d'URI : fonctions pures -----------------------------------------


@pytest.mark.parametrize(
    ("upstream", "origin", "published"),
    [
        ("bench", "skill://bench/SKILL.md", "skill://bench/bench/SKILL.md"),
        ("splunk", "skill://splunk/apic.md", "skill://splunk/splunk/apic.md"),
        ("x", "skill://acme/billing/refunds/examples/email.md", "skill://x/acme/billing/refunds/examples/email.md"),
    ],
)
def test_prefix_and_resolve_are_inverse(upstream, origin, published):
    assert prefix_skill_uri(upstream, origin) == published
    assert resolve_skill_uri(published, {upstream}) == (upstream, origin)


def test_prefix_refuses_another_scheme():
    with pytest.raises(ValueError, match="hors schéma"):
        prefix_skill_uri("gh", "github://owner/repo/skills/x/SKILL.md")


@pytest.mark.parametrize(
    "uri",
    [
        "skill://ghost/bench/SKILL.md",  # premier segment : pas un upstream vivant
        "skill://bench",  # rien après le premier segment
        "skill://bench/",
        "file:///etc/passwd",
        "bench/SKILL.md",
    ],
)
def test_resolve_refuses_with_invalid_params(uri):
    with pytest.raises(MCPError) as info:
        resolve_skill_uri(uri, {"bench"})
    assert info.value.error.code == types.INVALID_PARAMS
    assert uri in info.value.error.message


def test_prefix_entry_rewrites_every_uri_and_keeps_digests():
    entry = {
        "uri": "skill://tips/SKILL.md",
        "frontmatter": {"name": "tips", "description": "d"},
        "resources": [
            {"uri": "skill://tips/SKILL.md", "digest": "sha256:aa", "size": 1},
            {"uri": "skill://tips/annexes/detail.md", "digest": "sha256:bb", "size": 2},
        ],
    }
    assert prefix_skill_entry("fx", entry) == {
        "uri": "skill://fx/tips/SKILL.md",
        "frontmatter": {"name": "tips", "description": "d"},
        "resources": [
            {"uri": "skill://fx/tips/SKILL.md", "digest": "sha256:aa", "size": 1},
            {"uri": "skill://fx/tips/annexes/detail.md", "digest": "sha256:bb", "size": 2},
        ],
    }


def test_prefix_entry_leaves_dynamic_resources_alone():
    entry = {"uri": "skill://d/SKILL.md", "frontmatter": {}, "resources": "dynamic"}
    assert prefix_skill_entry("up", entry)["resources"] == "dynamic"


# --- Rien ne change sans skill -----------------------------------------------------


async def test_no_skill_served_publishes_nothing_new():
    server, served = await _proxy({"fx": InProcessUpstream(FIXTURE, config={})})
    assert served is False
    async def run():
        async with Client(server, mode="auto") as client:
            return client.server_capabilities
    caps = await _unwrapped(run)
    assert not caps.extensions
    assert caps.resources is None
    with pytest.raises(MCPError) as info:
        await _request(server, "skills/list", types.PaginatedRequestParams())
    assert info.value.error.code == types.METHOD_NOT_FOUND


async def test_unrelayed_skills_are_logged_only_when_informative(capsys):
    """Le journal ne signale des skills non relayées que pour un upstream forcé
    en legacy par la config (jamais interrogé en moderne, on ne peut pas savoir
    s'il en sert). Rien pour un moderne qui déclare l'extension (relayé), ni
    pour un moderne sans elle, ni pour un legacy constaté par la sonde, dont
    l'ère est déjà au journal et qui ne peut pas servir l'extension."""
    serving = HttpUpstream("http://127.0.0.1:9/mcp")
    serving.protocol_version = "2026-07-28"
    serving.capabilities = types.ServerCapabilities(extensions={SKILLS_EXTENSION_ID: {}})
    plain = HttpUpstream("http://127.0.0.1:9/mcp")
    plain.protocol_version = "2026-07-28"
    plain.capabilities = types.ServerCapabilities(extensions={"com.example/other": {}})
    probed_legacy = StdioUpstream("true", [])
    probed_legacy.protocol_version = "2025-11-25"
    probed_legacy.capabilities = types.ServerCapabilities()
    forced = StdioUpstream("true", [], protocol_era="legacy")
    upstreams = {"serving": serving, "plain": plain, "probed": probed_legacy, "forced": forced}
    server = build_proxy_server(upstreams, {})
    assert await install_skills(server, upstreams) is False
    lines = [line for line in capsys.readouterr().err.splitlines() if "skills" in line]
    assert len(lines) == 1
    assert "forced" in lines[0] and "forcé en ère legacy par la config" in lines[0]
    assert await forced.list_skills() == []


# --- Une skill facultative suffit --------------------------------------------------


async def test_optional_skill_alone_publishes_the_capabilities(optional_skills_dir):
    server, served = await _proxy(
        {"fx": InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)})}
    )
    assert served is True
    async def run():
        async with Client(server, mode="auto") as client:
            return client.server_capabilities
    caps = await _unwrapped(run)
    assert caps.extensions == {SKILLS_EXTENSION_ID: {}}
    assert caps.resources is not None


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_skills_list_aggregates_prefixed_entries(optional_skills_dir, mode):
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)})
    server, _ = await _proxy({"bench": InProcessUpstream("mcp_bench"), "fx": fx})
    result = await _request(server, "skills/list", types.PaginatedRequestParams(), mode)
    by_uri = {entry["uri"]: entry for entry in result["skills"]}
    assert set(by_uri) == {"skill://bench/bench/SKILL.md", "skill://fx/tips/SKILL.md"}
    tips = by_uri["skill://fx/tips/SKILL.md"]
    assert tips["frontmatter"] == {"name": "tips", "description": "Astuces facultatives pour ping et pong"}
    assert tips["resources"] == [
        {
            "uri": "skill://fx/tips/SKILL.md",
            "digest": skill_digest(OPTIONAL_SKILL_MD.encode("utf-8")),
            "size": len(OPTIONAL_SKILL_MD.encode("utf-8")),
        },
        {
            "uri": "skill://fx/tips/annexes/detail.md",
            "digest": skill_digest(OPTIONAL_ANNEX.encode("utf-8")),
            "size": len(OPTIONAL_ANNEX.encode("utf-8")),
        },
    ]
    assert (result["resultType"], result["ttlMs"], result["cacheScope"]) == ("complete", 300000, "public")


async def test_skills_get_serves_the_prefixed_entry(optional_skills_dir):
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)})
    server, _ = await _proxy({"fx": fx})
    result = await _request(server, "skills/get", GetSkillParams(uri="skill://fx/tips/SKILL.md"))
    listed = await _request(server, "skills/list", types.PaginatedRequestParams())
    assert result["skill"] == listed["skills"][0]
    assert (result["resultType"], result["ttlMs"], result["cacheScope"]) == ("complete", 300000, "public")


@pytest.mark.parametrize(
    "uri",
    [
        "skill://fx/ghost/SKILL.md",  # upstream connu, skill inconnue
        "skill://fx/tips/annexes/detail.md",  # annexe, pas un SKILL.md
        "skill://ghost/tips/SKILL.md",  # upstream inconnu
        "skill://tips/SKILL.md",  # URI d'origine, non préfixée
    ],
)
async def test_skills_get_of_an_unserved_uri_is_invalid_params(optional_skills_dir, uri):
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)})
    server, _ = await _proxy({"fx": fx})
    with pytest.raises(MCPError) as info:
        await _request(server, "skills/get", GetSkillParams(uri=uri))
    assert info.value.error.code == types.INVALID_PARAMS
    # Le message cite l'URI du CLIENT, jamais l'URI d'origine de l'upstream.
    assert uri in info.value.error.message


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
@pytest.mark.parametrize(
    ("uri", "content"),
    [
        ("skill://fx/tips/SKILL.md", OPTIONAL_SKILL_MD),
        ("skill://fx/tips/annexes/detail.md", OPTIONAL_ANNEX),
    ],
)
async def test_resources_read_relays_the_bytes_under_the_proxy_uri(optional_skills_dir, uri, content, mode):
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)})
    server, _ = await _proxy({"fx": fx})
    [item] = (await _read(server, uri, mode)).contents
    assert str(item.uri) == uri
    assert item.mime_type == "text/markdown"
    # CRLF compris : l'empreinte publiée vaut pour les octets relayés.
    assert item.text == content


@pytest.mark.parametrize(
    "uri",
    [
        "skill://fx/tips/annexes/absent.md",
        "skill://ghost/tips/SKILL.md",
        "skill://tips/SKILL.md",
    ],
)
async def test_resources_read_of_an_unserved_file_is_invalid_params(optional_skills_dir, uri):
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)})
    server, _ = await _proxy({"fx": fx})
    with pytest.raises(MCPError) as info:
        await _read(server, uri)
    assert info.value.error.code == types.INVALID_PARAMS


async def test_resources_list_publishes_every_skill_file(optional_skills_dir):
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)})
    server, _ = await _proxy({"fx": fx, "bench": InProcessUpstream("mcp_bench")})
    async def run():
        async with Client(server, mode="2026-07-28") as client:
            return (await client.list_resources()).resources
    resources = {str(r.uri): r for r in await _unwrapped(run)}
    assert set(resources) == {
        "skill://fx/tips/SKILL.md",
        "skill://fx/tips/annexes/detail.md",
        "skill://bench/bench/SKILL.md",
        "skill://bench/bench/dns.md",
    }
    skill = resources["skill://fx/tips/SKILL.md"]
    assert (skill.name, skill.description, skill.mime_type) == (
        "tips",
        "Astuces facultatives pour ping et pong",
        "text/markdown",
    )


async def test_two_instances_of_one_module_keep_their_own_skills(tmp_path, optional_skills_dir):
    other = tmp_path / "other"
    (other / "solo").mkdir(parents=True)
    (other / "solo" / "SKILL.md").write_text("---\nname: solo\ndescription: Seule\n---\n")
    server, _ = await _proxy(
        {
            "a": InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)}),
            "b": InProcessUpstream(FIXTURE, config={"skills_dir": str(other)}),
        }
    )
    result = await _request(server, "skills/list", types.PaginatedRequestParams())
    assert sorted(e["uri"] for e in result["skills"]) == [
        "skill://a/tips/SKILL.md",
        "skill://b/solo/SKILL.md",
    ]


# --- Ordre réel : le lifespan de build_app ---------------------------------------------


async def test_lifespan_installs_skills_after_upstreams_start(capsys):
    """`build_proxy_server()` précède `start()` : seul le lifespan, après le
    démarrage, sait si une skill est servie. Ce test passe par lui, et non par
    un `install_skills` appelé à la main."""
    from mcp_proxy.app import build_app
    from tests.test_proxy import _run_lifespan

    upstreams = {"bench": InProcessUpstream("mcp_bench")}
    server = build_proxy_server(upstreams, {})
    app = build_app(server, upstreams)
    async with _run_lifespan(app):
        result = await _request(server, "skills/list", types.PaginatedRequestParams())
    assert [e["uri"] for e in result["skills"]] == ["skill://bench/bench/SKILL.md"]
    assert server.extensions == {SKILLS_EXTENSION_ID: {}}
    assert "Extension Skills servie." in capsys.readouterr().err


# --- `_meta` des outils : relais et réécriture ------------------------------------

import json  # noqa: E402

from mcp_base import REQUIRES_SKILL_META_KEY  # noqa: E402
from mcp_proxy.server import ToolCatalogCache, relay_tool_meta  # noqa: E402
from mcp_proxy.skills import build_skills_blocks, format_skills_block  # noqa: E402
from mcp_proxy.server import aggregate_instructions  # noqa: E402
from tests.proxy_client import list_tools  # noqa: E402


def test_relay_rewrites_requires_skill_and_keeps_other_keys():
    meta = {REQUIRES_SKILL_META_KEY: "skill://bench/SKILL.md", "other/key": [1]}
    assert relay_tool_meta("bench", meta) == {
        REQUIRES_SKILL_META_KEY: "skill://bench/bench/SKILL.md",
        "other/key": [1],
    }


@pytest.mark.parametrize("value", ["github://o/r/x/SKILL.md", 42, ["skill://x/SKILL.md"]])
def test_relay_drops_a_requires_skill_it_cannot_prefix(value):
    assert relay_tool_meta("up", {REQUIRES_SKILL_META_KEY: value, "k": "v"}) == {"k": "v"}
    assert relay_tool_meta("up", {REQUIRES_SKILL_META_KEY: value}) is None


def test_relay_of_no_meta_is_none():
    assert relay_tool_meta("up", None) is None
    assert relay_tool_meta("up", {}) is None


async def test_tools_list_relays_requires_skill_on_the_wire():
    upstreams = {"bench": InProcessUpstream("mcp_bench")}
    server, _ = await _proxy(upstreams)
    result = await list_tools(server)
    wire = json.loads(result.model_dump_json(by_alias=True, exclude_none=True))
    metas = {t["name"]: t.get("_meta") for t in wire["tools"] if t["name"] != "read_skill"}
    assert metas and all(
        meta == {REQUIRES_SKILL_META_KEY: "skill://bench/bench/SKILL.md"} for meta in metas.values()
    )


async def test_tools_without_meta_carry_no_meta_key(optional_skills_dir):
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)})
    server, _ = await _proxy({"fx": fx})
    wire = json.loads((await list_tools(server)).model_dump_json(by_alias=True, exclude_none=True))
    assert all("_meta" not in tool for tool in wire["tools"] if tool["name"] != "read_skill")


# --- Cache d'outils ------------------------------------------------------------------


def test_catalog_remembers_the_tool_meta(tmp_path):
    cat = ToolCatalogCache(tmp_path / "c.json")
    tool = types.Tool(
        name="echo",
        description="d",
        input_schema={"type": "object"},
        **{"_meta": {REQUIRES_SKILL_META_KEY: "skill://bench/SKILL.md"}},
    )
    cat.remember("remote", [tool, types.Tool(name="bare", input_schema={"type": "object"})])
    tools, _ = cat.recall("remote")
    assert tools[0].meta == {REQUIRES_SKILL_META_KEY: "skill://bench/SKILL.md"}
    assert tools[1].meta is None
    assert "_meta" not in json.loads((tmp_path / "c.json").read_text())["remote"]["tools"][1]


@pytest.mark.parametrize("meta", [None, "pas un objet", ["liste"]])
def test_catalog_reads_entries_without_a_usable_meta(tmp_path, meta):
    """Ancienne forme (pas de clé) ou valeur inattendue : l'outil reste servi."""
    raw = {"name": "echo", "description": "d", "inputSchema": {}}
    if meta is not None:
        raw["_meta"] = meta
    (tmp_path / "c.json").write_text(json.dumps({"remote": {"known_at": 1.0, "tools": [raw]}}))
    [tool], _ = ToolCatalogCache(tmp_path / "c.json").recall("remote")
    assert tool.name == "echo" and tool.meta is None


async def test_stale_tool_keeps_its_requires_skill(tmp_path):
    """Un upstream non autorisé resservi depuis le cache garde sa déclaration."""
    cat = ToolCatalogCache(tmp_path / "c.json")
    cat.remember(
        "remote",
        [
            types.Tool(
                name="search",
                input_schema={"type": "object"},
                **{"_meta": {REQUIRES_SKILL_META_KEY: "skill://splunk/SKILL.md"}},
            )
        ],
    )

    class _Pending:
        authorization_pending = True
        last_error = None

    server = build_proxy_server(
        {"remote": HttpUpstream("https://example.test/mcp")},
        {},
        authorizers={"remote": _Pending()},
        catalog=cat,
    )
    tools = {t.name: t for t in (await list_tools(server)).tools}
    assert tools["remote__search"].meta == {REQUIRES_SKILL_META_KEY: "skill://remote/splunk/SKILL.md"}


# --- Bloc généré dans les instructions ---------------------------------------------------

BENCH_BLOCK = (
    "Skills MCP servies par `bench` — pas des skills locales : leur nom ne "
    "suffit pas, elles se lisent par leur URI complète :\n"
    "- `bench` (skill://bench/bench/SKILL.md), obligatoire avant tout appel d'un "
    "outil `bench__…` : Règle de restitution à appliquer après tout usage d'un outil "
    "`bench`, banc d'essai du développement de MIAOU."
)


async def test_bench_block_is_the_validated_text():
    upstreams = {"bench": InProcessUpstream("mcp_bench")}
    server, _ = await _proxy(upstreams)
    assert await build_skills_blocks(upstreams) == {"bench": BENCH_BLOCK}


def _entry(name: str, description: str) -> dict:
    return {
        "uri": f"skill://{name}/SKILL.md",
        "frontmatter": {"name": name, "description": description},
        "resources": [],
    }


def test_block_lines_for_mandatory_partial_and_optional_skills():
    block = format_skills_block(
        "fx",
        [_entry("rules", "Règles d'usage de ping"), _entry("tips", "Astuces\n  sur deux lignes")],
        {"skill://rules/SKILL.md": ["fx__ping", "fx__pong"]},
        tool_count=3,
    )
    assert block == (
        "Skills MCP servies par `fx` — pas des skills locales : leur nom ne "
        "suffit pas, elles se lisent par leur URI complète :\n"
        "- `rules` (skill://fx/rules/SKILL.md), obligatoire avant tout appel de "
        "`fx__ping`, `fx__pong` : Règles d'usage de ping\n"
        "- `tips` (skill://fx/tips/SKILL.md), facultative : Astuces sur deux lignes"
    )


def test_block_says_every_tool_only_when_every_tool_requires_it():
    block = format_skills_block("fx", [_entry("rules", "R")], {"skill://rules/SKILL.md": ["fx__ping"]}, 1)
    assert "obligatoire avant tout appel d'un outil `fx__…` : R" in block


async def test_optional_skill_only_upstream_gets_a_section(optional_skills_dir):
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)})
    upstreams = {"fx": fx}
    await _proxy(upstreams)
    assert fx.instructions is None
    text = aggregate_instructions(upstreams, await build_skills_blocks(upstreams))
    section = text.split("## fx\n\n", 1)[1]
    assert section == (
        "Skills MCP servies par `fx` — pas des skills locales : leur nom ne "
        "suffit pas, elles se lisent par leur URI complète :\n"
        "- `tips` (skill://fx/tips/SKILL.md), facultative : "
        "Astuces facultatives pour ping et pong"
    )


async def test_block_follows_the_free_text_of_the_section():
    upstreams = {"bench": InProcessUpstream("mcp_bench")}
    await _proxy(upstreams)
    text = aggregate_instructions(upstreams, await build_skills_blocks(upstreams))
    section = text.split("## bench\n\n", 1)[1]
    assert section == upstreams["bench"].instructions.strip() + "\n\n" + BENCH_BLOCK


async def test_requires_skill_of_an_unserved_skill_is_logged_and_relayed(tmp_path, capsys):
    """Le module refuse ce cas à la construction ; un upstream tiers peut
    l'avoir. On le simule en retirant la skill APRÈS la construction."""
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "SKILL.md").write_text("---\nname: rules\ndescription: R\n---\n")
    (tmp_path / "tips").mkdir()
    (tmp_path / "tips" / "SKILL.md").write_text("---\nname: tips\ndescription: T\n---\n")
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(tmp_path), "requires_skill": {"ping": "rules"}})
    upstreams = {"fx": fx}
    server, _ = await _proxy(upstreams)
    skills = fx._skills()
    skills.sources = [s for s in skills.sources if s.name != "rules"]
    blocks = await build_skills_blocks(upstreams)
    assert "rules" not in blocks["fx"]
    assert "fx           skill exigée mais non servie par 'fx' : skill://rules/SKILL.md (1 outil)" in (
        capsys.readouterr().err
    )
    tools = {t.name: t for t in (await list_tools(server)).tools}
    assert tools["fx__ping"].meta == {REQUIRES_SKILL_META_KEY: "skill://fx/rules/SKILL.md"}


async def test_lifespan_publishes_the_block_in_instructions():
    from mcp_proxy.app import build_app
    from tests.test_proxy import _run_lifespan

    upstreams = {"bench": InProcessUpstream("mcp_bench")}
    server = build_proxy_server(upstreams, {})
    app = build_app(server, upstreams)
    async with _run_lifespan(app):
        assert server.create_initialization_options().instructions.endswith(BENCH_BLOCK)


# --- Outil de repli `read_skill` -----------------------------------------------------

from mcp_proxy.skills import SKILLS_FALLBACK_META_KEY  # noqa: E402
from tests.proxy_client import call_tool  # noqa: E402


async def _tool_names(server) -> list[str]:
    return [t.name for t in (await list_tools(server)).tools]


async def test_fallback_is_absent_without_skills():
    server, _ = await _proxy({"fx": InProcessUpstream(FIXTURE, config={})})
    assert "read_skill" not in await _tool_names(server)
    result = await call_tool(server, "read_skill", {"uri": "skill://fx/x/SKILL.md"})
    assert result.is_error  # nom inconnu, comme avant
    assert "Outil inconnu" in result.content[0].text


async def test_fallback_is_listed_bare_and_marked_when_a_skill_is_served(optional_skills_dir):
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)})
    server, _ = await _proxy({"fx": fx})
    tools = {t.name: t for t in (await list_tools(server)).tools}
    tool = tools["read_skill"]
    assert tool.meta == {SKILLS_FALLBACK_META_KEY: True}
    assert tool.input_schema["required"] == ["uri"]


async def test_fallback_appears_through_the_lifespan_only():
    """Construit avant start(), le serveur ne sait pas encore s'il servira des
    skills : le repli doit se décider à l'appel, pas à la construction."""
    from mcp_proxy.app import build_app
    from tests.test_proxy import _run_lifespan

    upstreams = {"bench": InProcessUpstream("mcp_bench")}
    server = build_proxy_server(upstreams, {})
    app = build_app(server, upstreams)
    async with _run_lifespan(app):
        assert "read_skill" in await _tool_names(server)


async def test_fallback_reads_bench_skill_labelled_with_its_origin():
    server, _ = await _proxy({"bench": InProcessUpstream("mcp_bench")})
    result = await call_tool(server, "read_skill", {"uri": "skill://bench/bench/SKILL.md"})
    assert not result.is_error
    [block] = result.content
    expected = (Path(__file__).parent.parent / "servers/skills/bench/bench/SKILL.md").read_text()
    assert block.text == (
        "[Fichier de skill servi par le serveur MCP `bench` — skill://bench/bench/SKILL.md]\n\n"
        + expected
        + "\n\n[Autres fichiers de cette skill, lisibles par leur URI : "
        "skill://bench/bench/dns.md]"
    )


async def test_fallback_lists_the_other_files_after_a_skill_md(optional_skills_dir):
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)})
    server, _ = await _proxy({"fx": fx})
    result = await call_tool(server, "read_skill", {"uri": "skill://fx/tips/SKILL.md"})
    assert result.content[0].text.endswith(
        "\n\n[Autres fichiers de cette skill, lisibles par leur URI : "
        "skill://fx/tips/annexes/detail.md]"
    )


async def test_fallback_reads_an_annex_without_the_files_note(optional_skills_dir):
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)})
    server, _ = await _proxy({"fx": fx})
    result = await call_tool(server, "read_skill", {"uri": "skill://fx/tips/annexes/detail.md"})
    assert result.content[0].text == (
        "[Fichier de skill servi par le serveur MCP `fx` — skill://fx/tips/annexes/detail.md]\n\n"
        + OPTIONAL_ANNEX
    )


async def test_fallback_returns_a_binary_file_as_an_embedded_blob(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "SKILL.md").write_text("---\nname: bin\ndescription: B\n---\n")
    (tmp_path / "bin" / "logo.png").write_bytes(b"\x89PNG\xff\x00")
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(tmp_path)})
    server, _ = await _proxy({"fx": fx})
    result = await call_tool(server, "read_skill", {"uri": "skill://fx/bin/logo.png"})
    label, embedded = result.content
    assert label.text == "[Fichier de skill servi par le serveur MCP `fx` — skill://fx/bin/logo.png]"
    assert str(embedded.resource.uri) == "skill://fx/bin/logo.png"
    assert embedded.resource.mime_type == "image/png"


@pytest.mark.parametrize(
    "arguments",
    [
        {"uri": "skill://fx/tips/annexes/absent.md"},
        {"uri": "skill://ghost/tips/SKILL.md"},
        {"uri": "skill://tips/SKILL.md"},
        {"uri": "file:///etc/passwd"},
        {"uri": "https://example.test/SKILL.md"},
    ],
)
async def test_fallback_refuses_any_other_uri(optional_skills_dir, arguments):
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)})
    server, _ = await _proxy({"fx": fx})
    result = await call_tool(server, "read_skill", arguments)
    assert result.is_error
    assert result.content[0].text.startswith(f"Aucun fichier de skill servi à {arguments['uri']}.")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "`uri` manquant"),
        ({"uri": ""}, "`uri` manquant"),
        ({"uri": 3}, "`uri` manquant"),
        ({"uri": "skill://fx/tips/SKILL.md", "page": 2}, "Argument(s) inconnu(s) : page"),
    ],
)
async def test_fallback_rejects_malformed_arguments(optional_skills_dir, arguments, message):
    fx = InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)})
    server, _ = await _proxy({"fx": fx})
    result = await call_tool(server, "read_skill", arguments)
    assert result.is_error
    assert message in result.content[0].text


# --- Isolation des pannes d'upstream ------------------------------------------------


class _BrokenUpstream(InProcessUpstream):
    """Upstream dont le listage échoue, comme un upstream distant mort."""

    def __init__(self, *, skills: bool = True, tools: bool = True) -> None:
        super().__init__(FIXTURE, config={})
        self._skills_fail, self._tools_fail = skills, tools

    async def list_skills(self):
        if self._skills_fail:
            raise MCPError(-32000, "Connection closed")
        return []

    async def list_tools(self):
        if self._tools_fail:
            raise MCPError(-32000, "Connection closed")
        return await super().list_tools()


async def test_a_failing_upstream_is_omitted_from_skills_listings(optional_skills_dir, capsys):
    """`skills/list` et `resources/list` répondent pour les upstreams sains ;
    celui dont le listage échoue est omis et signalé."""
    upstreams = {
        "broken": _BrokenUpstream(),
        "fx": InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)}),
    }
    server, served = await _proxy(upstreams)
    assert served is True
    listed = await _request(server, "skills/list", types.PaginatedRequestParams())
    assert [e["uri"] for e in listed["skills"]] == ["skill://fx/tips/SKILL.md"]
    resources = await _request(server, "resources/list", types.PaginatedRequestParams())
    assert len(resources["resources"]) == 2
    assert "Skills de 'broken' non listées (MCPError: Connection closed)" in capsys.readouterr().err


async def test_a_failing_upstream_loses_only_its_own_skills_block(optional_skills_dir, capsys):
    upstreams = {
        "broken": _BrokenUpstream(skills=False),
        "fx": InProcessUpstream(FIXTURE, config={"skills_dir": str(optional_skills_dir)}),
    }
    for upstream in upstreams.values():
        await upstream.start()
    blocks = await build_skills_blocks(upstreams)
    assert list(blocks) == ["fx"]
    assert "Bloc des skills de 'broken' non généré" in capsys.readouterr().err
