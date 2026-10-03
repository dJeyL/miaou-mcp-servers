# `mcp_proxy/` — proxy MCP

Agrégation des upstreams, configuration, override du proxy réseau. L'auth OAuth
(entrante et sortante) est dans `docs/auth.md`.


Agrège plusieurs serveurs MCP upstream et expose leurs outils préfixés :
`bench__echo`, `bench__get_image`, `weather__get_weather`, etc.

Trois types d'upstream supportés :
- **inprocess** : import Python direct, pas de subprocess (défaut pour tous les serveurs)
- **stdio** : subprocess externe communiquant via stdin/stdout
- **http** : serveur MCP distant en streamable-http (lot AB-2.1)

Les entrées inprocess acceptent un champ `env` pour injecter des variables d'environnement
avant l'import du module (`os.environ.setdefault`). `mcp_brave` lit sa clef en priorité
dans le bloc `config` de son entrée (cf. `docs/servers.md`), et retombe sur `BRAVE_API_KEY`
dans l'environnement.

Une entrée `http` prend `url` (obligatoire, l'endpoint `/mcp` du serveur distant),
`headers` (optionnel, en-têtes statiques — un serveur tiers peut exiger une clef d'API
sans OAuth) et `timeout` (optionnel, défaut `_HTTP_HANDSHAKE_TIMEOUT_S` = 30 s). La
négociation MCP (sonde `server/discover`, repli éventuel sur `initialize`, cf. « Ère des
upstreams stdio et http ») est **bornée** : un serveur distant qui accepte la connexion
puis ne répond jamais bloquerait sinon le démarrage du proxy entier. Cette borne est une
constante distincte de celle des subprocess stdio — les deux mesurent des choses
différentes, et partager la constante ferait bouger l'une en croyant ne toucher qu'à
l'autre.

### Override du proxy réseau vu par les upstreams (`--proxy` / `--noproxy`)

Deux options CLI, mutuellement exclusives, pour contrôler `http_proxy`/`https_proxy`
(et variantes `HTTP_PROXY`/`HTTPS_PROXY`) vus par les serveurs upstream servis :

- `--proxy [http://]host:port` : force les 4 variantes de casse à cette valeur
  (`http://` ajouté si le schéma est absent).
- `--noproxy` : force l'absence de proxy (les 4 variantes supprimées/non transmises).

Absolus : ils priment sur toute variable déjà présente dans l'environnement du process
proxy **et** sur un `env` explicite d'une entrée `config.json` — pas seulement sur un
héritage implicite. Sans l'un ou l'autre, comportement inchangé.

Trois chemins d'application distincts selon le type d'upstream :

- **inprocess** : partage le process du proxy, donc `main()` pose/efface directement les
  4 clés dans `os.environ` du process (`apply_proxy_env_overrides_to_process`) avant
  `build_upstreams` — `make_opener()` (`servers/mcp_base.py`) relit `os.environ` à chaque
  requête via `ProxyHandler()`, aucun changement requis dans `InProcessUpstream`.
- **stdio** : le SDK MCP (`mcp.client.stdio.get_default_environment`) n'hérite qu'une
  whitelist restreinte (`HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`) — les variables
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

  **Limite connue** : httpx2 lit aussi `ALL_PROXY` et `NO_PROXY`, que `_PROXY_ENV_KEYS`
  (les quatre variantes de casse de `http_proxy`/`https_proxy`) ne gère pas. Un
  environnement portant `ALL_PROXY` verrait donc `--noproxy` partiellement inopérant
  pour un upstream http. Non corrigé délibérément : étendre `_PROXY_ENV_KEYS` changerait
  aussi le `env` transmis aux subprocess stdio existants, ce qui n'est pas gratuit.


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
├── config.py      lit config.json → build_upstreams()
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
│   ├── lifespan : start/stop de chaque upstream, puis install_skills() et
│   │              écriture des instructions (bloc des skills compris)
│   └── auth (facultative) : routes RFC 9728 + RequireAuthMiddleware sur /mcp
└── entry.py       main() — CLI ; et run_with_dev_auth() : --with-dev-auth, proxy
                   ET AS de développement dans ce process, sur DEUX ports
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
l'`Origin: null` de MIAOU en `file://` exige.

`build_app()` ne retourne pas directement le `Starlette` mais une fonction ASGI qui l'enveloppe :
`Mount("/mcp", ...)` redirige `/mcp` → `/mcp/` en 307 par défaut (strict-slash Starlette), et
certains clients MCP ne suivent pas les redirections sur POST/DELETE. Le wrapper réécrit
`scope["path"]` de `/mcp` vers `/mcp/` avant le routeur pour servir la requête directement,
sans redirection.


## Ère des upstreams stdio et http

Le proxy parle à un upstream stdio ou http la révision 2026-07-28 quand celui-ci la
parle, `initialize` sinon. Rien n'est écrit ici : `StdioUpstream.start()` et
`HttpUpstream._serve()` ouvrent un `Client(<transport>, mode=…, cache=None)` du SDK,
qui sonde et se replie lui-même (`mcp/client/_probe.py`, `negotiate_auto`), puis lisent
sur `client.session` ce que les deux ères posent pareil : `instructions`,
`protocol_version`, `server_capabilities` (recopiés sur `Upstream.instructions`,
`protocol_version`, `capabilities`). `mode` vient de la clé `protocol` de l'entrée :
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
une section pour ce bloc seul. Un upstream sans instructions ni skills n'a pas
de section ; si aucun n'en a, le champ vaut `None` et l'`InitializeResult` est
celui d'avant le lot, à l'octet près.

Trois points qui ne se devinent pas :

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
  `instructions` (et sa surface de skills) : la section d'un upstream non autorisé au
  démarrage, qui n'avait jamais été interrogé, apparaît à la connexion suivante
  (`docs/auth.md`). Observé au passage sur un upstream Jira derrière WSO2 : `tools/list`
  y répond avant autorisation, seul `tools/call` refuse.

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
transmise, il répond `-32602` pour ce qu'il ne sert pas. Une liste blanche tirée de son
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
`protocol: "legacy"`, ne reçoit ainsi aucune requête de plus, et ce que le proxy publie
pour lui ne change pas. L'inprocess, sans fil, sert toujours (`serves_skills` vrai, ses
skills lues sur le `MCPServer`).

Ce qui part : `skills/list` (en suivant `nextCursor`, au plus `_SKILLS_MAX_PAGES` pages,
arrêt sur un curseur déjà vu) et `skills/get` par `session.send_request` — le SDK client
n'a pas de verbe pour eux —, résultat lu en `dict` brut, sans modèle : le proxy relaie,
le client vérifie (empreintes comprises). `skills/get` rend l'entrée déballée de `skill`,
comme l'inprocess. `resources/read` par `session.read_resource`, qui pose `Mcp-Name` en
ère moderne ; le SDK y valide le résultat contre la révision 2026-07-28 (`cacheScope` et
`resultType` requis), ce qu'un upstream d'un autre SDK peut ne pas tenir — erreur rendue
en `INTERNAL_ERROR`, seul recours `protocol: "legacy"`, qui coupe aussi ses skills.
`skills/*` n'est pas validé par le SDK (méthode hors de son tableau). Le tampon moderne
(`_meta`, en-têtes d'ère) est posé par le SDK, rien à écrire ici. Chaque requête est
bornée par le délai de l'upstream (`timeout` http, 15 s stdio) : un upstream muet ne
retient pas `skills/list` du proxy jusqu'au délai de lecture du transport (300 s).

**`HttpUpstream` : une seule garde.** `call_tool` et les trois méthodes de skills passent
par `_on_session`, qui fait courir la requête contre la mort de la tâche de service et
déballe l'`ExceptionGroup` de son task group (`docs/auth.md`). Sans le déballage, une
`MCPError` de l'upstream sur `skills/get` sortait du handler en erreur JSON-RPC de code 0.
`StdioUpstream`, sans tâche de service, appelle la session en direct.

**Erreurs relayées.** Un `-32602` de l'upstream ressort avec l'URI du client ; toute autre
`MCPError` (un `-32603` d'une skill devenue illisible) garde code et message. Une panne
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
config…) » reste pour un upstream en `protocol: "legacy"` : jamais interrogé en moderne,
il peut servir des skills que le proxy ne relaiera pas. Rien pour un legacy constaté par
la sonde, qui ne peut pas servir l'extension.

**Upstream absent.** Non autorisé (y compris resservi depuis `ToolCatalogCache`) : non
vivant, aucune skill relayée, aucune requête ; ses outils restent listés avec leur
`requiresSkill` préfixé, qui désigne alors une skill absente (garde ouverte côté client,
et l'appel est de toute façon refusé). Mort en cours de vie : omis des listages ; les
capacités du proxy restent publiées (la spec admet un `skills/list` vide) et le bloc des
`instructions` reste en place, statique par construction — **il ment donc pendant la
panne**, comme la section de texte libre d'un upstream dont les outils ont disparu : le
rendre dynamique casserait la stabilité du message système. Les lectures échouent en
`-32602` (résolution refusée, upstream non vivant) ou avec l'erreur du transport. Mort au
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

```json
{
  "port": 8765,
  "host": "127.0.0.1",
  "mcpServers": {
    "bench": { "type": "inprocess", "module": "mcp_bench" },
    "web":   { "type": "inprocess", "module": "mcp_web" },
    "duckduckgo": { "type": "inprocess", "module": "mcp_ddg" },
    "brave": {
      "type": "inprocess",
      "module": "mcp_brave",
      "config": { "api_key": "your-key-here" }
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
`protocol` sur une entrée stdio ou http : `"auto"` (défaut, l'ère est négociée) ou
`"legacy"` (`initialize` d'emblée, sans sonde) — cf. « Ère des upstreams stdio et http ».
Toute autre valeur, ou la clé sur une entrée inprocess, est une erreur de config signalée
au démarrage. Un bloc
`auth` sur une entrée `http` active l'auth **sortante** (le proxy devient client
OAuth de ce serveur) ; il n'a de sens que là, et l'exiger ailleurs est une erreur
de config signalée au démarrage.
`"disabled": true` sur une entrée `mcpServers` → upstream ignoré au démarrage.
L'ancienne orthographe `_disabled` reste lue si `disabled` est absente ; elle n'est plus celle qu'on écrit — dans ce fichier, un souligné en tête signale ailleurs (`_comment`) une clé ignorée, ce que cet interrupteur n'est justement pas. Le proxy le signale au démarrage, une ligne par bloc concerné, pour que la migration se voie plutôt que de traîner indéfiniment ; une config déjà en `disabled` ne dit rien.
`env` sur une entrée inprocess → variables posées via `os.environ.setdefault` avant l'import.

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

