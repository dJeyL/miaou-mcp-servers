# `mcp_proxy.py` — proxy MCP

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


## Architecture de `mcp_proxy.py`

```
mcp_proxy.py (racine)
├── ajoute servers/ à sys.path au démarrage
├── lit config.json → build_upstreams()
│   ├── InProcessUpstream  : importlib.import_module(module) → module.mcp._tool_manager
│   ├── StdioUpstream      : stdio_client + ClientSession (subprocess MCP)
│   └── HttpUpstream       : streamablehttp_client + ClientSession (serveur distant)
├── build_proxy_server()   : mcp.server.Server avec list_tools / call_tool dynamiques
│   ├── list_tools → agrège tous les upstreams, préfixe les noms avec "{name}__"
│   └── call_tool  → dépréfixe, route vers l'upstream concerné
├── build_app()            : Starlette + StreamableHTTPSessionManager + CORSMiddleware
│   ├── lifespan : start/stop de chaque upstream
│   └── auth (facultative) : routes RFC 9728 + RequireAuthMiddleware sur /mcp
└── run_with_dev_auth()    : --with-dev-auth — proxy ET AS de développement dans
                             ce process, sur DEUX ports (deux origines)
```

`build_app()` ne retourne pas directement le `Starlette` mais une fonction ASGI qui l'enveloppe :
`Mount("/mcp", ...)` redirige `/mcp` → `/mcp/` en 307 par défaut (strict-slash Starlette), et
certains clients MCP ne suivent pas les redirections sur POST/DELETE. Le wrapper réécrit
`scope["path"]` de `/mcp` vers `/mcp/` avant le routeur pour servir la requête directement,
sans redirection.


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
      "_disabled": true,
      "type": "http",
      "url": "http://127.0.0.1:8798/mcp"
    },
    "_example_stdio": {
      "_disabled": true,
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
`"_disabled": true` sur une entrée `mcpServers` → upstream ignoré au démarrage.
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
`mcp_proxy.py`) appelle `module.build(config)` si elle existe, sinon retombe
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

