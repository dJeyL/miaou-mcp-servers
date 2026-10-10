"""Lecture de la config (couches : `layers.py`) et construction de la table
d'upstreams."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contract import is_disabled
from .layers import load_layers
from .netproxy import merge_proxy_env_overrides
from .upstream import (
    HttpUpstream,
    InProcessUpstream,
    StdioUpstream,
    Upstream,
    _HTTP_HANDSHAKE_TIMEOUT_S,
)


# Valeurs de la clé `protocol_era` d'une entrée stdio ou http : l'ère est négociée
# ("auto", défaut) ou imposée en legacy — le recours pour un upstream moderne
# d'un autre SDK que la validation de la révision 2026-07-28 ferait échouer.
_PROTOCOL_ERA_VALUES = ("auto", "legacy")


def _protocol_era(name: str, srv: dict[str, Any]) -> str:
    era = srv.get("protocol_era", "auto")
    if era not in _PROTOCOL_ERA_VALUES:
        raise ValueError(
            f"Serveur '{name}' : 'protocol_era' vaut {era!r}, attendu "
            f"{' ou '.join(repr(v) for v in _PROTOCOL_ERA_VALUES)}."
        )
    return era


def _config_instructions(name: str, srv: dict[str, Any]) -> str | None:
    text = srv.get("instructions")
    if text is not None and not isinstance(text, str):
        raise ValueError(
            f"Serveur '{name}' : 'instructions' doit être une chaîne, "
            f"reçu {type(text).__name__}."
        )
    return text


def load_config(path: str | Path) -> dict[str, Any]:
    """Config effective d'un fichier seul (fusion et chaîne : `layers`)."""
    return load_layers([Path(path)]).cfg


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
            if "protocol_era" in srv:
                raise ValueError(
                    f"Serveur '{name}' : la clé 'protocol_era' n'a de sens que sur un "
                    f"upstream 'stdio' ou 'http' (un inprocess n'a pas de fil)."
                )
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
                protocol_era=_protocol_era(name, srv),
            )
        elif srv_type == "http":
            url = srv.get("url")
            if not url:
                raise ValueError(f"Serveur '{name}' http sans clé 'url'.")
            upstreams[name] = HttpUpstream(
                url=url,
                headers=srv.get("headers"),
                timeout=srv.get("timeout", _HTTP_HANDSHAKE_TIMEOUT_S),
                protocol_era=_protocol_era(name, srv),
            )
        else:
            raise ValueError(f"Type de serveur inconnu pour '{name}': '{srv_type}'.")
        upstreams[name].config_instructions = _config_instructions(name, srv)
    return upstreams
