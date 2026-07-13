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
│   ├── mcp_web/          # téléchargement d'URL (port 8768), package (voir plus bas)
│   ├── mcp_ddg.py        # recherche DuckDuckGo HTML (port 8769)
│   ├── mcp_brave.py      # recherche Brave Search API (port 8770)
│   └── mcp_docs/         # extraction PDF/Office/Zip (port 8771), package (voir plus bas)
├── tests/
│   ├── test_bench.py
│   ├── test_weather.py
│   ├── test_web.py
│   ├── test_web_structure.py
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

### `servers/mcp_web/` — téléchargement d'URL (port 8768)

Package (pas un fichier plat, même seuil que `mcp_docs` : plusieurs responsabilités
distinctes — serveur, cache disque, extraction de structure) :

```
servers/mcp_web/
├── __init__.py    # serveur FastMCP + définition des outils (fetch_url/fetch_read/fetch_list/fetch_resource)
├── __main__.py    # point d'entrée `python -m mcp_web` / `uv run servers/mcp_web`
├── cache.py        # cache disque par checksum d'URL (texte, HTML brut, structure JSON)
└── structure.py    # extraction stdlib (html.parser) des headings/liens, sans dépendance tierce
```

Quatre outils. `fetch_url(url, max_bytes=5242880)` branch sur le `Content-Type` :

| Content-Type | Traitement | Résultat |
|---|---|---|
| `text/html` | html2text (script/style supprimés) | `TextResourceContents` `text/plain` |
| `text/*` | texte brut | `TextResourceContents` avec le vrai mime |
| tout le reste | base64 | `BlobResourceContents` |

Taille *téléchargée* bornée à `max_bytes` (défaut 5 Mo), troncature notée dans le texte.
Erreurs réseau retournées comme chaînes (pas de stack trace).

Le texte produit (HTML converti ou `text/*`) est en plus plafonné en sortie à
`MIAOU_WEB_READ_CAP` caractères (défaut 20000, cf. `servers/mcp_web/cache.py`) — une page
HTML de plusieurs Mo convertie par html2text peut sinon saturer la fenêtre de contexte de
l'appelant malgré `max_bytes`. Le texte complet (et le HTML brut, si `text/html`) est écrit
sur disque (`servers/mcp_web/cache.py`), clé = SHA256 de l'URL — pas de session_id ni de
contrat REF_UNKNOWN comme mcp_docs : ce cache est indépendant de toute conversation MIAOU,
une URL déjà récupérée reste paginable même depuis un autre client. Sweep TTL opportuniste
(`MIAOU_WEB_CACHE_TTL_H`, défaut 24h), même pattern que le TTL de session de `mcp_docs`.

`fetch_read(url, char_start=0, char_end=None)` relit le texte déjà mis en cache sans
retélécharger, pour paginer au-delà du cap (offset caractère uniquement, pas de mode ligne —
une page web n'a pas la structure en unités logiques d'un document). Chaque appel reste
plafonné à `MIAOU_WEB_READ_CAP` caractères : `char_end` ne lève pas ce cap, il déplace juste
la fenêtre (un `char_end` très éloigné de `char_start` est silencieusement ramené au cap) —
sinon un seul appel `fetch_read` sur le reliquat d'une page volumineuse recrée exactement le
problème de saturation de contexte que le cap sur `fetch_url` visait à éliminer. Erreur claire
si l'URL n'a jamais été passée à `fetch_url`, ou si le cache a expiré. Le contenu binaire
(image, etc.) n'est pas concerné par ce cache : déjà borné par `max_bytes`, ce n'est pas lui
qui sature le contexte du modèle.

`fetch_list(url, entry_start=0, entry_end=None)` extrait la structure de navigation (headings
h1-h6 et liens `<a href>`, dans l'ordre d'apparition, un lien sans texte ou un heading vide
étant ignoré) du HTML brut déjà mis en cache par `fetch_url` — sans retélécharger, et sans
dépendance de parsing tierce (`servers/mcp_web/structure.py`, stdlib `html.parser`). La
structure extraite est elle-même mise en cache (JSON) au premier appel, pour ne pas reparser
le HTML à chaque page. Pagination par **index d'entrée** (`entry_start`/`entry_end`, 0-indexé,
exclusif sur `entry_end`) plutôt que par ligne de texte ou par caractère : chaque heading ou
lien est une unité atomique numérotée, une pagination par ligne serait ambiguë sur du texte
rendu. Chaque appel reste plafonné à `MIAOU_WEB_LIST_CAP` entrées (défaut 100) — même logique
que le cap de `fetch_read` : `entry_end` ne lève pas le cap, il déplace la fenêtre. N'a de sens
que pour une URL dont `fetch_url` a renvoyé du HTML (`text/html`) ; erreur claire sinon, ou si
l'URL n'a jamais été récupérée, ou si le cache a expiré.

`fetch_resource(url, max_bytes=5242880)` transfère les **octets bruts** d'une URL au
**client** (pour matérialisation en ressource `res_…` côté MIAOU) sans jamais les faire
transiter par le contexte du modèle. À l'inverse de `fetch_url` (qui met le texte rendu en
contexte, paginé), `fetch_resource` renvoie une liste de deux blocs : un bloc `text`
descripteur factuel (mime détecté, taille en octets, URL d'origine, note de troncature) —
seul élément réinjecté au modèle — et un `EmbeddedResource`/`BlobResourceContents` portant
les octets en base64. Côté MIAOU, `extractResultParts` route un bloc `resource.blob` en
`store_binary` (→ IDB → `res_…`, hors contexte) et un bloc `text` en passthrough (→ modèle) :
c'est le canal existant, réutilisé tel quel, pas un nouveau canal. Le contenu est **toujours**
encodé en binaire (`.blob`), même pour du texte ou du JSON — pour rester exploitable côté
client (réinjection vers `docs__*` via `content_b64`, ou `js__eval`) plutôt que lu inline.
Fetch autonome : ne requiert pas de `fetch_url` préalable, et ne lit ni n'écrit le cache
`mcp_web` (qui ne contient que du texte rendu, pas les octets bruts) — chaque appel
re-télécharge. Téléchargement borné à `max_bytes` (défaut 5 Mo, `MIAOU_WEB_RESOURCE_MAX_BYTES`),
troncature notée dans le descripteur. Erreurs réseau et schéma non http/https retournés comme
chaînes (un seul bloc `text`), pas de stack trace. Le descripteur ne contient ni timestamp ni
id (stabilité KV-cache) ; l'id `res_…` est généré par le client, pas par le serveur. Le
paramètre transverse `miaou_intent` (breadcrumb rédigé par le modèle) n'est **pas** déclaré :
MIAOU le strippe des arguments avant l'envoi sur le wire, aucun serveur MCP ne le reçoit.

Variables d'environnement (toutes optionnelles, défauts constants) :

| Variable | Défaut | Rôle |
|---|---|---|
| `MIAOU_WEB_WORKDIR` | `./miaou-web` (relatif au répertoire de travail) | Racine du cache par checksum d'URL |
| `MIAOU_WEB_CACHE_TTL_H` | `24` | TTL avant sweep d'une entrée de cache inactive |
| `MIAOU_WEB_READ_CAP` | `20000` | Cap de caractères en sortie de `fetch_url`/`fetch_read` |
| `MIAOU_WEB_LIST_CAP` | `100` | Cap du nombre d'entrées en sortie de `fetch_list` |
| `MIAOU_WEB_RESOURCE_MAX_BYTES` | `5242880` (5 Mo) | Plafond de téléchargement de `fetch_resource` (octets transférés au client) |

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
- `brave_image_search(query, count=5)` : recherche d'images. Renvoie
  `TextResourceContents` `application/json` — tableau
  `[{title, page_url, image_url, thumbnail_url, source}]`. Les entrées sans
  `properties.url` sont écartées. URI : `miaou://brave-images/{query}`.

`count` est plafonné à [1, 20] symétriquement sur les deux outils (l'API Brave web
plafonne aussi à 20 au-delà, 422). Les deux outils partagent un helper commun
`_brave_call` (requête HTTP, chaîne d'erreurs réseau) — seul le mapping des résultats
diffère.

### `servers/mcp_docs/` — extraction de documents (port 8771)

Serveur d'extraction (lecture seule, pas de génération/modification) pour PDF, Office
(docx/xlsx/pptx) et Zip. Conçu autour d'un cache de session côté serveur (répertoire
`<workdir>/<session_id>/`, `session_id` = id de conversation MIAOU) et de lectures
paginées : jamais le document entier en un seul appel.

Organisé en package plutôt qu'en fichier plat (module trop volumineux sinon), comme
`mcp_web/` :

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

Cinq outils exposés (préfixés `docs__` par le proxy) :

| Outil | Rôle |
|---|---|
| `drop_session(session_id)` | Supprime le cache d'une session (nettoyage sur suppression de conversation MIAOU) |
| `list(ref, path?, session_id?, content_b64?, filename?)` | Structure du document sans contenu (pages/feuilles/slides/entrées zip) |
| `read(ref, path?, selector?, char_start?, char_end?, line_start?, line_end?, session_id?, content_b64?, filename?)` | Extrait borné (plage de pages/lignes/slides), plafonné par `MIAOU_DOCS_READ_CAP` ; plage char/ligne pour paginer une unité au-delà du cap |
| `search(ref, query, path?, session_id?, content_b64?, filename?)` | Recherche de texte groupée par unité (page/feuille/slide/membre zip), plafonnée par `MIAOU_DOCS_SEARCH_CAP` |
| `extract(ref, path, session_id?, content_b64?, filename?)` | Transfère le texte **intégral** d'un membre texte de zip au client, **sans READ_CAP** (voir exception ci-dessous) ; membre structuré (docx/xlsx/pptx/pdf/zip imbriqué) refusé, orienté vers `read`/`list` |

Signature commune inflatable : `ref: str, content_b64: str | None = None, session_id: str
| None = None` sur `list`/`read`/`search`/`extract` — obligatoire pour la détection de
capability du dispatcher MIAOU (voir « Contrat docs » ci-dessous). `drop_session` n'a
volontairement pas `ref` : le hook client reste inerte dessus.

**Exception `extract` — membre complet, transfert pas contexte.** `extract` est le SEUL
outil `docs__` qui renvoie un membre en entier sans le borner par `MIAOU_DOCS_READ_CAP`.
Ce n'est pas une brèche du cap : `READ_CAP` borne ce qui entre dans le **contexte du
modèle**, pas ce **transfert**. Les octets partent en `resource.blob` (mimeType textuel)
via le canal `content_b64` vers le client MIAOU, qui les matérialise en ressource `res_…`
de classe `inline` — jamais restitués au modèle en tool result. Le modèle reçoit seulement
un handle, qu'il passe ensuite à `js__eval(handle, code)` pour compter/filtrer/agréger sur
le membre complet sans jamais payer son poids en tokens. `path` est obligatoire (le membre
précis à extraire) ; un `ref` qui n'est pas une archive, ou un membre structuré
(pdf/docx/xlsx/pptx/zip imbriqué), est refusé avec un message orientant vers `read`/`list`.
Mêmes gardes zip que `read` (zip-slip, chiffrement, taille en flux).

`extract` ne déclare **ni** `char_start`/`line_start` **ni** `query` : côté MIAOU, la
sélection applicative de l'outil de lecture de contenu (`findDocsInflationTool` /
`_declaresContentReadSignature`, cf. `docs/mcp.md` point 14 du repo client) exige la
signature de pagination (`char_start`/`line_start`) — `extract` n'y répond pas et n'est
donc jamais pris à tort pour l'outil de lecture par ce chemin sans-modèle, exactement
comme `search` (écarté par son `query`). C'est le **modèle** qui appelle `extract`
nommément ; le hook d'inflation (`toolDeclaresAttachmentInflation`) ne vérifie que
`ref`+`content_b64`, tous deux présents.

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

`read` — plage char/ligne : `char_start`/`char_end` (offset caractère, `char_start`
obligatoire) OU `line_start`/`line_end` (numéro de ligne 1-indexé inclusif, `line_start`
obligatoire), les deux modes mutuellement exclusifs. La plage porte sur le **texte que
`read` produit déjà** pour l'unité sélectionnée (donc combinable avec `selector` : « lignes
500-800 de la page 3 ») — pas sur un flux global fabriqué, ce qui garde la sémantique
déterministe. Chaque appel reste plafonné à `MIAOU_DOCS_READ_CAP` : la plage déplace une
fenêtre glissante (la notice annonce l'offset suivant), elle ne lève pas le cap. Sert à lire
une unité volumineuse (grande page/section) au-delà de 20k caractères en plusieurs appels.
Formats concernés : pdf, docx, pptx, membre zip texte brut (et membre imbriqué structuré,
plage transmise au dispatch récursif). **xlsx exclu** (grille sans flux texte naturel :
rejet explicite, garder son selector `Feuille!A1:C10`). Sur un pdf/pptx la plage porte sur
le body rendu, en-tête `--- Page N ---`/`--- Slide N ---` compris.

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
Un PDF sans texte extractible (scan) renvoie une note explicite, pas d'OCR en v1. Un
attachement texte brut (kind `inconnu`, hors pdf/docx/xlsx/pptx/zip) ne transite jamais
par `mcp_docs` : MIAOU inline ce type de fichier côté client.

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
uv run servers/mcp_ddg.py                            # HTTP 127.0.0.1:8769
BRAVE_API_KEY=<key> uv run servers/mcp_brave.py      # HTTP 127.0.0.1:8770

# mcp_web et mcp_docs sont des packages (pas des scripts plats) — lancement différent :
uv run --directory servers python -m mcp_web         # HTTP 127.0.0.1:8768
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
python servers/mcp_ddg.py [options]
BRAVE_API_KEY=<key> python servers/mcp_brave.py [options]
python -m mcp_web [options]     # depuis servers/ (package, pas un script plat)
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

Tout appel réseau ou bloquant à l'intérieur d'un outil `async` (urllib dans
weather/ddg/brave/web, résolution DNS dans bench, parsing de document dans
`mcp_docs`) est enveloppé dans `asyncio.to_thread(...)` — un outil async qui appelle
directement une fonction bloquante gèlerait l'event loop pendant tout le round-trip,
sérialisant les appels concurrents. Isoler l'I/O bloquante dans une fonction
synchrone dédiée (ex. `_fetch_bytes`, `_fetch_ddg_html`, `_fetch_brave_bytes`) puis
l'appeler via `await asyncio.to_thread(...)` est le pattern à suivre pour tout
nouvel outil qui ferait de l'I/O.

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
Les tests de mcp_web monkeypatchent `mcp_web.cache.WORKDIR` (fixture `tmp_path`, autouse)
pour la même raison ; `tests/test_web_structure.py` exerce `mcp_web.structure.extract_structure`
en isolation (pas de HTTP, pas de cache) sur des fragments HTML construits à la main.

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
- **Format de `ref`** : whitelist de préfixe (`session.py:_REF_RE`) — `att-<N>` (pièce
  jointe de message), `file-<id>` (fichier de bibliothèque d'espace, MIAOU lot Cbis) ou
  `res_<id>` (ressource de session côté client, MIAOU lot K, id en base36 après un
  underscore — pas un tiret). `ref` reste une clé opaque : aucune des trois familles n'est
  parsée pour un index ou un chemin, la détection de type reste par magic bytes. Un ref
  hors de ces trois formes est rejeté par `validate_ref` (« ref invalide : … (attendu
  att-<N>, file-<id> ou res_…) »).

## Posture sécurité

CORS ouvert (`allow_origins=["*"]`), pas d'auth, réseau local uniquement. Délibéré :
c'est du banc d'essai. En production, ces serveurs seraient derrière un proxy
(Caddy, nginx) qui porte les tokens côté serveur — cf. brief D6 de MIAOU.

## Ajouter un outil

Décorer une fonction avec `@self.mcp.tool()` dans le `__init__` du serveur concerné.
FastMCP génère le schéma JSON automatiquement depuis la signature Python et la docstring.
Aucune déclaration manuelle dans un registre.

Chaque serveur appelle `self.finalize_tools()` (défini dans `mcp_base.py`) en dernière
ligne de son `__init__`, après l'enregistrement de tous les outils — à conserver en
ajoutant un outil ou un serveur. Cet appel normalise ce que `tools/list` expose :
`inspect.cleandoc` sur les descriptions (une docstring assignée via
`func.__doc__ = f"""..."""` partirait sinon sur le wire avec l'indentation source de
chaque ligne de continuation), et suppression des clés `"title"` auto-générées par
Pydantic dans les schémas de paramètres (bruit pur, le payload `tools/list` est renvoyé
au modèle à chaque requête).

Les descriptions d'outils sont volontairement compactes, mais chaque garantie
comportementale qui y reste est contractuelle (valeurs de caps interpolées, « la plage
déplace la fenêtre, ne lève pas le cap », exclusivité char/ligne, exclusion xlsx de la
pagination, labels de `search` réutilisables comme selectors, niveau unique d'imbrication
zip) : ne pas les couper pour gagner des tokens, le modèle appelant les prend au pied de
la lettre.

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
