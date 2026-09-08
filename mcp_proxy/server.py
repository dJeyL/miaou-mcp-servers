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

from .contract import (
    AUTHORIZATION_REQUIRED,
    AuthorizationRequired,
    authorize_path,
)
from .logging import _log
from .upstream import HttpUpstream, Upstream, _unwrap_exception_group


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
                    "inputSchema": t.inputSchema,
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
                        inputSchema=raw.get("inputSchema") or {},
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


_AUTHORIZATION_SENTINEL = "__MIAOU_AUTHORIZATION_REQUIRED__"
"""Marqueur interne, jamais vu du client.

Il traverse le `except Exception` que le SDK pose autour de tout handler
d'outil, seul chemin par lequel un refus levé côté proxy peut ressortir en
erreur JSON-RPC plutôt qu'en `isError` textuel. Distinct de la constante de
contrat AUTHORIZATION_REQUIRED, qui, elle, est publique et voyage dans
`error.data.code`.
"""


class UpstreamNotAuthorized(Exception):
    """Appel d'un outil dont l'upstream n'est pas (encore) autorisé.

    Interne au proxy : jamais vue du client, qui reçoit l'erreur JSON-RPC
    produite par _wrap_authorization_required.
    """

    def __init__(self, upstream_name: str) -> None:
        # Le sentinel voyage DANS le message : c'est la seule voie qui traverse
        # le `except Exception` du SDK (cf. _wrap_authorization_required). Il
        # est retiré du message avant que celui-ci n'atteigne le client.
        #
        # Le message nomme QUI peut agir, et ne donne pas d'adresse à suivre :
        # il est lu par un modèle, qui ne peut ni ouvrir un lien ni résoudre un
        # chemin relatif contre l'origine du proxy. Le chemin y figure en
        # diagnostic — pour que le modèle puisse le citer à l'utilisateur, seul
        # capable de l'ouvrir. L'affordance cliquable, elle, passe par le
        # `_meta` de `tools/list`, adressé au client.
        message = (
            f"{_AUTHORIZATION_SENTINEL} Le serveur '{upstream_name}' exige une "
            f"autorisation OAuth qui n'a pas encore été accordée. Seul "
            f"l'utilisateur peut l'accorder, depuis son client MCP "
            f"(chemin {authorize_path(upstream_name)} sur ce proxy)."
        )
        super().__init__(message)
        self.upstream_name = upstream_name
        self.authorization_path = authorize_path(upstream_name)


def _wrap_authorization_required(
    server: Server,
    upstreams: dict[str, Upstream],
    authorizers: dict[str, Any],
) -> None:
    """Relève UpstreamNotAuthorized en vraie erreur JSON-RPC.

    Même contrainte que pour REF_UNKNOWN, et pour la même raison : le SDK
    (@server.call_tool()) attrape TOUTE exception de l'outil appelé et la
    transforme en CallToolResult(isError=True) — un `isError` textuel, que le
    client ne peut distinguer d'un échec métier que par de la sous-chaîne.
    On remplace donc le handler déjà enregistré et on relève l'exception une
    fois hors de sa portée, où _handle_request la convertit.

    Wrapper SÉPARÉ de _wrap_ref_unknown_sentinel, et non une généralisation des
    deux : ils n'observent pas la même chose au même moment. REF_UNKNOWN
    inspecte un résultat APRÈS exécution (le sentinel n'existe qu'une fois
    l'outil appelé) ; celui-ci intercepte un refus levé AVANT tout appel. Les
    fondre imposerait un mécanisme qui fait les deux mal.
    """
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData, INVALID_REQUEST

    original_handler = server.request_handlers[types.CallToolRequest]

    async def handler(req: types.CallToolRequest):
        result = await original_handler(req)
        # L'exception a déjà été avalée par le SDK : on la reconnaît à son
        # sentinel dans le texte du résultat isError, exactement comme le fait
        # _wrap_ref_unknown_sentinel. Attraper l'exception elle-même serait plus
        # direct, mais impossible — le `except Exception` du SDK est À
        # L'INTÉRIEUR du handler qu'on enveloppe, donc aucun wrapper externe ne
        # peut la voir passer. Vérifié à l'exécution, pas déduit.
        call_result = result.root
        if not (call_result.isError and call_result.content):
            return result
        text = getattr(call_result.content[0], "text", "") or ""
        if _AUTHORIZATION_SENTINEL not in text:
            return result

        name = req.params.name
        prefix = name.split("__", 1)[0] if "__" in name else None
        raise McpError(
            ErrorData(
                code=INVALID_REQUEST,
                message=text.replace(_AUTHORIZATION_SENTINEL, "").strip(),
                # Slot applicatif : `code` au niveau de l'erreur reste l'entier
                # protocolaire. Le client teste data.code par ÉGALITÉ de
                # constante, jamais par sous-chaîne du message.
                #
                # `authorization_url` porte désormais un CHEMIN RELATIF
                # (cf. authorize_path) là où il portait une URL absolue : celle
                # d'un parcours avorté, qui menait à un callback orphelin. Le
                # nom du champ est conservé — c'est le contrat publié à MIAOU,
                # le renommer casserait davantage que le changement de forme.
                data={
                    "code": AUTHORIZATION_REQUIRED,
                    "upstream": prefix,
                    "authorization_url": (
                        authorize_path(prefix) if prefix else None
                    ),
                },
            )
        )

    server.request_handlers[types.CallToolRequest] = handler


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
        inputSchema={"type": "object", "properties": {}},
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
    server: Server = Server("miaou-proxy")
    authorizers = authorizers or {}

    @server.list_tools()
    async def handle_list_tools() -> types.ListToolsResult:
        # Style NOUVEAU (retour ListToolsResult) et non `list[types.Tool]` :
        # le SDK enveloppe un retour de style ancien en
        # `ListToolsResult(tools=result)`, SANS `_meta`, donc il n'existe aucun
        # moyen d'en porter un sans migrer. Le dispatch du SDK se fait sur la
        # SIGNATURE du handler (`create_call_wrapper`), pas sur son type de
        # retour : l'appelant est inchangé.
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
                        inputSchema=tool.inputSchema,
                    )
                )
        if authorizers:
            tools.append(_status_tool())

        # `**{"_meta": ...}` et non `meta=...` : pydantic ne sérialise sous
        # l'alias que si le champ a été peuplé PAR l'alias. La version du SDK
        # installée refuse `meta=` d'un TypeError, mais ce n'était pas le cas
        # partout, et la propriété qui compte n'est pas ce refus : c'est que la
        # clé arrive sur le fil en `_meta`. Un test le vérifie sur la CHAÎNE
        # JSON émise, pas sur l'objet Python — `result.meta` rend la même chose
        # quelle que soit la clé sérialisée, donc un test sur l'objet passerait
        # aussi bien sur une sortie invalide.
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

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[Any]:
        # Nom NU : traité AVANT la résolution par préfixe, qui partirait sinon
        # chercher un upstream appelé « status ».
        if name == STATUS_TOOL_NAME and authorizers:
            return [
                types.TextContent(
                    type="text",
                    text=build_status_report(upstreams, authorizers, catalog),
                )
            ]

        if name in tool_map:
            upstream_name, orig_name = tool_map[name]
        else:
            resolved = _resolve_via_prefix(name, upstreams)
            if resolved is None:
                from mcp.shared.exceptions import McpError
                from mcp.types import INVALID_PARAMS, ErrorData
                raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Outil inconnu : '{name}'"))
            upstream_name, orig_name = resolved

        upstream = upstreams[upstream_name]
        if not upstream_is_live(upstream, authorizers.get(upstream_name)):
            # Refus AVANT l'appel, et non conversion d'un résultat après coup :
            # c'est ce qui distingue ce contrat de REF_UNKNOWN, dont le sentinel
            # ne peut être reconnu qu'une fois l'outil exécuté. Levée ici, elle
            # serait avalée par le SDK en isError — d'où _wrap_authorization_
            # required, qui la relève en vraie erreur JSON-RPC.
            raise UpstreamNotAuthorized(upstream_name)
        try:
            return await upstream.call_tool(orig_name, arguments or {})
        except Exception as e:
            # L'AS peut ne réclamer l'autorisation qu'ICI : un upstream qui
            # accepte `initialize` et `tools/list` sans jeton n'a encore rien
            # révélé, et le refus n'a donc pas pu être posé plus tôt. Le
            # parcours OAuth du SDK client démarre alors au milieu de CET
            # appel, `_on_redirect` le trouve non interactif et lève.
            #
            # Sans cette conversion, l'exception traverse le transport sans
            # être reconnue et l'appel reste suspendu jusqu'à son timeout ;
            # le refus n'arrive qu'au tour SUIVANT, une fois l'état posé.
            # C'est ce tour perdu qu'on supprime — le premier appel doit
            # refuser aussi nettement que les suivants.
            #
            # `_unwrap_exception_group` : anyio empaquette ce qui traverse un
            # task group, la cause réelle n'est pas toujours au premier plan.
            if isinstance(_unwrap_exception_group(e), AuthorizationRequired):
                raise UpstreamNotAuthorized(upstream_name) from e
            raise

    # Deux wrappers indépendants, chacun sur son propre sentinel : ils
    # inspectent le même résultat mais ne se marchent pas dessus (un texte
    # d'erreur ne peut pas porter les deux marqueurs). L'ordre est donc
    # indifférent — ce qui n'allait PAS de soi : la première version levait
    # l'exception au lieu de la marquer, et se faisait avaler par le
    # `except Exception` que le SDK pose à l'intérieur du handler d'outil.
    _wrap_ref_unknown_sentinel(server, upstreams)
    _wrap_authorization_required(server, upstreams, authorizers)
    return server


def _wrap_ref_unknown_sentinel(server: Server, upstreams: dict[str, Upstream]) -> None:
    """Convertit le sentinel REF_UNKNOWN (texte isError) en erreur JSON-RPC.

    Le SDK MCP (@server.call_tool(), voir mcp/server/lowlevel/server.py) avale
    toute exception levée par l'outil appelé — y compris McpError — et la
    transforme en CallToolResult(isError=True). C'est incompatible avec le
    contrat client (brief A, D6) qui attend une vraie erreur JSON-RPC
    data.code == 'REF_UNKNOWN' pour déclencher le rejeu avec contenu inliné.

    Seule voie compatible SDK : remplacer le handler déjà enregistré sous
    types.CallToolRequest (server.request_handlers), inspecter son résultat, et
    lever McpError quand le sentinel est détecté — _handle_request (L777)
    convertit alors l'exception en erreur JSON-RPC (`response = err.error`).

    Portée (PRX1) : la conversion ne s'applique qu'aux outils routés vers un
    upstream inprocess dont le module expose lui-même REF_UNKNOWN_SENTINEL et
    REF_UNKNOWN_ERROR_CODE (lus au start(), cf. InProcessUpstream — le proxy ne
    connaît ni mcp_docs ni aucun sentinel en propre). Le sentinel étant cherché
    par sous-chaîne (FastMCP
    préfixe le message avant qu'il n'atteigne isError, le match ne peut pas être
    ancré en tête), sans ce scoping un message d'erreur quelconque contenant
    « REF_UNKNOWN » — autre serveur, outil citant la constante — déclencherait un
    rejeu client inutile. Les upstreams stdio sont hors périmètre : le proxy ne
    peut pas lire de constante dans un subprocess, un serveur stdio qui voudrait
    ce contrat devrait lever l'erreur JSON-RPC lui-même.
    """
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    original_handler = server.request_handlers[types.CallToolRequest]

    async def handler(req: types.CallToolRequest):
        result = await original_handler(req)
        name = req.params.name
        prefix = name.split("__", 1)[0] if "__" in name else None
        upstream = upstreams.get(prefix) if prefix is not None else None
        # Contrat résolu à l'appel, pas à la construction : build_proxy_server()
        # s'exécute avant le lifespan qui démarre les upstreams, or
        # ref_unknown_contract n'est renseigné que par start(). Un instantané pris
        # ici capturerait un dict vide et désactiverait REF_UNKNOWN en silence.
        contract = getattr(upstream, "ref_unknown_contract", None)
        # Forme validée, pas dépaquetée à l'aveugle : `ref_unknown_contract` est
        # un attribut d'upstream (déclaratif, renseigné hors de ce module) — une
        # valeur mal formée doit laisser passer le résultat tel quel, pas faire
        # planter chaque tools/call sur un ValueError d'unpacking.
        if not (isinstance(contract, tuple) and len(contract) == 2):
            return result
        sentinel, error_code = contract
        if not isinstance(sentinel, str) or not isinstance(error_code, int):
            return result
        call_result = result.root
        if call_result.isError and call_result.content:
            first = call_result.content[0]
            text = getattr(first, "text", "")
            # FastMCP préfixe le message d'exception ("Error executing tool
            # <name>: ...") avant qu'il n'atteigne isError — le sentinel n'est
            # donc pas forcément en tête du texte final, juste présent dedans.
            if sentinel in text:
                raise McpError(
                    ErrorData(
                        code=error_code,
                        message=text,
                        data={"code": "REF_UNKNOWN"},
                    )
                )
        return result

    server.request_handlers[types.CallToolRequest] = handler
