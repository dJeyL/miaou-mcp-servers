# CLAUDE.md — miaou-mcp-servers

Instructions pour travailler dans ce dépôt.

## Ce qu'est le projet

Deux serveurs MCP de développement (bench + weather) et un serveur proxy qui les agrège,
extraits du dépôt [MIAOU](https://github.com/dJeyL/miaou), un client de chat web pour
API OpenAI-compatible (single-file HTML). Ils servent à tester l'agrégation MCP de MIAOU :
connexion, invocation d'outils, rendu des résultats non-text.

Ils n'ont pas de rôle en production — ce sont des outils de banc d'essai, destinés à
tourner localement pendant le développement de MIAOU.

## Structure du projet

```
miaou-mcp-servers/
├── mcp_proxy.py          # serveur proxy (racine, point d'entrée principal)
├── servers/
│   ├── mcp_base.py       # classe de base partagée (MiaouMCPBase)
│   ├── mcp_bench.py      # implémentation bench (port 8765)
│   └── mcp_weather.py    # implémentation weather (port 8766)
├── tests/
│   ├── test_bench.py
│   ├── test_weather.py
│   └── test_proxy.py
├── config.sample.json    # template de config pour le proxy
├── config.json           # (gitignored) config active du proxy
├── requirements.txt      # pour les utilisateurs sans uv
└── .gitignore
```

## Les trois serveurs

### `servers/mcp_bench.py` — banc d'essai général (port 8765)

Sert à exercer les différents chemins de traitement des résultats dans MIAOU :

| Outil | Résultat | Chemin MIAOU exercé |
|---|---|---|
| `echo(text)` | bloc `text` | D9 : texte réinjecté au modèle |
| `add(a, b)` | bloc `text` | D9 |
| `dns_lookup(hostname)` | bloc `text` | D9 — résolution via réseau local du serveur |
| `reverse_dns(ip)` | bloc `text` | D9 — PTR record |
| `get_image()` | bloc `image` (PNG) | D8.1 : binaire → IDB → `<img>` dans l'UI, descripteur statique au modèle |
| `get_json_resource()` | `EmbeddedResource` texte JSON | D8.2 : resource inline → IDB → chip `resource_stored`, texte brut + descripteur au modèle |

Les outils ont un `asyncio.sleep(2)` intentionnel pour simuler la latence réseau et
vérifier que le patienteur animé et les acks MCP (`mcp_call`) s'affichent correctement
pendant le round-trip.

### `servers/mcp_weather.py` — météo réelle (port 8766)

Un seul outil `get_weather(city, state?, country?)` qui interroge wttr.in et renvoie
un `EmbeddedResource` texte JSON. Sert à tester un outil avec données réelles et
paramètres optionnels, ainsi que le chemin resource inline (D8.2).

### `mcp_proxy.py` — proxy MCP (configurable via config.json)

Agrège plusieurs serveurs MCP upstream et expose leurs outils préfixés :
`bench__echo`, `bench__get_image`, `weather__get_weather`, etc.

Deux types d'upstream supportés :
- **inprocess** : import Python direct, pas de subprocess (défaut pour bench/weather)
- **stdio** : subprocess externe communiquant via stdin/stdout

## Transport et configuration MIAOU

Les trois serveurs utilisent **streamable-http** (JSON-RPC 2.0, endpoint unique POST `/mcp`,
réponses JSON ou SSE `event:message`/`data:`). C'est le transport implémenté par MIAOU V2.

Pour connecter les serveurs depuis MIAOU → Paramètres → Serveurs MCP :

| Champ | bench | weather | proxy |
|---|---|---|---|
| Nom | `bench` | `weather` | `proxy` |
| URL | `http://127.0.0.1:8765/mcp` | `http://127.0.0.1:8766/mcp` | `http://127.0.0.1:8767/mcp` |
| Transport | `streamable-http` | idem | idem |
| Activé | oui | oui | oui |

## Lancement

### Avec uv (recommandé)

Les scripts utilisent PEP 723 (bloc `# ///` en tête). Les dépendances sont installées
automatiquement par `uv run` dans un venv isolé.

```bash
# Serveurs unitaires
uv run servers/mcp_bench.py                          # HTTP 127.0.0.1:8765
uv run servers/mcp_bench.py --transport stdio        # mode stdio
uv run servers/mcp_bench.py --host 0.0.0.0           # toutes interfaces

uv run servers/mcp_weather.py                        # HTTP 127.0.0.1:8766

# Proxy
cp config.sample.json config.json     # puis éditer config.json
uv run mcp_proxy.py                   # port défini dans config.json
uv run mcp_proxy.py --port 8767       # override port
uv run mcp_proxy.py --config autre.json
```

### Avec pip

```bash
pip install -r requirements.txt
python servers/mcp_bench.py [options]
python servers/mcp_weather.py [options]
python mcp_proxy.py [options]
```

## Architecture de `mcp_proxy.py`

```
mcp_proxy.py (racine)
├── ajoute servers/ à sys.path au démarrage
├── lit config.json → build_upstreams()
│   ├── InProcessUpstream  : importlib.import_module(module) → module.mcp._tool_manager
│   └── StdioUpstream      : stdio_client + ClientSession (subprocess MCP)
├── build_proxy_server()   : mcp.server.Server avec list_tools / call_tool dynamiques
│   ├── list_tools → agrège tous les upstreams, préfixe les noms avec "{name}__"
│   └── call_tool  → dépréfixe, route vers l'upstream concerné
└── build_app()            : Starlette + StreamableHTTPSessionManager + CORSMiddleware
    └── lifespan : start/stop de chaque upstream
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
    "external_example": {
      "command": "uv",
      "args": ["run", "servers/mcp_bench.py", "--transport", "stdio"]
    }
  }
}
```

`type` absent → `stdio` (défaut). `port` est obligatoire, `host` est optionnel.
`"_disabled": true` sur une entrée `mcpServers` → upstream ignoré au démarrage.

## Architecture commune des serveurs

```python
class MyServer(MiaouMCPBase):
    def __init__(self):
        super().__init__("nom-du-serveur", default_port=9000)

        @self.mcp.tool()
        async def mon_outil(arg: str) -> ...: ...

server = MyServer()
mcp = server.mcp  # exposé pour InProcessUpstream du proxy

if __name__ == "__main__":
    server.main()
```

Deux points à ne pas toucher sans bonne raison :

- **`enable_dns_rebinding_protection=False`** : MIAOU peut être servi en `file://`
  (ouverture directe de `dist/miaou.html`), qui envoie `Origin: null`. Le SDK MCP
  renverrait 403 avant même la couche CORS si la protection est active.

- **`expose_headers=["Mcp-Session-Id"]`** dans le middleware CORS : MIAOU lit ce
  header après `initialize` pour maintenir la session. Sans lui, le navigateur masque
  le header et chaque appel repart sans session → erreur 404 ou réinitialisation.

## Tests

```bash
# Avec uv
uv run --with pytest --with pytest-asyncio pytest tests/

# Avec pip (après pip install -r requirements.txt)
pytest tests/
```

Les tests de bench mockent `asyncio.sleep` pour éviter les délais de 2 s.
Les tests de weather mockent `urllib.request.OpenerDirector.open`.
Les tests du proxy mockent les upstreams ou utilisent InProcessUpstream sur mcp_bench réel.

## Posture sécurité

CORS ouvert (`allow_origins=["*"]`), pas d'auth, réseau local uniquement. Délibéré :
c'est du banc d'essai. En production, ces serveurs seraient derrière un proxy
(Caddy, nginx) qui porte les tokens côté serveur — cf. brief D6 de MIAOU.

## Ajouter un outil

Décorer une fonction avec `@self.mcp.tool()` dans le `__init__` du serveur concerné.
FastMCP génère le schéma JSON automatiquement depuis la signature Python et la docstring.
Aucune déclaration manuelle dans un registre.

## Ce que MIAOU attend d'un serveur MCP

1. `initialize` (handshake JSON-RPC) → capte `Mcp-Session-Id`
2. `notifications/initialized`
3. `tools/list` → liste les outils, les préfixe du nom de serveur, les met en cache
4. Pour chaque appel : `tools/call { name, arguments }` → `{ content: [...blocks], isError }`

Blocs de résultat : `text` (D9), `image`/`resource` binaire (D8.1), `resource` texte (D8.2).
Si `isError: true`, MIAOU marque l'ack en rouge dans le thread.
