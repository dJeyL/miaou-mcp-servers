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
sans OAuth) et `timeout` (optionnel, défaut `_HTTP_HANDSHAKE_TIMEOUT_S` = 30 s). Le
handshake `initialize` est **borné** : un serveur distant qui accepte la connexion puis
ne répond jamais bloquerait sinon le démarrage du proxy entier. Cette borne est une
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
  client httpx construit par le SDK (`create_mcp_http_client`) garde le défaut
  `trust_env=True`, donc il relit `os.environ` du process, déjà modifié par `main()`.
  C'est une propriété d'une **bibliothèque tierce**, pas de notre code : un test
  (`test_mcp_sdk_http_client_still_trusts_env`) l'épingle, faute de quoi un SDK qui
  passerait un jour `trust_env=False` rendrait `--noproxy` silencieusement inopérant sur
  ce seul type d'upstream.

  **Limite connue** : httpx lit aussi `ALL_PROXY` et `NO_PROXY`, que `_PROXY_ENV_KEYS`
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
├── upstream.py    les trois types, même surface `Upstream` :
│   ├── InProcessUpstream  : importlib.import_module(module) → module.mcp._tool_manager
│   ├── StdioUpstream      : stdio_client + ClientSession (subprocess MCP)
│   └── HttpUpstream       : streamablehttp_client + ClientSession (serveur distant)
├── netproxy.py    override --proxy / --noproxy (calcul pur, n'applique rien)
├── config.py      lit config.json → build_upstreams()
├── server.py      build_proxy_server() : mcp.server.Server, list_tools / call_tool
│   ├── list_tools → agrège tous les upstreams, préfixe les noms avec "{name}__"
│   ├── call_tool  → dépréfixe, route vers l'upstream concerné
│   └── aggregate_instructions() : compose `instructions` de l'InitializeResult
├── auth_in.py     auth entrante — Resource Server (docs/auth.md)
├── auth_out/      auth sortante — client OAuth de tiers (docs/auth.md), lui-même
│                  découpé : debug (traces) → probe (sonde) → storage (jetons)
│                  → authorizer (parcours, renouvellement, routes)
├── app.py         build_app() : Starlette + StreamableHTTPSessionManager + CORS
│   ├── lifespan : start/stop de chaque upstream, puis écriture des instructions
│   └── auth (facultative) : routes RFC 9728 + RequireAuthMiddleware sur /mcp
└── entry.py       main() — CLI ; et run_with_dev_auth() : --with-dev-auth, proxy
                   ET AS de développement dans ce process, sur DEUX ports
```

`build_app()` ne retourne pas directement le `Starlette` mais une fonction ASGI qui l'enveloppe :
`Mount("/mcp", ...)` redirige `/mcp` → `/mcp/` en 307 par défaut (strict-slash Starlette), et
certains clients MCP ne suivent pas les redirections sur POST/DELETE. Le wrapper réécrit
`scope["path"]` de `/mcp` vers `/mcp/` avant le routeur pour servir la requête directement,
sans redirection.


## Consigne de portée serveur (`instructions`)

Une consigne qui vaut pour un serveur ENTIER (« lire telle documentation avant
d'appeler ces outils ») n'a pas d'emplacement au niveau outil : les seuls champs
qu'un client relaie au modèle par outil sont `name`, `description` et
`inputSchema`. La recopier dans chaque docstring donne N copies d'un texte qui ne
discrimine aucun outil — ce qui reste dans une docstring d'outil doit être ce qui
distingue CET outil.

Le protocole prévoit `instructions` sur l'`InitializeResult`, destiné au system
prompt du modèle. Côté serveur, `MiaouMCPBase(..., instructions=...)` le passe à
`FastMCP`. Côté proxy, trois captures, une par type d'upstream :

| Upstream | Capture |
|---|---|
| `InProcessUpstream` | `fastmcp.instructions` — pas d'`initialize` sur ce chemin |
| `StdioUpstream` | retour d'`await session.initialize()` |
| `HttpUpstream` | retour d'`await session.initialize()` |

`aggregate_instructions()` compose le champ unique du proxy à partir des N
upstreams : un préambule, puis une section `## <nom>` par upstream qui en
déclare. **Le titre de section est le préfixe d'outil** (`bench` pour
`bench__echo`) — c'est ce qui rend la portée d'une consigne déductible par le
modèle sans convention supplémentaire à lui faire connaître. Un upstream sans
instructions n'a pas de section ; si aucun n'en a, le champ vaut `None` et
l'`InitializeResult` est celui d'avant le lot, à l'octet près.

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
  une capability, et `initialize` ne se rejoue pas après une autorisation
  obtenue en cours de route : une section omise manquerait définitivement, alors
  qu'une section décrivant un outil temporairement absent ne coûte qu'un
  paragraphe. Observé au passage sur un upstream Jira derrière WSO2 : `tools/list`
  y répond avant autorisation, seul `tools/call` refuse.

Le champ n'atteint le modèle que si le CLIENT le lit et l'injecte dans son system
prompt : le proxy le publie correctement, mais un client qui ignore
l'`InitializeResult` n'en transmet rien. MIAOU le fait (cf.
`docs/miaou-contract.md`) ; un client tiers, pas forcément.


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
Une entrée `http` exige `url` ; `headers` et `timeout` y sont optionnels. Un bloc
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
-> FastMCP` en plus du singleton `mcp` — `InProcessUpstream.start()` (dans
`mcp_proxy/upstream.py`) appelle `module.build(config)` si elle existe, sinon retombe
sur `module.mcp` (comportement actuel, inchangé pour tous les serveurs qui
n'ont pas de `build()`) :

```python
def build(config: dict | None = None) -> FastMCP:
    return MyServer(config or {}).mcp

server = MyServer()
mcp = server.mcp  # toujours exposé, pour compat avec le chemin sans build()
```

`self.config` (posé par `MiaouMCPBase.__init__`) transporte ce dict — chaque
serveur lit ce qu'il veut dedans, aucune validation de schéma imposée par la
base. `env` reste le mécanisme pour stdio ou pour un serveur inprocess qui
préfère réellement lire `os.environ` (un seul jeu de valeurs par process).

