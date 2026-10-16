"""Point d'entrée CLI : parsing d'arguments, amorçage, lancement uvicorn.

`kill -USR1 <pid>` redémarre le proxy sur place en relisant le code (voir
`serve_http`) ; sous un superviseur qui sait le relancer, il sort plutôt en
`RESTART_EXIT_CODE`. Une mise à jour appliquée (lot AT) le fait sortir en
`UPDATE_EXIT_CODE`, pour que son superviseur le relance par `uv run`.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

from mcp_base import enable_system_trust_store

from .app import build_app
from .auth_out import build_callback_url, enable_auth_debug
from .configwatch import ConfigWatch, LaunchOptions, prepare_config
from .lifecycle import Lifecycle, ProcessIdentity
from .layers import (
    DEFAULT_CHAIN,
    load_layers,
    mask_secrets,
    migrate_local,
    migration_diff,
    needs_slimming,
    resolve_chain,
)
from .logging import _log, _warn
from .netproxy import apply_proxy_env_overrides_to_process
from .server import InFlight, ToolCatalogCache, build_proxy_server
from .state import default_tools_cache_path, migrate_legacy_state, state_dir, tools_cache_beside
from .updater import RESTART_BY_EXIT_ENV, SUPERVISED_ENV, SelfUpdater, UpdateConfig, is_supervised, restartable  # noqa: F401 - RESTART_BY_EXIT_ENV ré-exporté
from . import selfupdate
from .volatile_store import VolatileSecrets
from .webapp import APP_PREFIX, miaou_dist_warnings

#: Redémarrage sur place : un hook post-commit local l'envoie à l'instance du
#: poste. Pas SIGHUP : c'est ce que reçoit le processus quand on ferme son
#: terminal, il renaîtrait orphelin en gardant le port. `None` sous Windows,
#: qui n'a pas de SIGUSR1 : le proxy y tourne sans redémarrage sur place.
RESTART_SIGNAL: signal.Signals | None = getattr(signal, "SIGUSR1", None)

#: Sortie après une mise à jour appliquée (lot AT) : `scripts/supervise.sh`
#: relance sur ce code, et sur lui seul. Hors de 0 (arrêt normal, Ctrl-C
#: compris), 1 (config), 2 (argparse), 3 (démarrage raté) et des signaux.
UPDATE_EXIT_CODE = 42

#: Sortie sur RESTART_SIGNAL sous superviseur, au lieu de l'exec, et sur
#: `miaou/lifecycle/restart` (lot AV) : la relance repasse par `uv run`
#: (dépendances installées) et par la fonction de lancement du superviseur
#: (proxy réseau redétecté). Exige `restartable()` : en plus de SUPERVISED_ENV,
#: RESTART_BY_EXIT_ENV, que seul pose un `supervise.sh` qui connaît ce code ;
#: un superviseur plus ancien, encore en mémoire après une mise à jour,
#: rendrait 43 au lieu de relancer, et le proxy resterait arrêté.
RESTART_EXIT_CODE = 43

#: À l'arrêt, les requêtes en cours finissent ; au-delà (flux SSE d'une session
#: MCP restée ouverte), elles sont coupées. Ne borne pas le shutdown du
#: lifespan, qui referme ensuite les upstreams.
SHUTDOWN_GRACE_S = 5


def _on_restart_signal(server: Any) -> Callable[[], bool]:
    """Pose le gestionnaire de RESTART_SIGNAL : il demande à `server` (un
    `uvicorn.Server`) de s'arrêter comme sur Ctrl-C. Renvoie de quoi savoir,
    une fois l'arrêt fini, si c'est ce signal qui l'a provoqué. Sans
    RESTART_SIGNAL (Windows), ne pose rien et ne redémarre jamais."""
    if RESTART_SIGNAL is None:
        return lambda: False
    requested = False

    def on_restart(signum: int, frame: FrameType | None) -> None:
        nonlocal requested
        requested = True
        server.should_exit = True

    signal.signal(RESTART_SIGNAL, on_restart)
    return lambda: requested


class ExitRequest:
    """Demande de sortie posée depuis l'app, lue par `serve_http` une fois
    l'arrêt COMPLET, lifespan compris : UPDATE_EXIT_CODE après une mise à
    jour appliquée (`SelfUpdater.request_restart`), RESTART_EXIT_CODE sur
    `miaou/lifecycle/restart`. La première demande fixe le code.

    Créée dans `main()` avant le serveur uvicorn, qui ne naît que dans
    `serve_http` : `bind` les relie. L'arrêt est celui de Ctrl-C, en douceur,
    donc la réponse JSON-RPC de la requête qui l'a demandé, encore en vol,
    part avant la coupure. Le proxy ne se relance pas lui-même : c'est le
    superviseur qui décide, et `uv run` réinstalle alors les dépendances."""

    def __init__(self) -> None:
        self.code: int | None = None
        self._server: Any = None

    @property
    def pending(self) -> bool:
        return self.code is not None

    def bind(self, server: Any) -> None:
        self._server = server
        if self.code is not None:
            server.should_exit = True

    def __call__(self, code: int = UPDATE_EXIT_CODE) -> None:
        if self.code is None:
            self.code = code
        if self._server is not None:
            self._server.should_exit = True


_EXIT_REASONS = {
    UPDATE_EXIT_CODE: "mise à jour appliquée",
    RESTART_EXIT_CODE: "redémarrage demandé par un client",
}


def _exit_if_requested(exit_request: ExitRequest | None) -> None:
    """Examinée AVANT la demande de redémarrage par signal : une sortie
    demandée doit repasser par `uv run`, ce que l'exec du SIGUSR1 ne fait
    pas. Les deux posées ensemble, la sortie l'emporte."""
    if exit_request is not None and exit_request.code is not None:
        reason = _EXIT_REASONS.get(exit_request.code, "sortie demandée")
        _log(f"sortie en {exit_request.code} : {reason}, relance par le superviseur.")
        sys.exit(exit_request.code)


def _reexec() -> None:
    """Remplace le processus par sa propre ligne de commande : même pid, même
    terminal, code relu. Appelé seulement après un arrêt COMPLET, lifespan
    compris — les upstreams stdio sont alors refermés, pas orphelins. Ne
    repasse pas par `uv run` : une dépendance ajoutée n'est pas installée."""
    _log(f"redémarrage : {' '.join(sys.orig_argv)}")
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, sys.orig_argv)


def _restart() -> None:
    """Suite d'un arrêt sur RESTART_SIGNAL : sortie en RESTART_EXIT_CODE sous
    un superviseur qui la relance, exec sur place sinon."""
    if restartable():
        _log(f"sortie en {RESTART_EXIT_CODE} : redémarrage demandé, relance par le superviseur.")
        sys.exit(RESTART_EXIT_CODE)
    _reexec()


def serve_http(app: Any, host: str, port: int, exit_request: ExitRequest | None = None) -> None:
    """uvicorn, plus un redémarrage sur RESTART_SIGNAL : arrêt en douceur, puis
    exec de la même ligne de commande. Un code qui ne s'importe plus meurt au
    redémarrage. `exit_request` posée (mise à jour appliquée) : même arrêt,
    puis sortie en UPDATE_EXIT_CODE au lieu de l'exec."""
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            app, host=host, port=port, log_level="info",
            timeout_graceful_shutdown=SHUTDOWN_GRACE_S,
        )
    )
    restart_requested = _on_restart_signal(server)
    if exit_request is not None:
        exit_request.bind(server)
    try:
        server.run()
    except KeyboardInterrupt:
        # Après un arrêt sur Ctrl-C, `Server.run` relève le SIGINT qu'il a
        # capturé ; `uvicorn.run` l'avale, sans quoi chaque arrêt laisse une
        # trace KeyboardInterrupt.
        pass
    if not server.started:
        # Ce que fait `uvicorn.run` (port pris, lifespan en échec) : sans lui,
        # un démarrage raté sortirait en 0.
        sys.exit(3)
    _exit_if_requested(exit_request)
    if restart_requested():
        _restart()


def run_with_dev_auth(
    proxy_app: Any,
    host: str,
    port: int,
    dev_auth_port: int,
    issuer_url: str,
    auto_approve: bool = False,
    exit_request: ExitRequest | None = None,
) -> None:
    """Sert le proxy ET le serveur d'autorisation de développement dans CE
    process, sur deux ports distincts.

    Confort de banc d'essai : sans lui il faut deux terminaux. **Deux ports, pas
    un** — chacun garde une origine à part entière, donc l'`issuer_url` reste une
    identité propre et non une sous-route du proxy. C'est ce qui fait de ce mode
    une commodité d'exécution et non une fusion : le proxy reste un Resource
    Server qui vérifie des jetons, l'AS reste seul à en émettre. Ne pas glisser
    vers un montage de l'AS dans l'app du proxy, qui effacerait cette frontière.

    `dev_auth_server` est importé ICI, pas au chargement du module : le proxy
    doit rester utilisable si ce fichier est absent (déploiement qui ne garde que
    le proxy), et surtout ne jamais charger de code d'émission de jetons quand on
    ne l'a pas demandé.
    """
    import asyncio

    import uvicorn

    try:
        import dev_auth_server
    except ImportError as e:  # pragma: no cover - dépend du déploiement
        print(
            f"Erreur : --with-dev-auth exige dev_auth_server.py à la racine "
            f"du projet, à côté du paquet mcp_proxy/ ({e}).",
            file=sys.stderr,
        )
        sys.exit(1)

    keys = dev_auth_server.DevKeyPair()
    provider = dev_auth_server.DevAuthProvider(
        issuer_url, keys, auto_approve=auto_approve
    )
    auth_app = dev_auth_server.build_app(provider, keys, issuer_url)

    for line in dev_auth_server.banner_lines(issuer_url, auto_approve):
        print(line, file=sys.stderr, flush=True)

    proxy_server = uvicorn.Server(
        uvicorn.Config(
            proxy_app, host=host, port=port, log_level="info",
            timeout_graceful_shutdown=SHUTDOWN_GRACE_S,
        )
    )
    auth_server = uvicorn.Server(
        uvicorn.Config(
            auth_app, host="127.0.0.1", port=dev_auth_port, log_level="warning"
        )
    )

    async def _serve_both() -> None:
        """Le proxy commande : quand il s'arrête (Ctrl-C, erreur), l'AS suit.

        Sans ce couplage, un proxy tombé laisserait un émetteur de jetons
        tourner seul — un serveur qui distribue des autorisations pour une
        ressource qui n'écoute plus.
        """
        auth_task = asyncio.create_task(auth_server.serve())
        try:
            await proxy_server.serve()
        finally:
            auth_server.should_exit = True
            try:
                await asyncio.wait_for(auth_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                auth_task.cancel()

    # Arrêter le proxy suffit : l'AS le suit (`_serve_both`).
    restart_requested = _on_restart_signal(proxy_server)
    if exit_request is not None:
        exit_request.bind(proxy_server)
    try:
        asyncio.run(_serve_both())
    except KeyboardInterrupt:
        # `uvicorn.run()` avale le Ctrl-C ; `asyncio.run()` non, et laisserait
        # une trace KeyboardInterrupt à chaque arrêt. Sortie silencieuse, pour
        # que les deux modes de lancement se terminent de la même façon.
        return
    if (exit_request is not None and exit_request.pending) or restart_requested():
        # La paire de clés de l'AS ne vit qu'en mémoire : la nouvelle ne
        # vérifie aucun jeton émis par l'ancienne. Vrai de toute relance,
        # par signal comme par sortie demandée.
        _log(
            "redémarrage : nouvelle clé du serveur d'autorisation de "
            "développement, les jetons déjà émis sont invalides "
            "(clients à ré-autoriser)."
        )
    _exit_if_requested(exit_request)
    if restart_requested():
        _restart()


def _migrate_and_exit(layer_paths: list[Path], local_path: Path, diff_only: bool) -> None:
    if diff_only:
        result, diff = migration_diff(layer_paths, local_path)
        if diff:
            sys.stdout.writelines(diff)
        print(f"Aperçu seulement : {local_path} n'est pas modifié.")
        print(f"  {len(result.removed)} valeur(s) identique(s) aux couches précédentes à retirer.")
    else:
        result = migrate_local(layer_paths, local_path)
        print(f"{local_path} allégé, original sauvegardé en {local_path.name}.bak.")
        print(f"  {len(result.removed)} valeur(s) identique(s) aux couches précédentes retirée(s).")
    if result.inherited:
        print(
            "  Reçu des couches précédentes, absent de ce fichier (à neutraliser "
            "ici si c'était un retrait voulu) :"
        )
        for path in result.inherited:
            print(f"    {path}")
    sys.exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serveur MCP proxy MIAOU")
    parser.add_argument(
        "--config",
        action="append",
        default=None,
        metavar="FICHIER",
        help=(
            "Fichier de configuration ; répétable, les suivants patchant les "
            "précédents (JSON Merge Patch). Remplace la chaîne par défaut : "
            f"{' → '.join(DEFAULT_CHAIN)}, chacun lu s'il existe."
        ),
    )
    parser.add_argument(
        "--migrate-config",
        action="store_true",
        help=(
            "Allège le dernier fichier de la chaîne (config.json par défaut) de "
            "ce que les précédents portent déjà, et de tous ses _comment, après "
            "l'avoir sauvegardé en <fichier>.bak. La config effective est "
            "inchangée. Puis quitte."
        ),
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help=(
            "Avec --migrate-config : affiche le diff de l'allègement (secrets "
            "masqués) sans rien écrire."
        ),
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help=(
            "Affiche la config effective (couches fusionnées, variables "
            "d'environnement substituées, secrets masqués), puis quitte."
        ),
    )
    parser.add_argument("--host", default=None, help="Override de l'adresse d'écoute")
    parser.add_argument("--port", type=int, default=None, help="Override du port")
    parser.add_argument(
        "--proxy",
        default=None,
        metavar="[http://]host:port",
        help=(
            "Force http_proxy/https_proxy (et variantes majuscules) vus par tous "
            "les serveurs MCP servis (inprocess et stdio), même si ces variables "
            "sont déjà définies dans l'environnement ou dans config.json. "
            "'http://' est ajouté si absent."
        ),
    )
    parser.add_argument(
        "--noproxy",
        action="store_true",
        help=(
            "Force l'absence de proxy pour tous les serveurs MCP servis (inprocess "
            "et stdio), même si http_proxy/https_proxy sont définis dans "
            "l'environnement ou dans config.json. Incompatible avec --proxy."
        ),
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        help=(
            "Force l'activation de l'auth OAuth entrante (exige la clé 'auth' "
            "dans la config). Incompatible avec --no-auth."
        ),
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help=(
            "Force la désactivation de l'auth OAuth entrante, même si la config "
            "porte une clé 'auth'. Incompatible avec --auth."
        ),
    )
    parser.add_argument(
        "--with-dev-auth",
        nargs="?",
        const=8787,
        type=int,
        metavar="PORT",
        help=(
            "Lance AUSSI le serveur d'autorisation de développement "
            "(dev_auth_server.py) dans ce process, sur PORT (défaut 8787), et "
            "pointe l'auth dessus. Confort de banc d'essai — JAMAIS en production."
        ),
    )
    parser.add_argument(
        "--dev-auth-auto-approve",
        action="store_true",
        help="Avec --with-dev-auth : approuve sans écran de consentement.",
    )
    parser.add_argument(
        "--tokens-file",
        default=None,
        metavar="FICHIER",
        help=(
            "Fichier des jetons OAuth des upstreams (défaut : state/tokens.json, "
            "à côté de config.json). Distinct de la config à dessein."
        ),
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help=(
            "Ouvre l'URL d'autorisation dans le navigateur par défaut de l'OS. "
            "Confort : le lien reste affiché, et c'est lui le mécanisme de "
            "référence (l'OS ne garantit ni le bon navigateur ni le bon profil)."
        ),
    )
    parser.add_argument(
        "--debug-auth",
        action="store_true",
        help=(
            "Trace le parcours OAuth sortant : chaque requête HTTP émise vers "
            "un upstream ou son serveur d'autorisation, avec son code de "
            "réponse et l'en-tête WWW-Authenticate. Sans elle, un parcours qui "
            "n'aboutit pas ne laisse AUCUNE trace — c'est ce qui a coûté "
            "plusieurs correctifs posés à l'aveugle. Les valeurs sensibles "
            "(jetons, codes, secrets) sont masquées."
        ),
    )
    args = parser.parse_args()

    if args.debug_auth:
        enable_auth_debug()

    # Avant TOUT : avant build_upstreams (qui importe les modules de serveurs, et
    # donc peut construire un contexte SSL), et avant le premier handshake TLS
    # d'un upstream http. truststore remplace ssl.SSLContext lui-même, donc cet
    # unique appel couvre aussi bien httpx (HttpUpstream, client OAuth du SDK)
    # qu'urllib (make_opener, dans les serveurs inprocess) : un upstream HTTPS
    # dont le certificat est signé par une AC d'entreprise interne cesse
    # d'échouer en CERTIFICATE_VERIFY_FAILED. Un contexte déjà construit
    # garderait l'ancienne classe — d'où la position en tête de main().
    enable_system_trust_store()

    if args.proxy and args.noproxy:
        print("Erreur : --proxy et --noproxy sont mutuellement exclusifs.", file=sys.stderr)
        sys.exit(1)

    if args.diff and not args.migrate_config:
        print("Erreur : --diff ne s'emploie qu'avec --migrate-config.", file=sys.stderr)
        sys.exit(1)

    if args.auth and args.no_auth:
        print("Erreur : --auth et --no-auth sont mutuellement exclusifs.", file=sys.stderr)
        sys.exit(1)

    if args.with_dev_auth is not None and args.no_auth:
        print(
            "Erreur : --with-dev-auth et --no-auth sont contradictoires "
            "(lancer un serveur d'autorisation puis n'exiger aucun jeton).",
            file=sys.stderr,
        )
        sys.exit(1)

    # Recopié avant que quoi que ce soit ne le modifie (`--proxy`, `env` des
    # upstreams inprocess) : c'est l'environnement que retrouvera un processus
    # relancé par son superviseur, donc celui de la validation (lot AV).
    launch_env = dict(os.environ)
    options = LaunchOptions.from_args(args)
    identity = ProcessIdentity()

    try:
        layer_paths, local_path = resolve_chain(args.config)
        if args.migrate_config:
            _migrate_and_exit(layer_paths, local_path, args.diff)
        loaded = load_layers(layer_paths, local_path, env=launch_env)
        if args.print_config:
            print(json.dumps(mask_secrets(loaded.cfg), indent=2, ensure_ascii=False))
            return
    except ValueError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        sys.exit(1)

    _log(f"Config : {' + '.join(str(p) for p in layer_paths)}")
    for name, where in loaded.unset_vars:
        _log(f"  attention : ${{{name}}} non définie (clé {where}) — remplacée par \"\".")
    for where, key, layer in loaded.plain_over_volatile:
        _warn(
            f"{where} : {layer} y déclare le secret volatil « {key} », mais une couche "
            f"suivante le remplace par une valeur écrite dans la config. Retirer cette "
            f"valeur : le secret se saisit alors dans MIAOU, et n'est plus écrit nulle part."
        )
    if needs_slimming(layer_paths, local_path):
        _log(
            f"  {local_path} répète des valeurs des couches précédentes : "
            f"`--migrate-config` l'allège (sauvegarde en {local_path.name}.bak)."
        )

    # La même fonction valide une config modifiée avant un redémarrage
    # (`configwatch`) : chaque refus de démarrer ici y est un refus de
    # redémarrer, sans quoi le proxy relancé sortirait en 1 pour de bon.
    try:
        prepared = prepare_config(loaded, options)
    except ValueError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        sys.exit(1)
    host, port = prepared.host, prepared.port
    miaou_dist, auth = prepared.miaou_dist, prepared.auth
    upstreams, authorizers = prepared.upstreams, prepared.authorizers
    tokens_path = prepared.tokens_path

    if args.tokens_file is None:
        for line in migrate_legacy_state(local_path):
            _log(f"  état : {line}")

    if prepared.proxy_overrides is not None:
        # Avant le premier `prepare` d'upstream (lifespan) : les upstreams
        # inprocess importent alors leur module (donc appellent potentiellement
        # make_opener()/ProxyHandler() dès le premier tool call) en partageant
        # l'environnement de ce process — poser les overrides ici les couvre
        # sans toucher InProcessUpstream.
        apply_proxy_env_overrides_to_process(prepared.proxy_overrides)

    tool_map: dict[str, tuple[str, str]] = {}
    # Cache d'outils à côté du fichier de jetons : pas un secret, mais même
    # durée de vie. Sans lui, un upstream non autorisé n'aurait rien à lister.
    catalog = ToolCatalogCache(
        tools_cache_beside(args.tokens_file) if args.tokens_file
        else default_tools_cache_path(local_path)
    )
    in_flight = InFlight()
    mcp_server = build_proxy_server(
        upstreams, tool_map, authorizers=authorizers, catalog=catalog, in_flight=in_flight
    )
    updater = None
    exit_request = ExitRequest()
    if prepared.self_update:
        updater = SelfUpdater(
            UpdateConfig(
                root=selfupdate.PROJECT_ROOT,
                miaou_dist=miaou_dist,
                supervised=is_supervised(),
                state_file=selfupdate.state_path(state_dir(local_path)),
            ),
            in_flight,
            request_restart=exit_request,
            exit_pending=lambda: exit_request.pending,
        )
    secrets = None
    if any(getattr(u, "volatile_key", None) for u in upstreams.values()):
        secrets = VolatileSecrets(upstreams, catalog, port=port, instance=identity.instance)
    # Clef `lifecycle`, true par défaut (lot AV) : `status` ne fait que lire,
    # `restart` exige le superviseur. La référence est la config chargée plus
    # haut. À false, ni méthode ni extension : le SIGUSR1 reste.
    lifecycle = None
    if prepared.lifecycle:
        lifecycle = Lifecycle(
            identity,
            ConfigWatch(options, launch_env, prepared),
            in_flight,
            request_exit=exit_request,
            exit_pending=lambda: exit_request.pending,
            restartable=restartable,
            exit_code=RESTART_EXIT_CODE,
            applying=updater.applying if updater is not None else None,
        )
    app = build_app(
        mcp_server, upstreams, auth=auth, authorizers=authorizers, miaou_dist=miaou_dist,
        updater=updater, catalog=catalog, secrets=secrets, lifecycle=lifecycle,
    )

    if auth is not None:
        _log(f"Auth OAuth entrante ACTIVE — émetteur accepté : {auth['issuer_url']}")
        _log(f"  ressource protégée : {auth['resource_url']}")
    if authorizers:
        _log(
            f"Auth OAuth sortante — {len(authorizers)} upstream(s) : "
            f"{', '.join(sorted(authorizers))}"
        )
        _log(f"  jetons : {tokens_path}")
        _log(f"  redirection : {build_callback_url(host, port)}")
    if updater is not None:
        _log(
            "Mise à jour à la demande active"
            + ("" if updater.config.supervised else
               f" — sans {SUPERVISED_ENV}=1, l'application est refusée (aucun superviseur ne relancerait)")
            + "."
        )
    if miaou_dist is not None:
        _log(f"MIAOU servi depuis {miaou_dist}")
        for warning in miaou_dist_warnings(miaou_dist):
            _log(f"  attention : {warning}")
        print(f"MIAOU      → http://{host}:{port}{APP_PREFIX}/")
    print(f"miaou-proxy → http://{host}:{port}/mcp  (Ctrl-C pour arrêter)")

    if args.with_dev_auth is None:
        serve_http(app, host=host, port=port, exit_request=exit_request)
        return

    run_with_dev_auth(
        app,
        host=host,
        port=port,
        dev_auth_port=args.with_dev_auth,
        issuer_url=prepared.dev_auth_url or f"http://127.0.0.1:{args.with_dev_auth}",
        auto_approve=args.dev_auth_auto_approve,
        exit_request=exit_request,
    )
