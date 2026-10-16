"""Surveillance de la config (lot AV) : empreinte de la config fusionnée,
chemins changés depuis le démarrage, et validation partagée avec `main()`.

**Ce qu'on empreinte** : la fusion des couches AVANT substitution des `${VAR}`
(`LoadedConfig.merged`), sans les `_comment*`, en JSON canonique. Avant
substitution parce qu'`os.environ` bouge après le chargement (`--proxy`,
`env` d'un upstream inprocess) : une empreinte prise après changerait sans que
personne ait touché un fichier. Sans les commentaires parce qu'une note
ajoutée ne demande aucun redémarrage. Le CONTENU, jamais les dates : un
fichier ré-enregistré à l'identique est inchangé. Limite : une variable
d'environnement changée dans le script de lancement n'est pas une
modification de config (le redémarrage volontaire la couvre).

**La référence** est la fusion que `main()` a effectivement chargée, jamais
une relecture ultérieure : une édition faite pendant le démarrage passerait
sinon pour l'état initial, et ne serait jamais signalée.

**La chaîne est re-résolue à chaque lecture**, avec les mêmes arguments : un
`config.json` apparu depuis le démarrage compte, un `--config` disparu rend la
config invalide. Rien ici ne lève : un JSON à moitié écrit par un éditeur
rend `invalid` avec sa cause, et redevient `changed` à la lecture suivante.

**La validation** est `prepare_config`, la séquence même de `main()` (hôte et
port, `miaou_dist`, `self_update`, auth entrante, upstreams, autorisations
sortantes), rejouée avec les mêmes surcharges de la ligne de commande. Une
seule fonction pour les deux : une validation qui passerait là où `main()`
échoue laisserait redémarrer un proxy qui sort en 1, que le superviseur ne
relance pas. Elle ne fait que construire des objets : ni `prepare` d'upstream
(import des modules, `env`), ni `apply_proxy_env_overrides_to_process`, ni
écriture (les autorisations sortantes ne touchent au fichier de jetons qu'au
premier parcours). Son journal est coupé (`muted`). Ce qu'elle ne voit pas : un
module inprocess qui ne s'importe plus (l'upstream est alors écarté au
démarrage, le proxy démarre quand même).

**Le `bind` d'essai** (`probe_bind`) n'a lieu que si l'hôte ou le port
EFFECTIFS changent : c'est la seule panne du démarrage qui ne soit pas une
erreur de config et qui tue le proxy (sortie en 3). Il passe par
`loop.create_server`, comme uvicorn, pour que les options de socket soient
celles du vrai démarrage (`SO_REUSEADDR` posé sous POSIX, pas sous Windows, où
il permettrait de voler un port occupé). Course assumée : un port libre à
l'essai peut être pris avant la relance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .auth_in import resolve_auth_config
from .auth_out import _default_tokens_path, build_callback_url, build_upstream_authorizers
from .config import build_upstreams
from .layers import LoadedConfig, load_layers, resolve_chain, strip_comments
from .logging import muted
from .netproxy import compute_proxy_env_overrides
from .updater import resolve_self_update

#: Clef de config de l'extension `miaou/lifecycle` (lot AV) : true par défaut.
LIFECYCLE_KEY = "lifecycle"
from .webapp import resolve_miaou_dist

UNCHANGED = "unchanged"
CHANGED = "changed"
INVALID = "invalid"

#: Clef racine dont les changements se publient par entrée : le nom du
#: serveur suffit à dire ce qui bouge, le détail de son bloc non.
_SERVERS_KEY = "mcpServers"


@dataclass(frozen=True)
class LaunchOptions:
    """Les arguments de la ligne de commande qui pèsent sur la config, gardés
    tels quels pour être rejoués à chaque validation."""

    config: tuple[str, ...] | None = None
    host: str | None = None
    port: int | None = None
    # --auth / --no-auth : True, False, ou None (la config décide).
    cli_auth: bool | None = None
    with_dev_auth: int | None = None
    tokens_file: str | None = None
    open_browser: bool = False
    proxy: str | None = None
    noproxy: bool = False

    @classmethod
    def from_args(cls, args: Any) -> LaunchOptions:
        return cls(
            config=tuple(args.config) if args.config else None,
            host=args.host,
            port=args.port,
            cli_auth=True if args.auth else (False if args.no_auth else None),
            with_dev_auth=args.with_dev_auth,
            tokens_file=args.tokens_file,
            open_browser=args.open,
            proxy=args.proxy,
            noproxy=args.noproxy,
        )


@dataclass
class Prepared:
    """Ce que `main()` consomme de la config, une fois validée."""

    loaded: LoadedConfig
    cfg: dict[str, Any]
    host: str
    port: int
    miaou_dist: Path | None
    self_update: bool
    lifecycle: bool
    auth: dict[str, Any] | None
    upstreams: dict[str, Any]
    authorizers: dict[str, Any]
    tokens_path: Path | str
    dev_auth_url: str | None
    proxy_overrides: dict[str, str | None] | None


def resolve_lifecycle(cfg: dict[str, Any]) -> bool:
    """La clef `lifecycle` : booléen, absente (ou null) = `true`. À false, le
    proxy ne publie pas `miaou/lifecycle` : un déploiement qui ne relit ni ne
    relance rien (conteneur) n'a rien à en dire, et MIAOU s'y comporte comme
    devant un proxy antérieur à l'extension."""
    value = cfg.get(LIFECYCLE_KEY, True)
    if value is None:
        return True
    if not isinstance(value, bool):
        raise ValueError(
            f"'{LIFECYCLE_KEY}' doit être true ou false, reçu {type(value).__name__}."
        )
    return value


def prepare_config(loaded: LoadedConfig, options: LaunchOptions) -> Prepared:
    """La suite de `main()` après la lecture des couches : chaque erreur de
    config y est une ValueError (`AuthConfigError` comprise), dont le message
    est celui que `main()` affiche avant de sortir en 1."""
    cfg = loaded.cfg
    host = options.host or cfg.get("host", "127.0.0.1")
    try:
        port = options.port or int(cfg["port"])
    except (TypeError, ValueError):
        raise ValueError(f"'port' doit être un entier, reçu {cfg['port']!r}.") from None
    miaou_dist = resolve_miaou_dist(cfg, loaded.miaou_dist_base)
    self_update = resolve_self_update(cfg)
    lifecycle = resolve_lifecycle(cfg)

    proxy_overrides = compute_proxy_env_overrides(options.proxy, options.noproxy)
    cli_auth = options.cli_auth
    dev_auth_url: str | None = None
    if options.with_dev_auth is not None:
        # Le serveur de développement EST l'émetteur : on le pose dans la config
        # avant résolution, pour que --with-dev-auth suffise sans éditer
        # config.json. Une clé `auth` déjà présente n'est pas écrasée — la
        # config explicite de l'utilisateur prime toujours sur la commodité.
        dev_auth_url = f"http://127.0.0.1:{options.with_dev_auth}"
        if not isinstance(cfg.get("auth"), dict):
            cfg = {**cfg, "auth": {"issuer_url": dev_auth_url}}
        else:
            dev_auth_url = cfg["auth"].get("issuer_url", dev_auth_url)
        cli_auth = True

    auth = resolve_auth_config(cfg, cli_auth=cli_auth, host=host, port=port)
    upstreams = build_upstreams(cfg, proxy_env_overrides=proxy_overrides)
    tokens_path = options.tokens_file or _default_tokens_path(loaded.local)
    authorizers = build_upstream_authorizers(
        cfg,
        upstreams,
        tokens_path=tokens_path,
        callback_url=build_callback_url(host, port),
        open_browser=options.open_browser,
    )
    return Prepared(
        loaded=loaded, cfg=cfg, host=host, port=port, miaou_dist=miaou_dist,
        self_update=self_update, lifecycle=lifecycle, auth=auth, upstreams=upstreams,
        authorizers=authorizers, tokens_path=tokens_path,
        dev_auth_url=dev_auth_url, proxy_overrides=proxy_overrides,
    )


# --- Empreinte et chemins ---------------------------------------------------


def _canonical(merged: dict[str, Any]) -> dict[str, Any]:
    return strip_comments(merged)


def fingerprint(merged: dict[str, Any]) -> str:
    """SHA-256 du JSON canonique (clefs triées, sans `_comment*`)."""
    text = json.dumps(
        _canonical(merged), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def changed_paths(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Chemins des différences, triés : `mcpServers.<nom>` pour un serveur,
    la clef racine pour tout le reste. Ajout, retrait et modification
    confondus ; jamais de valeur (la config porte des secrets)."""
    before, after = _canonical(before), _canonical(after)
    paths: set[str] = set()
    for key in before.keys() | after.keys():
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if key == _SERVERS_KEY and isinstance(old, dict) and isinstance(new, dict):
            paths.update(
                f"{key}.{name}" for name in old.keys() | new.keys()
                if old.get(name) != new.get(name)
            )
        else:
            paths.add(key)
    return sorted(paths)


# --- bind d'essai -----------------------------------------------------------


async def probe_bind(host: str, port: int) -> str | None:
    """None si `host:port` s'ouvre comme l'ouvrirait uvicorn, la cause sinon.
    Le socket est refermé aussitôt."""
    loop = asyncio.get_running_loop()
    try:
        server = await loop.create_server(asyncio.Protocol, host=host, port=port)
    except (OSError, OverflowError, ValueError) as e:
        return f"{host}:{port} ne peut pas être ouvert ({e})."
    server.close()
    await server.wait_closed()
    return None


# --- Lecture ----------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ConfigCheck:
    state: str
    # Tous les chemins changés (le plafond est celui de qui publie).
    changed: list[str] = field(default_factory=list)
    # Cause brute si `invalid`, sinon None.
    detail: str | None = None
    # Fichiers lus, dans l'ordre ; vide si la chaîne même n'a pas pu se résoudre.
    layers: list[str] = field(default_factory=list)
    fingerprint: str | None = None
    checked_at: datetime = field(default_factory=_now)


@dataclass
class _Validation:
    detail: str | None
    endpoint: tuple[str, int] | None


class ConfigWatch:
    """La config telle que `main()` l'a chargée, et sa relecture à la demande.

    `env` : l'environnement du démarrage, recopié avant toute modification
    d'`os.environ` — c'est lui que retrouvera le processus relancé par son
    superviseur, et c'est contre lui que la validation substitue les `${VAR}`.
    """

    def __init__(self, options: LaunchOptions, env: Mapping[str, str], reference: Prepared) -> None:
        self.options = options
        self.env = dict(env)
        self._reference = _canonical(reference.loaded.merged)
        self.reference_fingerprint = fingerprint(self._reference)
        self.endpoint = (reference.host, reference.port)
        # Une validation par empreinte : une config inchangée depuis la
        # dernière lecture ne se revalide pas. Le `bind`, lui, se refait sur
        # demande (`fresh_bind`, au redémarrage).
        self._validated: dict[str, _Validation] = {}
        self._bind: dict[str, str | None] = {}

    def _read(self) -> tuple[LoadedConfig | None, ConfigCheck | None]:
        # `load_config` en deux temps : une couche illisible garde la liste
        # des fichiers, qui dit où regarder.
        layers: list[str] = []
        try:
            with muted():
                layer_paths, local_path = resolve_chain(
                    list(self.options.config) if self.options.config else None
                )
                layers = [str(p) for p in layer_paths]
                return load_layers(layer_paths, local_path, env=self.env), None
        except (OSError, ValueError) as e:
            return None, ConfigCheck(INVALID, detail=str(e), layers=layers)

    def _validate(self, loaded: LoadedConfig) -> _Validation:
        try:
            with muted():
                prepared = prepare_config(loaded, self.options)
        except ValueError as e:
            return _Validation(str(e), None)
        return _Validation(None, (prepared.host, prepared.port))

    async def check(self, fresh_bind: bool = False) -> ConfigCheck:
        loaded, failed = await asyncio.to_thread(self._read)
        if failed is not None:
            return failed
        assert loaded is not None
        layers = [str(p) for p in loaded.layers]
        current = fingerprint(loaded.merged)
        if current == self.reference_fingerprint:
            return ConfigCheck(UNCHANGED, layers=layers, fingerprint=current)
        changed = changed_paths(self._reference, loaded.merged)
        validation = self._validated.get(current)
        if validation is None:
            validation = await asyncio.to_thread(self._validate, loaded)
            self._validated[current] = validation
        detail = validation.detail
        if detail is None and validation.endpoint != self.endpoint:
            if fresh_bind or current not in self._bind:
                assert validation.endpoint is not None
                self._bind[current] = await probe_bind(*validation.endpoint)
            detail = self._bind[current]
        state = INVALID if detail is not None else CHANGED
        return ConfigCheck(state, changed=changed, detail=detail, layers=layers, fingerprint=current)
