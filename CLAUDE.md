# CLAUDE.md — miaou-mcp-servers

Instructions pour travailler dans ce dépôt.

## Ce qu'est le projet

Six serveurs MCP de développement (bench, weather, web, ddg, brave, docs) et un serveur
proxy qui les agrège, extraits du dépôt [MIAOU](https://github.com/dJeyL/miaou), un client de
chat web pour API OpenAI-compatible (single-file HTML). Ils servent à tester l'agrégation
MCP de MIAOU : connexion, invocation d'outils, rendu des résultats non-text.

Ils n'ont pas de rôle en production — ce sont des outils de banc d'essai, destinés à
tourner localement pendant le développement de MIAOU.

## Structure du projet

```
miaou-mcp-servers/
├── mcp_proxy.py          # serveur proxy (racine, point d'entrée principal)
├── dev_auth_server.py    # serveur d'autorisation OAuth de DÉVELOPPEMENT (jamais en prod)
├── servers/
│   ├── mcp_base.py       # classe de base partagée (MiaouMCPBase + make_opener)
│   ├── mcp_bench.py      # banc d'essai général (port 8766)
│   ├── mcp_weather.py    # météo réelle via wttr.in (port 8767)
│   ├── mcp_web/          # téléchargement d'URL (port 8768), package (voir plus bas)
│   ├── mcp_ddg.py        # recherche DuckDuckGo HTML (port 8769)
│   ├── mcp_brave.py      # recherche Brave Search API (port 8770)
│   └── mcp_docs/         # extraction PDF/Office/Zip (port 8771), package — OBSOLÈTE, désactivé par défaut
├── tests/
│   ├── live_call.py      # appel manuel d'un outil sur un serveur lancé (non collecté)
│   ├── test_base.py
│   ├── test_bench.py
│   ├── test_weather.py
│   ├── test_web.py
│   ├── test_web_structure.py
│   ├── test_ddg.py
│   ├── test_brave.py
│   ├── test_docs.py
│   ├── test_proxy.py
│   ├── test_proxy_auth.py  # auth OAuth entrante (lot AB-1)
│   ├── test_proxy_outbound_auth.py  # auth OAuth sortante (lot AB-2)
│   └── test_dev_auth_server.py  # serveur d'autorisation de développement (lot AB-1.3)
├── config.sample.json    # template de config pour le proxy
├── config.json           # (gitignored) config active du proxy
├── requirements.txt      # pour les utilisateurs sans uv
├── pyproject.toml        # métadonnées projet + config pytest (asyncio_mode=auto) + groupe dev
├── uv.lock               # lock uv, versionné
└── .gitignore
```

## Les serveurs

### `servers/mcp_bench.py` — banc d'essai général (port 8766)

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

### `servers/mcp_weather.py` — météo réelle (port 8767)

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

Deux outils. Requièrent une clef d'API, résolue par `resolve_api_key()` dans cet
ordre : clé `api_key` du bloc `config` de l'entrée `config.json` (mode inprocess),
sinon `BRAVE_API_KEY` dans l'environnement. Le bloc `config` prime pour permettre
plusieurs entrées du même module avec des clefs différentes — `os.environ` est
partagé par tout le process et ne peut pas les distinguer (cf. pattern
`build(config)`). Une clef vide ou blanche compte comme absente.

**Refus d'initialisation sans clef.** Le serveur ne s'initialise pas sans clef,
plutôt que d'exposer deux outils qui échoueraient à chaque appel : `build(config)`
lève `MissingAPIKeyError`, et le lancement standalone sort en code 1 avec un message
clair. Côté proxy, l'upstream est signalé au démarrage puis **retiré de la table de
routage** — les autres serveurs démarrent normalement et `tools/list` ne contient
alors aucun outil `brave__*`. Le singleton `server`/`mcp` du module reste construit
sans clef (`require_api_key=False`) : `import mcp_brave` doit rester possible sans
clef, sinon un simple import casserait. Clef présente mais invalide → toujours un
message d'erreur clair par appel (401), sans stack trace.

Les descriptions d'outils exposées aux clients MCP ne mentionnent pas la clef d'API :
c'est une affaire d'exploitation, pas une information actionnable pour le modèle
appelant, qui ne peut rien en faire — un outil visible est un outil configuré.

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

> **Obsolète, désactivé par défaut.** MIAOU ouvre désormais ces cinq formats
> lui-même (zip, PDF, Excel, Word, PowerPoint), sans serveur. Ce serveur reste en
> place et intact pour un seul usage, qui reste réel : le travail **hors
> connexion**, l'ouverture native de MIAOU téléchargeant ses moteurs depuis un CDN.
> `config.sample.json` porte `_disabled: true` sur son entrée `docs` ; le retirer
> suffit à le réveiller. Rien n'a été supprimé ici — ne pas « faire le ménage »
> dans ce package au motif qu'il ne sert plus par défaut. Détail et raisons dans
> le README (section « `mcp_docs` : obsolète, mais conservé pour le
> hors-connexion »).

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

Le balayage d'archive sans `path` est borné par deux budgets **globaux**, en plus du garde
par membre (`MIAOU_DOCS_MAX_UNZIP_MB`) : un cumul décompressé (`MIAOU_DOCS_MAX_UNZIP_MB`
réutilisé comme budget **total** du balayage) et un temps d'exécution
(`MIAOU_DOCS_SCAN_TIMEOUT_S`, défaut 30 s) — sans eux, une archive de N membres chacun
juste sous la limite individuelle reste balayée en entier (zip-bomb par accumulation).
Dépassement = arrêt **propre**, pas une erreur : les résultats déjà trouvés sont renvoyés,
et la note finale des zones aveugles nomme le budget dépassé et **tous** les membres non
couverts. `read`/`extract` ciblés (un seul membre) ne sont pas concernés : leur garde par
membre suffit.

**Locales des headings docx** — la granularité `search`/`list`/`read` par section repose sur
le **nom d'affichage** du style de paragraphe (`_HEADING_RE` dans `formats.py`), pas sur le
`styleId` OOXML `Heading1` (invariant par locale, mais absent d'un style créé à la main ou
hérité d'un gabarit localisé). Locales reconnues : anglais (`Heading N`), français
(`Titre N`), allemand (`Überschrift N`), espagnol (`Título N`), italien (`Titolo N`). Le
motif est ancré et exige le numéro — en fr/es/it, le style *Title* (distinct de *Heading 1*)
s'appelle `Titre`/`Título`/`Titolo` **sans** numéro et n'est donc pas pris pour un heading.
Un docx dans une autre locale, ou dont les styles ont été renommés, est traité comme sans
structure de heading (labels `(préambule)`/`(corps)`) : limitation documentée, pas un bug.

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
| `MIAOU_DOCS_MAX_UNZIP_MB` | `100` | Taille décompressée max d'une archive (garde header + flux), et budget cumulé total d'un balayage `search` |
| `MIAOU_DOCS_SCAN_TIMEOUT_S` | `30` | Budget de temps d'un balayage `search` multi-membres (arrêt propre, zones aveugles notées) |
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

Trois types d'upstream supportés :
- **inprocess** : import Python direct, pas de subprocess (défaut pour tous les serveurs)
- **stdio** : subprocess externe communiquant via stdin/stdout
- **http** : serveur MCP distant en streamable-http (lot AB-2.1)

Les entrées inprocess acceptent un champ `env` pour injecter des variables d'environnement
avant l'import du module (`os.environ.setdefault`). `mcp_brave` lit sa clef en priorité
dans le bloc `config` de son entrée (voir sa section), et retombe sur `BRAVE_API_KEY`
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

## Transport et configuration MIAOU

Les trois serveurs utilisent **streamable-http** (JSON-RPC 2.0, endpoint unique POST `/mcp`,
réponses JSON ou SSE `event:message`/`data:`). C'est le transport implémenté par MIAOU V2.

Pour connecter les serveurs depuis MIAOU → Paramètres → Serveurs MCP :

| Champ | bench | weather | web | ddg | brave | docs | proxy |
|---|---|---|---|---|---|---|---|
| Nom | `bench` | `weather` | `web` | `duckduckgo` | `brave` | `docs` | `proxy` |
| URL | `:8766/mcp` | `:8767/mcp` | `:8768/mcp` | `:8769/mcp` | `:8770/mcp` | `:8771/mcp` | `:8765/mcp` |
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
uv run servers/mcp_bench.py                          # HTTP 127.0.0.1:8766
uv run servers/mcp_bench.py --transport stdio        # mode stdio
uv run servers/mcp_bench.py --host 0.0.0.0           # toutes interfaces

uv run servers/mcp_weather.py                        # HTTP 127.0.0.1:8767
uv run servers/mcp_ddg.py                            # HTTP 127.0.0.1:8769
BRAVE_API_KEY=<key> uv run servers/mcp_brave.py      # HTTP 127.0.0.1:8770

# mcp_web et mcp_docs sont des packages (pas des scripts plats) — lancement différent :
uv run --directory servers python -m mcp_web         # HTTP 127.0.0.1:8768
uv run --directory servers python -m mcp_docs        # HTTP 127.0.0.1:8771

# Proxy (agrège tout sur un seul port)
cp config.sample.json config.json     # puis éditer config.json (BRAVE_API_KEY, etc.)
uv run mcp_proxy.py                   # port défini dans config.json
uv run mcp_proxy.py --port 8765       # override port
uv run mcp_proxy.py --config autre.json
uv run mcp_proxy.py --proxy 10.0.0.1:3128   # force le proxy réseau vu par les upstreams
uv run mcp_proxy.py --noproxy               # force l'absence de proxy vu par les upstreams
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

### Auth OAuth entrante (lot AB-1)

Le proxy sait exiger une autorisation OAuth 2.1 de ses clients. **Désactivée par
défaut** : sans clé `auth` dans `config.json`, le comportement est celui d'avant le
lot, à l'octet près — la boucle de développement locale (MIAOU ↔ proxy sans jeton) ne
doit jamais se mettre à exiger une autorisation.

Le proxy est un **Resource Server** : il vérifie des jetons, il n'en émet jamais.
L'émission appartient à un Authorization Server distinct. La révision 2025-03-26 de la
spec MCP faisait du serveur MCP son propre AS ; c'est abandonné depuis, ne pas le
réintroduire par commodité.

L'essentiel vient du SDK (`mcp.server.auth`) : `create_protected_resource_routes` sert
les métadonnées RFC 9728, `RequireAuthMiddleware` émet le 401. Ce que le SDK ne fait
PAS est à nous, et chaque point a son test :

- **`resource_metadata` dans le `WWW-Authenticate`** : le SDK ne l'ajoute que si on lui
  passe `resource_metadata_url`, et cette branche y porte un `# pragma: no cover`. Sans
  ce pointeur, le client reçoit un 401 nu et ne sait pas où aller — c'est exactement le
  symptôme qui a motivé le lot.
- **Les routes `/.well-known/*` restent publiques**, seul `/mcp` est protégé. Les
  englober fermerait la boucle : le client ne pourrait jamais apprendre où
  s'authentifier.
- **`WWW-Authenticate` dans `expose_headers`** (CORS), sinon un client navigateur
  cross-origin ne peut pas *lire* le header. Invisible en test `curl`, fatal en usage
  réel. `allow_credentials` reste **absent** : le combo avec `allow_origins=["*"]` est
  interdit par la spec CORS, et c'est ce qui fait accepter l'`Origin: null` de MIAOU
  ouvert en `file://`.
- **La validation d'audience (RFC 8707)** — `JwtTokenVerifier` (lot AB-1.2). C'est le
  point le plus important, et le SDK ne le couvre pas du tout : `BearerAuthBackend`
  appelle le verifier puis re-vérifie seulement `expires_at`, il ne regarde **jamais**
  `AccessToken.resource`. Sans elle, un jeton parfaitement valide émis pour un AUTRE
  Resource Server serait accepté — la *confused deputy* que RFC 8707 existe pour
  empêcher, et le défaut le plus fréquent des implémentations MCP. Elle n'est donc pas
  désactivable : c'est la raison d'être du mode auth.

`resolve_auth_config()` (pur, testable) normalise la clé `auth` et arbitre avec
`--auth`/`--no-auth`. `build_token_verifier()` en dérive le `JwtTokenVerifier`.

**Ce que vérifie `JwtTokenVerifier`**, dans cet ordre : signature (clef publique tirée
du JWKS de l'émetteur), `iss`, `exp` — exigé, un jeton sans expiration est refusé plutôt
qu'éternel — puis l'audience. La comparaison d'audience est **exacte** (`_audience_matches`,
pure) : ni préfixe ni sous-chaîne, sinon une audience `…/mcp-attacker` passerait pour
`…/mcp`. Un `aud` absent est un refus (un jeton sans audience est utilisable partout).
Seul le slash final est normalisé, les AS ne s'accordant pas dessus. `aud` accepte la
forme chaîne comme la forme liste (RFC 7519 §4.1.3).

Trois propriétés à ne pas défaire :

- **`verify_token` ne lève jamais.** Tout échec renvoie `None` — une exception
  remonterait en 500 alors qu'un jeton invalide est un 401. Le refus est journalisé avec
  sa cause : sans trace, un jeton rejeté à tort est indébuggable depuis le client, qui ne
  voit qu'un 401 nu.
- **La liste d'algorithmes est fermée** (défaut `["RS256"]`), jamais déduite de l'en-tête
  du jeton — sinon `alg: none` passerait, l'attaque classique sur les vérificateurs JWT.
- **Le JWKS est récupéré au premier jeton, pas au démarrage**, et un émetteur injoignable
  donne un refus, pas un crash : un proxy qui refuse de démarrer parce que son AS dort
  serait un mauvais compromis. `PyJWKClient` met les clefs en cache — pas un appel réseau
  par requête. Le `jwks_uri` vient de la config, sinon il est découvert auprès de
  l'émetteur (`_discover_jwks_uri` sonde RFC 8414 puis OpenID Connect Discovery : un AS
  OIDC ne sert souvent que le second). La récupération est bloquante (urllib), donc passée
  par `asyncio.to_thread` — pattern du dépôt pour toute I/O en contexte async.

`pyjwt[crypto]` est désormais importé directement, donc déclaré explicitement aux **trois**
endroits qui gouvernent un environnement : le bloc PEP 723 de `mcp_proxy.py` (celui qui
compte en mode inprocess), `requirements.txt` et `pyproject.toml`.

### `dev_auth_server.py` — Authorization Server de développement (lot AB-1.3)

**Jamais en production**, et l'avertissement est dans son docstring, sa bannière de
démarrage et le README. Il n'authentifie personne : ni compte, ni mot de passe, ni
session. L'écran de consentement demande un accord, pas une identité.

Sa raison d'être : le proxy sait dire où s'authentifier (AB-1.1) et vérifier un jeton
(AB-1.2), mais sans AS on ne peut éprouver ni le parcours complet à la main, ni écrire
la fixture Playwright d'AB-3. Contre un serveur tiers réel, l'AS existe déjà — ce
composant n'est pas un morceau du produit, c'est un banc d'essai.

**Deux origines, quel que soit le nombre de process.** Le proxy est un Resource
Server, jamais un Authorization Server (rev. 2025-06-18 de la spec ; la rev.
2025-03-26 mélangeait les deux, c'est abandonné).

`mcp_proxy --with-dev-auth [PORT]` lance les deux dans **un seul process**
(`run_with_dev_auth`), par confort de banc d'essai — mais sur **deux ports**, donc
deux origines distinctes : l'`issuer_url` reste une identité propre et non une
sous-route du proxy. C'est ce qui fait de ce mode une commodité d'exécution et non
une fusion. **Ne pas glisser vers un montage de l'app de l'AS dans celle du proxy**,
qui effacerait la frontière au lieu de la préserver.

Deux détails du mode combiné valent d'être connus : l'AS suit le proxy à l'arrêt
(sans ce couplage, un émetteur de jetons survivrait à la ressource qu'il autorise),
et `KeyboardInterrupt` est avalé explicitement — `uvicorn.run()` le fait pour nous,
`asyncio.run()` non, et Ctrl-C afficherait une trace à chaque arrêt. `--with-dev-auth`
est incompatible avec `--no-auth` (lancer un émetteur puis n'exiger aucun jeton), et
n'écrase jamais une clé `auth` explicite de la config.

Ce que le SDK apporte, et qu'il ne faut pas réimplémenter : les routes `/authorize`,
`/token`, `/register`, `/revoke`, la validation des requêtes, et **PKCE** — il n'accepte
que `S256` (typé `Literal["S256"]`) et compare lui-même le `code_verifier`. Des tests
l'épinglent quand même : c'est une garantie dont on dépend.

Trois choses sont à nous, chacune pour une raison précise :

- **La route `/jwks` et notre propre métadonnée.** Le modèle `OAuthMetadata` du SDK n'a
  **pas** de champ `jwks_uri` — sans notre route, le proxy ne pourrait jamais découvrir
  les clefs publiques, et toute la validation d'audience resterait inatteignable. Notre
  route est posée **avant** celle du SDK : Starlette retient la première qui matche, et
  écraser explicitement vaut mieux que laisser deux routes se disputer un chemin selon
  leur ordre de déclaration.
- **`aud` émis depuis le paramètre `resource`** (RFC 8707) de la requête d'autorisation.
  C'est le point qui relie cet AS à la validation d'audience du proxy : sans lui, le
  jeton serait valide mais destiné à personne, et refusé — à raison.
- **`ClientRegistrationOptions(enabled=True)`** : le défaut du SDK est `False`, la DCR ne
  s'active pas toute seule, et c'est précisément ce qu'on veut éprouver.

**L'audience survit au refresh.** Le jeton rafraîchi porte la même `aud` que le
jeton initial, sans quoi il serait refusé par le proxy — à raison — dès la
première expiration, et le parcours réel dure plus longtemps qu'un TTL. La
valeur est reportée depuis l'**émission** (table `refresh_resources`, indexée par
jeton), pas lue dans la requête de refresh, pour deux raisons. Le SDK ne nous la
donne pas : son handler parse bien `resource` (`RefreshTokenRequest` le déclare)
mais appelle `exchange_refresh_token(client, refresh_token, scopes)` sans le
transmettre, et son modèle `RefreshToken` n'a pas de champ pour le porter —
contrairement à `AuthorizationCode`. Et c'est plus sûr : un client qui
demanderait au refresh une ressource différente de celle du code initial
obtiendrait sinon une élévation silencieuse.

Clefs RSA **générées à chaque démarrage**, stockage **en mémoire** : redémarrer invalide
tout ce qui a été émis. C'est un choix, pas un raccourci — une clef privée de
développement qui traînerait sur disque finirait réutilisée ailleurs.

`--auto-approve` saute l'écran de consentement, pour les tests automatisés qui ne peuvent
pas cliquer. C'est le **seul** écart entre le mode manipulable au navigateur et la
fixture déterministe : tout le reste est partagé, pour que la fixture éprouve exactement
ce qu'on éprouve à la main.

### Auth OAuth sortante (lot AB-2) — stockage des jetons

Symétrique de l'auth entrante, et sans rapport avec elle : là, le proxy *vérifie*
les jetons de ses clients ; ici, il en *obtient* auprès de serveurs tiers, et les
détient **à la place de MIAOU** (qui tourne en `file://` et ne peut pas être un
client OAuth : pas de redirect URI, refresh tokens en `localStorage`, refresh
concurrents multi-onglets). Les deux cohabitent sans se connaître.

Le parcours lui-même vient du SDK (`OAuthClientProvider`, un `httpx.Auth` :
découverte, DCR, PKCE S256, échange de code, refresh). Ce qui est à nous est le
**stockage**, parce qu'il touche au disque et aux secrets — `UpstreamTokenStorage`
implémente le protocol `mcp.client.auth.TokenStorage`.

Un fichier **distinct de `config.json`** (`<config>-tokens.json` par défaut,
ajouté au `.gitignore`) : `config.json` est ouvert et édité à la main, un refresh
token n'y a rien à faire. Un seul fichier pour tous les upstreams, une entrée par
nom, relu à chaque écriture pour ne pas écraser l'entrée d'un voisin.

Deux gardes d'écriture, chacune pour sa raison (`_write_secret_file`) :
permissions `0600` posées **à la création** (`os.open`, pas un `chmod` après coup
— entre le write et le chmod, le refresh token est lisible par tous), et écriture
**atomique** (fichier temporaire dans le même répertoire puis `os.replace`) : une
écriture interrompue laisserait sinon un fichier tronqué, ce qui coûterait une
ré-autorisation manuelle de **tous** les upstreams.

**`expires_in` est une durée, pas une date** — la relire telle quelle après un
redémarrage la ferait courir à nouveau depuis maintenant. Le stockage persiste
donc l'instant absolu (`expires_at`) et recalcule la durée restante au
chargement. Ce n'est pas un raffinement : le SDK charge les jetons dans
`_initialize()` **sans** repasser par `update_token_expiry()`, donc
`token_expiry_time` reste `None` et `is_token_valid()` rendrait `True` pour un
jeton expiré depuis des heures — le proxy l'enverrait, prendrait un 401, et
repartirait dans un parcours interactif au lieu de rafraîchir.

**Credentials pré-provisionnés** (`build_client_info_override`) : pour un AS qui
ne fait pas d'enregistrement dynamique, `client_id`/`client_secret` viennent de
la config. Aucune branche à ajouter au SDK — celui-ci ne fait de DCR que
`if not self.context.client_info:`, alimenté par `get_client_info()`, donc rendre
l'override suffit à la court-circuiter. La config **gagne** sur un enregistrement
mémorisé (c'est l'intention explicite de l'utilisateur), mais l'enregistrement
reste écrit, pour qu'on y retombe si l'override disparaît de la config.

#### Le parcours d'autorisation

Le parcours vit dans **deux routes publiques**, `/authorize/{name}` (déclencher)
et `/callback` (recevoir le code). Publiques à dessein : seul `/mcp` est
enveloppé par `RequireAuthMiddleware`, et le navigateur qui revient d'un AS tiers
ne porte aucun jeton du proxy — exiger le nôtre ici fermerait la boucle, le même
piège que celui déjà payé sur les routes `/.well-known` en AB-1.

**Le démarrage ne déclenche jamais de parcours interactif.** `UpstreamAuthorizer`
porte un drapeau `interactive`, faux par défaut : au démarrage, un upstream sans
jeton lève `AuthorizationRequired` au lieu d'attendre. C'est structurel, pas
prudentiel — le `start()` d'un upstream tourne dans le lifespan, **avant**
qu'uvicorn n'ouvre le port : y attendre un clic sur `/callback` est un
interblocage franc (le proxy attend une redirection vers une route qu'il ne sert
pas encore, donc le port n'ouvre jamais, donc le clic ne peut pas aboutir).
Le drapeau n'est levé que par `authorize()`, appelée depuis `/authorize/{name}`,
port déjà ouvert.

**Un upstream « pas encore autorisé » n'est PAS retiré de la table de routage**,
contrairement à un upstream en panne. C'est l'unique exception à la règle du
lifespan, et elle a sa raison : une panne ne se répare pas toute seule, une
autorisation manquante se répare par un clic — et retirer l'upstream rendrait
`/authorize/{name}` incapable de le retrouver.

Le lien d'autorisation est **imprimé sur stderr**, copiable. C'est le mécanisme
de référence, pas un repli : `mcp_proxy` est une CLI, et confier l'ouverture à
l'OS ne garantit ni le bon navigateur ni le bon **profil** (celui où la session
tierce est ouverte). `--open` reste disponible en confort explicite.

#### Scopes : qui décide, et le 403 qui n'est pas une panne

**La clé `scope` d'un upstream ne fait pas ce qu'elle promet.** Le client du SDK
applique la stratégie de la spec MCP (`get_client_metadata_scopes`) : il prend
d'abord le scope annoncé dans le `WWW-Authenticate`, sinon **tous** les
`scopes_supported` des métadonnées, et ne retombe sur celui de la configuration
que si aucun des deux n'existe. Configurer `"scope": "mcp:read"` face à un AS qui
annonce `mcp:read mcp:write` donne donc un jeton portant les deux. Ce n'est pas
un bug — c'est la spec — mais c'est contre-intuitif, et vérifié en réel.

Côté entrant, `required_scopes` est comparé par `RequireAuthMiddleware` aux
scopes du jeton, en **conjonction** : tous doivent être présents, et le premier
manquant produit un **403 `insufficient_scope`** nommant le scope attendu. La
chaîne complète — claim `scope` du JWT → `AccessToken.scopes` (notre
`verify_token`, qui découpe la chaîne séparée par des espaces de la RFC 6749
§3.3, et accepte aussi la forme liste que certains AS émettent) →
`AuthCredentials` → comparaison du middleware — est couverte de bout en bout par
`tests/test_proxy_auth.py`. Chaque maillon pouvait être juste isolément avec le
raccord faux : c'est exactement ce qu'un test unitaire de chaque côté ne voit
pas.

**Un 403 n'est pas une autorisation manquante.** Le jeton a bien été obtenu ; le
serveur le refuse pour scopes insuffisants. Relancer le parcours ne répare rien —
c'est la configuration qui est en cause. `status` le dit explicitement
(`last_error` de l'authorizer), sans quoi un scope mal configuré se présente
exactement comme un upstream jamais autorisé et l'exploitant reclique
indéfiniment sur un lien qui ne peut pas l'aider.

#### Le troisième état : « connu mais pas autorisé »

Un upstream OAuth sans jeton n'est ni disponible ni en panne. Ses outils
**restent listés** et **refusent** à l'invocation, au lieu de disparaître
silencieusement de `tools/list` — un outil absent ne donne au modèle aucune
piste, un outil qui refuse en donne une actionnable.

Le prédicat est unique : `upstream_is_live()`, partagé par le listing, le refus
d'appel, le rapport de `status` et le `_meta` de `tools/list` — des endroits qui
doivent répondre la même chose, sous peine d'annoncer un outil qu'on refuse
ensuite pour une raison qu'on ne rapporte pas. Un prédicat n'est unique que si
tous ses consommateurs y passent : ajouter une surface, c'est y brancher un
consommateur de plus, jamais réécrire le filtre sur place.

**Cache d'outils** (`ToolCatalogCache`, `<config>-tools.json`) : sans lui, un
upstream non autorisé serait muet, `tools/list` répondant 401 avant de rien
dire. Il couvre aussi le redémarrage du proxy. Les outils resservis depuis le
cache portent une **marque explicite** dans leur description
(`format_stale_description`), avec la date de dernière connaissance : présenter
une liste périmée comme vivante serait mentir au modèle, qui n'a aucun autre
moyen de le savoir. La date est **absolue** — un texte qui changerait à chaque
tour invaliderait le cache KV du modèle.

**Contrat d'erreur `AUTHORIZATION_REQUIRED`** — surface de contact avec MIAOU
(lot AB-3), sur le motif déjà éprouvé de `REF_UNKNOWN` : le code
applicatif voyage dans `error.data.code` d'une vraie erreur JSON-RPC (`code` au
niveau de l'erreur reste l'entier protocolaire), accompagné de `upstream` et
`authorization_url`. **Le client teste par ÉGALITÉ de constante, jamais par
sous-chaîne** dans le message — un message est de la prose, il se reformule.

`authorization_url` porte un **chemin relatif** (`/authorize/{name}`), pas une
URL absolue : cf. « Où l'on autorise » ci-dessous. Le nom du champ est conservé
malgré le changement de forme — c'est le contrat publié, le renommer casserait
davantage.

Détail d'implémentation non négociable, trouvé à l'exécution : le refus est levé
comme exception par le handler d'outil, mais le SDK pose un `except Exception`
**à l'intérieur** du handler qu'on enveloppe. Aucun wrapper externe ne peut donc
l'intercepter. Le refus voyage par un **sentinel interne** dans le texte du
résultat (`_AUTHORIZATION_SENTINEL`, retiré avant que le message n'atteigne le
client), que `_wrap_authorization_required` reconnaît — même mécanique que
`_wrap_ref_unknown_sentinel`. Les deux wrappers restent **séparés** : l'un
inspecte un résultat après exécution, l'autre un refus posé avant tout appel ;
les fondre imposerait un mécanisme qui fait les deux mal.

#### Où l'on autorise, et à qui on le dit (lot AB-4)

**`authorize_path(name)` est la source unique du chemin d'autorisation**
(`/authorize/{name}`). Ses trois consommateurs — le contrat d'erreur, le rapport
`status` et le `_meta` de `tools/list` — passent tous par elle : recomposer la
chaîne ailleurs laisserait deux natures de lien pour une même action. Un test
(`test_authorize_path_matches_the_route_actually_served`) la fait matcher contre
l'objet `Route` réellement servi, pour qu'un renommage de route casse un test
plutôt que le parcours.

**Le chemin est relatif, jamais absolu.** Le proxy ne connaît que son adresse
d'écoute (`build_callback_url` replie `0.0.0.0` sur `127.0.0.1`) : derrière un
reverse proxy, une URL absolue serait injoignable. Composer l'origine appartient
au client, seul à savoir comment il joint réellement le proxy.

**Ne pas publier `last_authorization_url` comme cible d'action.** Elle est
peuplée dès le démarrage à froid — la branche non interactive de `_on_redirect`
la mémorise avant de lever — mais elle porte le `state` et le PKCE challenge
d'une transaction que le provider a abandonnée : la suivre mène à `/callback`
sans `pending`, donc à une `RuntimeError`. Bonne à afficher en diagnostic,
mauvaise à suivre. Elle reste stockée pour cette seule raison.

**Deux publics, deux canaux, et ils ne disent pas la même chose.** La
description marquée et le rapport `status` s'adressent au **modèle**, qui ne peut
ni ouvrir un lien ni résoudre un chemin relatif : ils nomment donc l'utilisateur
comme seul capable d'autoriser, et citent le chemin pour qu'il puisse le lui
transmettre — jamais une adresse présentée comme ouvrable par lui. Le `_meta` de
`tools/list` s'adresse au **client**, qui lui peut composer l'origine et rendre
une affordance. Ne pas fondre les deux : un champ pour deux destinataires ment à
l'un des deux.

**`_meta` sur `tools/list`** (`UNAUTHORIZED_UPSTREAMS_META_KEY`,
`miaou/unauthorized_upstreams`) — contrat lu par MIAOU (lot AB-5) :

```json
{"miaou/unauthorized_upstreams": [
  {"name": "jira", "authorize_path": "/authorize/jira"}
]}
```

Une **liste** dès la première version (N upstreams d'un même proxy peuvent être
non autorisés en même temps). **Clé absente**, jamais liste vide, quand il n'y a
rien à signaler. Préfixe `miaou/` délibéré : `_meta` est un espace partagé, une
clé nue collisionnerait avec une extension du SDK ou d'un autre agrégateur.

Un upstream non vivant **sans authorizer** n'y figure pas : il est injoignable,
pas non autorisé, n'a aucun parcours à proposer, et le publier enverrait le
client sur un `/authorize/{name}` qui répond 404.

**Piège de construction.** `types.ListToolsResult(meta={...})` sérialise la clé
en `meta`, pas `_meta` — pydantic ne sérialise sous l'alias que si le champ a
été peuplé PAR l'alias. La forme correcte est `**{"_meta": {...}}`. La version
actuelle du SDK refuse `meta=` d'un `TypeError`, mais **la propriété à garder
n'est pas ce refus** : c'est que la clé arrive sur le fil en `_meta`. Le test
assertionne donc sur la **chaîne JSON émise**, jamais sur l'objet Python —
`result.meta` rend la même chose dans les deux cas, donc un test sur l'objet
passerait aussi bien sur une sortie invalide.

`handle_list_tools` rend un `ListToolsResult` (style nouveau) et non une
`list[types.Tool]` : le SDK enveloppe un retour de style ancien **sans `_meta`**,
il n'existe aucun moyen d'en porter un sans migrer. Le dispatch se fait sur la
**signature** du handler (`create_call_wrapper`), pas sur son type de retour.

**Outil `status`** — nom **nu**, sans préfixe : MIAOU préfixe déjà par le nom de
la carte serveur, donc `proxy__status` donnerait `miaou-proxy__proxy__status`.
Conséquence à ne pas rater — la table de routage résout tout par préfixe
(`_resolve_via_prefix`), donc ce nom est un **cas particulier explicite** dans
`handle_call_tool`, sinon l'appel part chercher un upstream nommé « status ».
Il n'est exposé que si l'auth sortante est configurée.

#### `HttpUpstream` : le transport vit dans sa propre tâche

Contrainte anyio, payée trois fois avant d'être comprise. Les contextes
asynchrones du SDK (`streamablehttp_client`, `ClientSession`) portent des cancel
scopes qu'anyio **interdit** d'ouvrir dans une tâche et de refermer dans une
autre. Une `AsyncExitStack` ouverte par `start()` et refermée par `stop()` fait
exactement ce croisement dès que les deux ne tournent pas dans la même tâche —
ce qui est le cas ici (démarrage dans le lifespan, arrêt ailleurs,
ré-autorisation dans une tâche de fond).

D'où le patron : une **tâche de service** (`_serve`) ouvre le transport, signale
qu'elle est prête, attend l'ordre d'arrêt, puis referme au même endroit. Elle est
hébergée par le task group **du lifespan** (`host_tasks_in`), jamais par un task
group créé dans `start()` : le scope appartiendrait alors à la tâche appelante,
qui peut être éphémère (une requête `/authorize/{name}`), et resterait ouvert
dans une tâche morte.

Le symptôme, quand on s'y prend mal, est toujours le même et c'est ce qui rend le
piège coûteux : `Attempted to exit cancel scope in a different task…`
**remplace** la cause réelle. Un simple « autorisation requise » est ainsi
remonté trois fois illisible jusqu'au log. Même raison pour
`_unwrap_exception_group` : anyio enveloppe ce qui sort d'un task group, et
laisser l'enveloppe remonter donnerait `unhandled errors in a TaskGroup` en guise
de diagnostic.

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

## Vérification TLS : magasin de confiance système (truststore)

Un upstream HTTPS dont le certificat est signé par une **AC d'entreprise interne**
échouait en `CERTIFICATE_VERIFY_FAILED` (« certificate is not trusted »), alors que le
même hôte s'ouvre sans erreur dans un navigateur : l'AC est bien installée, mais dans le
magasin du **système** (schannel sous Windows, Keychain sous macOS, ca-certificates sous
Linux), que Python ne consulte pas — il s'en tient au bundle CA figé de certifi/OpenSSL.
Côté proxy, le symptôme est un upstream marqué `unavailable` au démarrage dont les outils
disparaissent de `tools/list`.

`enable_system_trust_store()` (`servers/mcp_base.py`) appelle
`truststore.inject_into_ssl()`, qui **remplace la classe `ssl.SSLContext` elle-même**.
C'est ce qui rend cet appel unique suffisant pour toute la sortie HTTPS du process,
quelle que soit la bibliothèque : urllib (`make_opener()`, dans weather/ddg/brave/web)
comme httpx (`HttpUpstream`, et le client OAuth du SDK MCP) construisent leur contexte via
`ssl.create_default_context()`, donc via la classe patchée. **Aucun appel HTTP n'est
réécrit, et rien n'est passé explicitement à un client** — c'est la raison d'être du point
d'injection unique, et pourquoi il n'y a pas eu de migration `requests`→`httpx` ici : ce
dépôt n'a jamais utilisé `requests`.

Deux points d'appel, un par mode de lancement, et pas un de plus :

- **`MiaouMCPBase.main()`** — seul point traversé par les six lancements standalone.
- **`mcp_proxy.main()`**, en **tête**, avant `build_upstreams` (qui importe les modules de
  serveurs) et avant tout handshake TLS. L'ordre est contractuel : un contexte SSL déjà
  construit garde la classe d'origine et continue d'ignorer le magasin système.
  `test_main_enables_system_trust_store_before_building_upstreams` épingle l'ordre, pas
  seulement le fait que l'appel existe.

Le mode inprocess ne passe pas par `main()` des serveurs — d'où l'appel propre du proxy,
qui couvre alors tout le process, upstreams inprocess compris.

**Best-effort volontaire** : `truststore` absent (installation pip minimale) ou plateforme
non supportée renvoie `False` avec un avertissement sur stderr, sans lever. Sur un poste
sans AC interne, la vérification par bundle CA fonctionne déjà : faire échouer le démarrage
y serait une régression pure pour un bénéfice nul.

Un test épingle que le client httpx du SDK construit bien son contexte via la classe
injectée (`test_sdk_http_client_uses_injected_ssl_context`) — c'est une propriété d'une
**bibliothèque tierce**, exactement comme `test_mcp_sdk_http_client_still_trusts_env` :
un SDK qui câblerait un jour un `ssl_context` explicite ou un bundle certifi rendrait
l'injection silencieusement inopérante sur le seul type d'upstream qui a motivé le
changement.

`truststore` est déclaré aux trois endroits qui gouvernent un environnement
(blocs PEP 723, `requirements.txt`, `pyproject.toml`), blocs PEP 723 de **tous** les
serveurs compris — `mcp_bench` n'a pas de sortie HTTPS, mais son `main()` appelle le
helper comme les autres, et la dépendance manquante y produirait un avertissement au
lancement.

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
# Avec uv — commande canonique, ne dépend pas de pyproject.toml/uv.lock
uv run --with pytest --with pytest-asyncio --with html2text --with pymupdf \
  --with python-docx --with openpyxl --with python-pptx --with truststore pytest tests/

# Avec uv — alternative via pyproject.toml (groupe dev) + uv.lock, équivalente
# depuis que [project.dependencies] couvre l'union runtime (DD7)
uv run --group dev pytest tests/

# Avec pip (après pip install -r requirements.txt)
pytest tests/
```

`pyproject.toml` porte `asyncio_mode = "auto"` (pytest-asyncio) — les deux commandes
uv ci-dessus s'appuient dessus, la commande canonique n'a juste pas besoin du lock.

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

### `tests/live_call.py` — appel réel d'un outil

Script PEP 723 (dépendance unique : `mcp`), **pas un test pytest** : son nom ne commence
pas par `test_`, il n'est donc jamais collecté malgré sa place dans `tests/`. Il parle le
vrai transport streamable-http, comme MIAOU (`initialize`, `notifications/initialized`,
`tools/call`) — c'est le chemin que le stack in-process des tests unitaires ne couvre pas
(cf. mémoire « Vérifier le transport HTTP réel »). Le serveur visé doit déjà tourner.

```bash
uv run tests/live_call.py brave__brave_search '{"query": "blabla"}'   # proxy, port 8765
uv run tests/live_call.py --port 8769 ddg_search '{"query": "chat"}'  # serveur unitaire
uv run tests/live_call.py --list                                      # outils exposés
uv run tests/live_call.py --url http://host:8765/mcp echo '{"text": "hi"}'
```

`--port` défaut 8765 (le proxy), `--host` défaut `127.0.0.1`, `--url` prime sur les deux.
Arguments JSON optionnels. L'outil est vérifié contre `tools/list` avant l'appel (nom
inconnu → liste des disponibles, code 2). Rendu des trois familles de blocs : `text` brut,
`image`/`resource` binaire résumés (mime + taille base64, jamais le base64 lui-même),
`resource` texte avec son URI, plus `structuredContent` s'il existe. Codes de sortie : 0
succès, 1 `isError` ou échec de connexion, 2 erreur d'usage. Les `ExceptionGroup` d'anyio
sont aplatis avant affichage (`_flatten`) — sans ça, un serveur injoignable ne produit que
« unhandled errors in a TaskGroup », sans la cause.

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
  **idempotente** (ré-acceptation silencieuse d'un ref déjà connu, sans réécriture — le
  fichier déjà matérialisé fait foi, jamais une erreur).
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
  **Déclaration du contrat, côté serveur** : le proxy ne connaît aucun sentinel en propre
  et n'importe pas `mcp_docs` — un upstream inprocess *déclare* le contrat en exposant
  `REF_UNKNOWN_SENTINEL` (str) et `REF_UNKNOWN_ERROR_CODE` (int) au niveau module ;
  `InProcessUpstream.start()` les lit par `getattr`, et le wrapper ne convertit le sentinel
  que pour les outils routés vers un upstream qui les déclare. Conséquences : un proxy
  configuré sans `docs` n'importe ni `mcp_docs` ni ses libs de parsing, et un message
  d'erreur d'un autre serveur qui contiendrait « REF_UNKNOWN » n'est pas converti (le match
  reste par sous-chaîne : FastMCP préfixe le message, il ne peut pas être ancré en tête).
  Les upstreams **stdio** sont hors périmètre : le proxy ne peut pas lire de constante dans
  un subprocess, un serveur stdio devrait lever l'erreur JSON-RPC lui-même.
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

La docstring ne documente que l'outil dans son ensemble (clé `description` du schéma
`tools/list`) — un paramètre nu (`text: str`) n'a jamais de clé `description` dans son
propre schéma, quelle que soit la qualité de la docstring. Pour qu'un paramètre précis
porte sa propre description, l'annoter avec `Annotated[type, Field(description="...")]`
(import `from pydantic import Field`, `from typing import Annotated`) — fonctionne aussi
sous `from __future__ import annotations`. Réservé aux paramètres dont le nom seul ne
suffit pas (contrainte fine documentée seulement dans la docstring globale : exclusivité
avec un autre paramètre, cap non levé par une plage, clamp silencieux, format dépendant du
type de document) — ne pas annoter systématiquement tous les paramètres, l'info doit
migrer d'un endroit à l'autre, pas se dupliquer. Exemples appliqués : `mcp_docs.read`
(`selector`, `char_start`/`char_end` vs `line_start`/`line_end`), `mcp_bench.reverse_dns.ip`
(accepte aussi un hostname), `mcp_web.fetch_url.max_bytes` (clamp silencieux au plafond).

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
