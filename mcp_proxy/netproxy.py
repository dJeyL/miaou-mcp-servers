"""Override CLI du proxy réseau (`--proxy` / `--noproxy`).

Calcule les variables d'environnement `*_proxy` vues par les upstreams, sans
toucher au processus tant que `apply_proxy_env_overrides_to_process` n'est pas
appelé.
"""

from __future__ import annotations

import os


# Les 4 variantes de casse lues par urllib (make_opener) comme par la plupart
# des clients HTTP — on les pose/efface toutes pour ne pas laisser une variante
# existante contredire l'override CLI.
_PROXY_ENV_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")


def resolve_proxy_url(raw: str) -> str:
    """Ajoute "http://" si l'argument --proxy ne porte aucun schéma."""
    return raw if "://" in raw else f"http://{raw}"


def compute_proxy_env_overrides(
    proxy: str | None, noproxy: bool
) -> dict[str, str | None] | None:
    """Calcule les overrides d'environnement proxy à appliquer partout (inprocess
    via os.environ du process, stdio via le dict env du subprocess).

    None → aucun override (comportement inchangé, ni --proxy ni --noproxy).
    Une valeur str → variable posée à cette valeur (ex. "http://host:port").
    Une valeur None → variable à supprimer/ne pas transmettre (--noproxy).

    --proxy et --noproxy sont absolus : ils priment sur tout http_proxy/https_proxy
    déjà défini dans le `env` d'une entrée config.json (choix explicite : la CLI
    est la garantie ultime de contrôle du proxy réseau vu par les upstreams).
    """
    if noproxy:
        return {key: None for key in _PROXY_ENV_KEYS}
    if proxy:
        url = resolve_proxy_url(proxy)
        return {key: url for key in _PROXY_ENV_KEYS}
    return None


def apply_proxy_env_overrides_to_process(overrides: dict[str, str | None]) -> None:
    """Applique les overrides à os.environ du process proxy lui-même — couvre tous
    les upstreams inprocess, qui partagent ce process (make_opener() dans
    mcp_base.py relit os.environ à chaque requête via ProxyHandler())."""
    for key, value in overrides.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def merge_proxy_env_overrides(
    env: dict[str, str] | None, overrides: dict[str, str | None] | None
) -> dict[str, str] | None:
    """Fusionne les overrides proxy dans le `env` d'un upstream stdio (config.json
    prioritaire par défaut, mais CLI écrase toujours — cf. compute_proxy_env_overrides).
    Ignoré si overrides est None (ni --proxy ni --noproxy) : env repart inchangé,
    y compris None (StdioServerParameters distingue env=None de env={})."""
    if overrides is None:
        return env
    merged = dict(env) if env else {}
    for key, value in overrides.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged
