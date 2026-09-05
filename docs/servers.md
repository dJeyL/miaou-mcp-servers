# Les serveurs

Détail par serveur : outils exposés, contrats, variables d'environnement, décisions
de conception. Le proxy a son propre document (`docs/proxy.md`) ; l'auth OAuth aussi
(`docs/auth.md`).


## `servers/mcp_bench.py` — banc d'essai général (port 8766)

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

## `servers/mcp_weather.py` — météo réelle (port 8767)

Un seul outil `get_weather(city, state?, country?, astronomy?, hourly?, extract?)` qui
interroge wttr.in et renvoie un `EmbeddedResource` JSON. Sert à tester un outil avec
données réelles et paramètres optionnels, ainsi que le chemin resource inline (D8.2) —
et, avec `extract`, le chemin resource binaire (D8.1) sur un contenu textuel.

Les trois booléens sont indépendants et valent `false` par défaut ; sans eux, le
comportement est celui d'avant leur ajout, à l'octet près.

- **`astronomy`** et **`hourly`** réintègrent chacun le bloc du même nom, que la
  réponse allégée retire de chaque jour. Ils sont séparés plutôt que réunis sous un
  seul `full` (première forme livrée, remplacée) parce que leurs coûts n'ont rien de
  comparable — mesuré sur Paris, en caractères de JSON : 1535 sans rien, 2045 avec
  `astronomy` seul (+510), 25221 avec `hourly` seul (+23686), 25731 avec les deux.
  `hourly` pèse donc ~46 fois plus qu'`astronomy` : sous un booléen unique, demander
  les heures de lever du soleil coûtait le découpage horaire des trois jours.
- **`extract`** bascule du canal `resource.text` (store_inline, JSON réinjecté au
  modèle) vers `resource.blob` (store_binary, matérialisation en `res_…` **hors
  contexte du modèle**), accompagné d'un `TextContent` descripteur — même patron que
  `fetch_resource` de `mcp_web`, cf. le tableau des trois actions dans
  `docs/miaou-contract.md`. Le JSON est encodé en base64 bien qu'il soit du texte :
  c'est précisément ce qui le route en `store_binary`. Le descripteur nomme les blocs
  effectivement inclus (« allégée », « avec astronomy », « avec astronomy et hourly ») :
  c'est la seule chose que le modèle reçoit, il n'a aucun autre moyen de vérifier qu'il
  a obtenu le niveau de détail demandé.

Le nom de la ressource est `weather-<lieu slugifié>-<yyyymmdd>.json`. Le slug passe le
lieu en ASCII minuscule à tirets (`Saint-Étienne,France` → `saint-etienne-france`) —
sans ça accents, espaces et virgule se retrouveraient dans un nom de fichier côté
client. La date est celle du premier jour du bulletin (`weather[0].date`) quand elle
est au format attendu, sinon la date locale du serveur : un nom de ressource ne doit
pas dépendre de la bonne volonté du champ renvoyé par wttr.in.

Le format `j1` de wttr.in ne renvoie pas que l'instantané : `current_condition` plus
un tableau `weather` de trois entrées (le jour même et les deux suivants). La docstring
de l'outil le dit explicitement — sans ça, un modèle à qui on demande les prochains
jours conclut que l'outil ne sait faire que la météo actuelle et renonce (constaté en
usage). Elle précise aussi que le retrait de `hourly` supprime le découpage horaire,
pas les prévisions : ce qui reste par jour, ce sont les min/max et moyennes.

## `servers/mcp_web/` — téléchargement d'URL (port 8768)

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

## `servers/mcp_ddg.py` — recherche DuckDuckGo (port 8769)

Un seul outil `ddg_search(query, max_results=5)`. POST sur l'endpoint HTML de DDG
(`html.duckduckgo.com/html/`), parsing stdlib uniquement (classes `result__a` /
`result__snippet`). Renvoie `TextResourceContents` `application/json` — tableau
`[{title, url, snippet}]`. Fragile si DDG change son markup.

## `servers/mcp_brave.py` — recherche Brave Search (port 8770)

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

## `servers/mcp_docs/` — extraction de documents (port 8771)

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
capability du dispatcher MIAOU (cf. `docs/miaou-contract.md`). `drop_session` n'a
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

