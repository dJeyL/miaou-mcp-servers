"""Serveur MCP proxy pour MIAOU — agrège plusieurs serveurs MCP upstream.

Transport streamable-http uniquement (HTTP ↔ MIAOU). Les upstreams peuvent être
in-process (import Python direct, pas de subprocess) ou stdio (subprocess externe).

Tous les outils upstream sont exposés préfixés du nom de serveur suivi de "__" :
    bench__echo, bench__get_image, weather__get_weather, …

Configuration : config.json (non versionné, copier config.sample.json).

Lancement :
    uv run -m mcp_proxy                            # lit config.json, port dedans
    uv run -m mcp_proxy --config mon_config.json   # config alternative
    uv run -m mcp_proxy --host 0.0.0.0             # override host
    uv run -m mcp_proxy --port 8765                # override port

Dans MIAOU → Paramètres → Serveurs MCP → Ajouter :
    Nom       : proxy
    URL       : http://127.0.0.1:<port>/mcp
    Transport : streamable-http
    Activé    : oui

Ce module ré-exporte la surface publique du fichier plat `mcp_proxy.py` dont il
prend la suite : `import mcp_proxy` puis `mcp_proxy.X` continue de fonctionner
pour les symboles qui existaient avant la mise en paquet. Une exception voulue,
`_AUTH_DEBUG` — voir son docstring dans `auth_out.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Rend les modules servers/ importables directement ("mcp_bench", "mcp_weather")
# quand le proxy est lancé depuis la racine du projet. DOIT précéder les imports
# de sous-modules ci-dessous : `upstream` et `entry` importent `mcp_base`, qui
# n'est atteignable qu'une fois ce chemin inséré.
#
# `parent.parent` et non `parent` : ce fichier vit dans le paquet, un niveau plus
# bas que l'ancien `mcp_proxy.py` qui était à la racine.
_SERVERS_DIR = Path(__file__).parent.parent / "servers"
if _SERVERS_DIR.exists() and str(_SERVERS_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVERS_DIR))

from .app import build_app  # noqa: E402
from .auth_in import (  # noqa: E402
    AuthConfigError,
    JwtAudienceError,
    JwtTokenVerifier,
    _audience_matches,
    _discover_jwks_uri,
    build_token_verifier,
    resolve_auth_config,
)
from .auth_out import (  # noqa: E402
    _AUTH_PROBE_TOOL,
    _AUTHORIZATION_WAIT_S,
    _AUTHORIZE_ROUTE_WAIT_S,
    _CALLBACK_PAGE,
    _MCP_PROTOCOL_VERSION,
    _MUTATING_HINTS,
    _REFRESH_MARGIN_S,
    _REFRESH_POLL_INTERVAL_S,
    _RENEWAL_NOOP_FRACTION,
    _SENSITIVE_QUERY_KEYS,
    _TOKENS_FILE_MODE,
    PendingAuthorization,
    UpstreamAuthorizer,
    UpstreamTokenStorage,
    _auth_debug_enabled,
    _default_tokens_path,
    _looks_mutating,
    _pick_probe_tool,
    _redact_url,
    _refresh_poll_interval,
    _write_secret_file,
    build_authorize_route,
    build_callback_route,
    build_callback_url,
    build_client_info_override,
    build_oauth_metadata_override,
    build_upstream_authorizers,
    enable_auth_debug,
    format_authorization_notice,
    render_callback_page,
)
from .config import build_upstreams, load_config  # noqa: E402
from .contract import (  # noqa: E402
    AUTHORIZATION_REQUIRED,
    AuthorizationRequired,
    authorize_path,
)
from .entry import main, run_with_dev_auth  # noqa: E402
from .logging import _log  # noqa: E402
from .netproxy import (  # noqa: E402
    _PROXY_ENV_KEYS,
    apply_proxy_env_overrides_to_process,
    compute_proxy_env_overrides,
    merge_proxy_env_overrides,
    resolve_proxy_url,
)
from .server import (  # noqa: E402
    _AUTHORIZATION_SENTINEL,
    _INSTRUCTIONS_PREAMBLE,
    STATUS_TOOL_NAME,
    UNAUTHORIZED_UPSTREAMS_META_KEY,
    ToolCatalogCache,
    UpstreamNotAuthorized,
    _resolve_via_prefix,
    _status_tool,
    _wrap_authorization_required,
    _wrap_ref_unknown_sentinel,
    aggregate_instructions,
    build_proxy_server,
    build_status_report,
    format_stale_description,
    upstream_is_live,
)
from .upstream import (  # noqa: E402
    _HTTP_HANDSHAKE_TIMEOUT_S,
    _STDIO_HANDSHAKE_TIMEOUT_S,
    HttpUpstream,
    InProcessUpstream,
    StdioUpstream,
    Upstream,
    _unwrap_exception_group,
)
