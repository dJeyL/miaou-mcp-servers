"""Point d'entrée CLI : parsing d'arguments, amorçage, lancement uvicorn."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from mcp_base import enable_system_trust_store

from .app import build_app
from .auth_in import AuthConfigError, resolve_auth_config
from .auth_out import (
    _default_tokens_path,
    build_callback_url,
    build_upstream_authorizers,
    enable_auth_debug,
)
from .config import build_upstreams, load_config
from .logging import _log
from .netproxy import apply_proxy_env_overrides_to_process, compute_proxy_env_overrides
from .server import ToolCatalogCache, build_proxy_server


def run_with_dev_auth(
    proxy_app: Any,
    host: str,
    port: int,
    dev_auth_port: int,
    issuer_url: str,
    auto_approve: bool = False,
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
        uvicorn.Config(proxy_app, host=host, port=port, log_level="info")
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

    try:
        asyncio.run(_serve_both())
    except KeyboardInterrupt:
        # `uvicorn.run()` avale le Ctrl-C ; `asyncio.run()` non, et laisserait
        # une trace KeyboardInterrupt à chaque arrêt. Sortie silencieuse, pour
        # que les deux modes de lancement se terminent de la même façon.
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Serveur MCP proxy MIAOU")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Chemin vers le fichier de configuration (défaut: config.json)",
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
            "Fichier des jetons OAuth des upstreams (défaut : <config>-tokens.json, "
            "à côté de la config). Distinct de config.json à dessein."
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

    config_path = Path(args.config)
    if not config_path.exists():
        print(
            f"Erreur : config introuvable '{config_path}'. "
            "Copier config.sample.json → config.json et l'adapter.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        cfg = load_config(config_path)
        host = args.host or cfg.get("host", "127.0.0.1")
        port = args.port or int(cfg["port"])
    except ValueError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        sys.exit(1)

    proxy_overrides = compute_proxy_env_overrides(args.proxy, args.noproxy)
    if proxy_overrides is not None:
        # Avant build_upstreams : les upstreams inprocess importent leur module
        # (donc appellent potentiellement make_opener()/ProxyHandler() dès le
        # premier tool call) en partageant l'environnement de ce process — poser
        # les overrides ici les couvre sans toucher InProcessUpstream.
        apply_proxy_env_overrides_to_process(proxy_overrides)

    cli_auth = True if args.auth else (False if args.no_auth else None)
    dev_auth_url: str | None = None
    if args.with_dev_auth is not None:
        # Le serveur de développement EST l'émetteur : on le pose dans la config
        # avant résolution, pour que --with-dev-auth suffise sans éditer
        # config.json. Une clé `auth` déjà présente n'est pas écrasée — la
        # config explicite de l'utilisateur prime toujours sur la commodité.
        dev_auth_url = f"http://127.0.0.1:{args.with_dev_auth}"
        if not isinstance(cfg.get("auth"), dict):
            cfg = {**cfg, "auth": {"issuer_url": dev_auth_url}}
        else:
            dev_auth_url = cfg["auth"].get("issuer_url", dev_auth_url)
        cli_auth = True

    try:
        auth = resolve_auth_config(cfg, cli_auth=cli_auth, host=host, port=port)
    except AuthConfigError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        sys.exit(1)

    upstreams = build_upstreams(cfg, proxy_env_overrides=proxy_overrides)

    tokens_path = args.tokens_file or _default_tokens_path(args.config)
    try:
        authorizers = build_upstream_authorizers(
            cfg,
            upstreams,
            tokens_path=tokens_path,
            callback_url=build_callback_url(host, port),
            open_browser=args.open,
        )
    except ValueError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        sys.exit(1)

    tool_map: dict[str, tuple[str, str]] = {}
    # Cache d'outils à côté du fichier de jetons : pas un secret, mais même
    # durée de vie. Sans lui, un upstream non autorisé n'aurait rien à lister.
    catalog = ToolCatalogCache(Path(tokens_path).with_name(
        Path(tokens_path).stem.replace("-tokens", "") + "-tools.json"
    ))
    mcp_server = build_proxy_server(
        upstreams, tool_map, authorizers=authorizers, catalog=catalog
    )
    app = build_app(mcp_server, upstreams, auth=auth, authorizers=authorizers)

    import uvicorn

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
    print(f"miaou-proxy → http://{host}:{port}/mcp  (Ctrl-C pour arrêter)")

    if args.with_dev_auth is None:
        uvicorn.run(app, host=host, port=port, log_level="info")
        return

    run_with_dev_auth(
        app,
        host=host,
        port=port,
        dev_auth_port=args.with_dev_auth,
        issuer_url=dev_auth_url or f"http://127.0.0.1:{args.with_dev_auth}",
        auto_approve=args.dev_auth_auto_approve,
    )
