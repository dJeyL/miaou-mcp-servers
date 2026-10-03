"""Le proxy relaie les skills de ses upstreams stdio et http.

Vrais handshakes, sans réseau, comme `test_proxy_era.py` dont l'outillage est
repris : `skills_fixture_server.py` (extension Skills de `mcp_base`) servi en
subprocess stdio et en app streamable-http sur `httpx2.ASGITransport`. Le relais
n'a lieu qu'en ère moderne ET si l'upstream déclare l'extension : un upstream
legacy, constaté ou forcé, ne reçoit aucune requête de skills.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest

import mcp_proxy  # noqa: F401  (ajoute servers/ à sys.path)
import mcp.types as types
from mcp.shared.exceptions import MCPError

from mcp_base import SKILLS_EXTENSION_ID, skill_digest
from mcp_proxy.contract import AuthorizationRequired
from mcp_proxy.server import build_proxy_server
from mcp_proxy.skills import install_skills
from mcp_proxy.upstream import StdioUpstream
from tests import skills_fixture_server
from tests.test_proxy_era import (
    LEGACY,
    MODERN,
    TESTS,
    _app,
    _hosted,
    _http_upstream,
    _legacy_only,
    _received,
    _Recorder,
    _stdio_legacy,
)
from tests.test_proxy_skills import _read, _request

SKILL_MD = "---\r\nname: tips\r\ndescription: Astuces 🐈 pour ping\r\n---\r\n\r\nVoir `notes.md`.\r\n"
NOTES = "﻿# Notes\r\n\r\nLigne CRLF.\r\n"
LOGO = bytes(range(256))

SKILL_METHODS = {"skills/list", "skills/get", "resources/list", "resources/read"}


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "tips").mkdir(parents=True)
    (root / "tips" / "SKILL.md").write_bytes(SKILL_MD.encode("utf-8"))
    (root / "tips" / "notes.md").write_bytes(NOTES.encode("utf-8"))
    (root / "tips" / "logo.bin").write_bytes(LOGO)
    return root


def _stdio_skills(skills_dir: Path, protocol: str = "auto") -> StdioUpstream:
    return StdioUpstream(
        command=sys.executable,
        args=[str(TESTS / "skills_fixture_server.py"), str(skills_dir), "tips"],
        protocol=protocol,
    )


def _skills_server(skills_dir: Path) -> tuple[Any, Any]:
    """Le serveur de fixture et son app streamable-http — construite AVANT
    `session_manager.run()`, que le SDK ne crée qu'avec elle."""
    fixture = skills_fixture_server.build({"skills_dir": str(skills_dir), "requires_skill": "tips"})
    return fixture, _app(fixture)


async def _proxy_over(upstreams: dict[str, Any]) -> tuple[Any, bool]:
    """Ordre réel : construction AVANT le démarrage (déjà fait par l'appelant),
    puis `install_skills`, comme le lifespan."""
    server = build_proxy_server(upstreams, {})
    return server, await install_skills(server, upstreams)


async def _assert_relayed(server: Any, name: str) -> None:
    """Ce que le client du proxy voit d'une skill relayée : entrée préfixée,
    empreintes de l'upstream intactes, octets intacts (CRLF, BOM, binaire)."""
    listed = await _request(server, "skills/list", types.PaginatedRequestParams())
    [entry] = listed["skills"]
    assert entry["uri"] == f"skill://{name}/tips/SKILL.md"
    by_uri = {r["uri"]: r for r in entry["resources"]}
    assert set(by_uri) == {
        f"skill://{name}/tips/SKILL.md",
        f"skill://{name}/tips/notes.md",
        f"skill://{name}/tips/logo.bin",
    }
    got = await _request(server, "skills/get", {"uri": entry["uri"]})
    assert got["skill"]["uri"] == entry["uri"]

    expected = {"SKILL.md": SKILL_MD.encode(), "notes.md": NOTES.encode(), "logo.bin": LOGO}
    for file_name, raw in expected.items():
        uri = f"skill://{name}/tips/{file_name}"
        [content] = (await _read(server, uri)).contents
        assert content.uri == uri
        data = (
            content.text.encode("utf-8")
            if isinstance(content, types.TextResourceContents)
            else base64.b64decode(content.blob)
        )
        assert data == raw
        assert by_uri[uri]["digest"] == skill_digest(raw)

    with pytest.raises(MCPError) as info:
        await _request(server, "skills/get", {"uri": f"skill://{name}/ghost/SKILL.md"})
    assert info.value.error.code == types.INVALID_PARAMS
    assert f"skill://{name}/ghost/SKILL.md" in info.value.error.message


# --- Ère moderne, extension déclarée : relayé ----------------------------------------


async def test_stdio_upstream_skills_are_relayed(skills_dir):
    upstream = _stdio_skills(skills_dir)
    try:
        await upstream.start()
        assert upstream.serves_skills is True
        server, served = await _proxy_over({"st": upstream})
        assert served is True
        await _assert_relayed(server, "st")
    finally:
        await upstream.stop()


async def test_http_upstream_skills_are_relayed_with_mcp_name_on_reads(skills_dir):
    """`resources/read` part avec `Mcp-Name` (posé par le SDK), `skills/*` sans."""
    fixture, app = _skills_server(skills_dir)
    recorder = _Recorder()
    async with fixture.session_manager.run():
        async with _hosted(_http_upstream(app, recorder)) as upstream:
            await upstream.start()
            assert upstream.serves_skills is True
            server, served = await _proxy_over({"ht": upstream})
            assert served is True
            await _assert_relayed(server, "ht")
    reads = [r for r in recorder.requests if r["method"] == "resources/read"]
    assert reads and all(r["headers"]["mcp-name"] == r["body"]["params"]["uri"] for r in reads)
    assert all(
        "mcp-name" not in r["headers"] for r in recorder.requests if r["method"] in ("skills/list", "skills/get")
    )


async def test_relayed_requires_skill_designates_a_served_skill(skills_dir, capsys):
    """Le `_meta` de l'outil est réécrit vers une skill que le proxy sert
    désormais : plus de ligne « skill non servie » au journal."""
    from mcp_proxy.skills import build_skills_blocks
    from tests.proxy_client import list_tools

    upstream = _stdio_skills(skills_dir)
    try:
        await upstream.start()
        server, _ = await _proxy_over({"st": upstream})
        tools = {t.name: t for t in (await list_tools(server)).tools}
        assert tools["st__ping"].meta == {"miaou/requiresSkill": "skill://st/tips/SKILL.md"}
        blocks = await build_skills_blocks({"st": upstream})
    finally:
        await upstream.stop()
    assert "(skill://st/tips/SKILL.md), obligatoire avant tout appel d'un outil `st__…`" in blocks["st"]
    assert "skill non servie" not in capsys.readouterr().err


# --- Legacy, constaté ou forcé : aucune requête de skills ------------------------------


async def test_forced_legacy_http_upstream_receives_no_skills_request(skills_dir):
    """Un 2.x forcé en legacy RÉPONDRAIT à `skills/list` (mesuré) : seule la
    condition d'appel l'en protège."""
    fixture, app = _skills_server(skills_dir)
    recorder = _Recorder()
    async with fixture.session_manager.run():
        async with _hosted(_http_upstream(app, recorder, protocol="legacy")) as upstream:
            await upstream.start()
            assert upstream.protocol_version == LEGACY
            assert upstream.serves_skills is False
            server, served = await _proxy_over({"lg": upstream})
            assert served is False
            assert await upstream.list_skills() == []
            with pytest.raises(MCPError):
                await upstream.read_skill_file("skill://tips/SKILL.md")
    assert not SKILL_METHODS & set(recorder.methods())


async def test_probed_legacy_http_upstream_receives_no_skills_request(skills_dir):
    fixture, app = _skills_server(skills_dir)
    recorder = _Recorder()
    async with fixture.session_manager.run():
        async with _hosted(_http_upstream(_legacy_only(app), recorder)) as upstream:
            await upstream.start()
            assert upstream.protocol_version == LEGACY
            server, served = await _proxy_over({"lg": upstream})
            assert served is False
    assert not SKILL_METHODS & set(recorder.methods())


async def test_legacy_stdio_upstream_receives_no_skills_request(tmp_path):
    log = tmp_path / "legacy.jsonl"
    upstream = _stdio_legacy(log)
    try:
        await upstream.start()
        server, served = await _proxy_over({"old": upstream})
        assert served is False
    finally:
        await upstream.stop()
    assert not SKILL_METHODS & {m.get("method") for m in _received(log)}


async def test_modern_upstream_without_the_extension_receives_no_skills_request():
    from tests import era_fixture_server

    fixture = era_fixture_server.build()
    app = _app(fixture)
    recorder = _Recorder()
    async with fixture.session_manager.run():
        async with _hosted(_http_upstream(app, recorder)) as upstream:
            await upstream.start()
            assert upstream.protocol_version == MODERN
            assert upstream.serves_skills is False
            _, served = await _proxy_over({"md": upstream})
            assert served is False
    assert not SKILL_METHODS & set(recorder.methods())


# --- Erreurs d'un upstream http ---------------------------------------------------------

_REFUSAL = "skill devenue illisible"


def _refuse(method: str, app: Any, *, hang: bool = False) -> Any:
    """Répond à toute requête `method` une erreur JSON-RPC en 400 avec corps,
    comme un serveur moderne — ou ne répond jamais (`hang`). Le reste passe."""

    async def wrapped(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await app(scope, receive, send)
            return
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        request = json.loads(body)
        if isinstance(request, dict) and request.get("method") == method:
            if hang:
                await anyio.sleep_forever()
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32603, "message": _REFUSAL}}
            ).encode()
            await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": payload})
            return
        replayed = False

        async def replay() -> Any:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await app(scope, replay, send)

    return wrapped


async def test_http_upstream_jsonrpc_error_on_skills_get_keeps_code_and_message(skills_dir):
    """Le task group de la garde enveloppe la `MCPError` de l'upstream : non
    déballée, elle sortait du handler en erreur de code 0 « unhandled errors in
    a TaskGroup »."""
    fixture, app = _skills_server(skills_dir)
    async with fixture.session_manager.run():
        async with _hosted(_http_upstream(_refuse("skills/get", app), _Recorder())) as upstream:
            await upstream.start()
            # Sur l'upstream lui-même : c'est sa garde qui doit déballer.
            with pytest.raises(MCPError, match=_REFUSAL):
                await upstream.get_skill("skill://tips/SKILL.md")
            server, _ = await _proxy_over({"ht": upstream})
            with pytest.raises(MCPError) as info:
                await _request(server, "skills/get", {"uri": "skill://ht/tips/SKILL.md"})
            result = await mcp_call_read_skill(server, "skill://ht/tips/SKILL.md")
    assert info.value.error.code == types.INTERNAL_ERROR
    assert info.value.error.message == _REFUSAL
    # Le repli lit le fichier ; seule la note des autres fichiers, tirée de
    # `skills/get`, manque.
    assert result.is_error is not True
    assert "Autres fichiers" not in result.content[0].text


async def mcp_call_read_skill(server: Any, uri: str) -> Any:
    from tests.proxy_client import call_tool

    return await call_tool(server, "read_skill", {"uri": uri})


async def test_silent_http_upstream_is_bounded_and_omitted(skills_dir, capsys):
    """Un upstream qui ne répond pas à `skills/list` est borné par son délai :
    omis du listage (les autres répondent), et `skills/get` rend une
    `MCPError` lisible plutôt que d'attendre le délai de lecture du transport."""
    fixture, app = _skills_server(skills_dir)
    async with fixture.session_manager.run():
        silent = _refuse("skills/list", _refuse("resources/read", app, hang=True), hang=True)
        async with _hosted(_http_upstream(silent, _Recorder())) as upstream:
            await upstream.start()
            upstream._timeout = 0.5
            # Extension déclarée : publiée quand même, listage vide.
            assert await install_skills(build_proxy_server({"ht": upstream}, {}), {"ht": upstream}) is True
            server = build_proxy_server({"ht": upstream}, {})
            from mcp_proxy.skills import collect_skills

            assert await collect_skills({"ht": upstream}) == []
            with pytest.raises(TimeoutError):
                await upstream.read_skill_file("skill://tips/SKILL.md")
    assert "Skills de 'ht' non listées (TimeoutError" in capsys.readouterr().err


@pytest.mark.xfail(
    strict=True,
    reason=(
        "défaut antérieur, commun à tools/call : la requête en cours reçoit "
        "« Connection closed » de la session AVANT que la garde ne voie la mort "
        "de la tâche de service, si bien que sa cause (AuthorizationRequired) "
        "n'est pas relevée"
    ),
)
async def test_authorization_required_mid_session_keeps_its_contract(skills_dir):
    """L'AS qui ne réclame son jeton qu'à certaines requêtes : la tâche de
    service meurt de `AuthorizationRequired`, la garde relève SA cause, et le
    client du proxy reçoit le contrat AUTHORIZATION_REQUIRED, comme sur un
    `tools/call`."""
    from mcp_proxy.contract import AUTHORIZATION_REQUIRED

    class _RefuseSkillsGet(httpx2.Auth):
        def auth_flow(self, request):
            if request.method == "POST" and b'"skills/get"' in request.content:
                raise AuthorizationRequired("fixture")
            yield request

    fixture, app = _skills_server(skills_dir)
    async with fixture.session_manager.run():
        upstream = _http_upstream(app, _Recorder(), auth=_RefuseSkillsGet())
        async with _hosted(upstream):
            await upstream.start()
            server, _ = await _proxy_over({"ht": upstream})
            with pytest.raises(MCPError) as info:
                await _request(server, "skills/get", {"uri": "skill://ht/tips/SKILL.md"})
    assert info.value.error.data["code"] == AUTHORIZATION_REQUIRED
    assert info.value.error.data["upstream"] == "ht"


async def test_dead_stdio_upstream_read_is_a_readable_error(skills_dir):
    """Une panne de transport sur `resources/read` sort en `MCPError` du proxy,
    jamais en erreur de code 0."""
    from tests.test_proxy_era import _kill_children

    upstream = _stdio_skills(skills_dir)
    try:
        await upstream.start()
        server, _ = await _proxy_over({"st": upstream})
        _kill_children("skills_fixture_server.py")
        await anyio.sleep(0.2)
        with pytest.raises(MCPError) as info:
            await _read(server, "skill://st/tips/SKILL.md")
        result = await mcp_call_read_skill(server, "skill://st/tips/SKILL.md")
        listed = await _request(server, "skills/list", types.PaginatedRequestParams())
    finally:
        await upstream.stop()
    assert info.value.error.code != 0
    assert result.is_error is True
    assert listed["skills"] == []


# --- Entrées fabriquées : ce que nos serveurs n'émettent jamais -------------------------

from tests import fabricated_skills_server  # noqa: E402


def _fabricated(listing: str = "fabricated") -> tuple[Any, Any]:
    fixture = fabricated_skills_server.build(listing)
    return fixture, _app(fixture)


async def test_fabricated_listing_is_relayed_except_what_cannot_be_prefixed(capsys):
    """Pagination suivie jusqu'au curseur répété ; écartées (avec une trace,
    une seule fois) les seules entrées que le préfixage ne sait pas traiter ;
    relayé tel quel tout le reste, `dynamic` et entrées incomplètes compris."""
    fixture, app = _fabricated()
    recorder = _Recorder()
    async with fixture.session_manager.run():
        async with _hosted(_http_upstream(app, recorder)) as upstream:
            await upstream.start()
            server, served = await _proxy_over({"fab": upstream})
            assert served is True
            first = await _request(server, "skills/list", types.PaginatedRequestParams())
            second = await _request(server, "skills/list", types.PaginatedRequestParams())
    assert first == second
    by_uri = {e["uri"]: e for e in first["skills"]}
    assert list(by_uri) == [
        "skill://fab/alpha/SKILL.md",
        "skill://fab/dyn/SKILL.md",
        "skill://fab/nodesc/SKILL.md",
        "skill://fab/anon/SKILL.md",
        "skill://fab/beta/SKILL.md",
    ]
    assert by_uri["skill://fab/dyn/SKILL.md"]["resources"] == "dynamic"
    assert by_uri["skill://fab/alpha/SKILL.md"]["resources"][0]["uri"] == "skill://fab/alpha/SKILL.md"
    assert by_uri["skill://fab/nodesc/SKILL.md"]["frontmatter"] == {"name": "nodesc"}
    # Deux pages par listage : la seconde rend le même curseur, arrêt.
    # (install_skills ne liste pas : l'extension est déclarée.)
    assert recorder.methods().count("skills/list") == 2 * 2
    err = capsys.readouterr().err
    skipped = [line for line in err.splitlines() if "Skill de 'fab' ignorée" in line]
    assert len(skipped) == 3
    assert any("github://owner/repo/SKILL.md" in line for line in skipped)
    assert any("fichier sans URI skill://" in line for line in skipped)
    assert any("pas un objet" in line for line in skipped)


async def test_cache_hints_are_the_strictest_of_proxy_and_upstreams():
    """La seconde page dit `private` et 1 000 ms : le proxy ne publie pas plus
    large. `skills/get` d'un upstream qui n'en pose pas : constantes du proxy."""
    fixture, app = _fabricated()
    async with fixture.session_manager.run():
        async with _hosted(_http_upstream(app, _Recorder())) as upstream:
            await upstream.start()
            server, _ = await _proxy_over({"fab": upstream})
            listed = await _request(server, "skills/list", types.PaginatedRequestParams())
            got = await _request(server, "skills/get", {"uri": "skill://fab/alpha/SKILL.md"})
    assert (listed["ttlMs"], listed["cacheScope"]) == (fabricated_skills_server.PAGE_TWO_TTL_MS, "private")
    # GetSkillResult de la fixture : défauts du SDK (0, private) — les plus restrictifs.
    assert (got["ttlMs"], got["cacheScope"]) == (0, "private")


async def test_unlisted_served_skill_is_readable_through_the_proxy():
    """La spec exige de savoir lire une skill servie mais non listée : c'est
    l'upstream distant qui juge, pas une liste blanche tirée de son listage."""
    fixture, app = _fabricated()
    async with fixture.session_manager.run():
        async with _hosted(_http_upstream(app, _Recorder())) as upstream:
            await upstream.start()
            server, _ = await _proxy_over({"fab": upstream})
            got = await _request(server, "skills/get", {"uri": "skill://fab/hidden/SKILL.md"})
            [content] = (await _read(server, "skill://fab/hidden/SKILL.md")).contents
            fallback = await mcp_call_read_skill(server, "skill://fab/hidden/SKILL.md")
            with pytest.raises(MCPError) as info:
                await _read(server, "skill://fab/ghost/SKILL.md")
    assert got["skill"]["uri"] == "skill://fab/hidden/SKILL.md"
    assert content.text.endswith("hidden\n")
    assert fallback.is_error is not True and "hidden" in fallback.content[0].text
    assert info.value.error.code == types.INVALID_PARAMS
    assert "skill://fab/ghost/SKILL.md" in info.value.error.message


async def test_block_degrades_on_incomplete_entries():
    from mcp_proxy.skills import SKILL_DESCRIPTION_MAX_CHARS, build_skills_blocks

    fixture, app = _fabricated()
    async with fixture.session_manager.run():
        async with _hosted(_http_upstream(app, _Recorder())) as upstream:
            await upstream.start()
            block = (await build_skills_blocks({"fab": upstream}))["fab"]
    lines = block.splitlines()[1:]
    assert lines[2] == "- `nodesc` (skill://fab/nodesc/SKILL.md), facultative"
    anon = lines[3]
    assert anon.startswith("- `anon` (skill://fab/anon/SKILL.md), facultative : ")
    assert anon.endswith(" : " + "x" * SKILL_DESCRIPTION_MAX_CHARS)


async def test_declared_extension_with_an_empty_listing_publishes_the_capabilities():
    """Un listage vide n'est pas « aucune skill » : l'extension est publiée
    pour un upstream distant qui la déclare. (L'inprocess garde la règle « au
    moins une skill », cf. test_no_skill_served_publishes_nothing_new.)"""
    fixture, app = _fabricated("empty")
    async with fixture.session_manager.run():
        async with _hosted(_http_upstream(app, _Recorder())) as upstream:
            await upstream.start()
            server, served = await _proxy_over({"fab": upstream})
            assert served is True
            listed = await _request(server, "skills/list", types.PaginatedRequestParams())
            got = await _request(server, "skills/get", {"uri": "skill://fab/hidden/SKILL.md"})
    assert listed["skills"] == []
    assert SKILLS_EXTENSION_ID in server.extensions
    assert got["skill"]["uri"] == "skill://fab/hidden/SKILL.md"


# --- Proxy chaîné : texte libre préfixé, repli de l'amont non republié -------------------

from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _upstream_proxy(skills_dir: Path):
    """Un proxy AMONT, comme un upstream http tiers : il sert la skill `tips` de
    son upstream inprocess `bench` (exigée par tous ses outils), publie son
    bloc d'instructions et son propre `read_skill`. Rend son app ASGI."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    from mcp_proxy import InProcessUpstream
    from mcp_proxy.server import aggregate_instructions
    from mcp_proxy.skills import build_skills_blocks

    inner_upstreams = {
        "bench": InProcessUpstream(
            "tests.skills_fixture_server", config={"skills_dir": str(skills_dir), "requires_skill": "tips"}
        )
    }
    inner = build_proxy_server(inner_upstreams, {})
    await inner_upstreams["bench"].start()
    assert await install_skills(inner, inner_upstreams)
    inner.instructions = aggregate_instructions(inner_upstreams, await build_skills_blocks(inner_upstreams))
    manager = StreamableHTTPSessionManager(app=inner)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await manager.handle_request(scope, receive, send)

    async with manager.run():
        yield app


async def test_chained_proxy_relays_with_a_double_prefix(skills_dir):
    """Le proxy aval relaie les skills de l'amont sous `skill://up/bench/…` :
    entrées, `requiresSkill`, bloc généré ET texte libre de l'amont (son bloc,
    qui citait ses propres URI) ; le `read_skill` de l'amont n'est pas
    republié, le sien le remplace. Octets intacts sur deux sauts."""
    from mcp_proxy.server import aggregate_instructions
    from mcp_proxy.skills import build_skills_blocks
    from tests.proxy_client import list_tools

    async with _upstream_proxy(skills_dir) as app:
        recorder = _Recorder()
        async with _hosted(_http_upstream(app, recorder)) as upstream:
            await upstream.start()
            assert upstream.serves_skills is True
            server, _ = await _proxy_over({"up": upstream})
            await _assert_relayed_chain(server)
            tools = {t.name: t for t in (await list_tools(server)).tools}
            instructions = aggregate_instructions({"up": upstream}, await build_skills_blocks({"up": upstream}))
    assert "up__read_skill" not in tools
    assert "read_skill" in tools
    assert tools["up__bench__ping"].meta == {"miaou/requiresSkill": "skill://up/bench/tips/SKILL.md"}
    # Bloc de l'aval : la forme « tout appel » tient, le repli de l'amont
    # n'étant plus compté parmi les outils.
    assert "(skill://up/bench/tips/SKILL.md), obligatoire avant tout appel d'un outil `up__…`" in instructions
    # Texte libre de l'amont : plus aucune URI dans son espace de noms à lui.
    assert "skill://bench/" not in instructions.replace("skill://up/bench/", "")


async def _assert_relayed_chain(server: Any) -> None:
    listed = await _request(server, "skills/list", types.PaginatedRequestParams())
    [entry] = listed["skills"]
    assert entry["uri"] == "skill://up/bench/tips/SKILL.md"
    for resource in entry["resources"]:
        [content] = (await _read(server, resource["uri"])).contents
        data = (
            content.text.encode("utf-8")
            if isinstance(content, types.TextResourceContents)
            else base64.b64decode(content.blob)
        )
        assert skill_digest(data) == resource["digest"]


async def test_chained_legacy_upstream_is_published_unchanged(skills_dir):
    """Forcé en legacy, l'amont n'est pas relayé : son repli reste un outil de
    l'aval, son texte libre n'est pas touché — le fil ne bouge pas pour lui."""
    from mcp_proxy.server import aggregate_instructions
    from tests.proxy_client import list_tools

    async with _upstream_proxy(skills_dir) as app:
        async with _hosted(_http_upstream(app, _Recorder(), protocol="legacy")) as upstream:
            await upstream.start()
            server, served = await _proxy_over({"upl": upstream})
            names = [t.name for t in (await list_tools(server)).tools]
            instructions = aggregate_instructions({"upl": upstream})
            upstream_text = upstream.instructions
    assert served is False
    assert "upl__read_skill" in names and "read_skill" not in names
    assert upstream_text.strip() in instructions
    assert "(skill://bench/tips/SKILL.md)" in instructions


def test_free_text_prefixing_is_reserved_to_relayed_upstreams():
    from mcp_proxy.server import prefix_free_text_skill_uris

    relayed = StdioUpstream("true", [])
    relayed.protocol_version = MODERN
    relayed.capabilities = types.ServerCapabilities(extensions={SKILLS_EXTENSION_ID: {}})
    legacy = StdioUpstream("true", [])
    legacy.protocol_version = LEGACY
    legacy.capabilities = types.ServerCapabilities()
    text = "Lire skill://acme/SKILL.md (et skill://acme/ref.md)."
    assert prefix_free_text_skill_uris("up", relayed, text) == (
        "Lire skill://up/acme/SKILL.md (et skill://up/acme/ref.md)."
    )
    assert prefix_free_text_skill_uris("up", legacy, text) == text
