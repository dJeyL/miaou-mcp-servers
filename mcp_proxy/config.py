"""Lecture de la config (couches : `layers.py`) et construction de la table
d'upstreams."""

from __future__ import annotations

import math
import re
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
    _CALL_TIMEOUT_S,
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


# Le nom d'un upstream préfixe ses outils (`<nom>__<outil>`, coupé au premier
# `__`) et ouvre les URI de ses skills (`skill://<nom>/…`, coupé au premier
# `/`) : il ne peut contenir ni l'un ni l'autre, ni finir par `_` (le `__`
# du préfixe se lirait alors un caractère trop tôt).
_UPSTREAM_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


def _check_upstream_name(name: str) -> None:
    if (
        not _UPSTREAM_NAME_RE.fullmatch(name)
        or "__" in name
        or name.endswith("_")
    ):
        raise ValueError(
            f"Serveur {name!r} : nom d'upstream invalide. Lettres, chiffres, `-` et `_` "
            f"seulement, sans `__` ni `_` final : le nom préfixe les outils "
            f"(`<nom>__<outil>`) et les URI de skills (`skill://<nom>/…`)."
        )


def _call_timeout(name: str, srv: dict[str, Any]) -> float:
    """Borne d'un appel d'outil vers un upstream stdio ou http, en secondes."""
    value = srv.get("call_timeout", _CALL_TIMEOUT_S)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(
            f"Serveur '{name}' : 'call_timeout' vaut {value!r}, attendu un nombre de "
            f"secondes fini et supérieur à 0."
        )
    return value


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


# Entrées que `config.defaults.json` portait pour des serveurs supprimés : un
# `config.json` qui les patchait encore n'en garde, fusionné, qu'un reste sans
# `type` ni `command`, que le message doit expliquer.
_REMOVED_ENTRIES = {"duckduckgo": "mcp_ddg", "brave": "mcp_brave"}


def _typed(name: str, srv: dict[str, Any], key: str, kinds: type | tuple, label: str) -> Any:
    """`srv[key]` s'il est de l'un des `kinds` (ou absent, ou `null`), sinon une
    ValueError qui nomme la clé : la forme d'une entrée se refuse ici, au
    chargement, et non en AttributeError au premier usage."""
    value = srv.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, kinds):
        raise ValueError(
            f"Serveur '{name}' : '{key}' doit être {label}, reçu {type(value).__name__}."
        )
    return value


def _check_entry_shape(name: str, srv: dict[str, Any], srv_type: Any) -> None:
    if srv_type == "inprocess":
        _typed(name, srv, "module", str, "une chaîne")
        _typed(name, srv, "env", dict, "un objet")
        _typed(name, srv, "config", dict, "un objet")
    elif srv_type == "stdio":
        _typed(name, srv, "command", str, "une chaîne")
        _typed(name, srv, "env", dict, "un objet")
        _typed(name, srv, "cwd", str, "une chaîne")
        args = _typed(name, srv, "args", list, "une liste de chaînes")
        if args is not None and not all(isinstance(a, str) for a in args):
            raise ValueError(f"Serveur '{name}' : 'args' doit être une liste de chaînes.")
    elif srv_type == "http":
        _typed(name, srv, "url", str, "une chaîne")
        _typed(name, srv, "headers", dict, "un objet")
        _typed(name, srv, "timeout", (int, float), "un nombre")


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
    servers = cfg.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"'mcpServers' doit être un objet, reçu {type(servers).__name__}.")
    for name, srv in servers.items():
        if not isinstance(srv, dict):
            raise ValueError(
                f"mcpServers.{name} doit être un objet, reçu {type(srv).__name__}."
            )
        if is_disabled(srv, f"mcpServers.{name}"):
            continue
        _check_upstream_name(name)
        srv_type = srv.get("type", "stdio")
        _check_entry_shape(name, srv, srv_type)
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
            for key in ("protocol_era", "call_timeout"):
                if key in srv:
                    raise ValueError(
                        f"Serveur '{name}' : la clé '{key}' n'a de sens que sur un "
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
                if name in _REMOVED_ENTRIES and "type" not in srv:
                    # Reste d'une entrée que `config.defaults.json` portait : la
                    # fusion n'en garde que ce que `config.json` y ajoutait.
                    raise ValueError(
                        f"Serveur '{name}' : {_REMOVED_ENTRIES[name]} a été supprimé "
                        f"(remplacé par search/image_search de 'web') et n'est plus "
                        f"dans config.defaults.json. Retirer mcpServers.{name} de "
                        f"config.json (une clef Brave va dans "
                        f"mcpServers.web.config.search.brave.api_key)."
                    )
                raise ValueError(f"Serveur '{name}' stdio sans clé 'command'.")
            env = merge_proxy_env_overrides(srv.get("env"), proxy_env_overrides)
            upstreams[name] = StdioUpstream(
                command=command,
                args=srv.get("args", []),
                env=env,
                cwd=srv.get("cwd"),
                protocol_era=_protocol_era(name, srv),
                call_timeout=_call_timeout(name, srv),
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
                call_timeout=_call_timeout(name, srv),
            )
        else:
            raise ValueError(f"Type de serveur inconnu pour '{name}': '{srv_type}'.")
        upstreams[name].config_instructions = _config_instructions(name, srv)
    return upstreams
