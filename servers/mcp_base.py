"""
Base class shared by MIAOU MCP servers.

Not a PEP 723 script — imported by mcp_bench.py and mcp_weather.py,
whose own PEP 723 blocks declare the shared dependencies.
"""

import argparse
import hashlib
import inspect
import json
import mimetypes
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server.extension import Extension, MethodBinding, ResourceBinding
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.resources import Resource
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from starlette.middleware.cors import CORSMiddleware

# Par défaut, ArgModelBase laisse Pydantic ignorer silencieusement
# tout argument d'outil non déclaré dans la signature de la fonction (extra="ignore"
# implicite) : un appelant qui hallucine un nom de paramètre (ex. `page` au lieu de
# `selector`) voit son argument avalé sans erreur, et l'outil retombe sur son défaut
# sans jamais signaler l'anomalie. Passage en extra="forbid" pour transformer ça en
# erreur de validation explicite, avant même l'exécution de l'outil.
ArgModelBase.model_config["extra"] = "forbid"

# Plafond du corps d'une requête HTTP entrante, en octets. Le SDK 2.x en pose un
# de 4 Mio par défaut (réponse 413 avant tout parsing), là où la 1.x n'en avait
# aucun. Or MIAOU envoie un fichier ENTIER, en base64, dans les arguments d'un
# outil qui déclare `ref` + `content_b64` (mcp_docs) — jusqu'à son propre
# plafond `MAX_INLINE_BYTES` de 64 Mo, soit environ 85,4 Mo une fois encodé,
# plus l'enveloppe JSON-RPC. Le défaut du SDK refuserait tout document de plus
# de 3 Mo environ. Couplé à MIAOU : si son plafond monte, celui-ci suit (cf.
# docs/miaou-contract.md). Partagé par les serveurs autonomes et le proxy.
MAX_REQUEST_BODY_BYTES = 96 * 1024 * 1024

# Le SDK 2.x expire par défaut une session restée 30 min sans requête (la 1.x
# ne l'expirait jamais). MIAOU sait ré-initialiser une session tuée, mais ces
# serveurs tournent en local : on garde le comportement d'avant plutôt que
# d'introduire un aller-retour de reconnexion après chaque pause.
SESSION_IDLE_TIMEOUT_S: float | None = None


def enable_system_trust_store() -> bool:
    """Fait vérifier les certificats TLS avec le magasin de confiance du système
    d'exploitation, au lieu du bundle CA figé qu'embarquent certifi/OpenSSL.

    Motivation : un upstream HTTPS dont le certificat est signé par une AC
    d'entreprise interne échoue sinon en CERTIFICATE_VERIFY_FAILED, alors que le
    même hôte s'ouvre sans erreur dans un navigateur — l'AC est bien installée,
    mais dans le magasin du système (schannel sous Windows, Keychain sous macOS,
    ca-certificates sous Linux), que Python ne consulte pas.

    `truststore.inject_into_ssl()` remplace `ssl.SSLContext` par sa propre
    implémentation, adossée au magasin système. C'est ce qui rend cet appel
    unique suffisant pour TOUTE la sortie HTTPS du process, quelle que soit la
    bibliothèque : urllib (make_opener, utilisé par weather/ddg/brave/web) comme
    httpx (HttpUpstream du proxy, et le client OAuth du SDK MCP) construisent
    leur contexte via `ssl.create_default_context()`, donc via la classe
    patchée. Aucun appel HTTP n'est à réécrire, et rien n'est à passer
    explicitement à un client — c'est précisément pourquoi le point d'injection
    est ici et pas dans chaque serveur.

    Doit être appelé AVANT que le premier contexte SSL ne soit construit (donc
    au démarrage, avant tout appel réseau) : un contexte déjà créé garde la
    classe d'origine et continue d'ignorer le magasin système.

    Best-effort volontaire : renvoie False sans lever si `truststore` est absent
    (installation pip minimale) ou si la plateforme n'est pas supportée. Sur un
    poste sans AC interne, la vérification par le bundle CA fonctionne déjà — un
    crash au démarrage y serait une régression pure, pour un bénéfice nul.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
        return True
    except Exception as e:  # ImportError, ou plateforme non supportée
        print(
            f"Avertissement : magasin de confiance système non activé ({e}). "
            "Les certificats signés par une AC interne peuvent échouer à la "
            "vérification ; installer `truststore` corrige ce cas.",
            file=sys.stderr,
        )
        return False


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
    """Construit un opener urllib proxy-aware (http_proxy/https_proxy, en
    majuscules ou minuscules, chaque variable mappée sur son propre scheme).

    Délègue à `ProxyHandler()` sans arguments (B2) plutôt que de construire le
    dict `proxies` à la main : `ProxyHandler(proxies)` explicite court-circuite
    `no_proxy`/`NO_PROXY`, alors que `ProxyHandler()` seul lit `getproxies()`
    (qui, lui, respecte `no_proxy`) à chaque requête via `proxy_bypass`."""
    return urllib.request.build_opener(urllib.request.ProxyHandler())


# ---------------------------------------------------------------------------
# Extension Skills (io.modelcontextprotocol/skills)
#
# Spec : specification/stable/skills.mdx du dépôt modelcontextprotocol/ext-skills.
# Le format d'une skill (dossier, `SKILL.md`, frontmatter YAML) est celui d'Agent
# Skills ; l'extension ne définit que le transport : chaque fichier est une
# ressource `skill://<nom>/<chemin>`, et `skills/list` / `skills/get` rendent une
# entrée par skill — frontmatter VERBATIM, manifeste COMPLET des fichiers avec
# empreinte et taille.
#
# Écrite à la main sur l'`Extension` publique du SDK : le jour où le SDK livre
# la sienne, on remplace cette classe, le format sur le fil ne bouge pas.
# ---------------------------------------------------------------------------

SKILLS_EXTENSION_ID = "io.modelcontextprotocol/skills"

REQUIRES_SKILL_META_KEY = "miaou/requiresSkill"
"""Clé du `_meta` d'un outil : URI du `SKILL.md` à lire avant de l'appeler.

L'URI est relative au serveur qui liste l'outil (un proxy la réécrit). Préfixe
`miaou/` : `_meta` est un espace partagé, une clé nue collisionnerait."""

# Bornes par skill fixées par la spec : un hôte conforme DOIT accepter jusque-là,
# un serveur NE DEVRAIT PAS servir au-delà. On refuse au démarrage.
SKILL_MAX_FILES = 512
SKILL_MAX_TOTAL_BYTES = 16 * 1024 * 1024

# Fraîcheur annoncée sur `skills/list` et `skills/get` (champs REQUIS par la
# spec). Indice de cache, pas une propriété d'intégrité : les empreintes sont de
# toute façon recalculées à chaque appel.
SKILLS_TTL_MS = 300_000
SKILLS_CACHE_SCOPE = "public"

# Règles de nom d'Agent Skills : 1 à 64 caractères, minuscules, chiffres et
# tirets, ni tiret en bord ni double tiret.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_SKILL_NAME_MAX = 64
# Agent Skills borne `description` à 1024 caractères.
_SKILL_DESCRIPTION_MAX = 1024
# Un segment de chemin de fichier entre tel quel dans l'URI : on s'en tient aux
# caractères non réservés de la RFC 3986, plutôt que d'encoder des chemins que le
# modèle devrait ensuite recopier à l'identique.
_SKILL_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


class SkillError(ValueError):
    """Skill invalide, refusée au démarrage. Le message nomme la skill et la cause."""


def skill_digest(data: bytes) -> str:
    """Empreinte d'un fichier de skill au format de la spec : `sha256:<hex minuscule>`."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def validate_skill_name(name: Any) -> None:
    if not isinstance(name, str) or not name:
        raise SkillError("`name` absent ou vide")
    if len(name) > _SKILL_NAME_MAX:
        raise SkillError(f"`name` « {name} » dépasse {_SKILL_NAME_MAX} caractères")
    if not _SKILL_NAME_RE.match(name):
        raise SkillError(
            f"`name` « {name} » invalide : minuscules, chiffres et tirets "
            f"seulement, ni tiret en bord ni double tiret"
        )


def parse_skill_frontmatter(text: str) -> dict[str, Any]:
    """Frontmatter YAML d'un `SKILL.md`, rendu tel quel en dict JSON-compatible.

    La spec exige le frontmatter VERBATIM (tous les champs, pas une sélection) :
    d'où un vrai parseur YAML plutôt qu'une lecture ligne à ligne. Mais YAML type
    plus large que JSON — une date non quotée devient un `date` Python, `.nan` un
    flottant que JSON n'a pas : une telle valeur est refusée en nommant le champ,
    puisqu'on ne saurait pas la rendre sans la transformer.

    PyYAML est importé ici et non en tête de module : mcp_base est importé par
    tous les serveurs, seuls ceux qui servent des skills déclarent la dépendance.
    """
    import yaml

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise SkillError("SKILL.md ne commence pas par un frontmatter (`---`)")
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == "---":
            block = "".join(lines[1:index])
            break
    else:
        raise SkillError("frontmatter de SKILL.md non refermé (`---`)")
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as e:
        raise SkillError(f"frontmatter YAML illisible : {e}") from e
    if not isinstance(data, dict):
        raise SkillError("le frontmatter n'est pas un objet YAML")
    for key, value in data.items():
        if not isinstance(key, str):
            raise SkillError(f"clé de frontmatter non textuelle : {key!r}")
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as e:
            raise SkillError(
                f"champ `{key}` du frontmatter non représentable en JSON "
                f"({type(value).__name__}) — le quoter dans le YAML"
            ) from e
    return data


@dataclass(frozen=True)
class SkillSource:
    """Une skill sur disque : son nom, son dossier, et ses fichiers.

    L'ensemble des fichiers est figé au démarrage (chacun est enregistré comme
    ressource à ce moment-là) ; leur CONTENU est relu à chaque appel."""

    name: str
    root: Path
    files: tuple[str, ...]  # chemins relatifs, séparateur `/`, SKILL.md compris

    @property
    def uri(self) -> str:
        return self.file_uri("SKILL.md")

    def file_uri(self, rel_path: str) -> str:
        return f"skill://{self.name}/{rel_path}"


def _skill_files(root: Path) -> list[str]:
    """Fichiers d'une skill, triés. Un chemin dont un segment commence par un
    point est écarté (`.DS_Store`, `.git/`…) : ce n'est pas du contenu de skill,
    et le servir ferait changer l'empreinte au gré du système de fichiers."""
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if not path.is_file():
            continue
        for part in rel.parts:
            if not _SKILL_PATH_SEGMENT_RE.match(part):
                raise SkillError(
                    f"chemin « {rel.as_posix()} » : le segment « {part} » sort des "
                    f"caractères admis dans une URI (lettres, chiffres, `.`, `_`, `~`, `-`)"
                )
        files.append(rel.as_posix())
    return files


def scan_skills_dir(skills_dir: str | Path) -> list[SkillSource]:
    """Chaque sous-dossier de `skills_dir` qui contient un `SKILL.md` est une
    skill, et le nom du sous-dossier est son `name`. Valide tout ce qui peut
    l'être sans servir (nom, frontmatter, bornes), et lève `SkillError` à la
    première skill invalide : un serveur ne démarre pas avec une skill qu'il ne
    saurait pas servir conformément."""
    base = Path(skills_dir)
    if not base.is_dir():
        raise SkillError(f"dossier de skills introuvable : {base}")
    sources: list[SkillSource] = []
    for root in sorted(p for p in base.iterdir() if p.is_dir()):
        if root.name.startswith(".") or not (root / "SKILL.md").is_file():
            continue
        try:
            validate_skill_name(root.name)
            source = SkillSource(root.name, root, tuple(_skill_files(root)))
            build_skill_entry(source)  # frontmatter, nom, bornes
        except SkillError as e:
            raise SkillError(f"skill « {root.name} » ({root}) : {e}") from e
        sources.append(source)
    return sources


def build_skill_entry(source: SkillSource) -> dict[str, Any]:
    """Entrée `Skill` de la spec, recalculée depuis le disque à chaque appel.

    Relire à chaque `skills/list` / `skills/get` garde l'entrée vraie quand un
    fichier change sans redémarrage : le frontmatter doit être celui du
    `SKILL.md` servi, et les empreintes celles des octets que `resources/read`
    rendra."""
    resources: list[dict[str, Any]] = []
    total = 0
    skill_md: bytes | None = None
    if len(source.files) > SKILL_MAX_FILES:
        raise SkillError(f"{len(source.files)} fichiers, au-delà de la borne de {SKILL_MAX_FILES}")
    for rel in source.files:
        try:
            data = (source.root / rel).read_bytes()
        except OSError as e:
            raise SkillError(f"fichier « {rel} » illisible : {e}") from e
        if rel == "SKILL.md":
            skill_md = data
        total += len(data)
        resources.append({"uri": source.file_uri(rel), "digest": skill_digest(data), "size": len(data)})
    if total > SKILL_MAX_TOTAL_BYTES:
        raise SkillError(f"{total} octets, au-delà de la borne de {SKILL_MAX_TOTAL_BYTES}")
    if skill_md is None:
        raise SkillError("SKILL.md absent")
    try:
        text = skill_md.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SkillError(f"SKILL.md n'est pas de l'UTF-8 : {e}") from e
    frontmatter = parse_skill_frontmatter(text)
    validate_skill_name(frontmatter.get("name"))
    if frontmatter["name"] != source.name:
        raise SkillError(
            f"`name` « {frontmatter['name']} » différent du dossier « {source.name} » "
            f"(le dernier segment de l'URI doit être le nom)"
        )
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillError("`description` absente ou vide")
    if len(description) > _SKILL_DESCRIPTION_MAX:
        raise SkillError(f"`description` dépasse {_SKILL_DESCRIPTION_MAX} caractères")
    return {"uri": source.uri, "frontmatter": frontmatter, "resources": resources}


class SkillFileResource(Resource):
    """Un fichier de skill, relu sur disque à chaque `resources/read`.

    Rend les octets INTACTS : du texte s'ils sont de l'UTF-8 valide (décodage
    strict, réversible), un blob sinon. Le `FileResource` du SDK ne convient
    pas : il lit en `utf-8-sig` (BOM retiré) et en mode texte (CRLF ramené à LF),
    deux transformations qui feraient diverger les octets servis de l'empreinte
    publiée."""

    path: Path

    async def read(self) -> str | bytes:
        import anyio

        data = await anyio.to_thread.run_sync(self.path.read_bytes)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data


def skill_file_mime_type(rel_path: str) -> str:
    if rel_path.lower().endswith(".md"):
        return "text/markdown"
    return mimetypes.guess_type(rel_path)[0] or "application/octet-stream"


class ListSkillsResult(types.PaginatedResult, types.CacheableResult):
    result_type: types.ResultType = "complete"
    skills: list[dict[str, Any]]


class GetSkillParams(types.RequestParams):
    uri: str


class GetSkillResult(types.CacheableResult):
    result_type: types.ResultType = "complete"
    skill: dict[str, Any]


class Skills(Extension):
    """Sert les skills d'un dossier (`skills/list`, `skills/get`, et chaque
    fichier en ressource `skill://`).

    Pas de pagination : le catalogue d'un serveur de ce dépôt tient en une page.
    Pas de `directoryRead` : le manifeste complet de chaque entrée suffit."""

    identifier = SKILLS_EXTENSION_ID

    def __init__(self, skills_dir: str | Path) -> None:
        self.sources = scan_skills_dir(skills_dir)
        self._by_uri = {source.uri: source for source in self.sources}

    def skill_uri(self, name: str) -> str | None:
        """URI du `SKILL.md` de la skill `name`, ou None si ce serveur ne la sert pas."""
        for source in self.sources:
            if source.name == name:
                return source.uri
        return None

    def list_entries(self) -> list[dict[str, Any]]:
        """Entrées de toutes les skills. Une skill devenue invalide sur disque
        depuis le démarrage est omise et signalée, plutôt que de faire échouer
        la liste entière."""
        entries = []
        for source in self.sources:
            try:
                entries.append(build_skill_entry(source))
            except SkillError as e:
                print(f"Skill « {source.name} » omise : {e}", file=sys.stderr)
        return entries

    def get_entry(self, uri: str) -> dict[str, Any]:
        source = self._by_uri.get(uri)
        if source is None:
            raise MCPError(types.INVALID_PARAMS, f"No skill is served at {uri}")
        try:
            return build_skill_entry(source)
        except SkillError as e:
            raise MCPError(types.INTERNAL_ERROR, f"Skill {uri} unavailable: {e}") from e

    def resources(self) -> list[ResourceBinding]:
        bindings = []
        for source in self.sources:
            entry = build_skill_entry(source)
            for rel in source.files:
                fields: dict[str, Any] = {}
                if rel == "SKILL.md":
                    # Métadonnées que la spec recommande pour le SKILL.md.
                    fields = {
                        "name": entry["frontmatter"]["name"],
                        "description": entry["frontmatter"]["description"],
                    }
                bindings.append(
                    ResourceBinding(
                        SkillFileResource(
                            uri=source.file_uri(rel),
                            mime_type=skill_file_mime_type(rel),
                            path=(source.root / rel).resolve(),
                            **fields,
                        )
                    )
                )
        return bindings

    def methods(self) -> list[MethodBinding]:
        async def skills_list(ctx: Any, params: Any) -> ListSkillsResult:
            return ListSkillsResult(
                skills=self.list_entries(), ttl_ms=SKILLS_TTL_MS, cache_scope=SKILLS_CACHE_SCOPE
            )

        async def skills_get(ctx: Any, params: GetSkillParams) -> GetSkillResult:
            return GetSkillResult(
                skill=self.get_entry(params.uri), ttl_ms=SKILLS_TTL_MS, cache_scope=SKILLS_CACHE_SCOPE
            )

        return [
            MethodBinding("skills/list", types.PaginatedRequestParams, skills_list),
            MethodBinding("skills/get", GetSkillParams, skills_get),
        ]


def find_skills_extension(server: Any) -> Skills | None:
    """L'extension Skills d'un `MCPServer`, ou None s'il n'en sert pas.

    Lit `_extensions`, attribut PRIVÉ du SDK : aucune API publique ne rend les
    extensions d'un `MCPServer`, et ses méthodes `skills/*` ne sont joignables
    que par un transport. Seul point du dépôt qui y touche, couvert par un test
    qui casse si le SDK renomme l'attribut."""
    for extension in getattr(server, "_extensions", ()):
        if isinstance(extension, Skills):
            return extension
    return None


class MiaouMCPBase:
    """Base for MIAOU MCP servers.

    `config` transporte un dict libre propre à l'instance (base URL, credentials,
    etc.) — support du multi-instance inprocess : plusieurs entrées `mcpServers`
    du même module dans config.json, chacune avec sa propre clé `"config"` (voir
    `InProcessUpstream` dans mcp_proxy/upstream.py). Un serveur qui n'a pas besoin de
    multi-instance peut l'ignorer et continuer à lire `os.environ` comme avant.

    `instructions` porte une consigne valant pour le SERVEUR ENTIER, remontée
    dans l'`InitializeResult` (champ `instructions` de la spec MCP) et destinée
    au system prompt du modèle, pas à l'humain. C'est le seul emplacement du
    protocole pour une consigne de cette portée : les seuls champs qu'un client
    relaie au modèle par outil sont `name`, `description` et `inputSchema`, si
    bien qu'une consigne globale (« lire telle documentation avant d'appeler ces
    outils ») n'a autrement d'autre issue que d'être recopiée à l'identique dans
    chaque docstring — N copies d'un texte qui ne discrimine aucun outil. Ce
    qui reste dans une docstring d'outil doit être ce qui distingue CET outil.

    Facultatif et sans valeur par défaut : un serveur qui n'a pas de consigne de
    portée serveur n'en déclare pas, et son `InitializeResult` est inchangé.
    Ne vaut que si le client lit le champ — il n'atteint pas le modèle seul.

    `skills_dir` désigne un dossier de skills à servir par l'extension Skills
    (cf. `Skills`) : chaque sous-dossier contenant un `SKILL.md` en est une.
    `None` (défaut) : aucune extension, rien ne change. Une skill invalide fait
    échouer la construction avec un message qui la nomme.

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

    def __init__(
        self,
        name: str,
        default_port: int,
        config: dict | None = None,
        instructions: str | None = None,
        skills_dir: str | Path | None = None,
    ) -> None:
        self.default_port = default_port
        self.config = config or {}
        self.skills = Skills(skills_dir) if skills_dir is not None else None
        # `instructions` PAR MOT-CLEF : en 2.x, le deuxième paramètre
        # positionnel de MCPServer est `title`, et une consigne passée là
        # partirait en `serverInfo.title` sans erreur.
        self.mcp = MCPServer(
            name,
            instructions=instructions,
            extensions=[self.skills] if self.skills is not None else None,
        )

    def finalize_tools(self, requires_skill: str | dict[str, str] | None = None) -> None:
        """Normalise ce que tools/list expose, pour réduire le payload envoyé au
        modèle à chaque requête. À appeler en dernière ligne du __init__ de chaque
        serveur, après l'enregistrement de tous les outils. Idempotent.

        - Descriptions : inspect.cleandoc — une docstring assignée à la main
          (`func.__doc__ = f\"\"\"...\"\"\"`, pattern des caps interpolés) part sinon
          sur le wire avec l'indentation source de chaque ligne de continuation.
        - Schémas de paramètres : suppression des "title" auto-générés par Pydantic
          (voir _strip_schema_titles).

        `requires_skill` déclare la skill à lire avant d'appeler un outil, posée
        en `_meta["miaou/requiresSkill"]` (URI du `SKILL.md`) : un nom de skill
        vaut pour TOUS les outils du serveur, un dict `{outil: skill}` pour ceux
        qu'il nomme. Le nom doit désigner une skill servie par ce serveur, et
        l'outil exister : sinon ValueError, au démarrage.
        """
        tools = self.mcp._tool_manager._tools
        for tool in tools.values():
            if tool.description:
                tool.description = inspect.cleandoc(tool.description)
            _strip_schema_titles(tool.parameters)

        if requires_skill is None:
            return
        if isinstance(requires_skill, str):
            requires_skill = {name: requires_skill for name in tools}
        for tool_name, skill_name in requires_skill.items():
            if tool_name not in tools:
                raise ValueError(f"requires_skill : outil inconnu « {tool_name} »")
            uri = self.skills.skill_uri(skill_name) if self.skills is not None else None
            if uri is None:
                raise ValueError(
                    f"requires_skill : l'outil « {tool_name} » exige la skill "
                    f"« {skill_name} », que ce serveur ne sert pas"
                )
            tool = tools[tool_name]
            tool.meta = {**(tool.meta or {}), REQUIRES_SKILL_META_KEY: uri}

    def _make_app(self):
        """Build the Starlette ASGI app with CORS middleware."""
        # `transport_security` ici et nulle part ailleurs : en 2.x il a quitté
        # le constructeur, et l'omettre RÉACTIVE la protection DNS-rebinding
        # (hôte par défaut 127.0.0.1), qui refuse l'`Origin: null` de MIAOU
        # servi en file:// — cf. CLAUDE.md, « Deux points à ne pas toucher ».
        app = self.mcp.streamable_http_app(
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
            max_request_body_size=MAX_REQUEST_BODY_BYTES,
            session_idle_timeout=SESSION_IDLE_TIMEOUT_S,
        )
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

        Active le magasin de confiance système ici plutôt que dans chaque
        serveur : c'est le seul point traversé par les six lancements
        standalone. Le mode inprocess ne passe pas par là — le proxy fait le
        même appel de son côté, avant d'importer le moindre module de serveur.
        """
        enable_system_trust_store()

        # Detect legacy positional syntax: first arg looks like a host (no "--")
        args_raw = sys.argv[1:]
        positional = args_raw and not args_raw[0].startswith("--")

        if positional:
            host = args_raw[0]
            if len(args_raw) > 1:
                try:
                    port = int(args_raw[1])
                except ValueError:
                    print(f"Erreur : port invalide '{args_raw[1]}' (entier attendu)", file=sys.stderr)
                    sys.exit(1)
            else:
                port = self.default_port
            if len(args_raw) > 2:
                print(
                    f"Avertissement : argument(s) positionnel(s) ignoré(s) : {args_raw[2:]}",
                    file=sys.stderr,
                )
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
