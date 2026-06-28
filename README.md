# miaou-mcp-servers

Serveurs MCP de banc d'essai pour [MIAOU](https://github.com/dJeyL/miaou), un client de
chat web pour API OpenAI-compatible. Ces serveurs permettent de tester l'agrégation MCP
de MIAOU : connexion, invocation d'outils, rendu des résultats non-text.

## Serveurs disponibles

| Serveur | Port | Description |
|---|---|---|
| `servers/mcp_bench.py` | 8765 | Banc d'essai : echo, add, DNS, image PNG, resource JSON |
| `servers/mcp_weather.py` | 8766 | Météo réelle via wttr.in (resource JSON) |
| `mcp_proxy.py` | configurable | Proxy qui agrège les deux serveurs ci-dessus |

## Prérequis

**Avec uv** (recommandé — les dépendances sont gérées automatiquement) :
```bash
# Vérifier que uv est installé
uv --version
```

**Avec pip** :
```bash
pip install -r requirements.txt
```

## Démarrage rapide

### Serveurs unitaires

```bash
uv run servers/mcp_bench.py                   # HTTP 127.0.0.1:8765
uv run servers/mcp_weather.py                 # HTTP 127.0.0.1:8766
uv run servers/mcp_bench.py --transport stdio # mode stdio
```

### Proxy (agrège bench + weather sur un seul port)

```bash
cp config.sample.json config.json
# Éditer config.json si nécessaire (port, host)
uv run mcp_proxy.py
```

Par défaut `config.sample.json` configure bench et weather en **inprocess** (pas de
subprocess supplémentaire — les serveurs tournent dans le même processus Python que
le proxy).

## Configuration MIAOU

Dans MIAOU → Paramètres → Serveurs MCP → Ajouter :

```
bench    → http://127.0.0.1:8765/mcp   (streamable-http)
weather  → http://127.0.0.1:8766/mcp   (streamable-http)
proxy    → http://127.0.0.1:8767/mcp   (streamable-http)
```

Les outils du proxy apparaissent préfixés : `bench__echo`, `weather__get_weather`, etc.

## Options CLI

Tous les serveurs acceptent :

```
--transport http|stdio    Transport (défaut: http)
--host ADDR               Adresse d'écoute (défaut: 127.0.0.1)
--port PORT               Port (défaut: selon le serveur)
```

Le proxy accepte en plus :

```
--config FICHIER          Chemin vers config.json (défaut: config.json)
```

## Configuration du proxy (`config.json`)

```json
{
  "port": 8767,
  "host": "127.0.0.1",
  "mcpServers": {
    "bench": {
      "type": "inprocess",
      "module": "mcp_bench"
    },
    "weather": {
      "type": "inprocess",
      "module": "mcp_weather"
    }
  }
}
```

Pour un serveur externe (subprocess stdio) :
```json
{
  "external": {
    "command": "uv",
    "args": ["run", "servers/mcp_bench.py", "--transport", "stdio"]
  }
}
```

`type` absent → `stdio`. `port` est obligatoire, `host` optionnel.

## Tests

```bash
# Avec uv
uv run --with pytest --with pytest-asyncio pytest tests/ -v

# Avec pip (après pip install -r requirements.txt)
pytest tests/ -v
```

## Structure

```
miaou-mcp-servers/
├── mcp_proxy.py          # proxy (point d'entrée principal)
├── servers/
│   ├── mcp_base.py       # classe de base partagée
│   ├── mcp_bench.py      # implémentation bench
│   └── mcp_weather.py    # implémentation weather
├── tests/
│   ├── test_bench.py
│   ├── test_weather.py
│   └── test_proxy.py
├── config.sample.json
└── requirements.txt
```

## Sécurité

CORS ouvert, pas d'authentification — usage local uniquement. En production, placer
derrière un reverse proxy (Caddy, nginx) avec authentification côté serveur.
