# CLAUDE.md — miaou-mcp-servers

Instructions pour travailler dans ce dépôt.

## Ce qu'est le projet

Six serveurs MCP de développement (bench, weather, fetch, ddg, brave, docs) et un serveur
proxy qui les agrège, extraits du dépôt [MIAOU](https://github.com/dJeyL/miaou), un client de
chat web pour API OpenAI-compatible (single-file HTML). Ils servent à tester l'agrégation
MCP de MIAOU : connexion, invocation d'outils, rendu des résultats non-text.

Ils n'ont pas de rôle en production — ce sont des outils de banc d'essai, destinés à
tourner localement pendant le développement de MIAOU.

## Structure du projet

```
miaou-mcp-servers/
├── mcp_proxy.py          # serveur proxy (racine, point d'entrée principal)
├── servers/
│   ├── mcp_base.py       # classe de base partagée (MiaouMCPBase + make_opener)
│   ├── mcp_bench.py      # banc d'essai général (port 8765)
│   ├── mcp_weather.py    # météo réelle via wttr.in (port 8766)
│   ├── mcp_web.py      # téléchargement d'URL (port 8768)
│   ├── mcp_ddg.py        # recherche DuckDuckGo HTML (port 8769)
│   ├── mcp_brave.py      # recherche Brave Search API (port 8770)
│   └── mcp_docs/         # extraction PDF/Office/Zip (port 8771), package (voir plus bas)
├── tests/
│   ├── test_bench.py
│   ├── test_weather.py
│   ├── test_web.py
│   ├── test_ddg.py
│   ├── test_brave.py
│   ├── test_docs.py
│   └── test_proxy.py
├── config.sample.json    # template de config pour le proxy
├── config.json           # (gitignored) config active du proxy
├── requirements.txt      # pour les utilisateurs sans uv
└── .gitignore
```

## Les serveurs

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

### `servers/mcp_web.py` — téléchargement d'URL (port 8768)

Un seul outil `fetch_url(url, max_bytes=5242880)`. Branch sur le `Content-Type` :

| Content-Type | Traitement | Résultat |
|---|---|---|
| `text/html` | html2text (script/style supprimés) | `TextResourceContents` `text/plain` |
| `text/*` | texte brut | `TextResourceContents` avec le vrai mime |
| tout le reste | base64 | `BlobResourceContents` |

Taille bornée à `max_bytes` (défaut 5 Mo), truncation notée dans le texte.
Erreurs réseau retournées comme chaînes (pas de stack trace).

### `servers/mcp_ddg.py` — recherche DuckDuckGo (port 8769)

Un seul outil `ddg_search(query, max_results=5)`. POST sur l'endpoint HTML de DDG
(`html.duckduckgo.com/html/`), parsing stdlib uniquement (classes `result__a` /
`result__snippet`). Renvoie `TextResourceContents` `application/json` — tableau
`[{title, url, snippet}]`. Fragile si DDG change son markup.

### `servers/mcp_brave.py` — recherche Brave Search (port 8770)

Deux outils. Requièrent `BRAVE_API_KEY` dans l'environnement (ou via `env` dans
`config.json` pour le mode inprocess). Clef absente ou invalide → message d'erreur
clair sans stack trace.

- `brave_search(query, count=5)` : recherche web. Renvoie `TextResourceContents`
  `application/json` — tableau `[{title, url, description}]`.
- `brave_image_search(query, count=5)` : recherche d'images. `count` plafonné à 20.
  Renvoie `TextResourceContents` `application/json` — tableau
  `[{title, page_url, image_url, thumbnail_url, source}]`. Les entrées sans
  `properties.url` sont écartées. URI : `miaou://brave-images/{query}`.

### `servers/mcp_docs/` — extraction de documents (port 8771)

Serveur d'extraction (lecture seule, pas de génération/modification) pour PDF, Office
(docx/xlsx/pptx) et Zip. Conçu autour d'un cache de session côté serveur (répertoire
`<workdir>/<session_id>/`, `session_id` = id de conversation MIAOU) et de lectures
paginées : jamais le document entier en un seul appel.

Seul serveur du dépôt organisé en package plutôt qu'en fichier plat (module trop
volumineux sinon) :

```
servers/mcp_docs/
├── __init__.py    # serveur FastMCP + définition des outils (docs__*)
├── __main__.py     # point d'entrée `python -m mcp_docs` / `uv run servers/mcp_docs`
├── session.py      # sessions, sanitization, matérialisation, contrat REF_UNKNOWN
├── formats.py      # détection de type + parsers pdf/docx/xlsx/pptx/zip
└── search.py       # logique pure de recherche (fold, parse_query, match_unit, make_snippet,
                     # render_results) — aucune dépendance à une lib de parsing de document,
                     # testable en isolation sans fixture binaire
```

Quatre outils exposés (préfixés `docs__` par le proxy) :

| Outil | Rôle |
|---|---|
| `drop_session(session_id)` | Supprime le cache d'une session (nettoyage sur suppression de conversation MIAOU) |
| `list(ref, path?, session_id?, content_b64?, filename?)` | Structure du document sans contenu (pages/feuilles/slides/entrées zip) |
| `read(ref, path?, selector?, session_id?, content_b64?, filename?)` | Extrait borné (plage de pages/lignes/slides), plafonné par `MIAOU_DOCS_READ_CAP` |
| `search(ref, query, path?, session_id?, content_b64?, filename?)` | Recherche de texte groupée par unité (page/feuille/slide/membre zip), plafonnée par `MIAOU_DOCS_SEARCH_CAP` |

Signature commune inflatable : `ref: str, content_b64: str | None = None, session_id: str
| None = None` sur `list`/`read`/`search` — obligatoire pour la détection de capability du
dispatcher MIAOU (voir « Contrat docs » ci-dessous). `drop_session` n'a volontairement
pas `ref` : le hook client reste inerte dessus.

`search` : requête en ET implicite (termes espacés) + `"phrase exacte"` entre guillemets,
insensible casse/accents (fold Unicode NFKD + mapping manuel des ligatures œ/æ, non
décomposées par NFKD seul). Pas d'opérateurs OU/NON/parenthèses (YAGNI, le modèle compose
plusieurs appels). Un guillemet non fermé n'est pas une erreur : le reste de la requête
retombe en termes normaux. Une phrase exacte ne matche pas à cheval sur deux unités.
Granularité par format : page (pdf), slide (pptx), cellule `Feuille!Coord` (xlsx — balaie
toutes les lignes, n'hérite pas du cap de lignes de `read`), section heading (docx — texte
hors-section et docs sans heading utilisent les labels spéciaux `(préambule)`/`(corps)`,
tables sous `(tableaux)`, round-trip partiel assumé), membre de zip (texte brut
uniquement — un membre reconnu comme document structuré, chiffré ou binaire est ignoré et
listé en note finale des zones aveugles, pas de dispatch récursif contrairement à `read`).
Chaque label de résultat est réutilisable tel quel comme selector `read` (ou `path` pour
un membre de zip). Sans `path` sur une archive, `search` balaie tous ses membres texte ;
avec `path`, restreint à ce seul membre.

Détection de type : le contrat client ne transmet pas le nom de fichier d'origine à ce
jour, donc `filename` (optionnel, pour extension) retombe sur les magic bytes en son
absence (`%PDF`, `PK\x03\x04` puis sniff des dossiers internes `word/`/`xl/`/`ppt/` pour
distinguer docx/xlsx/pptx d'un zip brut). `path` (sur `read` et `list`) adresse un membre
de zip par chemin : texte brut si le membre n'est pas reconnu comme document structuré,
sinon dispatch récursif (`read`/`list` du format détecté sur les octets extraits en
mémoire, sans matérialisation disque) — un membre docx/xlsx/pptx/pdf dans un zip est donc
lisible/listable comme un document imbriqué, borné à **un seul niveau** d'imbrication
(un zip contenant un zip contenant un docx reste signalé, non extrait, pour borner la
récursion et le coût de parsing cumulé). Un membre imbriqué reconnu est en plus soumis à
`MIAOU_DOCS_MAX_FILE_MB` (pas seulement `MAX_UNZIP_MB`) avant parsing par une lib lourde.
Un PDF sans texte extractible (scan) renvoie une note explicite, pas d'OCR en v1.

**Sécurité archives (zip)** — non négociable même en contexte mono-utilisateur (coût
trivial, échec = dommage filesystem) :

- Zip-slip : tout chemin de membre absolu ou contenant `..` est rejeté avant extraction
  (`read`), mais reste visible dans `list` avec l'annotation « chemin suspect ».
- Taille : garde sur `ZipInfo.file_size` (en-tête) puis contrôle réel en flux (les
  en-têtes peuvent mentir) — `zipfile` lève aussi nativement `BadZipFile` sur incohérence
  CRC, convertie en erreur claire plutôt que de fuiter l'exception stdlib. `list` signale
  en plus si la taille décompressée totale déclarée dépasse `MIAOU_DOCS_MAX_UNZIP_MB`.
- Chiffrement : détecté via `ZipInfo.flag_bits & 0x1`, rejeté avant toute tentative de
  lecture (message clair, pas de `RuntimeError` stdlib brute).
- Archives imbriquées : un membre reconnu comme document structuré (pdf/docx/xlsx/pptx/zip)
  est extractible/listable via `path`, mais borné à **un seul niveau** — un membre zip qui
  contiendrait lui-même un zip reste signalé, jamais extrait au-delà (D-bis). `list`
  pré-signale aussi les entrées dont l'extension suggère une archive (`.zip`/`.docx`/
  `.xlsx`/`.pptx`) avant même lecture.

Variables d'environnement (toutes optionnelles, défauts constants) :

| Variable | Défaut | Rôle |
|---|---|---|
| `MIAOU_DOCS_WORKDIR` | `./miaou-docs` (relatif au répertoire de travail) | Racine du cache de sessions |
| `MIAOU_DOCS_TTL_H` | `24` | TTL avant sweep d'une session inactive |
| `MIAOU_DOCS_MAX_FILE_MB` | `20` | Taille max d'un fichier matérialisé (avant décodage b64) |
| `MIAOU_DOCS_MAX_SESSION_MB` | `200` | Quota disque total par session |
| `MIAOU_DOCS_MAX_UNZIP_MB` | `100` | Taille décompressée max d'une archive (garde header + flux) |
| `MIAOU_DOCS_READ_CAP` | `20000` | Cap de caractères par réponse `read` |
| `MIAOU_DOCS_SEARCH_CAP` | `50` | Cap du nombre de snippets par réponse `search` |

**Procédure manuelle (banc d'essai MIAOU, brief A)** — vérification réelle via l'UI MIAOU,
à exécuter uniquement sur demande explicite, pas automatisée ici :

1. Lancer le proxy (`uv run mcp_proxy.py`) avec `docs` activé dans `config.json`.
2. Dans MIAOU, joindre un PDF/docx/xlsx/pptx/zip à un message, demander au modèle de lister
   sa structure (`docs__list`) puis de lire un extrait (`docs__read`).
3. Vérifier le rejeu REF_UNKNOWN : recharger la page MIAOU en cours de conversation, puis
   redemander une lecture du même attachement — le contenu doit être ré-injecté sans erreur
   visible côté utilisateur (le rejeu est interne au dispatcher).
4. Joindre un zip contenant une entrée `../evil.txt` ou un membre chiffré (fixture de test
   possible : réutiliser les archives forgées de `tests/test_docs.py`) et vérifier que
   `list` les signale sans planter, et que `read` dessus renvoie une erreur claire.

Le sweep TTL est **opportuniste** (en tête de chaque appel d'outil, pas de tâche
périodique) — ni le proxy ni le mode standalone n'ont de machinerie de fond existante.

### `mcp_proxy.py` — proxy MCP (configurable via config.json)

Agrège plusieurs serveurs MCP upstream et expose leurs outils préfixés :
`bench__echo`, `bench__get_image`, `weather__get_weather`, etc.

Deux types d'upstream supportés :
- **inprocess** : import Python direct, pas de subprocess (défaut pour tous les serveurs)
- **stdio** : subprocess externe communiquant via stdin/stdout

Les entrées inprocess acceptent un champ `env` pour injecter des variables d'environnement
avant l'import du module (`os.environ.setdefault`) — utilisé par `mcp_brave` pour
`BRAVE_API_KEY`.

## Transport et configuration MIAOU

Les trois serveurs utilisent **streamable-http** (JSON-RPC 2.0, endpoint unique POST `/mcp`,
réponses JSON ou SSE `event:message`/`data:`). C'est le transport implémenté par MIAOU V2.

Pour connecter les serveurs depuis MIAOU → Paramètres → Serveurs MCP :

| Champ | bench | weather | fetch | ddg | brave | docs | proxy |
|---|---|---|---|---|---|---|---|
| Nom | `bench` | `weather` | `web` | `duckduckgo` | `brave` | `docs` | `proxy` |
| URL | `:8765/mcp` | `:8766/mcp` | `:8768/mcp` | `:8769/mcp` | `:8770/mcp` | `:8771/mcp` | `:8767/mcp` |
| Transport | `streamable-http` | idem | idem | idem | idem | idem | idem |

(préfixe `http://127.0.0.1` pour toutes les URLs)

En pratique : passer par le **proxy** expose tous les outils préfixés sur un seul port.
Les noms exposés côté proxy dépendent des clés dans `config.json` (`mcpServers`).

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
uv run servers/mcp_web.py                          # HTTP 127.0.0.1:8768
uv run servers/mcp_ddg.py                            # HTTP 127.0.0.1:8769
BRAVE_API_KEY=<key> uv run servers/mcp_brave.py      # HTTP 127.0.0.1:8770

# mcp_docs est un package (pas un script plat) — lancement différent :
uv run --directory servers python -m mcp_docs        # HTTP 127.0.0.1:8771

# Proxy (agrège tout sur un seul port)
cp config.sample.json config.json     # puis éditer config.json (BRAVE_API_KEY, etc.)
uv run mcp_proxy.py                   # port défini dans config.json
uv run mcp_proxy.py --port 8767       # override port
uv run mcp_proxy.py --config autre.json
```

### Avec pip

```bash
pip install -r requirements.txt
python servers/mcp_bench.py [options]
python servers/mcp_weather.py [options]
python servers/mcp_web.py [options]
python servers/mcp_ddg.py [options]
BRAVE_API_KEY=<key> python servers/mcp_brave.py [options]
python -m mcp_docs [options]    # depuis servers/ (package, pas un script plat)
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

`build_app()` ne retourne pas directement le `Starlette` mais une fonction ASGI qui l'enveloppe :
`Mount("/mcp", ...)` redirige `/mcp` → `/mcp/` en 307 par défaut (strict-slash Starlette), et
certains clients MCP ne suivent pas les redirections sur POST/DELETE. Le wrapper réécrit
`scope["path"]` de `/mcp` vers `/mcp/` avant le routeur pour servir la requête directement,
sans redirection.

## Configuration du proxy (`config.json`)

```json
{
  "port": 8767,
  "host": "127.0.0.1",
  "mcpServers": {
    "bench": { "type": "inprocess", "module": "mcp_bench" },
    "web":   { "type": "inprocess", "module": "mcp_web" },
    "duckduckgo": { "type": "inprocess", "module": "mcp_ddg" },
    "brave": {
      "type": "inprocess",
      "module": "mcp_brave",
      "env": { "BRAVE_API_KEY": "your-key-here" }
    },
    "docs": { "type": "inprocess", "module": "mcp_docs" },
    "_example_stdio": {
      "_disabled": true,
      "command": "uv",
      "args": ["run", "servers/mcp_bench.py", "--transport", "stdio"]
    }
  }
}
```

`type` absent → `stdio` (défaut). `port` est obligatoire, `host` est optionnel.
`"_disabled": true` sur une entrée `mcpServers` → upstream ignoré au démarrage.
`env` sur une entrée inprocess → variables posées via `os.environ.setdefault` avant l'import.

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
uv run --with pytest --with pytest-asyncio --with html2text --with pymupdf \
  --with python-docx --with openpyxl --with python-pptx pytest tests/

# Avec pip (après pip install -r requirements.txt)
pytest tests/
```

Les tests de bench mockent `asyncio.sleep` pour éviter les délais de 2 s.
Les tests de weather, fetch, ddg et brave mockent `urllib.request.OpenerDirector.open`.
Les tests de brave mockent aussi `os.environ` — aucun appel réseau réel, aucune clef requise.
Les tests du proxy mockent les upstreams ou utilisent InProcessUpstream sur mcp_bench réel.
Les tests de docs monkeypatchent `mcp_docs.session.WORKDIR` (fixture `tmp_path`) pour
isoler le filesystem par test, exercent chaque format (fixtures PDF/xlsx/docx/pptx/zip
générées à la volée par les libs elles-mêmes) via `formats.py` directement, et vérifient
REF_UNKNOWN à travers le stack proxy réel (InProcessUpstream sur mcp_docs + build_proxy_server),
pas seulement l'appel direct à l'outil.

## Contrat partagé `mcp_docs` ↔ MIAOU (dispatcher, lot A/D6)

Contrat entre le dispatcher client MIAOU et `mcp_docs` (et tout futur outil inflatable) —
mirror de `docs/mcp.md` §12 côté MIAOU, à tenir synchronisé si l'un des deux évolue.

- **Détection de capability** : le dispatcher n'active son hook que si l'outil déclare
  `ref` ET `content_b64` dans `inputSchema.properties` (cache `tools/list`). Tout outil
  inflatable doit donc avoir exactement les paramètres `ref: str, content_b64: str | None
  = None, session_id: str | None = None` (+ paramètres propres à l'outil).
- **`session_id`** : injecté par MIAOU sur chaque appel capable (id de conversation).
  Absent → erreur claire (appel hors MIAOU), jamais silencieusement ignoré.
- **`content_b64`** : injecté seulement au premier appel par (conversation, ref) ; un
  rechargement de page re-pousse le même contenu → la matérialisation doit être
  **idempotente** (réécriture silencieuse d'un ref déjà connu, jamais une erreur).
- **REF_UNKNOWN** : un `ref` inconnu sans `content_b64` doit produire une vraie **erreur
  JSON-RPC** avec `err.data.code === 'REF_UNKNOWN'` — le dispatcher la détecte et rejoue
  l'appel une fois avec le contenu inliné. Un `isError` textuel ne déclenche PAS le rejeu.
  Mécanisme : l'outil lève `ToolError(f"{REF_UNKNOWN_SENTINEL}: ...")` (constante
  `mcp_docs.REF_UNKNOWN_SENTINEL`) ; le proxy (`mcp_proxy._wrap_ref_unknown_sentinel`)
  détecte ce sentinel dans le résultat `isError` (le SDK MCP avale toute exception de
  l'outil en `CallToolResult(isError=True)`, y compris `McpError` — voir le commentaire
  du post-wrapper dans `mcp_proxy.py`) et lève `McpError(data={'code': 'REF_UNKNOWN'})`,
  que `_handle_request` du SDK convertit en erreur JSON-RPC. **Ce rejeu ne fonctionne que
  derrière le proxy** : en standalone (FastMCP pur, port 8771 direct), l'appel échoue en
  isError textuel sans déclencher le rejeu — documenté ici, pas une régression à corriger.
- **Adressage de membres d'archive** : paramètre `path` séparé (pas de suffixe `ref#path`)
  — `ref` reste `att-N`, `path` adresse un membre déjà listé par `list`. Écart assumé par
  rapport au brief original : la syntaxe `ref#path` ne matche pas la regex ancrée
  `^att-\d+$` du dispatcher déjà livré.

## Posture sécurité

CORS ouvert (`allow_origins=["*"]`), pas d'auth, réseau local uniquement. Délibéré :
c'est du banc d'essai. En production, ces serveurs seraient derrière un proxy
(Caddy, nginx) qui porte les tokens côté serveur — cf. brief D6 de MIAOU.

## Ajouter un outil

Décorer une fonction avec `@self.mcp.tool()` dans le `__init__` du serveur concerné.
FastMCP génère le schéma JSON automatiquement depuis la signature Python et la docstring.
Aucune déclaration manuelle dans un registre.

### Faire apparaître une valeur d'environnement résolue dans une docstring d'outil

Si la description d'un outil doit citer la valeur *active* d'une constante dérivée d'une
variable d'environnement (ex. `MIAOU_DOCS_READ_CAP`) plutôt que le nom de la variable,
`f"""..."""` en position de docstring ne fonctionne pas : Python n'assigne `__doc__`
qu'à partir d'un littéral de chaîne reconnu syntaxiquement, jamais depuis une expression
(f-string comprise) — la fonction se retrouve avec `__doc__ is None`, silencieusement.

Pattern à appliquer : définir la fonction sans décorateur, assigner `func.__doc__ = f"""..."""`
explicitement, puis appliquer le décorateur a posteriori en appel direct :

```python
async def read(...) -> str:
    ...

read.__doc__ = f"""Réponse plafonnée à {READ_CAP} caractères, ..."""
self.mcp.tool(name="read")(read)
```

Voir `servers/mcp_docs/__init__.py` (outils `read`/`search`) pour l'exemple appliqué.

## Ce que MIAOU attend d'un serveur MCP

1. `initialize` (handshake JSON-RPC) → capte `Mcp-Session-Id`
2. `notifications/initialized`
3. `tools/list` → liste les outils, les préfixe du nom de serveur, les met en cache
4. Pour chaque appel : `tools/call { name, arguments }` → `{ content: [...blocks], isError }`

Blocs de résultat : `text` (D9), `image`/`resource` binaire (D8.1), `resource` texte (D8.2).
Si `isError: true`, MIAOU marque l'ack en rouge dans le thread.
