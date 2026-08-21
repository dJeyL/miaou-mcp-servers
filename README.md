# miaou-mcp-servers

Serveurs MCP de banc d'essai pour [MIAOU](https://github.com/dJeyL/miaou), un client de
chat web pour API OpenAI-compatible. Ces serveurs permettent de tester l'agrégation MCP
de MIAOU : connexion, invocation d'outils, rendu des résultats non-text.

## Serveurs disponibles

| Serveur | Port | Description |
|---|---|---|
| `servers/mcp_bench.py` | 8766 | Banc d'essai : echo, add, DNS, image PNG, resource JSON |
| `servers/mcp_weather.py` | 8767 | Météo réelle via wttr.in (resource JSON) |
| `servers/mcp_web/` | 8768 | Téléchargement d'URL (HTML→texte, text/* et JSON/XML, binaire base64), package |
| `servers/mcp_ddg.py` | 8769 | Recherche DuckDuckGo HTML, sans clef API |
| `servers/mcp_brave.py` | 8770 | Recherche Brave Search API (clef requise) |
| `servers/mcp_docs/` | 8771 | Extraction PDF/Office/Zip, paginée, sessions par conversation |
| `mcp_proxy.py` | configurable | Proxy qui agrège les serveurs ci-dessus |

## Prérequis

**Avec uv** (recommandé — les dépendances sont gérées automatiquement) :
```bash
uv --version
```

**Avec pip** :
```bash
pip install -r requirements.txt
```

## Démarrage rapide

### Serveurs unitaires

```bash
uv run servers/mcp_bench.py                   # HTTP 127.0.0.1:8766
uv run servers/mcp_weather.py                 # HTTP 127.0.0.1:8767
uv run servers/mcp_ddg.py                     # HTTP 127.0.0.1:8769
BRAVE_API_KEY=<key> uv run servers/mcp_brave.py  # HTTP 127.0.0.1:8770

# mcp_web et mcp_docs sont des packages (pas des scripts plats) — lancement différent :
uv run --directory servers python -m mcp_web  # HTTP 127.0.0.1:8768
uv run --directory servers python -m mcp_docs # HTTP 127.0.0.1:8771

uv run servers/mcp_bench.py --transport stdio # mode stdio
```

### Proxy (agrège tout sur un seul port)

```bash
cp config.sample.json config.json
# Éditer config.json : BRAVE_API_KEY, activer/désactiver des serveurs
uv run mcp_proxy.py
```

`config.sample.json` active bench, weather, web, duckduckgo et docs par défaut en
inprocess. brave est désactivé jusqu'à ce qu'une clef d'API soit renseignée : sans
clef il refuse de démarrer, et le proxy l'écarte en servant les autres serveurs.

## Configuration MIAOU

Dans MIAOU → Paramètres → Serveurs MCP → Ajouter :

```
bench       → http://127.0.0.1:8766/mcp   (streamable-http)
weather     → http://127.0.0.1:8767/mcp   (streamable-http)
web         → http://127.0.0.1:8768/mcp   (streamable-http)
duckduckgo  → http://127.0.0.1:8769/mcp   (streamable-http)
brave       → http://127.0.0.1:8770/mcp   (streamable-http)
docs        → http://127.0.0.1:8771/mcp   (streamable-http)
proxy       → http://127.0.0.1:8765/mcp   (streamable-http)
```

Via le proxy, les outils apparaissent préfixés : `bench__echo`, `web__fetch_url`,
`duckduckgo__ddg_search`, `brave__brave_search`, `docs__list`, `docs__read`, etc.

## Options CLI

Tous les serveurs acceptent :

```
--transport http|stdio    Transport (défaut: http)
--host ADDR               Adresse d'écoute (défaut: 127.0.0.1)
--port PORT               Port (défaut: selon le serveur)
```

Le proxy accepte en plus :

```
--config FICHIER            Chemin vers config.json (défaut: config.json)
--proxy [http://]host:port  Force http_proxy/https_proxy (+ variantes majuscules) vus
                             par tous les serveurs servis, même si déjà définis dans
                             l'environnement ou config.json ("http://" ajouté si absent)
--noproxy                   Force l'absence de proxy pour tous les serveurs servis,
                             même si http_proxy/https_proxy sont définis ailleurs
                             (incompatible avec --proxy)
```

## Configuration du proxy (`config.json`)

```json
{
  "port": 8765,
  "host": "127.0.0.1",
  "mcpServers": {
    "bench":      { "type": "inprocess", "module": "mcp_bench" },
    "web":        { "type": "inprocess", "module": "mcp_web" },
    "duckduckgo": { "type": "inprocess", "module": "mcp_ddg" },
    "brave": {
      "type": "inprocess",
      "module": "mcp_brave",
      "config": { "api_key": "your-key-here" }
    },
    "docs": { "type": "inprocess", "module": "mcp_docs" }
  }
}
```

`type` absent → `stdio`. `port` est obligatoire, `host` optionnel.
`"_disabled": true` sur une entrée → upstream ignoré au démarrage.
`env` sur une entrée inprocess → variables d'environnement injectées avant l'import.

Pour un serveur externe (subprocess stdio) :
```json
{
  "external": {
    "command": "uv",
    "args": ["run", "servers/mcp_bench.py", "--transport", "stdio"]
  }
}
```

Les chemins relatifs des `args` sont résolus depuis le répertoire de travail du proxy.
Pour un serveur qui vit dans **un autre dépôt**, passer son dossier à uv avec
`--directory` :

```json
{
  "external": {
    "command": "uv",
    "args": [
      "run", "--directory", "/chemin/vers/autre-projet",
      "mon_serveur.py", "--transport", "stdio"
    ]
  }
}
```

soit la commande complète `uv run --directory /chemin/vers/autre-projet mon_serveur.py`.
`--directory` fait deux choses d'un coup :

- uv y découvre le projet (`pyproject.toml` / `uv.lock`) et utilise donc **ses**
  dépendances et son venv, pas ceux du proxy ;
- le subprocess y voit son répertoire de travail — ce dont dépend tout serveur à cache
  disque relatif, comme `MIAOU_DOCS_WORKDIR` (`./miaou-docs`) ou `MIAOU_WEB_WORKDIR`
  (`./miaou-web`), qui se créeraient sinon dans le dossier du proxy.

Une entrée stdio accepte aussi `cwd`, qui pose le répertoire de travail du subprocess
sans rien passer à uv — celui-ci découvre alors le projet depuis ce même répertoire, ce
qui revient au même pour un lancement `uv` (vérifié : même venv, même cwd). `cwd` reste
utile pour une commande qui n'est pas uv (`python`, `node`, un binaire) :

```json
{
  "external": {
    "command": "python",
    "args": ["mon_serveur.py", "--transport", "stdio"],
    "cwd": "/chemin/vers/autre-projet"
  }
}
```

Corollaire à ne pas manquer : le projet externe résout **ses** versions, indépendamment
de celles du proxy. Un serveur bâti sur `mcp.server.fastmcp` doit donc borner sa
dépendance (`"mcp>=1.28.1,<2"`) — un `mcp>=1.28.1` non borné y installe mcp 2.x, où ce
module n'existe plus, et le subprocess meurt au démarrage (le proxy le signale alors
`unavailable — Connection closed`).

Rappel de périmètre : un subprocess stdio n'hérite qu'une whitelist restreinte de
variables d'environnement (`HOME`, `PATH`, `SHELL`, …). Tout ce dont le serveur externe
a besoin — clefs d'API, `MIAOU_*_WORKDIR` — doit être posé explicitement dans son `env`.

## Tests

```bash
# Avec uv — commande canonique
uv run --with pytest --with pytest-asyncio --with html2text --with pymupdf \
  --with python-docx --with openpyxl --with python-pptx pytest tests/ -v

# Avec uv — alternative via pyproject.toml (groupe dev) + uv.lock
uv run --group dev pytest tests/ -v

# Avec pip (après pip install -r requirements.txt)
pytest tests/ -v
```

Tous les tests sont unitaires et mockent les appels réseau — aucune clef API requise.

## Structure

```
miaou-mcp-servers/
├── mcp_proxy.py          # proxy (point d'entrée principal)
├── servers/
│   ├── mcp_base.py       # classe de base + make_opener() proxy-aware
│   ├── mcp_bench.py      # banc d'essai (port 8766)
│   ├── mcp_weather.py    # météo wttr.in (port 8767)
│   ├── mcp_web/          # fetch URL (port 8768), package
│   ├── mcp_ddg.py        # recherche DDG (port 8769)
│   ├── mcp_brave.py      # recherche Brave (port 8770)
│   └── mcp_docs/         # extraction PDF/Office/Zip (port 8771), package
├── tests/
│   ├── __init__.py
│   ├── test_base.py
│   ├── test_bench.py
│   ├── test_weather.py
│   ├── test_web.py
│   ├── test_web_structure.py
│   ├── test_ddg.py
│   ├── test_brave.py
│   ├── test_docs.py
│   └── test_proxy.py
├── config.sample.json
├── requirements.txt
├── pyproject.toml        # métadonnées + config pytest (asyncio_mode=auto) + groupe dev
└── uv.lock               # lock uv, versionné
```

## Sécurité

CORS ouvert, pas d'authentification — usage local uniquement. En production, placer
derrière un reverse proxy (Caddy, nginx) avec authentification côté serveur.

`mcp_docs` applique en plus des gardes propres à l'extraction d'archives (zip-slip,
tailles, chiffrement, imbrication) et un contrat d'erreur `REF_UNKNOWN` partagé avec le
dispatcher MIAOU — détails dans `CLAUDE.md`.
