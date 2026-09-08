"""Auth sortante — le proxy est client OAuth 2.1 d'upstreams tiers (AB-2/AB-3).

Sans rapport avec l'auth entrante de `auth_in` : ici le proxy PRÉSENTE un jeton
à un serveur tiers, là il en VÉRIFIE un présenté par son client.

Découpé en quatre modules, du plus bas au plus haut :

    debug.py       --debug-auth : traces, masquage des valeurs sensibles
    probe.py       quel outil appeler pour provoquer un refus, et comment le lire
    storage.py     persistance des jetons (UpstreamTokenStorage)
    authorizer.py  le client OAuth : parcours, renouvellement, routes Starlette

Ce module ré-exporte leur surface : les importateurs (`app`, `entry`, les tests)
continuent d'écrire `from .auth_out import X` sans savoir lequel des quatre le
porte. Une exception voulue, `_AUTH_DEBUG` — il n'est PAS réexporté ici, parce
qu'un patch visant cette réexportation serait silencieusement inopérant ; voir
son docstring dans `debug.py`.
"""

from __future__ import annotations

# Réexporté bien que défini dans `contract` : le fichier plat d'avant le
# découpage l'exposait ici, et `app` comme les tests l'y importent toujours.
from ..contract import AuthorizationRequired  # noqa: F401

from .authorizer import (
    _AUTHORIZATION_WAIT_S,
    _AUTHORIZE_ROUTE_WAIT_S,
    _CALLBACK_PAGE,
    _REFRESH_MARGIN_S,
    _REFRESH_POLL_INTERVAL_S,
    _RENEWAL_NOOP_FRACTION,
    PendingAuthorization,
    UpstreamAuthorizer,
    _refresh_poll_interval,
    build_authorize_route,
    build_callback_route,
    build_callback_url,
    build_client_info_override,
    build_oauth_metadata_override,
    build_upstream_authorizers,
    format_authorization_notice,
    render_callback_page,
)
from .debug import (
    _SENSITIVE_QUERY_KEYS,
    _auth_debug_enabled,
    _redact_url,
    enable_auth_debug,
)
from .probe import (
    _AUTH_PROBE_TOOL,
    _MCP_PROTOCOL_VERSION,
    _MUTATING_HINTS,
    _looks_mutating,
    _pick_probe_tool,
)
from .storage import (
    _TOKENS_FILE_MODE,
    UpstreamTokenStorage,
    _default_tokens_path,
    _write_secret_file,
)
