"""Fabrique de l'application Starlette servie par uvicorn."""

from __future__ import annotations

import sys
from typing import Any

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount

from .auth_in import build_token_verifier
from .auth_out import (
    AuthorizationRequired,
    UpstreamAuthorizer,
    _refresh_poll_interval,
    build_authorize_route,
    build_callback_route,
)
from .logging import _log
from .server import aggregate_instructions, authorize_path
from .upstream import HttpUpstream, Upstream


def build_app(
    mcp_server: Server,
    upstreams: dict[str, Upstream],
    auth: dict[str, Any] | None = None,
    token_verifier: Any = None,
    authorizers: dict[str, UpstreamAuthorizer] | None = None,
) -> Any:
    """`auth` : sortie de resolve_auth_config(), ou None (auth désactivée —
    comportement d'avant le lot AB-1, à l'octet près).

    `token_verifier` : implémentation du protocol mcp.server.auth.provider.
    TokenVerifier. Injectable pour les tests ; None → construit depuis `auth`.

    `authorizers` : sortie de build_upstream_authorizers() (auth SORTANTE, lot
    AB-2). Non vide → la route /callback est servie. Rien à voir avec `auth`,
    qui gouverne l'auth entrante : un proxy peut faire l'une, l'autre, les deux
    ou aucune.
    """
    from contextlib import asynccontextmanager

    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        event_store=None,
        json_response=False,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        import anyio

        async def _start_upstreams() -> list[Upstream]:
            _log(f"Upstream servers ({len(upstreams)}):")
            # Un upstream qui refuse de démarrer (clef d'API absente, module
            # introuvable, subprocess qui ne répond pas) ne doit pas empêcher le
            # proxy de servir les autres : on le signale, on le retire de la table
            # de routage, et on continue. Retrait indispensable — un upstream resté
            # dans la table serait visible de _resolve_via_prefix et un appel
            # d'outil échouerait de façon obscure au lieu d'être simplement absent
            # de tools/list.
            #
            # UNE exception, et une seule : « pas encore autorisé » n'est pas une
            # panne. Ça ne se répare pas par un redémarrage mais par un clic, et
            # le retirer rendrait son propre parcours d'autorisation
            # inatteignable (/authorize/{name} ne le trouverait plus). Il reste
            # donc dans la table, sans session — cf. AuthorizationRequired.
            # Ce qu'on sait AVANT d'appeler quoi que ce soit : un upstream
            # OAuth dont le stockage ne porte pas de jeton utilisable exigera
            # une autorisation. Purement local (lecture de fichier), donc
            # gratuit et sans requête sortante — le boot n'est pas retardé.
            #
            # Sans cet amorçage, le seul événement qui révèle l'autorisation
            # manquante est un `_on_redirect`, lequel n'a lieu qu'au premier
            # appel réellement émis : un upstream qui accepte `initialize` ET
            # `tools/list` sans jeton (Jira d'entreprise, observé en
            # production) resterait donc annoncé sans réserve jusqu'à ce que
            # l'utilisateur se prenne l'échec. C'est précisément ce que la
            # surface `_meta` existe pour éviter.
            for name, authorizer in (authorizers or {}).items():
                storage = getattr(authorizer, "_storage", None)
                probe = getattr(storage, "has_usable_token", None)
                if probe is None:
                    continue
                try:
                    if not await probe():
                        authorizer.authorization_pending = True
                        continue
                    # Le stockage dit « utilisable », ce qui inclut UN JETON
                    # EXPIRÉ porteur d'un refresh token : utilisable au sens
                    # où il se renouvelle sans l'utilisateur, pas au sens où
                    # il partirait tel quel. Tenter le renouvellement ICI
                    # plutôt que d'attendre le premier appel ou le premier
                    # réveil de la boucle (plusieurs minutes), sans quoi le
                    # proxy démarre en annonçant un upstream disponible dont
                    # le tout premier appel d'outil échouera — mesuré sur le
                    # terrain le 2026-09-08.
                    #
                    # Ne renouvelle QUE si l'échéance est proche
                    # (`refresh_if_due` sort sans requête sinon) : un
                    # démarrage avec des jetons frais ne coûte rien.
                    refresher = getattr(authorizer, "refresh_if_due", None)
                    if refresher is not None:
                        await refresher()
                except Exception as e:
                    # Une surface facultative ne doit jamais empêcher un
                    # démarrage : au pire on ne signale pas, et le refus à
                    # l'appel reste le filet.
                    _log(f"  {name:<12} état d'autorisation indéterminé ({e})")

            started: list[Upstream] = []
            failed: list[str] = []
            for name, upstream in list(upstreams.items()):
                try:
                    await upstream.start()
                except AuthorizationRequired as e:
                    _log(f"  {name:<12} unauthorized — {e}")
                    _log(f"  {'':<12} autoriser : {authorize_path(name)}")
                    continue
                except Exception as e:
                    failed.append(name)
                    _log(f"  {name:<12} unavailable — {e}")
                    continue
                started.append(upstream)
                # Le nombre d'outils n'est connu qu'après start() : un upstream
                # inprocess n'a pas encore son _tool_manager avant, et un stdio
                # n'a pas fait son handshake. Un upstream qui démarre mais dont
                # list_tools() échoue ne doit pas empêcher le proxy de servir les
                # autres — on le signale sans propager.
                try:
                    count = len(await upstream.list_tools())
                    detail = f"{count} tool{'s' if count != 1 else ''}"
                except Exception as e:
                    detail = f"tools unavailable ({type(e).__name__})"
                _log(f"  {name:<12} {detail}")
            for name in failed:
                del upstreams[name]
            return started

        # UN task group, de la durée de vie du proxy, qui héberge deux sortes de
        # tâches : les tâches de service des upstreams HTTP (leurs contextes
        # anyio doivent être ouverts ET refermés dans la même tâche, cf.
        # HttpUpstream) et les parcours d'autorisation déclenchés par
        # /authorize/{name} (qui doivent survivre à la requête qui les lance,
        # sans quoi le navigateur resterait suspendu pendant qu'on attend son
        # propre retour sur /callback).
        async def _refresh_loop() -> None:
            """Renouvelle d'avance les jetons qui vont expirer.

            Le refresh du SDK étant passif (il n'a lieu qu'au passage d'une
            requête), un upstream inutilisé assez longtemps perd son access
            token PUIS son refresh token, et redemande une autorisation
            manuelle. Cette boucle couvre cette inactivité, et elle seule : le
            chemin normal reste le refresh passif, au fil des appels.

            Jamais bruyante quand tout va bien — `refresh_if_due` ne fait rien
            tant que l'échéance est loin, et ne log que lorsqu'il se passe
            quelque chose. Une exception ici ne doit pas emporter le task
            group, donc le proxy : on la signale et on continue.
            """
            while True:
                # Recalculé à CHAQUE tour : au premier réveil aucun jeton n'a
                # encore été observé, et la période se resserre d'elle-même dès
                # qu'on connaît la durée de vie réellement émise par l'AS.
                await anyio.sleep(_refresh_poll_interval(authorizers))
                for name, authorizer in (authorizers or {}).items():
                    try:
                        await authorizer.refresh_if_due()
                    except Exception as e:  # pragma: no cover - filet
                        _log(f"Renouvellement de '{name}' interrompu ({e}).")

        async with anyio.create_task_group() as tg:
            app.state.task_group = tg
            for upstream in upstreams.values():
                if isinstance(upstream, HttpUpstream):
                    upstream.host_tasks_in(tg)

            started: list[Upstream] = []
            try:
                started = await _start_upstreams()
                # Écriture différée, et non un paramètre de construction :
                # build_proxy_server() s'exécute AVANT start(), donc avant que
                # le moindre upstream ait été interrogé — les instructions y
                # seraient vides pour tout le monde, en silence. Le SDK relit
                # `Server.instructions` à chaque create_initialization_options()
                # (lowlevel/server.py), si bien qu'une écriture postérieure à la
                # construction est vue par tout `initialize` client, y compris
                # le premier : aucun client ne peut avoir fait son handshake
                # avant, session_manager.run() n'ayant pas encore démarré.
                mcp_server.instructions = aggregate_instructions(upstreams)
                # Lancée APRÈS le démarrage : elle n'a rien à faire tant qu'un
                # upstream n'a pas de jeton, et le premier réveil est de toute
                # façon différé d'un intervalle. Annulée avec le task group à
                # l'extinction (cancel_scope.cancel() du finally).
                if authorizers:
                    tg.start_soon(_refresh_loop)
                # session_manager.run() initialise le task group interne requis pour
                # traiter les requêtes MCP (sans ça : RuntimeError "Task group is not initialized").
                async with session_manager.run():
                    yield
            finally:
                for upstream in started:
                    try:
                        await upstream.stop()
                    except Exception:
                        pass
                # Sans annulation explicite, les tâches de service encore
                # vivantes retiendraient le task group — donc l'extinction.
                tg.cancel_scope.cancel()

    async def handle_mcp(scope: Any, receive: Any, send: Any) -> None:
        await session_manager.handle_request(scope, receive, send)

    routes: list[Any] = []
    middleware: list[Any] = []
    mcp_endpoint: Any = handle_mcp

    if auth is not None:
        from starlette.middleware import Middleware
        from starlette.middleware.authentication import AuthenticationMiddleware
        from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
        from mcp.server.auth.middleware.bearer_auth import (
            BearerAuthBackend,
            RequireAuthMiddleware,
        )
        from mcp.server.auth.routes import (
            build_resource_metadata_url,
            create_protected_resource_routes,
        )
        from pydantic import AnyHttpUrl

        if token_verifier is None:
            token_verifier = build_token_verifier(auth)

        # Métadonnées de ressource protégée (RFC 9728). Servies à
        # /.well-known/oauth-protected-resource/mcp — le chemin de la ressource
        # est SUFFIXÉ au well-known (RFC 9728 §3.1), ce n'est pas la racine.
        # Ces routes restent PUBLIQUES : un client non authentifié doit pouvoir
        # les lire, c'est tout leur objet. Les envelopper dans l'auth ferme la
        # boucle — le client ne peut alors jamais apprendre où s'authentifier.
        routes.extend(
            create_protected_resource_routes(
                resource_url=AnyHttpUrl(auth["resource_url"]),
                authorization_servers=[
                    AnyHttpUrl(u) for u in auth["authorization_servers"]
                ],
                scopes_supported=auth.get("scopes_supported"),
                resource_name="miaou-proxy",
            )
        )

        # AuthenticationMiddleware peuple scope["user"] SANS exiger de jeton ;
        # c'est RequireAuthMiddleware, posé sur le seul endpoint MCP, qui exige.
        middleware = [
            Middleware(
                AuthenticationMiddleware,
                backend=BearerAuthBackend(token_verifier),
            ),
            Middleware(AuthContextMiddleware),
        ]

        # resource_metadata_url est ce qui fait porter au 401 le pointeur
        # `resource_metadata="…"` du header WWW-Authenticate — sans lui, le
        # client reçoit un 401 nu et ne sait pas où aller. La branche qui
        # l'ajoute porte un `# pragma: no cover` dans le SDK : non couverte en
        # amont, donc épinglée par un test de ce dépôt.
        mcp_endpoint = RequireAuthMiddleware(
            handle_mcp,
            auth["required_scopes"],
            build_resource_metadata_url(AnyHttpUrl(auth["resource_url"])),
        )

    if authorizers:
        # Posées AVANT le Mount("/mcp") : une route explicite doit être trouvée
        # avant un montage de préfixe (Starlette retient la première qui matche).
        # Publiques à dessein, cf. build_callback_route.
        routes.append(build_callback_route(authorizers))
        routes.append(build_authorize_route(authorizers, upstreams))

    routes.append(Mount("/mcp", app=mcp_endpoint))

    app = Starlette(
        routes=routes,
        middleware=middleware,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
        allow_headers=["*"],
        # WWW-Authenticate est exposé sinon un client navigateur en
        # cross-origin ne peut pas LIRE le header du 401 qu'on prend soin
        # d'émettre : il verrait un 401 nu. Invisible en test curl (pas de
        # politique CORS), fatal en usage réel.
        # allow_credentials reste ABSENT : le combo avec allow_origins=["*"]
        # est interdit par la spec CORS, et c'est ce qui fait accepter
        # l'Origin: null de MIAOU ouvert en file://.
        expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],
    )

    # Mount("/mcp", ...) traite /mcp comme un préfixe et redirige en 307 vers
    # /mcp/ (strict-slash Starlette). Beaucoup de clients MCP tapent /mcp sans
    # slash final et ne suivent pas la redirection sur les requêtes POST/DELETE.
    # On réécrit le path en amont du routeur pour éviter la redirection.
    inner_app = app

    async def strip_trailing_slash_redirect(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"
        await inner_app(scope, receive, send)

    return strip_trailing_slash_redirect
