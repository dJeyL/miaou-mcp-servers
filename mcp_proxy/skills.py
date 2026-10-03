"""Extension Skills servie par le proxy : les skills de ses upstreams, agrégées.

Le proxy insère le nom d'upstream (clé de `mcpServers`) en PREMIER segment de
chaque URI : la skill `skill://bench/SKILL.md` de l'upstream `bench` est publiée
en `skill://bench/bench/SKILL.md`. Même axe que le préfixe `bench__` des outils,
et le dernier segment reste le `name`, comme l'exige la spec. Seules les URI
changent, jamais les octets : les empreintes de l'upstream restent valides.

Les handlers ne s'enregistrent que si une skill est servie — un upstream
inprocess qui en liste une, ou un upstream stdio/http moderne qui déclare
l'extension —, et APRÈS le démarrage des upstreams (`install_skills`, appelé au
lifespan, puis après chaque autorisation) : le SDK calcule les capacités à
chaque `server/discover`, donc un enregistrement tardif est vu par tous les
clients — et un proxy sans skill publie exactement ce qu'il publiait avant.
"""

from __future__ import annotations

from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.shared.exceptions import MCPError

from mcp_base import (
    SKILLS_CACHE_SCOPE,
    SKILLS_EXTENSION_ID,
    SKILLS_TTL_MS,
    GetSkillParams,
    GetSkillResult,
    ListSkillsResult,
    skill_file_mime_type,
)

from .logging import _log
from .upstream import InProcessUpstream, Upstream

SKILL_SCHEME = "skill://"

SKILL_DESCRIPTION_MAX_CHARS = 1024
"""Borne d'une description de skill (règle d'Agent Skills, que `mcp_base` impose
à nos serveurs) : celle d'un upstream tiers est coupée là dans le bloc des
instructions."""


def prefix_skill_uri(upstream_name: str, uri: str) -> str:
    """URI d'upstream → URI publiée par le proxy. Seul le schéma `skill://` est
    préfixable : son premier segment est l'espace de noms que le proxy étend."""
    if not uri.startswith(SKILL_SCHEME):
        raise ValueError(f"URI de skill hors schéma {SKILL_SCHEME} : {uri}")
    return f"{SKILL_SCHEME}{upstream_name}/{uri[len(SKILL_SCHEME):]}"


def resolve_skill_uri(uri: str, live_upstreams: set[str] | dict[str, Any]) -> tuple[str, str]:
    """URI publiée par le proxy → (upstream, URI d'origine).

    Refuse en `-32602` — le code de la spec pour une ressource ou une skill non
    servie — une URI hors schéma, sans chemin après le premier segment, ou dont
    le premier segment ne nomme pas un upstream vivant."""
    if uri.startswith(SKILL_SCHEME):
        upstream_name, sep, rest = uri[len(SKILL_SCHEME):].partition("/")
        if sep and rest and upstream_name in live_upstreams:
            return upstream_name, SKILL_SCHEME + rest
    raise MCPError(types.INVALID_PARAMS, f"No skill resource is served at {uri}")


def prefix_skill_entry(upstream_name: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Entrée d'upstream → entrée publiée : `uri` ET chaque `resources[].uri`."""
    resources = entry.get("resources")
    if isinstance(resources, list):
        resources = [
            {**resource, "uri": prefix_skill_uri(upstream_name, resource["uri"])}
            for resource in resources
        ]
    return {**entry, "uri": prefix_skill_uri(upstream_name, entry["uri"]), "resources": resources}


def _live_names(upstreams: dict[str, Upstream], authorizers: dict[str, Any]) -> list[str]:
    from .server import upstream_is_live

    return [
        name
        for name, upstream in upstreams.items()
        if upstream_is_live(upstream, authorizers.get(name))
    ]


def _unprefixable(entry: Any) -> str | None:
    """Pourquoi le proxy ne sait pas relayer cette entrée, ou None s'il le sait.

    Seul ce que le préfixage ne sait pas traiter est écarté : une `uri` hors
    `skill://`, des `resources` ni liste ni `"dynamic"`, un fichier sans `uri`
    `skill://`. Le reste (nom hors règle, dernier segment ≠ `name`,
    `description` absente, empreintes) est relayé tel quel : c'est le client
    qui vérifie, et il sait dire POURQUOI une entrée est invalide, là qu'une
    entrée écartée ici disparaîtrait sans explication."""
    if not isinstance(entry, dict):
        return "entrée qui n'est pas un objet"
    uri = entry.get("uri")
    if not isinstance(uri, str) or not uri.startswith(SKILL_SCHEME):
        return f"URI hors {SKILL_SCHEME} : {uri!r}"
    resources = entry.get("resources")
    if resources == "dynamic":
        return None
    if not isinstance(resources, list):
        return f"`resources` ni liste ni \"dynamic\" ({uri})"
    for resource in resources:
        resource_uri = resource.get("uri") if isinstance(resource, dict) else None
        if not isinstance(resource_uri, str) or not resource_uri.startswith(SKILL_SCHEME):
            return f"fichier sans URI {SKILL_SCHEME} ({uri})"
    return None


def _log_skipped_entry(name: str, upstream: Upstream, reason: str) -> None:
    """Une trace par entrée écartée et par vie de l'upstream : le catalogue est
    relu à chaque listage, le journal ne doit pas le répéter."""
    skipped = upstream.__dict__.setdefault("_skipped_skill_entries", set())
    if reason not in skipped:
        skipped.add(reason)
        _log(f"Skill de '{name}' ignorée, {reason}")


async def _collect(
    upstreams: dict[str, Upstream], authorizers: dict[str, Any]
) -> tuple[list[tuple[str, dict[str, Any]]], int | None, str | None]:
    """`collect_skills`, plus les indices de cache les plus restrictifs posés
    par les upstreams listés (None si aucun n'en a transmis)."""
    from .server import _describe_failure
    from .upstream import _strictest_cache_hints

    collected: list[tuple[str, dict[str, Any]]] = []
    hints: list[dict[str, Any]] = []
    for name in _live_names(upstreams, authorizers):
        upstream = upstreams[name]
        try:
            entries, ttl_ms, cache_scope = await upstream.list_skills_with_hints()
        except Exception as e:
            _log(f"Skills de '{name}' non listées ({_describe_failure(e)}).")
            continue
        hints.append({"ttlMs": ttl_ms, "cacheScope": cache_scope})
        for entry in entries:
            reason = _unprefixable(entry)
            if reason is not None:
                _log_skipped_entry(name, upstream, reason)
                continue
            collected.append((name, entry))
    return (collected, *_strictest_cache_hints(hints))


async def collect_skills(
    upstreams: dict[str, Upstream], authorizers: dict[str, Any] | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """(upstream, entrée d'ORIGINE) pour toutes les skills des upstreams
    vivants. Une entrée non préfixable est écartée et signalée
    (`_unprefixable`), sans faire tomber les autres.

    Chaque upstream est isolé : celui dont le listage échoue (session morte,
    autorisation réclamée en cours de route, réponse illisible) est omis et
    signalé, les autres répondent — un `skills/list` vide ou partiel est
    admis par la spec, une erreur pour tous ne le serait pas."""
    return (await _collect(upstreams, authorizers or {}))[0]


def _published_cache_hints(ttl_ms: int | None, cache_scope: str | None) -> dict[str, Any]:
    """Indices publiés par le proxy : les plus restrictifs de les siens et de
    ceux des upstreams concernés. Un upstream qui annonce `private` (des
    skills propres à l'utilisateur, derrière OAuth) ne doit pas devenir
    partageable en traversant le proxy."""
    ttl = SKILLS_TTL_MS if ttl_ms is None else min(SKILLS_TTL_MS, ttl_ms)
    scope = "private" if "private" in (SKILLS_CACHE_SCOPE, cache_scope) else SKILLS_CACHE_SCOPE
    return {"ttl_ms": ttl, "cache_scope": scope}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _skill_file_resources(name: str, entry: dict[str, Any]) -> list[types.Resource]:
    """Les fichiers d'une skill, en `Resource` pour `resources/list`. Tolère un
    frontmatter absent ou incomplet : l'entrée d'un upstream distant n'est pas
    de confiance."""
    resources = entry.get("resources")
    if not isinstance(resources, list):
        return []
    frontmatter = entry.get("frontmatter")
    frontmatter = frontmatter if isinstance(frontmatter, dict) else {}
    listed = []
    for resource in resources:
        uri = prefix_skill_uri(name, resource["uri"])
        size = resource.get("size")
        fields: dict[str, Any] = {
            "uri": uri,
            "name": uri.rsplit("/", 1)[-1],
            "mime_type": skill_file_mime_type(uri),
            "size": size if isinstance(size, int) and not isinstance(size, bool) else None,
        }
        if resource["uri"] == entry["uri"]:
            fields["name"] = _text(frontmatter.get("name")) or fields["name"]
            fields["description"] = _text(frontmatter.get("description"))
        listed.append(types.Resource(**fields))
    return listed


def _log_unrelayed_skills(name: str, upstream: Upstream) -> None:
    """Une ligne au démarrage pour un upstream stdio ou http forcé en legacy
    par la config : jamais interrogé en moderne, il peut servir des skills que
    le proxy ne relaiera pas, et rien d'autre ne le dirait. Rien pour un legacy
    constaté par la sonde : un serveur qui ne parle pas 2026-07-28 ne peut pas
    servir l'extension (la spec la lie à `server/discover`), et son ère est déjà
    dans le journal."""
    kind = type(upstream).__name__.replace("Upstream", "").lower()
    if upstream.mode == "legacy":
        _log(
            f"  {name:<12} skills non relayées (upstream {kind} forcé en ère legacy "
            f"par la config : extensions jamais demandées)"
        )


def _remote_failure(name: str, target: str, exc: BaseException) -> MCPError:
    """Ce que le client reçoit quand l'upstream ne répond pas à une requête de
    skills autrement que par une erreur JSON-RPC : panne de transport, session
    morte, délai dépassé, réponse que le SDK refuse de valider. Une `MCPError`
    toujours — une autre exception sortirait du handler en erreur de code 0.
    Une autorisation réclamée en cours de route garde son contrat."""
    from .contract import AuthorizationRequired
    from .server import UpstreamNotAuthorized, _describe_failure
    from .upstream import _unwrap_exception_group

    cause = _unwrap_exception_group(exc)
    if isinstance(cause, MCPError):
        return cause
    if isinstance(cause, AuthorizationRequired):
        return UpstreamNotAuthorized(name)
    return MCPError(
        types.INTERNAL_ERROR,
        f"Le serveur '{name}' n'a pas pu servir {target} ({_describe_failure(cause)}).",
    )


async def install_skills(
    server: Server,
    upstreams: dict[str, Upstream],
    authorizers: dict[str, Any] | None = None,
) -> bool:
    """Enregistre `skills/list`, `skills/get`, `resources/list` et
    `resources/read` sur le `Server` du proxy, et publie l'extension — si et
    seulement si un upstream inprocess sert au moins une skill, ou qu'un
    upstream stdio/http vivant déclare l'extension. À appeler après le
    démarrage des upstreams ; ré-appelable (enregistrement idempotent). Rend
    True si l'extension est servie."""
    authorizers = authorizers or {}

    for name, upstream in upstreams.items():
        if not upstream.serves_skills:
            _log_unrelayed_skills(name, upstream)

    # Un upstream distant qui DÉCLARE l'extension la fait publier, même si son
    # listage est vide : la spec interdit de lire un `skills/list` vide comme
    # « aucune skill » (une skill servie non listée, ou pointée par ses
    # instructions, doit rester lisible). L'inprocess, dont le listage est
    # complet par construction, la fait publier s'il sert au moins une skill.
    declared = any(
        upstreams[name].serves_skills and not isinstance(upstreams[name], InProcessUpstream)
        for name in _live_names(upstreams, authorizers)
    )
    if not declared and not await collect_skills(upstreams, authorizers):
        return False

    async def handle_list_skills(ctx: Any, params: Any) -> ListSkillsResult:
        collected, ttl_ms, cache_scope = await _collect(upstreams, authorizers)
        entries = [prefix_skill_entry(name, entry) for name, entry in collected]
        return ListSkillsResult(skills=entries, **_published_cache_hints(ttl_ms, cache_scope))

    async def handle_get_skill(ctx: Any, params: GetSkillParams) -> GetSkillResult:
        name, origin = resolve_skill_uri(params.uri, _live_names(upstreams, authorizers))
        try:
            entry, ttl_ms, cache_scope = await upstreams[name].get_skill_with_hints(origin)
        except MCPError as e:
            if e.error.code == types.INVALID_PARAMS:
                # Message réécrit : l'URI d'origine ne dit rien au client.
                raise MCPError(types.INVALID_PARAMS, f"No skill is served at {params.uri}") from e
            raise
        except Exception as e:
            raise _remote_failure(name, params.uri, e) from e
        reason = _unprefixable(entry)
        if reason is not None:
            raise MCPError(
                types.INTERNAL_ERROR, f"Skill {params.uri} non relayable par le proxy : {reason}"
            )
        return GetSkillResult(
            skill=prefix_skill_entry(name, entry), **_published_cache_hints(ttl_ms, cache_scope)
        )

    async def handle_list_resources(ctx: Any, params: Any) -> types.ListResourcesResult:
        resources: list[types.Resource] = []
        for name, entry in await collect_skills(upstreams, authorizers):
            resources.extend(_skill_file_resources(name, entry))
        return types.ListResourcesResult(resources=resources)

    async def handle_read_resource(
        ctx: Any, params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        uri = str(params.uri)
        name, origin = resolve_skill_uri(uri, _live_names(upstreams, authorizers))
        try:
            contents = await upstreams[name].read_skill_file(origin)
        except MCPError as e:
            if e.error.code == types.INVALID_PARAMS:
                raise MCPError(types.INVALID_PARAMS, f"No skill resource is served at {uri}") from e
            raise
        except Exception as e:
            raise _remote_failure(name, uri, e) from e
        return types.ReadResourceResult(
            contents=[content.model_copy(update={"uri": uri}) for content in contents]
        )

    server.add_request_handler("skills/list", types.PaginatedRequestParams, handle_list_skills)
    server.add_request_handler("skills/get", GetSkillParams, handle_get_skill)
    server.add_request_handler("resources/list", types.PaginatedRequestParams, handle_list_resources)
    server.add_request_handler("resources/read", types.ReadResourceRequestParams, handle_read_resource)
    server.extensions[SKILLS_EXTENSION_ID] = {}
    return True


async def build_skills_blocks(
    upstreams: dict[str, Upstream], authorizers: dict[str, Any] | None = None
) -> dict[str, str]:
    """Bloc GÉNÉRÉ qui termine la section d'instructions de chaque upstream
    servant des skills : une ligne par skill, obligatoire ou facultative.

    Sans lui, une skill serait inatteignable pour le modèle : il n'a pas
    `skills/list`, toute lecture exige l'URI, et l'upstream ne peut pas l'écrire
    dans son texte libre (le préfixe ajouté ici la rendrait fausse). Généré
    depuis les mêmes données que `skills/list` et le `_meta` des outils, il ne
    peut pas dériver d'eux.

    Journalise au passage chaque `miaou/requiresSkill` qui ne désigne aucune
    skill servie par son upstream : relayé quand même (la garde du client reste
    ouverte dans ce cas), mais c'est une erreur de configuration à voir.
    """
    from mcp_base import REQUIRES_SKILL_META_KEY

    from .server import _describe_failure, published_tools

    authorizers = authorizers or {}
    entries_by_upstream: dict[str, list[dict[str, Any]]] = {}
    for name, entry in await collect_skills(upstreams, authorizers):
        entries_by_upstream.setdefault(name, []).append(entry)

    blocks: dict[str, str] = {}
    for name in _live_names(upstreams, authorizers):
        entries = entries_by_upstream.get(name, [])
        served = {entry["uri"] for entry in entries}
        try:
            tools = published_tools(upstreams[name], await upstreams[name].list_tools())
        except Exception as e:
            # Sans ses outils, le bloc ne sait pas dire lesquels exigent quelle
            # skill : l'upstream n'a pas de bloc, les autres gardent le leur.
            _log(f"Bloc des skills de '{name}' non généré ({_describe_failure(e)}).")
            continue
        required_by: dict[str, list[str]] = {}
        unserved: dict[str, int] = {}
        for tool in tools:
            uri = (tool.meta or {}).get(REQUIRES_SKILL_META_KEY)
            if uri is None:
                continue
            if uri not in served:
                unserved[uri] = unserved.get(uri, 0) + 1
                continue
            required_by.setdefault(uri, []).append(f"{name}__{tool.name}")
        if unserved:
            # Une ligne par upstream, pas une par outil : un legacy forcé dont
            # tous les outils exigent la même skill en produisait sept.
            listed = ", ".join(
                f"{uri} ({count} outil{'s' if count > 1 else ''})" for uri, count in unserved.items()
            )
            _log(f"  {name:<12} skill exigée mais non servie par '{name}' : {listed}")
        if entries:
            blocks[name] = format_skills_block(name, entries, required_by, len(tools))
    return blocks


def format_skills_block(
    upstream_name: str,
    entries: list[dict[str, Any]],
    required_by: dict[str, list[str]],
    tool_count: int,
) -> str:
    """Texte du bloc, pur. `entries` en URI d'origine ; `required_by` associe
    l'URI d'origine d'un `SKILL.md` aux noms PRÉFIXÉS des outils qui l'exigent,
    et `tool_count` est le nombre d'outils de l'upstream."""
    # « pas des skills locales » : un client qui a ses propres skills apprend au
    # modèle à les lire par leur nom ; sans cette précision, un nom de skill MCP
    # l'envoie chercher une skill locale, et l'échec lui fait conclure que la
    # skill n'existe pas (observé). « leur nom ne suffit pas » reste vrai pour
    # un client qui listerait les skills MCP avec les siennes.
    lines = [
        f"Skills MCP servies par `{upstream_name}` — pas des skills locales : leur "
        f"nom ne suffit pas, elles se lisent par leur URI complète :"
    ]
    for entry in entries:
        # Dégradé, pas refusé : une entrée distante peut manquer de `name` ou
        # de `description` (le client la dira invalide). Nom pris du dossier
        # de la skill ; ligne sans description ; description bornée à la
        # limite d'Agent Skills, pour qu'un tiers n'allonge pas le message
        # système sans fin.
        frontmatter = entry.get("frontmatter")
        frontmatter = frontmatter if isinstance(frontmatter, dict) else {}
        name = _text(frontmatter.get("name")) or entry["uri"].rsplit("/", 2)[-2]
        # Une description YAML en bloc garderait ses sauts de ligne : la ligne
        # de liste doit rester une ligne.
        description = " ".join(str(frontmatter.get("description") or "").split())
        description = description[:SKILL_DESCRIPTION_MAX_CHARS]
        tools = required_by.get(entry["uri"])
        if not tools:
            status = "facultative"
        elif len(tools) == tool_count:
            status = f"obligatoire avant tout appel d'un outil `{upstream_name}__…`"
        else:
            status = "obligatoire avant tout appel de " + ", ".join(f"`{t}`" for t in tools)
        line = f"- `{name}` ({prefix_skill_uri(upstream_name, entry['uri'])}), {status}"
        lines.append(f"{line} : {description}" if description else line)
    return "\n".join(lines)


READ_SKILL_TOOL_NAME = "read_skill"
"""Outil de repli, nom NU comme `status` : MIAOU préfixe déjà par sa carte
serveur, et un nom sans `__` ne peut pas collisionner avec un outil d'upstream."""

SKILLS_FALLBACK_META_KEY = "miaou/skillsFallback"
"""Marque l'outil de repli pour qu'un client qui lit les skills lui-même le
masque sans dépendre de son nom."""


def read_skill_tool() -> types.Tool:
    """Repli pour les clients qui ne parlent pas l'extension : sans lui, une
    skill annoncée dans les instructions resterait illisible pour eux."""
    return types.Tool(
        name=READ_SKILL_TOOL_NAME,
        description=(
            "Lit une skill MCP servie par ce proxy, par son URI complète "
            "`skill://…`, celle que donnent les instructions : son SKILL.md, ou un "
            "autre fichier de la même skill. Les skills annoncées dans les "
            "instructions de ce proxy se lisent par cet outil et cette URI, pas "
            "par un nom de skill. Toute autre URI est refusée. Un chemin relatif "
            "cité dans un SKILL.md se résout contre le dossier de ce SKILL.md."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "URI complète du fichier, de la forme skill://<serveur>/<skill>/<fichier>.",
                }
            },
            "required": ["uri"],
            "additionalProperties": False,
        },
        **{"_meta": {SKILLS_FALLBACK_META_KEY: True}},
    )


def _tool_error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


async def call_read_skill(
    upstreams: dict[str, Upstream],
    authorizers: dict[str, Any] | None,
    arguments: dict[str, Any],
) -> types.CallToolResult:
    """Rend le fichier demandé, ÉTIQUETÉ de son serveur d'origine : un contenu de
    skill MCP ne doit pas pouvoir passer pour une consigne locale. Un refus est
    un `isError` lu par le modèle, pas une erreur protocolaire.

    Ne gère aucune approbation : c'est l'affaire du client."""
    authorizers = authorizers or {}
    extra = sorted(set(arguments) - {"uri"})
    if extra:
        return _tool_error(f"Argument(s) inconnu(s) : {', '.join(extra)}. Seul `uri` est attendu.")
    uri = arguments.get("uri")
    if not isinstance(uri, str) or not uri:
        return _tool_error("`uri` manquant : URI complète d'un fichier de skill (skill://…).")
    name = ""
    try:
        name, origin = resolve_skill_uri(uri, _live_names(upstreams, authorizers))
        contents = await upstreams[name].read_skill_file(origin)
        entry = None
        if origin.endswith("/SKILL.md"):
            try:
                entry = await upstreams[name].get_skill(origin)
            except Exception:
                # Un fichier nommé SKILL.md peut être une annexe ; et le
                # contenu est déjà lu : la note des autres fichiers est un
                # complément, son absence ne fait pas échouer la lecture.
                entry = None
    except Exception as e:
        failure = e if isinstance(e, MCPError) else _remote_failure(name, uri, e)
        if failure.error.code == types.INVALID_PARAMS:
            return _tool_error(
                f"Aucun fichier de skill servi à {uri}. Seuls les fichiers des "
                f"skills annoncées dans les instructions sont lisibles."
            )
        return _tool_error(f"Lecture de {uri} impossible : {failure.error.message}")

    label = f"[Fichier de skill servi par le serveur MCP `{name}` — {uri}]"
    blocks: list[Any] = [types.TextContent(type="text", text=label)]
    for content in contents:
        content = content.model_copy(update={"uri": uri})
        if isinstance(content, types.TextResourceContents):
            blocks[0] = types.TextContent(type="text", text=f"{label}\n\n{content.text}")
        else:
            blocks.append(types.EmbeddedResource(type="resource", resource=content))
    if entry is not None and _unprefixable(entry) is None and isinstance(entry["resources"], list):
        others = [
            prefix_skill_uri(name, r["uri"]) for r in entry["resources"] if r["uri"] != origin
        ]
        if others:
            note = "\n\n[Autres fichiers de cette skill, lisibles par leur URI : " + ", ".join(others) + "]"
            blocks[0] = types.TextContent(type="text", text=blocks[0].text + note)
    return types.CallToolResult(content=blocks)
