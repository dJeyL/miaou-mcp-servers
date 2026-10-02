"""Le serveur MCP proxy lui-même.

`build_proxy_server` monte un `mcp.server.Server` bas niveau qui route chaque
appel préfixé (`bench__echo`) vers son upstream, avec le catalogue d'outils, le
rapport de statut et l'agrégation des `instructions`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_REQUEST

from .contract import (
    AUTHORIZATION_REQUIRED,
    AuthorizationRequired,
    authorize_path,
)
from .logging import _log
from .upstream import HttpUpstream, InProcessUpstream, Upstream, _unwrap_exception_group


UNAUTHORIZED_UPSTREAMS_META_KEY = "miaou/unauthorized_upstreams"
"""Clé du `_meta` de `tools/list` énumérant les upstreams non autorisés.

Surface adressée au CLIENT, là où la description marquée
(`format_stale_description`) et le rapport `status` s'adressent au modèle : sans
elle, un client ne peut pas savoir qu'un upstream est dégradé sans faire appeler
un outil par un modèle, ce qui condamne l'utilisateur à découvrir le besoin
d'autorisation par un échec.

Valeur : une liste d'objets `{"name": …, "authorize_path": …}`. Une liste dès la
première version — N upstreams d'un même proxy peuvent être non autorisés
simultanément, et un objet singulier serait à refaire. Clé absente, jamais liste
vide, quand il n'y a rien à signaler.

Le préfixe `miaou/` est délibéré : `_meta` est un espace partagé, une clé nue
collisionnerait avec une extension future du SDK ou d'un autre agrégateur.
"""


class ToolCatalogCache:
    """Se souvient des outils d'un upstream, pour les servir quand il ne répond
    plus.

    Sans ce cache, un upstream non autorisé serait muet : `tools/list` répond
    401 avant de rien dire, donc on ne saurait pas quels outils annoncer, et le
    troisième état (« connu mais pas autorisé ») n'aurait rien à montrer. Il
    couvre du même geste le redémarrage du proxy, où rien n'est encore connu.

    Sur disque, à côté du fichier de jetons : ce n'est pas un secret, mais ça
    partage sa durée de vie.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            _log(f"Cache d'outils illisible ({e}) — ignoré.")
            return {}
        return data if isinstance(data, dict) else {}

    def remember(self, upstream_name: str, tools: list[types.Tool]) -> None:
        data = self._read()
        data[upstream_name] = {
            "known_at": time.time(),
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.input_schema,
                }
                for t in tools
            ],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, indent=2, sort_keys=True))
        except OSError as e:
            # Un cache non écrit dégrade, il ne casse pas : le proxy sert
            # toujours l'upstream, il l'oubliera seulement au redémarrage.
            _log(f"Cache d'outils non écrit ({e}).")

    def recall(self, upstream_name: str) -> tuple[list[types.Tool], float | None]:
        entry = self._read().get(upstream_name)
        if not isinstance(entry, dict):
            return [], None
        tools = []
        for raw in entry.get("tools", []):
            try:
                tools.append(
                    types.Tool(
                        name=raw["name"],
                        description=raw.get("description"),
                        input_schema=_object_schema(raw.get("inputSchema")),
                    )
                )
            except Exception:
                continue
        return tools, entry.get("known_at")


def format_stale_description(description: str | None, known_at: float | None) -> str:
    """Marque une description d'outil resservie depuis le cache.

    Le modèle appelant doit pouvoir distinguer un outil vivant d'un outil dont
    on se souvient : présenter une liste périmée comme vivante serait lui
    mentir, et il n'a aucun autre moyen de le savoir. La date est absolue (pas
    « il y a 3 h ») — un texte qui change à chaque tour casserait le cache KV
    du modèle.
    """
    base = (description or "").strip()
    when = ""
    if known_at:
        when = " (dernière liste connue : " + time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(known_at)
        ) + ")"
    notice = (
        f"[Serveur non autorisé — cet outil n'est PAS appelable pour l'instant{when}. "
        f"L'appeler renvoie une erreur {AUTHORIZATION_REQUIRED} indiquant comment "
        f"l'utilisateur peut l'autoriser.]"
    )
    return f"{notice} {base}".strip()


def _resolve_via_prefix(
    name: str, upstreams: dict[str, Upstream]
) -> tuple[str, str] | None:
    """Fallback quand `name` n'est pas (encore) dans tool_map : un client qui
    rappelle tools/call après reconnexion (cache local, sans repasser par
    tools/list) sinon reçoit "Outil inconnu" à tort. tool_map reste l'autorité
    pour tout nom qui contient lui-même "__" au-delà du premier segment."""
    if "__" not in name:
        return None
    prefix, orig_name = name.split("__", 1)
    if prefix in upstreams:
        return prefix, orig_name
    return None


class UpstreamNotAuthorized(MCPError):
    """Appel d'un outil dont l'upstream n'est pas (encore) autorisé.

    Une `MCPError`, levée telle quelle depuis `handle_call_tool` : le `Server`
    bas niveau du SDK 2.x la rend en erreur JSON-RPC, `code`/`message`/`data`
    intacts. C'est ce que le contrat AUTHORIZATION_REQUIRED demande — un champ
    machine que le client teste par égalité de constante, ce qu'un `isError`
    textuel ne porte pas. (En 1.x, le décorateur du SDK avalait toute exception
    d'outil en `isError` : il fallait un sentinel dans le texte et un wrapper qui
    le repêchait après coup. Les deux ont disparu avec la migration.)
    """

    def __init__(self, upstream_name: str) -> None:
        # Le message nomme QUI peut agir, et ne donne pas d'adresse à suivre :
        # il est lu par un modèle, qui ne peut ni ouvrir un lien ni résoudre un
        # chemin relatif contre l'origine du proxy. Le chemin y figure en
        # diagnostic — pour que le modèle puisse le citer à l'utilisateur, seul
        # capable de l'ouvrir. L'affordance cliquable, elle, passe par le
        # `_meta` de `tools/list`, adressé au client.
        message = (
            f"Le serveur '{upstream_name}' exige une "
            f"autorisation OAuth qui n'a pas encore été accordée. Seul "
            f"l'utilisateur peut l'accorder, depuis son client MCP "
            f"(chemin {authorize_path(upstream_name)} sur ce proxy)."
        )
        super().__init__(
            INVALID_REQUEST,
            message,
            # Slot applicatif : `code` au niveau de l'erreur reste l'entier
            # protocolaire. Le client teste data.code par ÉGALITÉ de
            # constante, jamais par sous-chaîne du message.
            #
            # `authorization_url` porte un CHEMIN RELATIF (cf. authorize_path),
            # là où il a un temps porté une URL absolue : celle d'un parcours
            # avorté, qui menait à un callback orphelin. Le nom du champ est
            # conservé — c'est le contrat publié à MIAOU.
            data={
                "code": AUTHORIZATION_REQUIRED,
                "upstream": upstream_name,
                "authorization_url": authorize_path(upstream_name),
            },
        )
        self.upstream_name = upstream_name
        self.authorization_path = authorize_path(upstream_name)


def _error_result(text: str) -> types.CallToolResult:
    """Un échec d'outil tel que le modèle doit le lire : `isError` et le texte."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=True
    )


def _object_schema(schema: Any) -> dict[str, Any]:
    """Schéma d'entrée tel que la spec l'exige : un objet JSON de `type` object.

    Le SDK 2.x valide les résultats de handler contre le schéma du protocole
    AVANT de les émettre : un seul outil dont l'`inputSchema` n'a pas
    `"type": "object"` fait rejeter le `tools/list` ENTIER en INTERNAL_ERROR —
    tous les upstreams avec lui. La 1.x laissait passer. Le cas se présente au
    moins pour le catalogue en cache (schéma absent → `{}`), et peut venir d'un
    upstream tiers peu rigoureux : on complète plutôt que de laisser un outil
    malformé éteindre les autres.
    """
    if not isinstance(schema, dict):
        return {"type": "object"}
    if schema.get("type") == "object":
        return schema
    return {**schema, "type": "object"}


STATUS_TOOL_NAME = "status"
"""Nom NU, sans préfixe de serveur.

MIAOU préfixe déjà par le nom de la carte serveur : `proxy__status` deviendrait
`miaou-proxy__proxy__status`. Conséquence à ne pas rater — la table de routage
résout tout par préfixe (`_resolve_via_prefix`), donc un nom sans `__` est un
cas particulier explicite dans handle_call_tool, sinon l'appel part chercher un
upstream nommé « status ».
"""


def _status_tool() -> types.Tool:
    return types.Tool(
        name=STATUS_TOOL_NAME,
        description=(
            "État des serveurs agrégés par ce proxy : type, disponibilité, "
            "nombre d'outils, et — pour un serveur exigeant une autorisation "
            "OAuth non encore accordée — le lien à ouvrir pour l'accorder."
        ),
        input_schema={"type": "object", "properties": {}},
    )


def build_status_report(
    upstreams: dict[str, Upstream],
    authorizers: dict[str, Any] | None = None,
    catalog: Any = None,
) -> str:
    """Rapport lisible par le modèle. Pur : ni réseau, ni I/O."""
    authorizers = authorizers or {}
    lines: list[str] = []
    for name, upstream in sorted(upstreams.items()):
        kind = type(upstream).__name__.replace("Upstream", "").lower()
        authorizer = authorizers.get(name)
        if authorizer is not None and not upstream_is_live(upstream, authorizer):
            lines.append(
                f"- {name} ({kind}) : NON AUTORISÉ. Ses outils sont listés mais "
                f"refusent à l'appel avec {AUTHORIZATION_REQUIRED}."
            )
            # Ce rapport est lu par un MODÈLE : il ne peut ouvrir aucun lien, et
            # un chemin relatif ne se résout pas sans l'origine du proxy, que le
            # proxy lui-même ne connaît pas. On nomme donc qui peut agir, et on
            # cite le chemin pour que le modèle puisse le transmettre.
            lines.append(
                f"  À autoriser par l'utilisateur depuis son client MCP "
                f"(chemin {authorize_path(name)} sur ce proxy)."
            )
            failure = getattr(authorizer, "last_error", None)
            if failure:
                lines.append(f"  Dernière tentative en échec : {failure}")
                if "403" in failure:
                    # Un 403 n'est PAS une autorisation manquante : le jeton a
                    # été obtenu, le serveur le refuse pour scope insuffisant.
                    # Relancer le parcours ne répare rien — c'est la config qui
                    # est en cause, et le dire évite un cycle de reclics.
                    lines.append(
                        "  (403 : le jeton a bien été obtenu mais ses scopes sont "
                        "insuffisants. Relancer l'autorisation n'y changera rien — "
                        "vérifier 'required_scopes' côté serveur et les scopes que "
                        "son émetteur sait accorder.)"
                    )
            if catalog is not None:
                known, known_at = catalog.recall(name)
                if known:
                    when = (
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(known_at))
                        if known_at
                        else "date inconnue"
                    )
                    lines.append(
                        f"  {len(known)} outil(s) connus, liste du {when}."
                    )
            continue
        lines.append(f"- {name} ({kind}) : disponible.")
    if not lines:
        return "Aucun serveur agrégé."
    return "Serveurs agrégés par ce proxy :\n" + "\n".join(lines)


def upstream_is_live(upstream: Upstream, authorizer: Any = None) -> bool:
    """« Cet upstream honorerait-il un appel d'outil maintenant ? »

    Prédicat UNIQUE, partagé par la liste, le refus d'appel et le rapport de
    status : trois endroits qui doivent répondre la même chose, sous peine
    d'annoncer un outil qu'on refuse ensuite pour une raison qu'on ne rapporte
    pas.

    DEUX conditions, et il faut les deux — la seconde a manqué jusqu'au
    2026-09-07. « Transport ouvert » ne vaut pas « autorisé » : un upstream
    OAuth peut accepter `initialize` ET `tools/list` sans jeton, et n'exiger
    l'autorisation qu'au premier `tools/call` (cas d'un Jira d'entreprise,
    observé en production). Le juger sur la seule session le déclarait vivant,
    donc ses outils listés sans réserve, son `_meta` vide, et son premier appel
    parti pour de bon vers un 401 — au lieu d'être refusé avant émission.

    L'`authorizer` est facultatif : les appelants qui n'en ont pas (un upstream
    sans OAuth, un test) obtiennent le comportement d'avant, à l'octet près.
    """
    if authorizer is not None and getattr(authorizer, "authorization_pending", False):
        return False
    if isinstance(upstream, HttpUpstream):
        return upstream._session is not None
    return True


_INSTRUCTIONS_PREAMBLE = (
    "Ce serveur agrège plusieurs serveurs MCP. Les outils sont préfixés par le "
    "nom de leur serveur d'origine (`<serveur>__<outil>`). Les sections "
    "ci-dessous portent les consignes propres à chaque serveur d'origine, "
    "titrées par ce même nom."
)


def aggregate_instructions(upstreams: dict[str, Upstream]) -> str | None:
    """Compose le champ `instructions` du proxy à partir de celui de chaque
    upstream (spec MCP : `InitializeResult.instructions`, destiné au system
    prompt du modèle).

    Le champ est unique en sortie et multiple en entrée, d'où le besoin de
    préserver le lien texte ↔ outils : les outils sont préfixés
    (`bench__echo`), et rien ne dirait au modèle qu'un paragraphe couvre
    `docs__read` mais pas `web__fetch_url`. Chaque section est donc titrée par
    le préfixe d'outil lui-même — la portée se déduit du titre, sans convention
    supplémentaire à faire connaître au modèle.

    Le préambule énonce la forme `<serveur>__<outil>`, littéralement vraie pour
    un client parlant à ce proxy en direct. Un client qui agrège LUI-MÊME
    plusieurs serveurs re-préfixe (MIAOU expose `miaou-proxy__bench__echo`) :
    la forme est alors fausse d'un cran, et c'est à ce client de la réécrire —
    il est seul à connaître le slug sous lequel il publie ce proxy. Le mettre
    en config ici dupliquerait une information qui vit chez le client, avec
    dérive garantie au premier renommage de carte serveur.

    Publie la section de TOUT upstream qui déclare des instructions, y compris
    non autorisé : c'est de la documentation, pas une capability. Une section
    décrivant un outil temporairement absent coûte moins qu'une section
    manquant définitivement, puisque `initialize` ne se rejoue pas après une
    autorisation obtenue en cours de route.

    Renvoie None si aucun upstream n'a d'instructions — l'InitializeResult est
    alors identique à celui d'avant ce lot, à l'octet près.
    """
    sections = [
        f"## {prefix}\n\n{upstream.instructions.strip()}"
        for prefix, upstream in upstreams.items()
        if upstream.instructions and upstream.instructions.strip()
    ]
    if not sections:
        return None
    return "\n\n".join([_INSTRUCTIONS_PREAMBLE, *sections])


def build_proxy_server(
    upstreams: dict[str, Upstream],
    tool_map: dict[str, tuple[str, str]],
    authorizers: dict[str, Any] | None = None,
    catalog: Any = None,
) -> Server:
    """Construit le Server MCP avec les handlers list_tools / call_tool.

    `authorizers`/`catalog` (lot AB-2.5) : non fournis → comportement d'avant le
    lot, à l'octet près. Fournis, ils ouvrent le troisième état d'upstream
    (« connu mais pas autorisé ») et l'outil `status`.
    """
    authorizers = authorizers or {}

    async def handle_list_tools(
        ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        tools: list[types.Tool] = []
        unauthorized: list[dict[str, str]] = []
        for prefix, upstream in upstreams.items():
            live = upstream_is_live(upstream, authorizers.get(prefix))
            if not live and prefix in authorizers:
                # Un upstream non vivant SANS authorizer est injoignable, pas
                # non autorisé : il n'a aucun parcours à proposer, et le
                # publier enverrait le client sur un /authorize/{name} qui
                # répond 404. Même prédicat d'appartenance que
                # build_status_report, et il passe par upstream_is_live, seul
                # juge de « cet upstream répond-il ? ».
                unauthorized.append(
                    {"name": prefix, "authorize_path": authorize_path(prefix)}
                )
            if live:
                upstream_tools = await upstream.list_tools()
                if catalog is not None:
                    catalog.remember(prefix, upstream_tools)
                stale_since = None
            elif catalog is not None:
                # Non autorisé : tools/list répondrait 401 avant de rien dire.
                # On ressert ce qu'on sait, marqué comme tel.
                upstream_tools, stale_since = catalog.recall(prefix)
            else:
                upstream_tools, stale_since = [], None

            for tool in upstream_tools:
                prefixed = f"{prefix}__{tool.name}"
                tool_map[prefixed] = (prefix, tool.name)
                description = tool.description
                if not live:
                    description = format_stale_description(description, stale_since)
                tools.append(
                    types.Tool(
                        name=prefixed,
                        description=description,
                        input_schema=_object_schema(tool.input_schema),
                    )
                )
        if authorizers:
            tools.append(_status_tool())

        # `**{"_meta": ...}` et non `meta=...` : pydantic ne sérialise sous
        # l'alias que si le champ a été peuplé PAR l'alias, et la propriété qui
        # compte est que la clé arrive sur le fil en `_meta`. Un test le vérifie
        # sur la CHAÎNE JSON émise, pas sur l'objet Python — `result.meta` rend
        # la même chose quelle que soit la clé sérialisée, donc un test sur
        # l'objet passerait aussi bien sur une sortie invalide.
        #
        # Clé ABSENTE quand il n'y a rien à signaler, plutôt qu'un tableau
        # vide : un client lit pareil dans les deux cas, et un proxy sain n'a
        # pas à publier un `_meta` à chaque tools/list.
        if not unauthorized:
            return types.ListToolsResult(tools=tools)
        return types.ListToolsResult(
            tools=tools,
            **{"_meta": {UNAUTHORIZED_UPSTREAMS_META_KEY: unauthorized}},
        )

    async def handle_call_tool(
        ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        """Route l'appel et rend ce que le CLIENT doit voir de son issue.

        Le `Server` bas niveau du SDK 2.x n'enveloppe plus rien : une exception
        qui sort d'ici devient une erreur JSON-RPC (code 0 si ce n'est pas une
        `MCPError`). La 1.x, elle, rendait TOUTE exception en `isError` portant
        `str(e)` — c'est ce que le modèle lisait, et ce que ce handler
        reproduit. Ne sortent en erreur JSON-RPC que les refus voulus comme
        tels : AUTHORIZATION_REQUIRED, et une `MCPError` levée délibérément par
        un outil inprocess (REF_UNKNOWN de mcp_docs).
        """
        name = params.name
        arguments = params.arguments or {}
        # Nom NU : traité AVANT la résolution par préfixe, qui partirait sinon
        # chercher un upstream appelé « status ».
        if name == STATUS_TOOL_NAME and authorizers:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=build_status_report(upstreams, authorizers, catalog),
                    )
                ]
            )

        if name in tool_map:
            upstream_name, orig_name = tool_map[name]
        else:
            resolved = _resolve_via_prefix(name, upstreams)
            if resolved is None:
                return _error_result(f"Outil inconnu : '{name}'")
            upstream_name, orig_name = resolved

        upstream = upstreams[upstream_name]
        if not upstream_is_live(upstream, authorizers.get(upstream_name)):
            # Refus AVANT l'appel : l'upstream n'a rien à dire tant qu'il n'est
            # pas autorisé.
            raise UpstreamNotAuthorized(upstream_name)
        try:
            return await upstream.call_tool(orig_name, arguments)
        except MCPError as e:
            # Erreur protocolaire voulue par l'outil (REF_UNKNOWN) : elle
            # traverse, `data` intact. Seulement d'un upstream inprocess — celle
            # d'un upstream DISTANT est sa réponse d'erreur à lui, que la 1.x
            # aplatissait en isError, comme on continue de le faire.
            if isinstance(upstream, InProcessUpstream):
                raise
            return _error_result(str(e))
        except Exception as e:
            # L'AS peut ne réclamer l'autorisation qu'ICI : un upstream qui
            # accepte `initialize` et `tools/list` sans jeton n'a encore rien
            # révélé, et le refus n'a donc pas pu être posé plus tôt. Le
            # parcours OAuth du SDK client démarre alors au milieu de CET
            # appel, `_on_redirect` le trouve non interactif et lève.
            #
            # `_unwrap_exception_group` : anyio empaquette ce qui traverse un
            # task group, la cause réelle n'est pas toujours au premier plan.
            if isinstance(_unwrap_exception_group(e), AuthorizationRequired):
                raise UpstreamNotAuthorized(upstream_name) from e
            return _error_result(str(e))

    return Server(
        "miaou-proxy",
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )
