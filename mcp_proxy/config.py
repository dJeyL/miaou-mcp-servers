"""Lecture de la config (couches : `layers.py`) et construction de la table
d'upstreams."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contract import is_disabled
from .layers import load_layers
from .netproxy import merge_proxy_env_overrides
from .volatile import VolatileMarkerError, format_path, iter_markers, keys_in
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


def _refuse_markers(where: str, value: Any, why: str) -> None:
    """Refus d'un marqueur volatil là où le proxy ne le résout pas : il
    arriverait tel quel, un objet à la place d'une chaîne, à un `env` de
    subprocess, à un en-tête httpx ou au bloc `auth`, et casserait loin de sa
    cause. Un marqueur mal formé à cet endroit est refusé de même."""
    try:
        found = next(iter_markers(value), None)
    except VolatileMarkerError as e:
        raise ValueError(f"{where} : {e}.") from None
    if found is not None:
        path, key = found
        sub = format_path(path)
        raise ValueError(
            f"{where}{'.' + sub if sub else ''} : secret volatil « {key} » {why}"
        )


def _volatile_key(name: str, config: Any) -> tuple[str | None, str | None]:
    """(clef, erreur) du `config` d'une entrée inprocess. Une clef par upstream
    en v1 : plusieurs, ou un marqueur mal formé, écartent l'upstream au
    démarrage avec ce motif (les autres démarrent)."""
    try:
        keys = keys_in(config)
    except VolatileMarkerError as e:
        return None, f"config.{e}"
    if len(keys) > 1:
        listed = ", ".join(f"« {k} »" for k in keys)
        return None, (
            f"plusieurs secrets volatils ({listed}) : un seul par upstream est "
            f"pris en charge"
        )
    return (keys[0] if keys else None), None


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
    _refuse_markers(
        "config", {k: v for k, v in cfg.items() if k != "mcpServers"},
        "hors du bloc 'config' d'un serveur inprocess : seul endroit où le "
        "proxy le résout.",
    )
    for name, srv in cfg.get("mcpServers", {}).items():
        if is_disabled(srv, f"mcpServers.{name}"):
            continue
        srv_type = srv.get("type", "stdio")
        _refuse_markers(
            f"mcpServers.{name}",
            {k: v for k, v in srv.items() if not (srv_type == "inprocess" and k == "config")},
            "hors du bloc 'config' d'un serveur inprocess : le proxy ne le "
            "résout que là (ni 'env', ni upstream stdio ou http).",
        )
        if srv_type == "inprocess":
            module = srv.get("module")
            if not module:
                raise ValueError(f"Serveur '{name}' inprocess sans clé 'module'.")
            if "protocol_era" in srv:
                raise ValueError(
                    f"Serveur '{name}' : la clé 'protocol_era' n'a de sens que sur un "
                    f"upstream 'stdio' ou 'http' (un inprocess n'a pas de fil)."
                )
            upstream = InProcessUpstream(
                module, env=srv.get("env"), config=srv.get("config")
            )
            upstream.volatile_key, upstream.volatile_error = _volatile_key(
                name, srv.get("config")
            )
            upstreams[name] = upstream
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
