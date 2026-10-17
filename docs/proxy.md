# `mcp_proxy/` — proxy MCP

Agrégation des upstreams, configuration, override du proxy réseau, et MIAOU servi
sous `/app/` (clé `miaou_dist`). L'auth OAuth (entrante et sortante) est dans
`docs/auth.md`.


Agrège plusieurs serveurs MCP upstream et expose leurs outils préfixés :
`bench__echo`, `bench__get_image`, `weather__get_weather`, etc.

Trois types d'upstream supportés :
- **inprocess** : import Python direct, pas de subprocess (défaut pour tous les serveurs)
- **stdio** : subprocess externe communiquant via stdin/stdout
- **http** : serveur MCP distant en streamable-http (lot AB-2.1)

Les entrées inprocess acceptent un champ `env` pour injecter des variables d'environnement
avant l'import du module (`os.environ.setdefault`). `mcp_web` lit les clefs de ses moteurs
de recherche en priorité dans le bloc `config.search` de son entrée (cf. `docs/servers.md`),
et retombe sur `BRAVE_API_KEY` / `OLLAMA_API_KEY` dans l'environnement.

Une entrée `http` prend `url` (obligatoire, l'endpoint `/mcp` du serveur distant),
`headers` (optionnel, en-têtes statiques — un serveur tiers peut exiger une clef d'API
sans OAuth), `auth` (le proxy devient client OAuth de l'upstream, cf. `docs/auth.md`)
et `timeout` (optionnel, défaut `_HTTP_HANDSHAKE_TIMEOUT_S` = 30 s). Un upstream qui
exige OAuth, déclaré sans `auth`, échoue au démarrage : le transport du SDK ne rend
de ce 401 que « Server returned an error response », sans le code. Un hook de réponse
posé par `_serve` sur le client httpx2 (`_note_refusal`, uniquement en l'absence
d'`auth`) retient ce 401, et `start()` complète alors le message avec le correctif à
apporter. La
négociation MCP (sonde `server/discover`, repli éventuel sur `initialize`, cf. « Ère des
upstreams stdio et http ») est **bornée** : un serveur distant qui accepte la connexion
puis ne répond jamais bloquerait sinon le démarrage du proxy entier. Cette borne est une
constante distincte de celle des subprocess stdio — les deux mesurent des choses
différentes, et partager la constante ferait bouger l'une en croyant ne toucher qu'à
l'autre.

### Override du proxy réseau vu par les upstreams (`--proxy` / `--noproxy`)

Deux options CLI, mutuellement exclusives, pour contrôler `http_proxy`/`https_proxy`
et `all_proxy` (et variantes `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`) vus par les
serveurs upstream servis :

- `--proxy [http://]host:port` : force les 6 variantes de casse à cette valeur
  (`http://` ajouté si le schéma est absent).
- `--noproxy` : force l'absence de proxy (les 6 variantes supprimées/non transmises).

Absolus : ils priment sur toute variable déjà présente dans l'environnement du process
proxy **et** sur un `env` explicite d'une entrée `config.json` — pas seulement sur un
héritage implicite. Sans l'un ou l'autre, comportement inchangé.

Trois chemins d'application distincts selon le type d'upstream :

- **inprocess** : partage le process du proxy, donc `main()` pose/efface directement les
  6 clés dans `os.environ` du process (`apply_proxy_env_overrides_to_process`) avant le
  premier `prepare` d'upstream, au lifespan (`build_upstreams` n'importe rien, et la
  validation d'une config modifiée le rejoue sans toucher à `os.environ`) — `make_opener()` (`servers/mcp_base.py`) relit `os.environ` à chaque
  requête via `ProxyHandler()`, aucun changement requis dans `InProcessUpstream`.
- **stdio** : le SDK MCP (`mcp.client.stdio.get_default_environment`) n'hérite qu'une
  whitelist restreinte (sous POSIX `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER` ;
  sous Windows une liste propre, `APPDATA`, `PATH`, `PATHEXT`, `SYSTEMROOT`, `TEMP`,
  `USERPROFILE`…) — les variables
  proxy du process proxy ne sont **jamais** vues par un subprocess sauf si explicitement
  posées dans son `env`. `build_upstreams` fusionne donc les overrides dans le `env` de
  chaque `StdioUpstream` via `merge_proxy_env_overrides` (CLI par-dessus `env` de
  config.json, `--noproxy` retire même une entrée explicite).
- **http** : rien à faire non plus, mais pour une raison différente des inprocess — le
  client httpx2 de l'upstream (`HttpUpstream._build_http_client`, seul client qu'il
  emploie depuis le SDK 2.x, qui ne le construit plus à notre place) garde le défaut
  `trust_env=True`, donc il relit `os.environ` du process, déjà modifié par `main()`.
  Deux tests l'épinglent sur CE client : `test_http_upstream_client_trusts_env`, et
  `test_noproxy_overrides_reach_http_upstream_via_process_env`, qui vérifie l'effet (un
  transport de proxy monté avec l'environnement d'origine, aucun après `--noproxy`).
  Ils visaient avant la migration le client que construisait le SDK
  (`create_mcp_http_client`) : restés verts, ils n'auraient plus rien prouvé.

  **`ALL_PROXY`** (mesuré le 2026-10-17) : httpx2 le lit, comme `http_proxy` — un client
  `trust_env` en monte un proxy —, urllib l'ignore (son `ProxyHandler` en tire un
  `all_open` que rien n'appelle), et le SDK MCP ne le transmet pas à un subprocess stdio
  (liste blanche). Il fige donc `--noproxy` pour un upstream http, d'où sa place dans
  `_PROXY_ENV_KEYS` ; le prix, assumé : `--proxy`/`--noproxy` posent/retirent aussi
  `all_proxy`/`ALL_PROXY` dans le `env` d'un upstream stdio. `NO_PROXY` reste à
  l'environnement, httpx2 comme urllib le lisent sans qu'on y touche.


## Architecture de `mcp_proxy/`

```
mcp_proxy/ (paquet, à la racine du projet)
├── __init__.py    ajoute servers/ à sys.path, PUIS ré-exporte la surface publique
│                  (l'ordre compte : les sous-modules importent mcp_base)
├── logging.py     _log — sans dépendance interne, pour rester importable de partout
├── contract.py    AUTHORIZATION_REQUIRED, authorize_path(), AuthorizationRequired
│                  — ce que server et auth_out nomment tous deux ; posé ici, leur
│                    dépendance reste à sens unique (pas de cycle entre eux)
├── upstream.py    les trois types, même surface `Upstream` (outils, et skills :
│   │              list_skills / get_skill / read_skill_file, « aucune » par défaut) :
│   ├── InProcessUpstream  : importlib.import_module(module) → module.mcp (MCPServer,
│   │                        API publique list_tools / call_tool ; skills lues sur
│   │                        l'extension du MCPServer)
│   ├── StdioUpstream      : stdio_client + Client (négociation d'ère) (subprocess MCP)
│   ├── HttpUpstream       : client httpx2 + streamable_http_client + Client
│   │                        (serveur distant), garde unique `_on_session`
│   └── _RemoteSkills      : skills de stdio/http par le fil, si `serves_skills`
│                            (ère moderne ET extension déclarée)
├── netproxy.py    override --proxy / --noproxy (calcul pur, n'applique rien)
├── layers.py      chaîne de config, fusion (RFC 7386), ${VAR}, --migrate-config
├── config.py      config fusionnée → build_upstreams()
├── state.py       state/ : tokens.json, tools-cache.json
├── server.py      build_proxy_server() : mcp.server.Server, on_list_tools / on_call_tool
│   ├── list_tools → agrège tous les upstreams, préfixe les noms avec "{name}__"
│   ├── call_tool  → dépréfixe, route vers l'upstream concerné
│   ├── aggregate_instructions() : compose `instructions` de l'InitializeResult
│   └── relay_tool_meta() : `_meta` d'outil republié, `miaou/requiresSkill` réécrit
├── skills.py      extension Skills : prefix_skill_uri / resolve_skill_uri,
│                  install_skills() (handlers skills/* et resources/*),
│                  build_skills_blocks() (bloc des instructions), repli read_skill
├── auth_in.py     auth entrante — Resource Server (docs/auth.md)
├── auth_out/      auth sortante — client OAuth de tiers (docs/auth.md), lui-même
│                  découpé : debug (traces) → probe (sonde) → storage (jetons)
│                  → authorizer (parcours, renouvellement, routes)
├── app.py         build_app() : Starlette + StreamableHTTPSessionManager + CORS
│   ├── lifespan : start des distants en parallèle puis des inprocess, stop de
│   │              chacun ; puis install_skills() et écriture des instructions
│   │              (bloc des skills compris)
│   ├── auth (facultative) : routes RFC 9728 + RequireAuthMiddleware sur /mcp
│   └── MIAOU (facultatif) : routes de webapp.py, posées après /mcp
├── webapp.py      clé `miaou_dist` : resolve_miaou_dist(), miaou_dist_warnings(),
│                  MiaouStaticFiles et build_miaou_routes() (/app/ et /)
├── selfupdate.py  état git du clone et fast-forward (lot AT), synchrone : probe(),
│                  apply_update(), run_git() borné et jamais interactif
├── updater.py     extension miaou/self-update : SelfUpdater, sonde de 5 min,
│                  miaou/update/status et miaou/update/apply ; restartable()
├── lifecycle.py   extension miaou/lifecycle (lot AV) : ProcessIdentity, Lifecycle,
│                  miaou/lifecycle/status et miaou/lifecycle/restart
├── configwatch.py lot AV : prepare_config(), la séquence de main() rejouée pour
│                  valider une config modifiée ; fingerprint(), changed_paths(),
│                  probe_bind(), ConfigWatch.check()
└── entry.py       main() — CLI ; serve_http() : uvicorn + redémarrage sur SIGUSR1
                   (exec, ou sortie en 43 sous superviseur)
                   + sortie demandée depuis l'app (ExitRequest) : 42 après une
                   mise à jour, 43 sur miaou/lifecycle/restart ;
                   et run_with_dev_auth() : --with-dev-auth, proxy ET AS de
                   développement dans ce process, sur DEUX ports
```

### Redémarrage sur place (`SIGUSR1`)

`kill -USR1 <pid>` arrête le proxy en douceur (`SHUTDOWN_GRACE_S` = 5 s pour les
requêtes en cours, au-delà un flux SSE resté ouvert est coupé), puis `os.execv` relance
sa propre ligne de commande (`sys.orig_argv`) : même pid, même terminal, code relu. Le
projet étant installé en editable par uv, c'est bien le code du dépôt. Usage : un hook
git `post-commit` local, non versionné, l'envoie à l'instance du poste après chaque
commit sur `main`.

- **Sous superviseur, sortie en 43 au lieu de l'exec** (`RESTART_EXIT_CODE`,
  `_restart`) : même arrêt complet, puis `sys.exit(43)`, et `scripts/supervise.sh` relance
  par sa fonction de lancement — `uv run` compris, donc dépendances installées et proxy
  réseau d'un fork redétecté. Exige `MIAOU_PROXY_SUPERVISED=1` ET
  `MIAOU_PROXY_RESTART_BY_EXIT=1`, posées toutes deux par `supervise.sh` ; la seconde
  n'existe que pour la transition : un `supervise.sh` d'avant le code 43, encore en
  mémoire dans le bash qui a relancé un proxy mis à jour, ne relance que sur 42 et
  rendrait 43 en arrêtant tout. Sans elle, exec comme hors supervision. Tout ce qui suit
  sur l'exec ne vaut donc que hors supervision.
- **Pas sous Windows** : le module `signal` n'y a pas de `SIGUSR1`. `RESTART_SIGNAL`
  vaut alors `None`, `_on_restart_signal` ne pose rien, et le proxy tourne sans
  redémarrage sur place — l'arrêt se fait au Ctrl-C comme avant.
- **Pas SIGHUP** : c'est ce que reçoit le processus à la fermeture de son terminal, il
  renaîtrait orphelin en gardant le port.
- **L'exec n'arrive qu'après l'arrêt COMPLET**, lifespan compris : les upstreams stdio
  sont refermés, pas laissés orphelins, et relancés avec leur code relu. Un shutdown de
  lifespan bloqué n'est pas borné par `SHUTDOWN_GRACE_S`.
- **Pas de `uv run` au redémarrage** : une dépendance ajoutée n'est pas installée, et un
  code qui ne s'importe plus meurt au redémarrage. Changer une dépendance, ou
  `serve_http`/`_on_restart_signal` eux-mêmes, exige un vrai relancement — une instance
  au code sans gestionnaire MEURT au SIGUSR1 (action par défaut).
- **Perdu** : les sessions MCP (le client reçoit un 404 et refait `initialize`) et les
  parcours OAuth sortants en cours. Jetons et cache d'outils sont dans `state/`.
- **`--with-dev-auth`** : le signal arrête le proxy, l'AS suit (`_serve_both`), puis
  exec. La paire de clés de l'AS ne vivant qu'en mémoire, les jetons entrants déjà émis
  deviennent invalides — le journal le dit, les clients sont à ré-autoriser.
- `serve_http` remplace `uvicorn.run` et en reprend deux gestes que `Server.run` seul ne
  fait pas : la sortie en 3 sur démarrage raté (port pris), et le `KeyboardInterrupt`
  avalé — `Server.run` relève le SIGINT capturé après l'arrêt, d'où une trace à
  chaque Ctrl-C sans lui.
- **Cibler le bon processus** : `uv run` reste le parent, et sa ligne de commande
  contient `-m mcp_proxy` en forme `uv run python -m mcp_proxy`. uv n'a pas de
  gestionnaire de SIGUSR1 : le motif `pkill -f` du hook s'ancre sur l'interpréteur
  (`^([^ ]*/)?python[0-9.]* ([^ ]*/bin/mcp_proxy|-m mcp_proxy)( |$)`). Il vise toutes
  les instances du poste, port compris.

### Mise à jour à la demande (clé `self_update`, lot AT)

Le proxy lancé depuis un clone git sait dire si la branche distante qu'il suit est en
avance, et s'y avancer à la demande d'un client (MIAOU). Contrat sur le fil (extension
`miaou/self-update`, méthodes, forme de l'état, refus) : `docs/miaou-contract.md`.
Désactivé par défaut ; il faut **deux clefs** pour appliquer :

- `"self_update": true` dans la config (booléen strict : une chaîne `${VAR}` est
  refusée au démarrage) — publie l'extension et lance la sonde ;
- `MIAOU_PROXY_SUPERVISED=1` dans l'environnement, posée par `scripts/supervise.sh` —
  sans elle l'état reste visible, mais `apply` est refusé : un proxy lancé à la main
  sortirait pour de bon.

**Git** (`selfupdate.py`). Racine du clone = dossier parent de `mcp_proxy/`, qui doit
être `--show-toplevel` (sinon « pas un clone », comme dans l'image Docker, sans `.git`).
Aucun nom de remote ni de branche en dur : `for-each-ref
%(upstream:remotename)/%(upstream:remoteref)` sur la branche courante ; HEAD détaché ou
branche sans upstream = `not_tracking`. Sonde : `ls-remote` du remote suivi, fetch
seulement si le sha annoncé est inconnu du clone (`cat-file -e`), position par
`rev-list --left-right --count`, jamais par dates. Application : `merge --ff-only <sha
affiché>`, jamais `pull` (qui dépend de `pull.rebase`/`pull.ff`), jamais au-delà du sha
proposé, après avoir tout revérifié (arbre propre, sha toujours contenu dans la branche
distante telle que le remote l'annonce maintenant, HEAD ancêtre). Un fichier suivi
modifié refuse en amont (`status --porcelain --untracked-files=no`) : c'est ce qui rend
sûr le retour arrière d'un **checkout interrompu** (fichier verrouillé sous Windows,
disque plein), `reset --hard <HEAD d'avant>` puis retrait des seuls fichiers que la
cible AJOUTE et que git a déjà écrits — jamais `git clean`, qui emporterait les fichiers
non suivis de l'utilisateur. Assouplir le refus de l'arbre sale exige de revoir ce
retour arrière. HEAD d'avant, cible et issue sont notés dans `state/self-update.json`.

Chaque appel git passe `-c credential.interactive=never`, `GCM_INTERACTIVE=never`,
`GIT_TERMINAL_PROMPT=0`, stdin fermé et `LC_ALL=C` (messages en anglais, pour reconnaître
l'échec d'authentification sous plusieurs formes, et `dubious ownership`). Sans les deux
premiers, Git Credential Manager ouvre une FENÊTRE sous Windows. Timeout dur (15 s en
local, 30 s pour `ls-remote`/fetch, 120 s pour le merge) qui tue le **groupe** de
processus — `start_new_session` + `killpg`, `CREATE_NEW_PROCESS_GROUP` + `taskkill /T /F`
sous Windows — parce que `git-remote-https`, petit-enfant, garderait le tube ouvert
au-delà du timeout ; repli sur le seul `kill` quand l'outil manque. Le proxy réseau :
`--proxy` écrit dans `os.environ`, git en hérite (remote HTTPS).

**Extension** (`updater.py`). Posée dans `build_app` si `self_update` est vrai, rien
sinon. Sonde au démarrage, en arrière-plan (état `checking` en attendant), puis toutes
les 5 min, dans le task group du lifespan ; une seule sonde à la fois. `apply` passe par
`asyncio.to_thread` et refuse, dans l'ordre : sans superviseur, cible mal formée,
application déjà en cours (verrou), sortie déjà demandée (`UPDATE_RESTARTING` : un
redémarrage de `miaou/lifecycle`, ou une mise à jour précédente dont l'arrêt est en
cours), appels d'outils en vol (`InFlight`, compté autour de
`handle_call_tool`, tous clients confondus ; revérifié dans le thread juste avant le
merge, genre connu), puis tout refus de l'étape git. Une mise à jour qui ne touche que
`miaou_dist` (genre `miaou`) ne redémarre rien et ignore les appels en vol : `/app/` relit
le disque à chaque requête. Une sonde réseau en cours retarde l'arrêt du proxy jusqu'à
sa fin (le thread ne s'annule pas), 60 s au pire.

**Sortie en 42** (`UPDATE_EXIT_CODE`, `entry.py`). Après un fast-forward qui exige un
redémarrage, le handler appelle l'`ExitRequest` créée par `main()` et liée au serveur
uvicorn dans `serve_http` (ou `run_with_dev_auth`) : même arrêt en douceur que Ctrl-C,
donc la réponse d'`apply`, requête en vol, part avant la coupure ; lifespan refermé ;
puis `sys.exit(42)`. Examinée AVANT la demande de SIGUSR1 : les deux posées, la sortie
l'emporte, seule la relance par `uv run` installant les dépendances. Le proxy ne se
relance jamais lui-même.

**`scripts/supervise.sh`** (bash : macOS, Linux, Git Bash). Pose
`MIAOU_PROXY_SUPERVISED=1` et `MIAOU_PROXY_RESTART_BY_EXIT=1`, relance tant que la
commande sort en 42 (mise à jour) ou 43 (SIGUSR1, `miaou/lifecycle/restart`), rend tout autre code tel quel (0 au
Ctrl-C). Exécuté, il relance la commande donnée (`uv run --directory
<clone> mcp_proxy` sans argument) ; sourcé, `miaou_supervise <fonction> [args…]` rappelle
la fonction à chaque tour, ce qui refait la détection du proxy réseau d'un fork à chaque
relance. Il ne construit jamais la ligne d'une installation : un fork l'appelle depuis
son propre `start.sh`, sans le modifier (aucun conflit au merge de l'upstream). Une
sortie en erreur (code 1 à 127) du lancement qui suit une mise à jour affiche la commande
de retour arrière, lue dans `state/self-update.json` (chemin par défaut) — une relance
sur 43 entre les deux la désarme : le code mis à jour a tourné ; pas un arrêt
par signal — SIGTERM rend 143, uvicorn relevant le signal après un arrêt propre (mesuré
sous `uv run`). `.gitattributes` force LF sur `*.sh` :
Git for Windows convertit en CRLF par défaut, et Git Bash échoue alors dès la première
ligne.

```bash
scripts/supervise.sh                                  # uv run mcp_proxy, depuis le clone
scripts/supervise.sh uv run mcp_proxy --port 8765     # commande explicite
```

**Ce que `call_tool` relaie.** Un upstream stdio ou http rend son `CallToolResult` à travers
`relay_call_result` (upstream.py) : `content`, `isError` et `_meta` passent — sauf, dans
`_meta`, la clé `io.modelcontextprotocol/serverInfo` dont un upstream moderne signe ses
résultats : le serveur du proxy ne pose son propre `serverInfo` que si la clé est absente,
et relayer celle de l'upstream ferait passer son identité pour celle du proxy. Pas
`structuredContent` — par cohérence avec `tools/list`, qui ne publie pas l'`outputSchema` des
upstreams. Jusqu'au lot AI, seul `content` passait : le SDK ré-enveloppait la liste en
`isError=False`, si bien qu'un échec signalé par l'upstream arrivait au client comme un
succès, et le `_meta` d'un résultat (`miaou/web` de `mcp_web`) disparaissait. Un upstream
inprocess passe par l'API publique `MCPServer.call_tool`, qui rend un `CallToolResult`
complet (`_meta` d'un outil qui le pose compris), et le proxy le laisse traverser tel quel.

**Ce que `call_tool` rend en erreur.** Le `Server` bas niveau du SDK 2.x n'enveloppe plus
rien : une exception qui sort du handler devient une erreur JSON-RPC (code 0 si ce n'est
pas une `MCPError`), là où la 1.x rendait TOUTE exception en `isError` portant `str(e)` —
ce que le modèle lisait. `handle_call_tool` reproduit donc ce comportement lui-même, avec
deux exceptions voulues, qui sortent en erreur JSON-RPC `data` intact :

- `UpstreamNotAuthorized` (une `MCPError`), le contrat AUTHORIZATION_REQUIRED ;
- une `MCPError` levée par un outil **inprocess** — REF_UNKNOWN de `mcp_docs`. Celle d'un
  upstream **distant** est la réponse d'erreur de ce serveur, aplatie en `isError` comme
  en 1.x, son message compris : `HttpUpstream.call_tool` la déballe de l'`ExceptionGroup`
  dont l'enveloppe son task group (`docs/auth.md`), faute de quoi le client lisait
  « unhandled errors in a TaskGroup » au lieu du motif.

« Outil inconnu » reste un `isError` textuel, comme l'appel d'un outil dont l'upstream
**sans authorizer** n'a plus de session : « injoignable », avec la cause de la fermeture,
et la mention que le proxy ne s'y reconnecte qu'à son redémarrage. Seul un upstream qui a
un parcours d'autorisation est refusé en AUTHORIZATION_REQUIRED — sans quoi un serveur
sans OAuth, arrêté en cours de vie, envoyait l'utilisateur l'autoriser.

**Un appel d'outil est borné** : clé `call_timeout` d'une entrée stdio ou http (secondes,
nombre fini > 0, défaut 300 — `_CALL_TIMEOUT_S`, aligné sur le délai de lecture http ;
`bench__sleep` plafonne à 60 s), validée au chargement (`ValueError` sinon, et sur une
entrée inprocess, qui n'a pas de fil). `route_call_tool` l'applique autour de
`upstream.call_tool` (`Upstream.call_timeout`, None = pas de borne) : au dépassement,
l'appel est abandonné et rendu en `isError` « Le serveur '<up>' n'a pas répondu à
'<outil>' sous <n> s », et le compteur `InFlight` est libéré — sans quoi une mise à jour
ou un `restart` resteraient refusés tant que l'upstream muet ne répond pas. Une exception
d'outil au message vide se lit par son type (`_describe_failure`), jamais en `isError`
sans texte.

**Une panne d'upstream reste la sienne.** `tools/list` liste chaque upstream vivant à
part : celui dont le listage échoue est omis (une ligne au journal), les autres répondent.
Le cas est celui d'un upstream mort en cours de vie — un subprocess stdio tué reste dans
la table, et `upstream_is_live` ne voit pas sa mort ; sans l'isolation, `tools/list`
échouait en entier, pour tous, à chaque appel. Un upstream sans authorizer et sans session
n'est pas resservi depuis `ToolCatalogCache` : ses outils resservis porteraient la mention
« non autorisé », fausse pour lui. Le cache reste réservé au troisième état
(`docs/auth.md`). Aucune reconnexion automatique : l'upstream revient au redémarrage du
proxy. Même isolation pour les listages de skills et le bloc des instructions (« Extension
Skills »). En 1.x, les deux contrats passaient par un
sentinel dans le texte de l'`isError`, repêché par deux wrappers qui remplaçaient le
handler enregistré : ni l'un ni l'autre n'existe plus.

**Schémas d'entrée complétés.** Le SDK 2.x valide un résultat contre le schéma du
protocole AVANT de l'émettre : un seul outil dont l'`inputSchema` n'a pas
`"type": "object"` ferait rejeter le `tools/list` ENTIER en INTERNAL_ERROR, les autres
upstreams avec. `_object_schema` complète le schéma (catalogue en cache sans schéma,
upstream tiers peu rigoureux) plutôt que de laisser un outil éteindre les autres.

**`x-mcp-header` retiré des schémas publiés** (`_strip_param_headers`, à toute profondeur,
mais jamais dans les noms de propriétés ni les données d'`enum`/`const`/`default`/`examples`).
L'annotation demande au client de recopier un argument dans un en-tête `Mcp-Param-*`.
Republiée, c'est le serveur DU PROXY qui l'exigeait de son client — 400 `-32020` sur un
`tools/call` moderne sans l'en-tête (mesuré), quelle que soit l'ère de l'upstream. Vers
un upstream moderne, l'en-tête est émis par le SDK du proxy depuis sa propre liste
(« Ère des upstreams stdio et http ») ; un upstream legacy n'en a pas l'usage. Vaut pour
le catalogue en cache aussi, qui repasse par le même chemin.

**Gestionnaire de sessions.** `build_app` passe à `StreamableHTTPSessionManager` les deux
réglages que partagent les serveurs autonomes (`servers/mcp_base.py`) :
`MAX_REQUEST_BODY_BYTES` (96 Mio, contre 4 Mio par défaut en 2.x, cf.
`docs/miaou-contract.md` sur `content_b64`) et `SESSION_IDLE_TIMEOUT_S = None` (le SDK
expirerait sinon une session inactive au bout de 30 min). `security_settings` reste
absent : dans le gestionnaire, `None` vaut protection DNS-rebinding désactivée, ce que
l'`Origin: null` de MIAOU en `file://` exige. Le plafond de corps est appliqué par le
middleware du SDK, qui accumule le corps EN MÉMOIRE avant d'appeler l'application : N
requêtes concurrentes proches du plafond coûtent N × 96 Mio, et sans `auth` n'importe
quel hôte qui joint le port peut les envoyer — posture de banc d'essai sur réseau local,
assumée.

`build_app()` ne retourne pas directement le `Starlette` mais une fonction ASGI qui l'enveloppe :
`Mount("/mcp", ...)` redirige `/mcp` → `/mcp/` en 307 par défaut (strict-slash Starlette), et
certains clients MCP ne suivent pas les redirections sur POST/DELETE. Le wrapper réécrit
`scope["path"]` de `/mcp` vers `/mcp/` avant le routeur pour servir la requête directement,
sans redirection.


### Redémarrage à la demande (extension `miaou/lifecycle`, lot AV)

Le proxy dit si sa config a changé depuis son démarrage, et se redémarre à la demande
d'un client (MIAOU), sous superviseur. Contrat sur le fil (méthodes, forme de l'état,
refus) : `docs/miaou-contract.md`. **Publiée par défaut** ; clef `lifecycle`
(`configwatch.resolve_lifecycle`, booléen strict, absente ou `null` = `true`) : à `false`,
`main()` ne construit pas le `Lifecycle`, donc ni méthode ni extension — pour un
déploiement où la config ne bouge pas sur place et où rien ne relance le proxy
(conteneur). Le SIGUSR1 n'en dépend pas, ni l'`instance` des secrets volatils. Aucune
garde d'origine.

**Empreinte** (`configwatch.py`). `LoadedConfig.merged` est la fusion des couches AVANT
`interpolate_env` : c'est elle qu'on empreinte (sans `_comment*`, JSON canonique,
SHA-256), parce qu'`os.environ` bouge après le chargement (`--proxy`, `env` d'un
inprocess) et qu'une empreinte prise après la substitution changerait toute seule. La
référence est le dict que `main()` a chargé, passé à `ConfigWatch` — jamais une relecture
ultérieure, qui ferait d'une édition faite pendant le démarrage l'état initial. À chaque
`status`, la chaîne est **re-résolue** avec les mêmes arguments (`--config` tel quel,
chaîne par défaut relative au dossier courant) et relue dans un thread ; rien n'y lève,
`OSError` et `ValueError` deviennent `invalid` avec leur message. La table de validations
par empreinte est bornée (`VALIDATION_CACHE_MAX`, la plus ancienne sort d'abord).

**Validation partagée.** `prepare_config(loaded, options)` est la suite de `main()` après
la lecture des couches : hôte et port, `miaou_dist`, `self_update`, `lifecycle`, `--with-dev-auth`,
auth entrante, `build_upstreams`, `build_upstream_authorizers`. `main()` l'appelle ; la
validation la rejoue, avec les mêmes `LaunchOptions` (surcharges de la ligne de commande),
et substitue les `${VAR}` contre l'environnement du démarrage, recopié en tête de
`main()` avant toute modification — celui que retrouvera le processus relancé. Toute
erreur de config y est une `ValueError` dont le message est celui que `main()` affiche
— y compris une FORME fausse (`mcpServers` qui n'est pas un objet, entrée non-objet,
`env`/`headers`/`config` non-objet, `args` non-liste de chaînes, `port` hors 1-65535 ou
non entier : `build_upstreams` et `_resolve_port`) ; en dernier filet,
`ConfigWatch._validate` rend `invalid` pour toute autre exception plutôt que de laisser
tomber `status`/`restart` (`main()`, lui, la laisserait remonter : c'est un bug) :
`tests/test_proxy_configwatch.py` tient l'inventaire, un cas par sortie en 1. Elle ne fait
que construire : ni `prepare` d'upstream (import, `env`), ni
`apply_proxy_env_overrides_to_process`, ni écriture (les autorisations sortantes ne
touchent au fichier de jetons qu'au premier parcours) ; son journal est coupé par
`logging.muted()`, variable de contexte que `to_thread` recopie. Elle n'a lieu que si
l'empreinte diffère de la référence, une fois par empreinte. Une nouvelle option de
ligne de commande qui pèse sur la config va dans `LaunchOptions`, sinon la validation
divergera du démarrage.

**`bind` d'essai** (`probe_bind`), seulement si l'hôte ou le port EFFECTIFS changent
(une surcharge `--port` rend sans effet un changement de `port`) : par
`loop.create_server`, comme `uvicorn.Server.startup`, donc avec les mêmes options de
socket par construction — `SO_REUSEADDR` sous POSIX, pas sous Windows, où il
permettrait de prendre un port déjà occupé. Mis en cache par empreinte pour `status`,
refait à chaque `restart`. Course assumée : un port libre à l'essai peut être pris avant
la relance.

**Redémarrage** (`lifecycle.py`). `restart` exige `restartable()` (`updater.py`), la
condition même du chemin 43 du SIGUSR1 (`_restart`) : une seule source, lue par
`status`, la garde et le signal. Refus dans l'ordre : superviseur, application d'une mise
à jour en cours (`SelfUpdater.applying`), config `invalid` (relue, `bind` refait), appels
en vol (`InFlight`, levé par `force`). Les demandes sont sérialisées par un verrou ; une
sortie déjà demandée rend la réponse d'acceptation sans rien redemander. Puis
`ExitRequest(RESTART_EXIT_CODE)` : même arrêt en douceur que pour la mise à jour, la
réponse part, lifespan refermé, `sys.exit(43)`, journal « redémarrage demandé par un
client ». `ExitRequest` garde le PREMIER code demandé (42 ou 43) ; une fois une sortie
demandée, `apply` est refusé (`UPDATE_RESTARTING`). Jamais d'exec : hors supervision, le
refus `RESTART_NOT_SUPERVISED`, et sous Windows le chemin 43 fonctionne, n'ayant besoin
d'aucun signal (non éprouvé sur le poste : dette). `--with-dev-auth` : toute relance,
signal ou sortie demandée, invalide les jetons émis par l'AS de développement, et le
journal le dit.

**Durée de la relance.** MIAOU attend le retour du proxy trois minutes
(`PROXY_RESTART_WAIT_MS`, en sondant toutes les 2 s, retour reconnu à un autre
`instance`), puis annonce « démarrage échoue ». Mesuré : sous `supervise.sh`, nouvel
`instance` en 0,8 s sans changement de dépendances, et une réinstallation complète par
`uv run` tient en 0,7 s quand le cache uv est chaud. Rien ne borne en revanche un `uv`
qui doit TÉLÉCHARGER (cache froid, réseau d'entreprise lent ou bloqué) : ni `uv` ni
`supervise.sh` n'ont de délai, et le client ne distingue pas une relance lente d'une
relance échouée. Une relance qui échoue arrête le superviseur (pas de nouvelle
tentative), et seul son journal le dit : l'expiration des trois minutes côté MIAOU est le
premier signe visible.

**Identité de processus** (`ProcessIdentity`) : `instance` et `started_at`, tirés une
fois dans `main()`, passés à `Lifecycle` ET à `VolatileSecrets` — une seule identité, que
le client compare pour reconnaître le processus relancé (`head`, l'indice d'AT, ne bouge
pas sans mise à jour).

## Ordre et concurrence du démarrage

Le lifespan (`app.py`, `_start_upstreams`) démarre les upstreams **distants d'abord et
ensemble** : tout ce qui n'est pas `InProcessUpstream` (stdio, http) part dans une tâche
fille d'un task group éphémère, puis les inprocess se démarrent dans la tâche du
lifespan pendant que les distants avancent. Avant, les `start()` s'enchaînaient et les
bornes s'additionnaient (15 s par stdio muet, 30 s par http injoignable) ; le démarrage
dure désormais autant que le plus lent. Un `start()` qui échoue ne touche pas les
autres : l'upstream est retiré de la table, comme avant (`AuthorizationRequired` seul y
reste). Tout ce qui suit le démarrage (consommateurs de secrets volatils, authorizers,
`publish_surface`, amorçage des catalogues) attend la fin du task group et garde son
ordre. Le sondage d'état des authorizers (jeton utilisable, renouvellement) précède
toujours les démarrages, en série.

**Journal.** Chaque upstream rédige ses lignes (nombre d'outils, skills, ère, ou
`unavailable`/`unauthorized`/`pending`) puis un vidage les écrit dans l'ORDRE DE LA
CONFIG, dès que tous les précédents sont écrits : déterministe d'un lancement à l'autre
et aussi précoce que possible, un lent en tête de config retenant l'affichage des
suivants sans retarder leur démarrage.

**Annulation.** `started` se remplit à mesure que les `start()` aboutissent ; le
`finally` du lifespan arrête ceux-là. Un `start()` interrompu (Ctrl-C pendant le boot)
nettoie lui-même : celui d'un stdio referme son transport avant de relever l'annulation,
celui d'un http laisse sa tâche de service au task group du lifespan, annulé à
l'extinction. L'ordre d'arrêt est l'ordre de fin de démarrage, pas celui de la config.

**Modèle de tâche des stdio.** Les contextes du SDK (`Client`, `stdio_client`) portent des
cancel scopes anyio qu'il faut ouvrir ET refermer dans la même tâche ; un `Client` ouvert
par une tâche fille de démarrage et refermé par le lifespan lèverait « cancel scope in a
different task ». `StdioUpstream` adopte donc le patron de `HttpUpstream` : `_serve()`,
dans une tâche asyncio propre (`ensure_future`, sans task group hôte — la tâche fille qui
appelle `start()` ne lui survit pas), ouvre le `Client`, signale `_ready`, attend
`_stop_event`, referme au même endroit. `start()` attend `_ready` sous la borne du
handshake (15 s, amorçage du listage compris) ; sur échec, délai ou annulation, il annule
le `CancelScope` de la tâche de service (la seule prise sur un handshake bloqué), attend
sa fin sous bouclier (`_STDIO_STOP_TIMEOUT_S`, 10 s) et relève : aucun subprocess
orphelin. `stop()` demande la fermeture et attend, bornée, sans toucher un scope.

## Ère des upstreams stdio et http

Le proxy parle à un upstream stdio ou http la révision 2026-07-28 quand celui-ci la
parle, `initialize` sinon. Rien n'est écrit ici : `StdioUpstream.start()` et
`HttpUpstream._serve()` ouvrent un `Client(<transport>, mode=…, cache=None)` du SDK,
qui sonde et se replie lui-même (`mcp/client/_probe.py`, `negotiate_auto`), puis lisent
sur `client.session` ce que les deux ères posent pareil : `instructions`,
`protocol_version`, `server_capabilities` (recopiés sur `Upstream.instructions`,
`protocol_version`, `capabilities`). `mode` vient de la clé `protocol_era` de l'entrée :
`"auto"` par défaut, `"legacy"` pour imposer `initialize` — le recours, sans code, pour un
upstream moderne d'un autre SDK que la validation de la révision 2026-07-28 ferait
échouer. L'ère retenue figure dans le journal de démarrage, à la suite du nombre
d'outils (`demo         2 tools (2026-07-28)`) ; rien pour un inprocess, sans fil. Le transport est construit par le proxy et passé
tel quel, jamais l'URL : pour http, le client httpx2 de `_build_http_client` reste le
seul employé (en-têtes, délais, OAuth, proxy réseau). `Client` s'ouvre et se referme dans
la tâche de service, comme la `ClientSession` d'avant (`docs/auth.md`, « `HttpUpstream` :
le transport vit dans sa propre tâche »). `cache=None` : le cache de réponses de `Client`
ne sert pas les appels faits sur `client.session`, il est écarté sans ambiguïté.

**Quand le SDK se replie sur `initialize`.** Sur TOUTE `MCPError` reçue en réponse à
`server/discover` — erreur JSON-RPC d'un serveur qui ne connaît pas la méthode, 400 d'un
serveur 1.x, mais aussi :

- **délai dépassé** : la sonde a son propre délai de 10 s, et son expiration est une
  `MCPError`. C'est ce qui donne l'ère moderne à un subprocess lent à démarrer (mesuré :
  un serveur 2.x prêt en 12 s est abordé en moderne) ; un serveur qui aurait traité la
  sonde trop tard répond `-32022` à l'`initialize` de repli, et le SDK re-sonde ;
- **4xx sans corps JSON-RPC** (401/403 d'une passerelle) : le transport les convertit en
  `MCPError(-32603)`, statut perdu. `initialize` reçoit le même refus, et l'erreur finale
  est celle d'avant, une requête plus tard.

Toute autre exception traverse la sonde sans repli (réseau, annulation,
`AuthorizationRequired` levée par l'`Auth` OAuth) : une panne n'est jamais un verdict
d'ère.

**Bornes.** La négociation entière reste sous la borne existante (15 s stdio, `timeout`
http), délai de sonde compris : un serveur muet échoue toujours à la borne (« n'a pas
répondu à la négociation MCP »). Un `timeout` http configuré sous 10 s rend le repli sur
délai inatteignable : un serveur lent échoue à la borne, comme avant. L'ère est
recalculée à chaque `start()`, jamais mémorisée — le redémarrage d'`authorize()` refait
la sonde, avec le jeton. Un upstream qui change d'ère en cours de vie : rien
d'automatique, ses appels échouent jusqu'au prochain démarrage.

**Upstream legacy : identique, une requête près.** Après le refus de la sonde, la
session est exactement celle du mode legacy — `initialize` sans en-tête de version (le
transport efface celui de la sonde), offre `2025-11-25`, puis `Mcp-Session-Id`, flux GET
et DELETE à l'arrêt ; seul l'`id` JSON-RPC avance d'un cran. `tests/test_proxy_era.py`
le vérifie message par message, contre le mode legacy, sur les deux transports. Coût :
une requête par démarrage, et pour un upstream stdio SDK 1.x, une rafale de
`Failed to validate request` sur son stderr, que le proxy hérite (le filtrer masquerait
aussi ses vraies erreurs).

**Upstream moderne : ce qui change.** Les `instructions` viennent du `DiscoverResult`
(même contenu pour un serveur 2.x ; non garanti pour un autre SDK). Le SDK pose sur
chaque requête l'enveloppe `_meta` (`protocolVersion`, `clientInfo`,
`clientCapabilities`, vides) et, en http, `MCP-Protocol-Version`, `Mcp-Method`,
`Mcp-Name` ; ni session, ni flux GET, ni DELETE. Il valide les résultats contre la
révision 2026-07-28, et retire de `tools/list` un outil dont une annotation
`x-mcp-header` est invalide (avertissement sur le logger nommé `"client"`).

**`Mcp-Param-*` exige un listage préalable.** Le SDK n'émet ces en-têtes, pour un
`tools/call` http, que depuis les annotations du DERNIER `tools/list` de la session
(sur stdio, aucun en-tête : seul le `_meta` part, et le serveur ne valide les
`Mcp-Param-*` que sur son routeur HTTP). Le proxy liste à chaque `tools/list` de son
client, mais pas après le redémarrage d'`authorize()`, ni quand il ressert son catalogue
en cache : un appel arrivé dans cette fenêtre partait sans en-tête et l'upstream le
refusait (`-32020`, mesuré). `start()` liste donc les outils d'un upstream moderne dès
l'ouverture de la session (`_prime_tool_listing`) ; une erreur de l'upstream sur ce
listage n'empêche pas le démarrage, une panne du transport si. Rien en legacy.

Le proxy ne republie AUCUNE capacité d'upstream : les siennes restent celles qu'il implémente, et
relayer une extension inconnue promettrait au client des méthodes qu'il ne sait pas
router. Seule l'extension Skills, qu'il implémente lui-même, se sert de celles de
l'upstream : déclarée par un upstream moderne, elle fait relayer ses skills (« Extension
Skills »).


## Consigne de portée serveur (`instructions`)

Une consigne qui vaut pour un serveur ENTIER (« lire telle documentation avant
d'appeler ces outils ») n'a pas d'emplacement au niveau outil : les seuls champs
qu'un client relaie au modèle par outil sont `name`, `description` et
`inputSchema`. La recopier dans chaque docstring donne N copies d'un texte qui ne
discrimine aucun outil — ce qui reste dans une docstring d'outil doit être ce qui
distingue CET outil.

Le protocole prévoit `instructions` sur l'`InitializeResult`, destiné au system
prompt du modèle. Côté serveur, `MiaouMCPBase(..., instructions=...)` le passe à
`MCPServer` — par mot-clef : en 2.x, son deuxième paramètre positionnel est `title`, où
une consigne passée en position partirait sans erreur. Côté proxy, trois captures, une par type d'upstream :

| Upstream | Capture |
|---|---|
| `InProcessUpstream` | `server.instructions` (le `MCPServer` importé) — pas d'`initialize` sur ce chemin |
| `StdioUpstream` | `session.instructions` après négociation : `DiscoverResult` en ère moderne, `InitializeResult` en legacy |
| `HttpUpstream` | `session.instructions` après négociation, idem |

`aggregate_instructions()` compose le champ unique du proxy à partir des N
upstreams : un préambule, puis une section `## <nom>` par upstream qui en
déclare. **Le titre de section est le préfixe d'outil** (`bench` pour
`bench__echo`) — c'est ce qui rend la portée d'une consigne déductible par le
modèle sans convention supplémentaire à lui faire connaître. La section d'un
upstream qui sert des skills se TERMINE par un bloc généré (cf. « Extension
Skills » ci-dessous), et un upstream qui en sert sans déclarer d'instructions a
une section pour ce bloc seul. La clé `instructions` d'une entrée `config.json`
ajoute une consigne de l'opérateur (`Upstream.config_instructions`, posée par
`build_upstreams()`) : elle suit le texte de l'upstream et précède le bloc des skills,
ou ouvre seule la section d'un upstream muet. Un upstream sans instructions ni skills n'a pas
de section ; si aucun n'en a, le champ vaut `None` et l'`InitializeResult` est
celui d'avant le lot, à l'octet près.

Quatre points qui ne se devinent pas :

- **Écriture différée, pas paramètre de construction.** `build_proxy_server()`
  s'exécute AVANT `start()` : les instructions y seraient vides pour tout le
  monde, en silence (même piège que l'état d'upstream figé à la construction).
  Le lifespan écrit `mcp_server.instructions` après `_start_upstreams()`. C'est
  sûr parce que le SDK relit l'attribut à chaque
  `create_initialization_options()`, et qu'aucun client n'a pu faire son
  handshake avant — `session_manager.run()` n'a pas encore démarré.

- **Le préambule énonce `<serveur>__<outil>`, non préfixé.** Littéralement vrai
  pour un client parlant à ce proxy en direct. Un client qui agrège LUI-MÊME
  plusieurs serveurs re-préfixe (MIAOU expose `miaou-proxy__bench__echo`, et le
  slug est celui de la carte serveur : `proxy` ailleurs) : la forme est alors
  fausse d'un cran, et c'est à ce client de réécrire la phrase — il est seul à
  connaître le slug sous lequel il publie ce proxy. Le mettre en `config.json`
  dupliquerait une information qui vit chez le client, avec dérive garantie au
  premier renommage de carte serveur.

- **Un upstream non autorisé garde sa section.** C'est de la documentation, pas
  une capability, et un client déjà connecté ne refait pas son handshake après une
  autorisation obtenue en cours de route : une section omise lui manquerait jusqu'à
  sa reconnexion, alors qu'une section décrivant un outil temporairement absent ne
  coûte qu'un paragraphe. Après un `authorize()` réussi, le proxy recompose ses
  `instructions` (et sa surface de skills, `publish_surface`) : la section d'un upstream non autorisé au
  démarrage, qui n'avait jamais été interrogé, apparaît à la connexion suivante
  (`docs/auth.md`). Observé au passage sur un upstream Jira derrière WSO2 : `tools/list`
  y répond avant autorisation, seul `tools/call` refuse.

- **La consigne de la config n'est pas re-préfixée.** Le texte d'un upstream dont
  les skills sont relayées voit ses URI `skill://` passées dans l'espace de noms du
  proxy ; celle de l'opérateur, non : écrite pour la surface publiée, elle est réputée
  la viser déjà. Elle vit dans la section de son upstream et en partage le sort : gardée
  pour un upstream non autorisé, emportée avec lui quand `start()` échoue et qu'il est
  retiré de la table.

Le champ n'atteint le modèle que si le CLIENT le lit et l'injecte dans son system
prompt : le proxy le publie correctement, mais un client qui ignore
l'`InitializeResult` n'en transmet rien. MIAOU le fait (cf.
`docs/miaou-contract.md`) ; un client tiers, pas forcément.


## Extension Skills (`io.modelcontextprotocol/skills`)

Le proxy sert l'extension Skills (spec : `specification/stable/skills.mdx` du dépôt
`modelcontextprotocol/ext-skills`) avec les skills de ses upstreams. Côté serveur,
c'est `Skills` de `servers/mcp_base.py` (cf. `docs/servers.md`) ; ici, l'agrégation.
Contrat publié au client : `docs/miaou-contract.md`.

**URI préfixées.** Le proxy insère le nom d'upstream (clé de `mcpServers`) en premier
segment : `skill://bench/SKILL.md` de l'upstream `bench` est publiée
`skill://bench/bench/SKILL.md`. Même axe que le préfixe `bench__` des outils, et le
dernier segment reste le `name`, comme l'exige la spec. Seules les URI changent, jamais
les octets : les empreintes de l'upstream restent valides. Un seul couple de fonctions
pures réécrit, `prefix_skill_uri` / `resolve_skill_uri` ; la seconde refuse en `-32602`
une URI hors `skill://`, sans chemin, ou dont le premier segment n'est pas un upstream
vivant. Le message d'erreur cite toujours l'URI du client, jamais celle de l'upstream.

**Enregistrement au lifespan, seulement si une skill est servie.** `install_skills()`
pose `skills/list`, `skills/get`, `resources/list`, `resources/read` et
`Server.extensions` APRÈS le démarrage des upstreams — `build_proxy_server()` s'exécute
avant, et ne sait rien des skills. Ça tient parce que le SDK calcule les capacités à
chaque `server/discover` (mesuré) : un enregistrement tardif est vu par tout client.
« Servie » : un upstream inprocess qui liste au moins une skill, OU un upstream stdio/http
vivant qui DÉCLARE l'extension, même avec un `skills/list` vide — la spec interdit de lire
un listage vide comme « aucune skill », et une skill servie non listée doit rester
lisible. Un proxy sans ni l'un ni l'autre ne publie RIEN de neuf — vérifié à l'octet
contre l'ancien code. Effet de bord : avec une skill servie, l'`initialize` legacy
annonce aussi `resources` (le SDK dérive cette capacité du handler `resources/list`).

**Quand le catalogue est lu.** Au démarrage, par `install_skills` et
`build_skills_blocks` ; puis EN DIRECT à chaque `skills/list` et `resources/list` du
client (comme `tools/list`) ; `skills/get` et `resources/read` sont transmis à chaque
appel. Jamais au `server/discover` du client, dont les capacités se calculent sur les
handlers du proxy. Le proxy n'a aucun cache de skills : le `ttlMs` d'un upstream ne sert
qu'aux indices qu'il publie. Pour un upstream inprocess, les entrées sont relues de même
(empreintes, tailles, frontmatter) — le CONTENU de fichiers dont l'ensemble, lui, est figé
au démarrage de l'upstream : un fichier ajouté ou supprimé exige de le redémarrer
(`docs/servers.md`, « Fraîcheur »).

**Indices de cache** : les plus restrictifs de ceux du proxy (`ttlMs: 300000`,
`cacheScope: "public"`) et de ceux des upstreams concernés — `ttlMs` minimal, `private`
dès qu'un upstream le dit (un upstream OAuth peut servir des skills propres à
l'utilisateur, qu'un cache partagé ne doit pas resservir). Toutes les pages d'un
`skills/list` paginé comptent.

**Qui juge qu'un fichier est servi.** `resources/list` ne liste que les fichiers de
skills. `resources/read` d'un upstream INPROCESS ne lit que les fichiers déclarés par une
entrée (liste blanche), son `MCPServer` pouvant servir d'autres ressources sous d'autres
schémas. Pour un upstream stdio/http, c'est l'UPSTREAM qui juge : la requête lui est
transmise, il répond `-32602` pour ce qu'il ne sert pas (la liste blanche de l'inprocess
est l'ensemble figé au démarrage, pas le résultat de `list_entries` : un fichier disparu
du disque rend `-32603` « indisponible », pas « non servi »). Une liste blanche tirée de son
`skills/list` refuserait les fichiers d'une skill servie mais non listée, que la spec
exige de savoir charger depuis sa seule URI ; la liste blanche du SEP est l'affaire du
client.

**Entrées distantes : pas de confiance, mais pas de filtre.** `collect_skills` écarte,
avec une trace par entrée et par vie de l'upstream, ce que le préfixage ne sait pas
traiter (`_unprefixable`) : entrée qui n'est pas un objet, `uri` hors `skill://`,
`resources` ni liste ni `"dynamic"`, fichier sans `uri` `skill://`. Tout le reste est
relayé tel quel — nom hors règle, dernier segment ≠ `name`, `description` absente,
`resources` vide ou `"dynamic"`, tailles et empreintes : le client vérifie, et sait dire
pourquoi une entrée est invalide, là qu'une entrée écartée par le proxy disparaîtrait sans
explication. `skills/get` d'une entrée non préfixable : `-32603`. `resources/list` et le
bloc des instructions tolèrent un frontmatter absent ou incomplet.

**Upstreams stdio et http : relayés en ère moderne, extension déclarée.** Les trois
méthodes de skills de `StdioUpstream` et `HttpUpstream` (`_RemoteSkills`, upstream.py)
passent par le fil MCP, sous UNE condition, `Upstream.serves_skills` : ère moderne
négociée ET `io.modelcontextprotocol/skills` dans les `extensions` du `server/discover`
de l'upstream. Faux, elles rendent « aucune skill » sans émettre de requête. Il faut les
deux : un serveur 2.x répond à `skills/*` même abordé en `initialize` (mesuré), la
réponse ne dit donc pas si l'on avait le droit de demander — et la spec lie l'extension
à `server/discover`. Un upstream legacy, constaté par la sonde ou forcé par
`protocol_era: "legacy"`, ne reçoit ainsi aucune requête de plus, et ce que le proxy publie
pour lui ne change pas. L'inprocess, sans fil, sert toujours (`serves_skills` vrai, ses
skills lues sur le `MCPServer`).

Ce qui part : `skills/list` (en suivant `nextCursor`, au plus `_SKILLS_MAX_PAGES` pages,
arrêt sur un curseur déjà vu) et `skills/get` par `session.send_request` — le SDK client
n'a pas de verbe pour eux —, résultat lu en `dict` brut, sans modèle : le proxy relaie,
le client vérifie (empreintes comprises). `skills/get` rend l'entrée déballée de `skill`,
comme l'inprocess. `resources/read` par `session.read_resource`, qui pose `Mcp-Name` en
ère moderne ; le SDK y valide le résultat contre la révision 2026-07-28 (`cacheScope` et
`resultType` requis), ce qu'un upstream d'un autre SDK peut ne pas tenir — erreur rendue
en `INTERNAL_ERROR`, seul recours `protocol_era: "legacy"`, qui coupe aussi ses skills.
`skills/*` n'est pas validé par le SDK (méthode hors de son tableau). Le tampon moderne
(`_meta`, en-têtes d'ère) est posé par le SDK, rien à écrire ici. Chaque requête est
bornée par le délai de l'upstream (`timeout` http, 15 s stdio) : un upstream muet ne
retient pas `skills/list` du proxy jusqu'au délai de lecture du transport (300 s).

**`HttpUpstream` : une seule garde.** `call_tool` et les trois méthodes de skills passent
par `_on_session`, qui fait courir la requête contre la mort de la tâche de service et
déballe l'`ExceptionGroup` de son task group (`docs/auth.md`). Sans le déballage, une
`MCPError` de l'upstream sur `skills/get` sortait du handler en erreur JSON-RPC de code 0.
`StdioUpstream` appelle la session en direct : sa tâche de service ne fait qu'héberger le
transport, la mort du subprocess remonte en `MCPError` « Connection closed ».

**Erreurs relayées.** Un `-32602` de l'upstream ressort avec l'URI du client ; toute autre
`MCPError` garde son code (un `-32603` d'une skill devenue illisible, le `-32000`
« Connection closed » d'un transport mort) et son message, encadré de `Le serveur
'<up>' n'a pas pu servir <uri> (<message>).` ; seules les erreurs à `data` (contrats
AUTHORIZATION_REQUIRED, SECRET_REQUIRED) passent intactes. Une panne
de transport, un délai dépassé ou une réponse que le SDK refuse deviennent une
`MCPError(INTERNAL_ERROR, "Le serveur '<up>' n'a pas pu servir <uri> (…)")`
(`_remote_failure`), jamais une erreur de code 0 ; une `AuthorizationRequired` en cours
d'appel, le contrat AUTHORIZATION_REQUIRED. Sur `skills/list` et `resources/list`,
l'upstream en panne est omis (une ligne au journal) et les autres répondent. Le repli
`read_skill` rend toutes ces issues en `isError` lisible.

**`_meta` des outils.** Relayé en entier par `tools/list` (`relay_tool_meta`), sauf
`miaou/requiresSkill`, dont l'URI est relative au serveur qui liste l'outil : elle reçoit
le préfixe d'upstream. Une valeur non préfixable (pas une chaîne `skill://`) est retirée
plutôt que relayée fausse. `ToolCatalogCache` mémorise ce `_meta` : un upstream non
autorisé resservi depuis le cache garde sa déclaration ; un fichier de cache plus ancien,
sans la clé, se relit sans erreur. Un `requiresSkill` qui ne désigne aucune skill servie
par son upstream est journalisé au démarrage et relayé quand même (la garde du client
reste ouverte dans ce cas). Pour un upstream relayé, il désigne désormais une skill que le
proxy sert ; il reste « non servi » pour un upstream legacy forcé dont les outils le
portent, ou un upstream en erreur au démarrage.

**Outil de repli d'un upstream relayé : non republié** (`published_tools`). Un outil
d'upstream marqué `_meta["miaou/skillsFallback"]` (le `read_skill` d'un proxy pris comme
upstream) dit « un client qui lit les skills lui-même me masque » : pour ses upstreams, ce
client, c'est le proxy, dont le propre `read_skill` lit les mêmes fichiers sous les URI
qu'il publie — celui de l'amont attendrait les URI de l'amont. Il est retiré de
`tools/list`, du compte d'outils du journal et de celui du bloc (sans quoi la forme « tout
appel d'un outil `<up>__…` » ne tiendrait plus). Un upstream non relayé (legacy) garde le
sien.

**Texte libre d'un upstream relayé : URI préfixées** (`prefix_free_text_skill_uris`).
La spec autorise un serveur à citer l'URI d'une skill dans ses `instructions` ;
republiée telle quelle, elle désignerait une skill du proxy qui n'existe pas. Pour un
upstream dont les skills sont relayées (inprocess compris), `skill://` devient
`skill://<up>/` dans son texte libre : insertion au début de l'URI, sans analyse de
bornes, la ponctuation qui suit reste juste. Cas courant : un proxy pris comme upstream,
dont le bloc généré cite ses propres URI — sans le préfixe, le proxy aval publiait les
URI de l'amont dans son propre espace de noms. Dans une chaîne, le bloc de l'amont,
devenu juste, double alors celui de l'aval : artefact accepté. Jamais pour un upstream
legacy : ce que le proxy publie pour lui ne bouge pas.

**Journal de démarrage.** La ligne d'un upstream qui sert des skills relayées en donne le
nombre (`up           7 tools, 1 skill (2026-07-28)`, inprocess compris), et le compte
d'outils exclut le repli retiré. Une ligne par upstream, avec l'URI et le nombre
d'outils, pour une skill exigée mais non servie (`skill exigée mais non servie par 'x' :
skill://…/SKILL.md (7 outils)`). « skills non relayées (… forcé en ère legacy par la
config…) » reste pour un upstream en `protocol_era: "legacy"` : jamais interrogé en moderne,
il peut servir des skills que le proxy ne relaiera pas. Rien pour un legacy constaté par
la sonde, qui ne peut pas servir l'extension.

**Upstream absent.** Non autorisé (y compris resservi depuis `ToolCatalogCache`) : non
vivant, aucune skill relayée, aucune requête — à la différence d'un upstream en attente
de son secret volatil, dont les skills sont resservies du cache (« Upstream en attente
de son secret », plus bas) ; ses outils restent listés avec leur
`requiresSkill` préfixé, qui désigne alors une skill absente (garde ouverte côté client,
et l'appel est de toute façon refusé). Mort en cours de vie : omis des listages ; les
capacités du proxy restent publiées (la spec admet un `skills/list` vide) et le bloc des
`instructions` reste en place, statique par construction — **il ment donc pendant la
panne**, comme la section de texte libre d'un upstream dont les outils ont disparu : le
rendre dynamique casserait la stabilité du message système. Les lectures d'une skill d'un upstream
connu mais non vivant (`resolve_published_uri`) répondent comme `tools/call` : `-32603`
« Le serveur '<up>' est injoignable : sa session s'est fermée (…). Le proxy ne s'y
reconnecte qu'à son redémarrage. » (le texte de `_unreachable_message`), ou le contrat
AUTHORIZATION_REQUIRED / SECRET_REQUIRED pour un upstream en attente d'autorisation ou de
secret ; `-32602` reste réservé à ce qui n'est pas servi (premier segment inconnu). Le
repli `read_skill` dit « Lecture de <uri> impossible : <cause> ». Mort au
démarrage : retiré de la table, rien publié pour lui.

**Bloc généré dans les instructions.** `build_skills_blocks()` (lifespan) termine la
section de chaque upstream qui sert des skills :

```
Skills MCP servies par `bench` — pas des skills locales : leur nom ne suffit pas, elles se lisent par leur URI complète :
- `bench` (skill://bench/bench/SKILL.md), obligatoire avant tout appel d'un outil `bench__…` : Règle de restitution […]
```

Dégradé pour une entrée distante incomplète : nom pris du dossier de la skill si
`name` manque, ligne sans « : <description> » si elle manque, description coupée à
1 024 caractères (`SKILL_DESCRIPTION_MAX_CHARS`, la borne d'Agent Skills) pour qu'un tiers
n'allonge pas le message système sans limite.

Une ligne par skill, obligatoire ou FACULTATIVE (aucun outil ne l'exige) : sans annonce,
une skill facultative serait inatteignable — le modèle n'a pas `skills/list`, toute
lecture exige l'URI, et nos serveurs ne l'écrivent pas dans leur texte libre
(`docs/servers.md`) ; un tiers peut le faire, son URI y est alors préfixée (ci-dessous). Le statut précède la description (sinon il se
colle à une description sans point final) ; « tout appel d'un outil `<up>__…` » quand
TOUS les outils de l'upstream l'exigent, la liste des noms préfixés sinon. Le bloc ne
nomme aucun outil de lecture : le repli est masqué par les clients qui lisent les skills
eux-mêmes. Calculé une fois au démarrage, pour un message système stable : une
description modifiée sur disque n'y apparaît qu'au redémarrage, alors que `skills/list`
la sert à jour.

L'en-tête « pas des skills locales : leur nom ne suffit pas » est une correction
MESURÉE : un client qui a ses propres skills (MIAOU) apprend au modèle à les lire par leur
nom ; sans cette phrase, un gros modèle (gpt-oss:120b) cherchait `bench` parmi les skills
locales, ne la trouvait pas, et s'interdisait l'outil. « leur nom ne suffit pas » plutôt
que « absentes de la liste locale », qui serait faux chez un hôte listant les skills MCP
avec les siennes.

**Outil de repli `read_skill(uri)`.** Pour les clients qui ne parlent pas l'extension.
Nom NU, comme `status`, publié seulement si l'extension est servie — décidé à l'appel,
par `SKILLS_EXTENSION_ID in server.extensions`, seule source de vérité avec les
capacités. Marqué `_meta["miaou/skillsFallback"] = true` pour qu'un client qui lit les
skills lui-même le masque sans dépendre de son nom. Même résolution et même liste
blanche que `resources/read` ; tout refus est un `isError` lisible par le modèle. Le
texte rendu est précédé de « [Fichier de skill servi par le serveur MCP `<upstream>` —
<uri>] » (un contenu de skill MCP ne doit pas passer pour une consigne locale) ; un
fichier binaire sort en `EmbeddedResource` après l'étiquette ; après un `SKILL.md`, une
note liste les autres fichiers de la skill par URI absolue. Aucune approbation : c'est
l'affaire du client.

## Configuration du proxy (`config.json`)

### Couches (`layers.py`)

La config effective est la fusion d'une chaîne, du général au particulier, chaque
fichier lu s'il existe (au moins un) :

| Fichier | Versionné | Rôle |
|---|---|---|
| `config.defaults.json` | dépôt public | la base, documentée par ses `_comment` ; se lit, ne se copie plus |
| `config.site.json` | fork seulement | valeurs communes d'une organisation (upstreams internes, URL, realms) |
| `config.json` | jamais | le propre d'une installation : clefs, identifiants, overrides |

`config.site.json` est **absent du dépôt public, à dessein** : un fichier qui n'existe
pas en amont ne crée aucun conflit quand le fork merge l'amont — un `config.site.json`
public, même vide, en recréerait un à chaque retouche. Il n'est pas gitignoré non plus,
pour que le fork le committe sans toucher au `.gitignore`.

`--config` (répétable) **remplace** la chaîne : `--config autre.json` seul lit ce seul
fichier, comme avant les couches, et `--config a.json --config b.json` pose `b` sur
`a`. Le dernier fichier de la chaîne (`config.json` pour la chaîne par défaut, même
absent) est le **maillon local** : cible de `--migrate-config`, emplacement de
`state/`. Le démarrage journalise la chaîne lue (`Config : a + b`).

**Fusion : JSON Merge Patch (RFC 7386), sans variante.** Les objets fusionnent
récursivement, `null` supprime la clé, tout le reste remplace — tableaux compris :
`search.order` ou `args` ne se concatènent jamais, un ordre de moteurs n'a pas de sens
mis bout à bout. Le choix d'une RFC plutôt que d'un deep-merge maison : la sémantique
se documente par un lien, et l'opération inverse (le patch minimal, cf. allègement)
est bien définie. La limite de la RFC — pas de valeur `null` posable — est sans effet :
aucune clé ne distingue `null` de l'absence. Pour retirer un serveur de la base,
`"disabled": true` se lit mieux que `null`. `port` est exigé sur le résultat, pas par
couche.

**`miaou_dist` relatif** se résout contre la DERNIÈRE couche qui déclare la clé
(`LoadedConfig.miaou_dist_base`), pas contre le maillon local : sinon un
`--config ailleurs/x.json` qui ne la redéclare pas déplacerait le chemin d'une autre
couche.

**Variables d'environnement.** Dans les valeurs chaînes : `${VAR}`, `${VAR:-défaut}`
(absente ou vide), `${VAR-défaut}` (absente seulement), `$$` pour un `$` littéral ; un
`$` suivi d'autre chose que `{` reste tel quel. Syntaxe de docker compose, pour qu'un
même `.env` serve aux deux (`env_file` facultatif dans `docker-compose.yml`), mais
seulement ces formes : **pas d'imbrication** (`${A:-${B}}` : le défaut s'arrête au
premier `}`, il reste un `}` et un `${B` littéraux), pas de substitution dans un
défaut, pas d'opérateurs `:?`/`:+`/`?`/`+` — une expression non reconnue reste telle
quelle, sans signalement (compose, lui, lèverait pour `${A:?erreur}`). Substitué
**après** la fusion : une valeur littérale de `config.json` qui remplace un
`${ORG_API_SECRET}` de `config.site.json` n'exige rien de l'environnement. Une variable
absente sans défaut devient `""` et est signalée au démarrage avec sa clé, comme le
fait compose — et une clé vide fait déjà écarter un moteur ou refuser un serveur
(« pas d'outil sans config fonctionnelle »). Clés `_comment*` jamais substituées (un
`_comment` cite la syntaxe ; même périmètre que `strip_comments`) — les autres clés à
souligné le sont, `_JAVA_OPTIONS` d'un `env` par exemple ; bloc `disabled` substitué quand même — `--auth`
réveille un bloc `auth` neutralisé — mais ses variables manquantes tues. Les
variables propres à un serveur (`BRAVE_API_KEY`, `OLLAMA_API_KEY`) restent lues par
lui en repli d'une clé vide : rien ne change pour elles.

**`--print-config`** affiche la config effective (fusion + substitution) et quitte.
Sortie faite pour être collée dans un ticket, donc masquée en `***` : valeurs non
vides des clés dont le NOM ressemble à un secret (`secret`, `pass` non suivi d'une
lettre, `password`, `passphrase`, `pwd`, `api_key`/`api-key`, `private_key`, `token`,
`auth`, `credential`, `cookie`, `bearer`, `session`, `jwt`, `signature`, et `key`/`sig`
comme mot final : `access_key`, `X-Key` — casse ignorée, d'où `LOGS_PASS` ou un
en-tête `X-Auth`), sauf suffixe public (`_endpoint`, `_url`, `_uri`, `_method`,
`_scope(s)`, `_timeout` : `token_endpoint` est ce qu'on vient lire dans un
diagnostic OAuth). Sous une telle clé, **tout le sous-arbre** (dict, liste, nombres)
est masqué, sauf pour `auth` en dict/liste (un BLOC de config : `auth` d'un serveur,
`authorization_servers`, dont les enfants sont jugés un à un) et pour un marqueur
`{"volatile": clef}` (il nomme un secret, n'en est pas un). Les noms de serveurs de
`mcpServers` ne sont jamais jugés. Aussi masqués : toutes les valeurs d'un `env`, aux
noms imprévisibles ; le mot de passe d'une URL `scheme://user:mdp@hôte` ; les
paramètres de query de nom secret d'une URL (`?api_key=***`, mêmes noms que les clés).
**Jamais masqués : les `args` d'un upstream stdio** — un secret va dans `env`, pas dans
`args` (`["--password", "hunter2"]` sortirait en clair). C'est une liste noire : elle
réduit l'exposition, elle ne la garantit pas — un nom de clé imprévu passe en clair.

**`--migrate-config`** allège le maillon local et quitte. Contrat : la config
effective est INCHANGÉE — `merge_patch(base, allégé) == merge_patch(base, local)` aux
`_comment*` près, propriété testée sur une copie retouchée du vrai
`config.defaults.json` ET vérifiée à chaque exécution (`_slim_local`, donc aussi pour
`--diff`) : si l'égalité ne tient pas, refus avant toute écriture. Sont retirées les valeurs identiques à la fusion des couches
qui le précèdent, et **tous** les `_comment*`, changés ou non (ils documentent la
base ; une copie locale ne fait que vieillir). Pas de comparaison à l'historique des
samples : une valeur copiée d'un ancien sample et changée depuis en amont reste comme
override, visible au diff. Les clés de la base absentes du fichier sont **listées,
jamais neutralisées d'office** : la fusion les ajoute déjà, migration ou non, et dans
un `config.json` léger l'absence veut dire « hériter » — un `disabled: true` écrit
d'office y couperait un serveur. Pour une config à l'ancienne, c'est la liste à
relire (serveur ôté de la copie qui revient actif). Sauvegarde en `<fichier>.bak`
(mode conservé, le fichier porte des secrets) ; refus si elle existe déjà, une
seconde migration écraserait l'original. La réécriture passe par `<fichier>.tmp` créé
en `O_EXCL` (un `.tmp` périmé, lien symbolique compris, est retiré d'abord), `fsync`
puis `os.replace` ; sur échec, `.tmp` et sauvegarde sont retirés et l'original reste. Jamais de réécriture au démarrage : le
proxy se borne à la proposer tant que le maillon local répète la base
(`needs_slimming`).

**`--migrate-config --diff`** montre l'allègement sans rien écrire (`migration_diff`) :
diff unifié entre le contenu actuel et l'allégé, suivi des mêmes comptes et de la
liste des clés héritées. Les deux côtés sont resérialisés comme la réécriture
(indentation 2) — la mise en forme d'origine, que la réécriture perd aussi, n'y
apparaît donc pas — et masqués comme `--print-config`, les lignes de contexte
portant les secrets restés dans le fichier. Le refus sur `.bak` existante ne
s'applique pas, rien n'étant écrit ; `--diff` seul est refusé.

### Secrets volatils (`{"volatile": "<clef>"}`, `volatile.py`, lot AU)

Dans le bloc `config` d'une entrée `inprocess`, une valeur peut être remplacée
par un **marqueur** : un objet dont la seule clé est `volatile`, et dont la
valeur est la **clef** du secret, une chaîne libre qui sert aussi de libellé
(`"password": {"volatile": "Mot de passe SSO"}`). La valeur n'est alors écrite
nulle part : l'utilisateur la saisit dans MIAOU, le proxy la tient en mémoire et
la substitue au marqueur, dans une copie de la config, juste avant
`build(config)`. Le serveur reçoit une chaîne, comme avant.

- Marqueur résolu à toute profondeur, listes comprises (trois sous-blocs
  d'instances portant la même clef), hors clés à souligné (`_comment`,
  `_example_*`). Un objet qui porte `volatile` À CÔTÉ d'autres clés n'est pas un
  marqueur et passe tel quel.
- **Une clef par upstream** en v1 : plusieurs clefs différentes, ou un marqueur
  sans clef utilisable (`{"volatile": ""}`), écartent l'upstream au démarrage
  avec ce motif ; les autres démarrent.
- **Ailleurs, c'est une erreur de config** (sortie 1, chemin de la clé dans le
  message) : dans `env`, dans une entrée `stdio` ou `http`, hors de
  `mcpServers`. Le marqueur y arriverait tel quel, un objet à la place d'une
  chaîne, et casserait loin de sa cause. Seul le `config` d'un `inprocess` est
  résolu.
- `--print-config` affiche le marqueur tel quel : c'est un nom, pas une valeur.
- **Avertissement de migration.** Un marqueur posé par une couche (typiquement
  `config.site.json` d'un fork) et remplacé, dans la config effective, par une
  chaîne d'une couche suivante (le mot de passe resté dans `config.json`) est
  signalé au démarrage, une ligne `WARNING:` par chemin (jaune sur un terminal,
  comme les avertissements d'uvicorn ; `logging._warn`). La valeur en clair
  continue de servir : rien n'est refusé.

**Upstream en attente de son secret.** Au démarrage, un upstream porteur d'une
clef est importé mais **pas construit** (`InProcessUpstream.prepare`) :

- le module doit exposer `build(config)` (seul à recevoir la config), sinon
  l'upstream est écarté ;
- son crochet facultatif `check_config(config)` est appelé, **marqueurs en
  place** (une copie) : il lève (`ConfigIncomplete` de `mcp_base`, ou toute
  exception) sur une config incomplète, sans rien contacter, et l'upstream est
  écarté comme aujourd'hui — il n'est donc pas consommateur du secret. Un
  module sans crochet est réputé complet ;
- sinon l'upstream reste dans la table, non construit (`secret_pending`),
  journalisé `pending — volatile secret « <clef> » to enter in MIAOU (N tools
  from cache)`.

Tant qu'il attend : `upstream_is_live` est faux ; ses outils sont resservis
depuis `state/tools-cache.json` **sans notice**, à l'octet près comme le live,
pour que la liste d'outils (et le cache KV du modèle) ne change pas à la
saisie — d'où l'écriture du cache sans tri des clés, qui réordonnait les
propriétés des schémas ; ses `instructions` (texte libre) aussi, mémorisées
dans le même fichier quand il a été construit ; et ses skills, sous la clé
`skills` de son entrée (`remember_skills`/`recall_skills`) : entrées d'ORIGINE,
avant préfixage, que `_collect` liste à sa place dans l'ordre de la config
(`_listed_names`) et que `prefix_skill_entry` préfixe sur le même chemin que
les vivantes — `skills/list` et `resources/list` à l'octet près (MIAOU lie
l'approbation au manifeste), une skill `"dynamic"` telle quelle, et le bloc des
`instructions` généré depuis ces entrées et les outils en cache, identique au
bloc vivant. Elles comptent pour publier l'extension (`install_skills`) : sans
elles, un proxy dont il est le seul upstream à skills ne la publierait pas avant
la saisie. Le cache n'écrit de skills que pour un upstream à `volatile_key`
(`_remembers_skills`), à chaque listage vivant (le contenu est relu, les
empreintes peuvent changer) et à l'acceptation ; `remember` préserve la clé.
Les lectures restent refusées en SECRET_REQUIRED (`resolve_published_uri`), d'un
message qui nomme la skill (`UpstreamSecretRequired(name, skill_uri)`). Un
upstream OAuth non autorisé n'est pas concerné : ses skills restent absentes ;
un appel est refusé en erreur JSON-RPC `data.code =
SECRET_REQUIRED` (`UpstreamSecretRequired`), avec un message qui dit au modèle
que seul l'utilisateur peut agir, depuis MIAOU. `data` ne porte que
l'upstream : les noms de clefs ne se lisent que par l'extension, gardée par
l'origine. Au tout premier démarrage d'une installation, le cache est vide :
aucun outil, jusqu'à la première saisie acceptée. Le rapport `status` (outil du
modèle, publié avec l'auth sortante) dit l'attente.

**Saisie, validation, oubli** : extension `miaou/volatile-secrets`
(`volatile_store.py::VolatileSecrets`, contrat dans `docs/miaou-contract.md`). Créée par
`main()` dès qu'un upstream porte une clef ; au lifespan, après le démarrage, elle
recense les consommateurs (`attach`) et ne s'installe que s'il en reste. `build` tourne
dans `asyncio.to_thread` : un serveur qui s'authentifie auprès d'un annuaire gèlerait
sinon le proxy entier, et rien ne l'interrompt (pas de délai propre, AU-f) — un
annuaire muet retient la requête jusqu'au délai du client. L'échec se classe par nom de
classe dans le MRO (`CredentialsRejected`, `ConfigIncomplete`, le reste indéterminé),
pour qu'une base importée sous un autre nom de module (`servers.mcp_base`) soit
reconnue ; son texte (gardé : c'est l'indice de diagnostic) est masqué
(`volatile.mask`) avant d'être journalisé ou rendu, sous toutes les formes de
`volatile.forms` : brute, `repr`, JSON ASCII et JSON brut (un `build` qui cite sa config
l'écrit échappée), les plus longues d'abord. Une instance acceptée laisse ses outils et
ses `instructions` dans le cache, sauf si elle y cite la valeur sous l'une de ces formes
(`volatile.cites`, `cache_forbidden`, ligne `WARNING:`) : le contrôle compare au JSON
sérialisé des outils et des entrées de skills (un `build` peut tirer ses skills de sa
config), d'où les formes échappées. `cache_forbidden` est posé AVANT le
contrôle et levé seulement s'il passe : si `_remember` ne peut pas le faire (`list_tools`
en échec), le listage de `tools/list` (`server.py`) n'écrit pas le catalogue de cette
instance (il l'écrivait sans contrôle avant le 2026-10-17). Une instance qui passe le
contrôle (nouvelle saisie) lève le drapeau. La validation d'un `submit` tourne dans une
tâche détachée de la requête (`_Key.task`, qui tient seule le verrou par clef) : un
`submit` annulé (onglet fermé) laisse le `build` finir et publier, et un `submit` de la
même valeur pendant ce temps attend cette tâche au lieu de lancer un second `build`
concurrent (risque de verrouillage de compte) ; une autre valeur se met en file sur le
verrou. Les empreintes des valeurs refusées sont bornées (`MAX_REJECTED` = 64 par clef,
les plus récentes : au-delà, la plus ancienne peut être retestée). Le journal dit chaque
verdict, jamais la valeur : `Secret volatil « <clef> » : <upstream> accepted.`

### `state/` (`state.py`)

Ce que le proxy écrit lui-même : `state/tokens.json` (jetons OAuth sortants — un
ÉTAT, le perdre redemande chaque autorisation) et `state/tools-cache.json` (cache
d'outils, reconstructible), plus `state/self-update.json` (HEAD d'avant, cible et
issue de la dernière mise à jour, lot AT). Nommés pour ne pas se confondre, et hors du motif
`config*.json`. À côté du maillon local ; une config d'un autre nom range son état
sous `state/<nom>/`, deux instances du même dossier ne partageant pas leurs jetons.
Les anciens `<config>-tokens.json` / `<config>-tools.json` sont **déplacés** au
démarrage (`os.replace`, contenu et mode 0600 intacts), jamais écrasant un état déjà
présent dans `state/` — l'ancien est alors laissé et signalé. `--tokens-file` explicite
court-circuite tout ça : rien n'est déplacé, le cache d'outils reste à côté de lui sous
son ancien nom. Sous Docker, `docker-compose.yml` monte le volume `miaou-state` sur
`/app/state` : sans lui, une recréation du conteneur jetait les jetons.

### Format

```json
{
  "port": 8765,
  "host": "127.0.0.1",
  "mcpServers": {
    "bench": { "type": "inprocess", "module": "mcp_bench" },
    "web": {
      "type": "inprocess",
      "module": "mcp_web",
      "config": { "search": { "order": ["brave", "ollama", "ddg"], "brave": { "api_key": "your-key-here" } } }
    },
    "docs": { "type": "inprocess", "module": "mcp_docs" },
    "_example_http": {
      "disabled": true,
      "type": "http",
      "url": "http://127.0.0.1:8798/mcp"
    },
    "_example_stdio": {
      "disabled": true,
      "command": "uv",
      "args": ["run", "servers/mcp_bench.py", "--transport", "stdio"]
    }
  }
}
```

`type` absent → `stdio` (défaut). `port` est obligatoire, `host` est optionnel.
Une entrée `http` exige `url` ; `headers` et `timeout` y sont optionnels.
`protocol_era` sur une entrée stdio ou http : `"auto"` (défaut, l'ère est négociée) ou
`"legacy"` (`initialize` d'emblée, sans sonde) — cf. « Ère des upstreams stdio et http ».
Toute autre valeur, ou la clé sur une entrée inprocess, est une erreur de config signalée
au démarrage. `call_timeout` (stdio et http) : cf. « Un appel d'outil est borné ». Le nom
d'une entrée `mcpServers` active ne contient que lettres, chiffres, `-` et `_`, sans `__`
ni `_` final (il préfixe les outils, coupés au premier `__`, et les URI de skills, coupées
au premier `/`) : sinon erreur de config au démarrage. Un bloc
`auth` sur une entrée `http` active l'auth **sortante** (le proxy devient client
OAuth de ce serveur) ; il n'a de sens que là, et l'exiger ailleurs est une erreur
de config signalée au démarrage.
`"disabled": true` sur une entrée `mcpServers` → upstream ignoré au démarrage. La valeur
est un **booléen** (`null` = absent) : une chaîne, même `"false"` ou `"0"` (un
`${DIS:-false}` en donne une), est une erreur de config — `bool("false")` est vrai, et le
bloc serait neutralisé en silence. Vaut pour `disabled`, `_disabled`, et le bloc `auth`.
L'ancienne orthographe `_disabled` reste lue si `disabled` est absente ; elle n'est plus celle qu'on écrit — dans ce fichier, un souligné en tête signale ailleurs (`_comment`) une clé ignorée, ce que cet interrupteur n'est justement pas. Le proxy le signale au démarrage, une ligne par bloc concerné, pour que la migration se voie plutôt que de traîner indéfiniment ; une config déjà en `disabled` ne dit rien.
`env` sur une entrée inprocess → variables posées via `os.environ.setdefault` avant l'import.
`instructions` (chaîne, tout type d'entrée) → consigne ajoutée par l'opérateur à la
section de cet upstream dans les `instructions` du proxy, après celle que l'upstream
publie — cf. « Consigne de portée serveur ». Une valeur qui n'est pas une chaîne est
une erreur de config signalée au démarrage.
`miaou_dist` (clé racine, chaîne) → dossier `dist/` de MIAOU servi sous `/app/` — cf.
« MIAOU servi sous `/app/` ». Absente, `null` ou `""` : rien n'est servi.

### Multi-instance inprocess (clé `config`)

`importlib.import_module` ne charge un module qu'une fois par process : `env`
(via `os.environ.setdefault`) est donc figé à la première instanciation et ne
permet pas plusieurs entrées `mcpServers` du même module avec des valeurs
différentes (ex. 3 environnements d'une même API : prod/uat/dev). Pour ce cas,
une entrée `mcpServers` accepte une clé `"config"` (dict libre, propre au
serveur) en plus de `"env"` :

```json
"api_prod": { "type": "inprocess", "module": "mcp_example_api", "config": { "base_url": "https://prod...", "client_id": "..." } },
"api_uat":  { "type": "inprocess", "module": "mcp_example_api", "config": { "base_url": "https://uat...",  "client_id": "..." } }
```

Un module qui veut supporter ça expose une factory `build(config: dict | None)
-> MCPServer` en plus du singleton `mcp` — `InProcessUpstream.start()` (dans
`mcp_proxy/upstream.py`) appelle `module.build(config)` si elle existe, sinon retombe
sur `module.mcp` (comportement actuel, inchangé pour tous les serveurs qui
n'ont pas de `build()`) :

```python
def build(config: dict | None = None) -> MCPServer:
    return MyServer(config or {}).mcp

server = MyServer()
mcp = server.mcp  # toujours exposé, pour compat avec le chemin sans build()
```

`self.config` (posé par `MiaouMCPBase.__init__`) transporte ce dict — chaque
serveur lit ce qu'il veut dedans, aucune validation de schéma imposée par la
base. `env` reste le mécanisme pour stdio ou pour un serveur inprocess qui
préfère réellement lire `os.environ` (un seul jeu de valeurs par process).

## MIAOU servi sous `/app/` (clé `miaou_dist`)

Une clé racine de `config.json` désigne le **dossier** `dist/` d'un clone de MIAOU.
Le proxy le sert alors, à côté de `/mcp`, pour que MIAOU puisse être ouvert en
`http://127.0.0.1:<port>/app/` plutôt qu'en `file://` — condition pour l'installer
comme application (une PWA ne s'installe pas depuis un fichier). Code : `webapp.py`.

```json
{ "port": 8765, "miaou_dist": "../miaou/dist", "mcpServers": { … } }
```

**Résolution.** Chemin relatif au fichier de config qui déclare la clé (cf. « Couches »),
pas au répertoire courant (qui dépend de la façon de lancer) ; `~` est développé ; absolu accepté. `null` ou `""`
valent absence. Une valeur qui n'est pas une chaîne est une erreur de config.

**Routes.** `/app/` rend `miaou.html` — qui ne s'appelle pas `index.html`, d'où une
sous-classe de `StaticFiles` (`MiaouStaticFiles`) plutôt que son mode `html`, qui ne
connaît qu'`index.html`. Le reste du dossier est servi sous `/app/` : manifeste,
icônes, service worker (portée `/app/`) et `version.json` que MIAOU y dépose. `/app`
sans slash est redirigé par le routeur de Starlette ; `/` redirige vers `/app/` en
**302** (un 301 resterait dans le cache du navigateur après le retrait de la clé). Ces
routes sont posées APRÈS `/mcp` et les routes OAuth, qui gardent la priorité, et HORS
de `RequireAuthMiddleware` : `/app/` reste public quand l'auth entrante est active —
l'appli doit pouvoir charger pour lancer son propre parcours. Lecture seule (405 sur
tout autre verbe que GET/HEAD). Clé absente : aucune de ces routes, `/` reste un 404.

**Revalidation.** `StaticFiles` pose `ETag` (mtime + taille) et `Last-Modified`, et
répond 304 sur `If-None-Match`/`If-Modified-Since`, mais ne pose AUCUN
`Cache-Control`. Le navigateur applique alors une fraîcheur heuristique (~10 % de l'âge
depuis `Last-Modified`) et peut resservir un vieux HTML sans revalider : un `git pull`
de MIAOU ne serait pas vu au rechargement. D'où `Cache-Control: no-cache` sur toute
réponse FICHIER de `/app/` — 200, 206 et 304 (c'est `file_response` qui le pose, avant le
test conditionnel, pour que `NotModifiedResponse` le recopie). Les autres réponses n'en
portent pas : 404 (fichier absent ou caché), 405, 416, et les redirections de `/` et
`/app` ; sans validateur ni `Last-Modified`, un navigateur ne leur applique aucune
fraîcheur heuristique, et le service worker de MIAOU ne garde que du `text/html` en 2xx. Pas seulement sur le HTML :
le service worker et `version.json` en ont besoin aussi, et une icône revalidée ne
coûte qu'un 304.

**Types de contenu.** Table explicite (`_MEDIA_TYPES` : `.html`, `.js`, `.json`,
`.webmanifest`, `.png`, `.svg`, `.ico`), repli sur `mimetypes` pour le reste. Le
module ne suffit pas : sous Windows il lit le registre, dont les entrées remplacent
ses valeurs par défaut (`.js` en `text/plain` sur certains postes), sous Linux
`/etc/mime.types` s'il existe — et un navigateur refuse d'enregistrer un service
worker servi sous un type qui n'est pas JavaScript.

**Ce qui n'est jamais servi.** Rien hors du dossier (garde `realpath`/`commonpath` de
`StaticFiles`, un `..` résiduel étant de toute façon refusé avec les fichiers cachés),
aucun fichier caché (`.gitkeep`, `.DS_Store`). Le filtre juge le chemin demandé, tout
chemin contenant `:` est refusé (flux NTFS `miaou.html::$DATA` sous Windows), et le chemin
RÉSOLU (`os.path.realpath`, relatif à la racine, qui rend le nom long sous Windows) :
le nom court 8.3 d'un fichier caché (`GITKEE~1`) est donc un 404 aussi. Le test simule la
résolution, faute de Windows en CI.

**Démarrage : un refus, des avertissements.**
- **Refus** (`ValueError`, sortie en erreur) si le dossier contient `config.json` ou
  `.git` (`_FORBIDDEN_ENTRIES`) : c'est la racine du dépôt MIAOU, pas son `dist/`, et la
  servir exposerait sa config au réseau dès que `host` vaut `0.0.0.0`.
- **Avertissements** (`miaou_dist_warnings`, au journal), le proxy démarrant quand
  même — une indisponibilité de MIAOU n'éteint pas les outils MCP : dossier
  introuvable ou `miaou.html` absent (`/app/` répond 404) ; `manifest.webmanifest`
  absent, illisible, sans icône, ou icône citée introuvable (MIAOU servi, mais pas
  installable). Les noms d'icônes sont lus dans `icons[].src` du manifeste, résolus
  contre son URL comme le ferait le navigateur : seul le nom du manifeste est figé
  dans le proxy. Une icône d'une autre origine n'est pas vérifiée.
- Les routes relisent le disque à chaque requête : un dossier rempli après le
  démarrage est servi sans redémarrage. `check_config` de `StaticFiles` est
  neutralisé, sans quoi un dossier absent lèverait à la première requête (500).

**Docker.** Le dossier `miaou_dist/` du dépôt est versionné vide (`.gitkeep`) et le
`Dockerfile` le copie toujours. Son contenu n'est PAS gitignoré : un fork peut y
versionner une copie embarquée de MIAOU sans toucher au `.gitignore` (ce qui lui
coûterait un conflit à chaque merge de l'amont) ; ici, une copie posée pour un build
apparaît donc en non suivi, à ne pas committer : copier le `dist/` de MIAOU
dedans AVANT le build, et poser `"miaou_dist": "miaou_dist"` dans le `config.json`
copié dans l'image (le `Dockerfile` copie la chaîne : `config.defaults.json`, puis
`config.site.json` et `config.json` s'ils existent). Sans copie préalable, l'image se
construit quand même ; le proxy
avertit alors au démarrage si la clé est posée, et ne sert rien si elle ne l'est pas.

**Pas de compression.** `miaou.html` pèse ~1,5 Mo et part tel quel : sans enjeu en
local ; derrière un reverse proxy, c'est à lui de compresser (Caddy : directive
`encode`).

