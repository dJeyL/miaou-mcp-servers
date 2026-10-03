"""Extension Skills servie par mcp_base (`Skills`, `skills_dir`, `requires_skill`).

Vecteurs : l'exemple de la spec (specification/stable/skills.mdx du dépôt
modelcontextprotocol/ext-skills), dont le contenu, les tailles et les empreintes
des fichiers sont donnés — les mêmes vecteurs servent au SHA-256 côté MIAOU.
"""

import base64
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

import mcp.types as types
from mcp.client import Client
from mcp.shared.exceptions import MCPError

import mcp_base
from mcp_base import (
    REQUIRES_SKILL_META_KEY,
    SKILLS_EXTENSION_ID,
    MiaouMCPBase,
    SkillError,
    Skills,
    build_skill_entry,
    find_skills_extension,
    parse_skill_frontmatter,
    scan_skills_dir,
    skill_digest,
)

from tests.proxy_client import _BaseExceptionGroup, _single_cause

# --- Exemple de la spec, à l'octet ------------------------------------------

SPEC_SKILL_MD = (
    "---\nname: pdf-processing\ndescription: Extract, fill, and assemble PDF documents\n"
    "---\n\n# PDF processing\n\nChoose the matching template from `templates/`.\n"
)
SPEC_INVOICE = "# Invoice\n\nCustomer:\nAmount:\n"
SPEC_PURCHASE_ORDER = "# Purchase order\n\nSupplier:\nItems:\n"
SPEC_CREDIT_NOTE = "# Credit note\n\nInvoice:\nCredit amount:\n"

SPEC_ENTRY = {
    "uri": "skill://pdf-processing/SKILL.md",
    "frontmatter": {
        "name": "pdf-processing",
        "description": "Extract, fill, and assemble PDF documents",
    },
    "resources": [
        {
            "uri": "skill://pdf-processing/SKILL.md",
            "digest": "sha256:99b737495721155ece826d57521e2d66141ebdc1344a400487481ea2642ab19e",
            "size": 151,
        },
        {
            "uri": "skill://pdf-processing/templates/invoice.md",
            "digest": "sha256:61f4ea6d2c75fde1b4977219e7e3107d491c3c26aefb6686e84d6281c088d9ee",
            "size": 29,
        },
        {
            "uri": "skill://pdf-processing/templates/purchase-order.md",
            "digest": "sha256:f2ff774b1737ff3dec81c47946f9976f18a1a9f69dda0a81f22eabd95173c158",
            "size": 35,
        },
    ],
}


def write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_bytes(content.encode("utf-8"))


def skill_md(name: str, description: str = "Fait une chose précise", extra: str = "") -> str:
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n# {name}\n"


@pytest.fixture
def spec_skills_dir(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    write(root / "pdf-processing" / "SKILL.md", SPEC_SKILL_MD)
    write(root / "pdf-processing" / "templates" / "invoice.md", SPEC_INVOICE)
    write(root / "pdf-processing" / "templates" / "purchase-order.md", SPEC_PURCHASE_ORDER)
    return root


class _Server(MiaouMCPBase):
    def __init__(self, skills_dir=None, requires_skill=None) -> None:
        super().__init__("skills-test", default_port=0, skills_dir=skills_dir)

        @self.mcp.tool()
        async def one() -> str:
            """Premier outil."""
            return "1"

        @self.mcp.tool()
        async def two() -> str:
            """Second outil."""
            return "2"

        self.finalize_tools(requires_skill=requires_skill)


async def _request(server: Any, method: str, params: Any, mode: str = "2026-07-28") -> dict:
    try:
        async with Client(server, mode=mode) as client:
            request = types.Request[Any, str](method=method, params=params)
            return await client.session.send_request(request, TypeAdapter(dict[str, Any]))
    except _BaseExceptionGroup as group:
        cause = _single_cause(group)
        if cause is None:
            raise
        raise cause from None


# --- Empreintes, entrée ------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "digest", "size"),
    [
        (SPEC_SKILL_MD, "sha256:99b737495721155ece826d57521e2d66141ebdc1344a400487481ea2642ab19e", 151),
        (SPEC_INVOICE, "sha256:61f4ea6d2c75fde1b4977219e7e3107d491c3c26aefb6686e84d6281c088d9ee", 29),
        (SPEC_PURCHASE_ORDER, "sha256:f2ff774b1737ff3dec81c47946f9976f18a1a9f69dda0a81f22eabd95173c158", 35),
        (SPEC_CREDIT_NOTE, "sha256:766bffa8d4908ce897e1727ca22e4bf0fa796620b35218ce2132a124545626f1", 39),
    ],
)
def test_digest_matches_spec_vectors(content, digest, size):
    data = content.encode("utf-8")
    assert skill_digest(data) == digest
    assert len(data) == size


def test_entry_of_spec_example_is_the_spec_entry(spec_skills_dir):
    [source] = scan_skills_dir(spec_skills_dir)
    assert build_skill_entry(source) == SPEC_ENTRY


def test_frontmatter_is_verbatim_not_a_selection():
    text = (
        "---\nname: x\ndescription: d\nlicense: Apache-2.0\nversion: 1.0\n"
        "metadata:\n  team: ops\n  tags: [a, b]\nallowed-tools: Bash\n---\nbody\n"
    )
    assert parse_skill_frontmatter(text) == {
        "name": "x",
        "description": "d",
        "license": "Apache-2.0",
        "version": 1.0,
        "metadata": {"team": "ops", "tags": ["a", "b"]},
        "allowed-tools": "Bash",
    }


def test_hidden_files_are_not_skill_content(spec_skills_dir):
    write(spec_skills_dir / "pdf-processing" / ".DS_Store", b"\x00\x01")
    write(spec_skills_dir / "pdf-processing" / ".git" / "HEAD", "ref")
    [source] = scan_skills_dir(spec_skills_dir)
    assert build_skill_entry(source) == SPEC_ENTRY


def test_folder_without_skill_md_is_not_a_skill(spec_skills_dir):
    write(spec_skills_dir / "notes" / "README.md", "rien")
    assert [s.name for s in scan_skills_dir(spec_skills_dir)] == ["pdf-processing"]


# --- Refus au démarrage, cause nommée -----------------------------------------


@pytest.mark.parametrize(
    ("folder", "content", "cause"),
    [
        ("Bad_Name", skill_md("Bad_Name"), "invalide"),
        ("-edge", skill_md("-edge"), "invalide"),
        ("dou--ble", skill_md("dou--ble"), "invalide"),
        ("a" * 65, skill_md("a" * 65), "dépasse 64"),
        ("mismatch", skill_md("other"), "différent du dossier"),
        ("nodesc", "---\nname: nodesc\n---\nbody\n", "`description` absente ou vide"),
        ("emptydesc", "---\nname: emptydesc\ndescription: '  '\n---\n", "`description` absente ou vide"),
        ("longdesc", skill_md("longdesc", "x" * 1025), "`description` dépasse 1024"),
        ("dated", skill_md("dated", extra="updated: 2026-10-03\n"), "champ `updated`"),
        ("nofm", "# pas de frontmatter\n", "ne commence pas par un frontmatter"),
        ("open", "---\nname: open\ndescription: d\n", "non refermé"),
        ("notmap", "---\n- a\n- b\n---\n", "pas un objet YAML"),
        ("badyaml", "---\nname: [unclosed\n---\n", "YAML illisible"),
    ],
)
def test_invalid_skill_is_refused_with_its_cause(tmp_path, folder, content, cause):
    write(tmp_path / folder / "SKILL.md", content)
    with pytest.raises(SkillError) as info:
        scan_skills_dir(tmp_path)
    assert f"skill « {folder} »" in str(info.value)
    assert cause in str(info.value)


def test_file_path_outside_uri_safe_characters_is_refused(spec_skills_dir):
    write(spec_skills_dir / "pdf-processing" / "templates" / "credit note.md", SPEC_CREDIT_NOTE)
    with pytest.raises(SkillError, match="credit note.md"):
        scan_skills_dir(spec_skills_dir)


def test_file_count_limit_is_enforced(spec_skills_dir, monkeypatch):
    monkeypatch.setattr(mcp_base, "SKILL_MAX_FILES", 2)
    with pytest.raises(SkillError, match="3 fichiers, au-delà de la borne de 2"):
        scan_skills_dir(spec_skills_dir)


def test_total_size_limit_is_enforced(spec_skills_dir, monkeypatch):
    monkeypatch.setattr(mcp_base, "SKILL_MAX_TOTAL_BYTES", 214)  # 151 + 29 + 35 = 215
    with pytest.raises(SkillError, match="215 octets, au-delà de la borne de 214"):
        scan_skills_dir(spec_skills_dir)


def test_server_construction_fails_on_invalid_skill(tmp_path):
    write(tmp_path / "x" / "SKILL.md", skill_md("y"))
    with pytest.raises(SkillError, match="skill « x »"):
        _Server(skills_dir=tmp_path)


def test_missing_skills_dir_is_refused(tmp_path):
    with pytest.raises(SkillError, match="introuvable"):
        _Server(skills_dir=tmp_path / "absent")


# --- Service sur le fil --------------------------------------------------------


async def test_no_skills_dir_publishes_no_extension():
    server = _Server()
    assert server.skills is None
    assert find_skills_extension(server.mcp) is None
    async with Client(server.mcp, mode="auto") as client:
        assert not (client.server_capabilities.extensions or {})


async def test_extension_and_resources_capabilities_are_published(spec_skills_dir):
    server = _Server(skills_dir=spec_skills_dir)
    # `auto` et non une version épinglée : épinglée, le client synthétise un
    # `server/discover` au lieu de le demander, et ne voit aucune capacité.
    async with Client(server.mcp, mode="auto") as client:
        caps = client.server_capabilities
        assert caps.extensions == {SKILLS_EXTENSION_ID: {}}
        assert caps.resources is not None


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_skills_list_serves_the_spec_entry(spec_skills_dir, mode):
    server = _Server(skills_dir=spec_skills_dir)
    result = await _request(server.mcp, "skills/list", types.PaginatedRequestParams(), mode)
    assert result["skills"] == [SPEC_ENTRY]
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == 300000
    assert result["cacheScope"] == "public"
    assert "nextCursor" not in result


async def test_skills_get_serves_the_spec_entry(spec_skills_dir):
    server = _Server(skills_dir=spec_skills_dir)
    result = await _request(
        server.mcp, "skills/get", mcp_base.GetSkillParams(uri=SPEC_ENTRY["uri"])
    )
    assert result["skill"] == SPEC_ENTRY
    assert (result["resultType"], result["ttlMs"], result["cacheScope"]) == ("complete", 300000, "public")


@pytest.mark.parametrize(
    "uri", ["skill://chargebacks/SKILL.md", "skill://pdf-processing/templates/invoice.md"]
)
async def test_skills_get_of_an_unserved_uri_is_invalid_params(spec_skills_dir, uri):
    server = _Server(skills_dir=spec_skills_dir)
    with pytest.raises(MCPError) as info:
        await _request(server.mcp, "skills/get", mcp_base.GetSkillParams(uri=uri))
    assert info.value.error.code == types.INVALID_PARAMS
    assert uri in info.value.error.message


@pytest.mark.parametrize(
    ("uri", "content"),
    [
        ("skill://pdf-processing/SKILL.md", SPEC_SKILL_MD),
        ("skill://pdf-processing/templates/invoice.md", SPEC_INVOICE),
        ("skill://pdf-processing/templates/purchase-order.md", SPEC_PURCHASE_ORDER),
    ],
)
async def test_every_listed_file_reads_back_as_its_digest(spec_skills_dir, uri, content):
    server = _Server(skills_dir=spec_skills_dir)
    async with Client(server.mcp, mode="2026-07-28") as client:
        result = await client.read_resource(uri)
    [item] = result.contents
    assert item.mime_type == "text/markdown"
    assert item.text == content
    expected = next(r for r in SPEC_ENTRY["resources"] if r["uri"] == uri)
    assert skill_digest(item.text.encode("utf-8")) == expected["digest"]


async def test_read_of_an_unserved_file_is_invalid_params(spec_skills_dir):
    server = _Server(skills_dir=spec_skills_dir)
    with pytest.raises(MCPError) as info:
        try:
            async with Client(server.mcp, mode="2026-07-28") as client:
                await client.read_resource("skill://pdf-processing/templates/credit-note.md")
        except _BaseExceptionGroup as group:
            raise _single_cause(group) from None
    assert info.value.error.code == types.INVALID_PARAMS


async def test_bytes_are_served_intact_bom_and_crlf_included(tmp_path):
    raw = "﻿---\r\nname: crlf\r\ndescription: d\r\n---\r\nbody\r\n".encode("utf-8")
    # Le BOM précède le `---` : le frontmatter ne serait pas reconnu. On le met
    # sur une annexe, et on garde le CRLF sur SKILL.md.
    write(tmp_path / "crlf" / "SKILL.md", raw.removeprefix("﻿".encode("utf-8")))
    write(tmp_path / "crlf" / "annex.md", raw)
    write(tmp_path / "crlf" / "blob.bin", b"\xff\xfe\x00binaire")
    server = _Server(skills_dir=tmp_path)
    entry = server.skills.get_entry("skill://crlf/SKILL.md")
    digests = {r["uri"]: r["digest"] for r in entry["resources"]}
    async with Client(server.mcp, mode="2026-07-28") as client:
        for uri in digests:
            [item] = (await client.read_resource(uri)).contents
            if isinstance(item, types.BlobResourceContents):
                data = base64.b64decode(item.blob)
            else:
                data = item.text.encode("utf-8")
            assert skill_digest(data) == digests[uri], uri


async def test_change_on_disk_is_served_without_restart(spec_skills_dir):
    server = _Server(skills_dir=spec_skills_dir)
    changed = SPEC_SKILL_MD.replace("Extract, fill", "Extract")
    write(spec_skills_dir / "pdf-processing" / "SKILL.md", changed)
    entry = server.skills.get_entry(SPEC_ENTRY["uri"])
    assert entry["frontmatter"]["description"] == "Extract, and assemble PDF documents"
    assert entry["resources"][0]["digest"] == skill_digest(changed.encode("utf-8"))
    async with Client(server.mcp, mode="2026-07-28") as client:
        [item] = (await client.read_resource(SPEC_ENTRY["uri"])).contents
    assert item.text == changed


async def test_skill_md_resource_carries_name_and_description(spec_skills_dir):
    server = _Server(skills_dir=spec_skills_dir)
    async with Client(server.mcp, mode="2026-07-28") as client:
        listed = {r.uri: r for r in (await client.list_resources()).resources}
    skill = listed["skill://pdf-processing/SKILL.md"]
    assert (skill.name, skill.description) == ("pdf-processing", SPEC_ENTRY["frontmatter"]["description"])
    assert set(listed) == {r["uri"] for r in SPEC_ENTRY["resources"]}


# --- requires_skill -------------------------------------------------------------


async def test_requires_skill_by_name_applies_to_every_tool(spec_skills_dir):
    server = _Server(skills_dir=spec_skills_dir, requires_skill="pdf-processing")
    tools = await server.mcp.list_tools()
    assert {t.name: (t.meta or {}).get(REQUIRES_SKILL_META_KEY) for t in tools} == {
        "one": SPEC_ENTRY["uri"],
        "two": SPEC_ENTRY["uri"],
    }


async def test_requires_skill_by_tool_applies_to_those_only(spec_skills_dir):
    server = _Server(skills_dir=spec_skills_dir, requires_skill={"two": "pdf-processing"})
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert not tools["one"].meta
    assert tools["two"].meta == {REQUIRES_SKILL_META_KEY: SPEC_ENTRY["uri"]}


async def test_requires_skill_reaches_the_wire(spec_skills_dir):
    server = _Server(skills_dir=spec_skills_dir, requires_skill="pdf-processing")
    async with Client(server.mcp, mode="legacy") as client:
        tools = (await client.list_tools()).tools
    assert all(t.meta == {REQUIRES_SKILL_META_KEY: SPEC_ENTRY["uri"]} for t in tools)


def test_requires_skill_of_an_unserved_skill_is_refused(spec_skills_dir):
    with pytest.raises(ValueError, match="« ghost », que ce serveur ne sert pas"):
        _Server(skills_dir=spec_skills_dir, requires_skill="ghost")


def test_requires_skill_without_skills_dir_is_refused():
    with pytest.raises(ValueError, match="que ce serveur ne sert pas"):
        _Server(requires_skill="pdf-processing")


def test_requires_skill_of_an_unknown_tool_is_refused(spec_skills_dir):
    with pytest.raises(ValueError, match="outil inconnu « three »"):
        _Server(skills_dir=spec_skills_dir, requires_skill={"three": "pdf-processing"})


# --- Accès à l'extension depuis le proxy -------------------------------------------


def test_find_skills_extension_reads_the_sdk_private_attribute(spec_skills_dir):
    """Garde du seul accès à un attribut privé du SDK (`MCPServer._extensions`) :
    s'il est renommé, ce test casse au lieu que le proxy cesse en silence de
    voir les skills de ses upstreams."""
    server = _Server(skills_dir=spec_skills_dir)
    assert isinstance(server.mcp._extensions, list)
    found = find_skills_extension(server.mcp)
    assert isinstance(found, Skills)
    assert found is server.skills
