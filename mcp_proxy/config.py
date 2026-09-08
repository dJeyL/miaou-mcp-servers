"""Lecture de `config.json` et construction de la table d'upstreams."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contract import is_disabled
from .netproxy import merge_proxy_env_overrides
from .upstream import (
    HttpUpstream,
    InProcessUpstream,
    StdioUpstream,
    Upstream,
    _HTTP_HANDSHAKE_TIMEOUT_S,
)


def load_config(path: str | Path) -> dict[str, Any]:
    try:
        cfg = json.loads(Path(path).read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"La config '{path}' n'est pas un JSON valide : {e}") from e
    if "port" not in cfg:
        raise ValueError(f"La config '{path}' doit contenir la clé 'port'.")
    return cfg


def build_upstreams(
    cfg: dict[str, Any],
    proxy_env_overrides: dict[str, str | None] | None = None,
) -> dict[str, Upstream]:
    """`proxy_env_overrides` (cf. compute_proxy_env_overrides) : appliqué au `env`
    de chaque upstream stdio. Les inprocess n'en ont pas besoin ici — ils partagent
    l'environnement du process proxy, déjà modifié directement par main() via
    apply_proxy_env_overrides_to_process() avant l'import des modules. Les http
    non plus, et pour la même raison : leur client httpx lit os.environ du
    process (trust_env=True par défaut), déjà modifié au même endroit."""
    upstreams: dict[str, Upstream] = {}
    for name, srv in cfg.get("mcpServers", {}).items():
        if is_disabled(srv, f"mcpServers.{name}"):
            continue
        srv_type = srv.get("type", "stdio")
        if srv_type == "inprocess":
            module = srv.get("module")
            if not module:
                raise ValueError(f"Serveur '{name}' inprocess sans clé 'module'.")
            upstreams[name] = InProcessUpstream(
                module, env=srv.get("env"), config=srv.get("config")
            )
        elif srv_type == "stdio":
            command = srv.get("command")
            if not command:
                raise ValueError(f"Serveur '{name}' stdio sans clé 'command'.")
            env = merge_proxy_env_overrides(srv.get("env"), proxy_env_overrides)
            upstreams[name] = StdioUpstream(
                command=command,
                args=srv.get("args", []),
                env=env,
                cwd=srv.get("cwd"),
            )
        elif srv_type == "http":
            url = srv.get("url")
            if not url:
                raise ValueError(f"Serveur '{name}' http sans clé 'url'.")
            upstreams[name] = HttpUpstream(
                url=url,
                headers=srv.get("headers"),
                timeout=srv.get("timeout", _HTTP_HANDSHAKE_TIMEOUT_S),
            )
        else:
            raise ValueError(f"Type de serveur inconnu pour '{name}': '{srv_type}'.")
    return upstreams
