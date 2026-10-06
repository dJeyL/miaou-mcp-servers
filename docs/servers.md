# Les serveurs

Détail par serveur : outils exposés, contrats, variables d'environnement, décisions
de conception. Le proxy a son propre document (`docs/proxy.md`) ; l'auth OAuth aussi
(`docs/auth.md`).


## Skills d'un serveur (`skills_dir`, extension Skills)

Tout serveur peut servir des skills (format Agent Skills, extension
`io.modelcontextprotocol/skills`) : `MiaouMCPBase(..., skills_dir=...)` installe
l'extension `Skills` de `servers/mcp_base.py`. Défaut `None` : aucune extension, rien ne
change sur le fil.

**Où vivent les skills.** Serveur mono-fichier (`servers/mcp_bench.py`) :
`servers/skills/<serveur>/`. Serveur en package (`servers/mcp_web/`…) :
`servers/mcp_<serveur>/skills/`, à côté de son code. Dans les deux cas, chaque
sous-dossier du `skills_dir` qui contient un `SKILL.md` est une skill, et le nom du
sous-dossier est son `name` ; ses autres fichiers sont des annexes, servies avec elle.
Servie en `skill://<name>/<fichier>`.

**Validé au démarrage**, la construction échouant avec un message qui nomme la skill et
la cause : frontmatter YAML en tête de `SKILL.md`, `name` égal au dossier et conforme à
Agent Skills (1-64 caractères, `a-z0-9-`, ni tiret en bord ni double tiret),
`description` non vide et d'au plus 1024 caractères, frontmatter représentable en JSON
(une date YAML non quotée est refusée : il faut la quoter), segments de chemin limités à
lettres, chiffres, `.`, `_`, `~`, `-`, bornes de la spec (512 fichiers, 16 Mio par
skill). Les fichiers dont un segment commence par un point (`.DS_Store`, `.git/`) sont
ignorés.

**Fraîcheur.** L'ensemble des fichiers est figé au démarrage (chacun est une ressource
enregistrée) ; leur contenu est relu à chaque `skills/list`, `skills/get` et
`resources/read`. Un fichier modifié est servi sans redémarrage ; un fichier AJOUTÉ ou
SUPPRIMÉ en demande un. Une skill devenue invalide sur disque est omise de `skills/list`
et rend `-32603` sur `skills/get` — c'est le sort d'une skill dont un fichier a été
supprimé à chaud, même une annexe : la relecture de l'ensemble figé échoue sur le
fichier disparu (`SkillError` « illisible »), et la skill entière disparaît avec lui,
`SKILL.md` compris. Serveur seul, `resources/list` (celui du SDK, qui liste les
ressources enregistrées) annonce pourtant toujours le fichier ; derrière le proxy, qui
dérive `resources/list` des entrées, la skill en disparaît aussi.

**Octets intacts.** `SkillFileResource` relit les octets bruts et rend du texte s'ils
sont de l'UTF-8 valide, un blob sinon. Le `FileResource` du SDK ne convient pas : il
retire le BOM (`utf-8-sig`) et ramène CRLF à LF, ce qui ferait diverger les octets servis
de l'empreinte publiée.

**Exiger une skill.** `self.finalize_tools(requires_skill="<skill>")` pose
`_meta["miaou/requiresSkill"]` (URI du `SKILL.md`) sur TOUS les outils ;
`requires_skill={"outil": "<skill>"}` sur ceux qu'il nomme. Skill non servie ou outil
inconnu : `ValueError` au démarrage. Une skill qu'aucun outil n'exige est facultative,
servie à l'identique.

**Importer la base par `from mcp_base import ...`, et sous aucun autre nom** — package
compris (`servers.mcp_base`, copie locale, import relatif bricolé). Un autre nom charge
une seconde copie du module, dont la classe `Skills` n'est pas celle que le proxy
reconnaît (`find_skills_extension` teste `isinstance`) : le serveur démarre, ses outils
exigent bien leur skill, `finalize_tools` ne lève rien, mais le proxy n'en voit aucune et
ne journalise que « skill exigée mais non servie ». Payé sur un package généré par un
autre modèle. Le démarrage d'un upstream inprocess le signale désormais par une ligne
« `Extension Skills de '<module>' ignorée` » qui nomme le module fautif.

**Dépendance.** Le frontmatter se lit avec PyYAML (`safe_load`, verbatim comme l'exige
la spec), importé paresseusement : seul un serveur qui sert des skills déclare `pyyaml`
dans son bloc PEP 723.

**Un serveur ne cite jamais l'URI de sa skill dans son texte libre** (instructions,
docstrings) : derrière le proxy elle reçoit le préfixe d'upstream. Le proxy la réécrit
dans des `instructions`, mais pas dans une docstring, et il génère déjà la ligne qui la
donne : l'écrire en plus la doublerait au mieux, la fausserait au pire. Le serveur la
nomme, en précisant qu'il s'agit d'une skill MCP lue par son URI (cf. `docs/proxy.md`).


## `servers/mcp_bench.py` — banc d'essai général (port 8766)

Sert à exercer les différents chemins de traitement des résultats dans MIAOU :

| Outil | Résultat | Chemin MIAOU exercé |
|---|---|---|
| `echo(text)` | bloc `text` | D9 : texte réinjecté au modèle |
| `add(a, b)` | bloc `text` | D9 |
| `sleep(seconds)` | bloc `text` | D9 — attente volontaire, simule un appel lent |
| `dns_lookup(hostname)` | bloc `text` | D9 — résolution via réseau local du serveur |
| `reverse_dns(ip)` | bloc `text` | D9 — PTR record |
| `get_image()` | bloc `image` (PNG) | D8.1 : binaire → IDB → `<img>` dans l'UI, descripteur statique au modèle |
| `get_json_resource()` | `EmbeddedResource` texte JSON | D8.2 : resource inline → IDB → chip `resource_stored`, texte brut + descripteur au modèle |

Les outils ont un `asyncio.sleep(2)` intentionnel pour simuler la latence réseau et
vérifier que le patienteur animé et les acks MCP (`mcp_call`) s'affichent correctement
pendant le round-trip.

`sleep(seconds)` sert le même objectif mais sur une durée **choisie par l'appelant**,
pour les cas que 2 secondes en dur n'atteignent pas (timeout client, patienteur sur une
attente longue). C'est ce qui lui vaut un plafond : la durée vient du modèle, et non
bornée elle immobiliserait la session (`sleep(86400)`). D'où `_SLEEP_CAP = 60.0` et un
clamp dans `[0, 60]` — silencieux, pas une erreur : hors plage, la valeur est ramenée
au plus proche. La valeur du cap est citée littéralement dans la description de l'outil
et dans celle de son paramètre, donc la changer demande de rééditer les deux.

Deux détails du clamp qui ne se devinent pas :

- **NaN est testé à part** (`seconds != seconds`), avant le clamp. `max(0, min(60, NaN))`
  renvoie NaN — NaN échoue toute comparaison, donc `max`/`min` le laissent passer — et
  `asyncio.sleep(NaN)` ne termine jamais : un vrai blocage, pas une lenteur simulée.
  `±inf`, lui, traverse le clamp correctement (60 et 0).
- **Le retour annonce la durée réellement attendue**, pas celle demandée, pour que le
  clamp soit observable côté client.

**Seul serveur du dépôt à publier des `instructions`** (consigne de portée serveur,
champ de l'`InitializeResult` — cf. `docs/proxy.md`), **et à servir une skill**
(extension Skills, `skills_dir` = `servers/skills/bench/`). Les instructions disent
la nature de banc d'essai et renvoient à la skill `bench`, PAR SON NOM : une URI y
serait fausse derrière le proxy, qui la préfixe du nom d'upstream (c'est le bloc
généré par le proxy qui la donne). Le renvoi dit « sa skill MCP `bench`, par son
URI » et non « sa skill `bench` » : un client qui a ses propres skills (MIAOU)
apprend au modèle à les lire par leur nom, et un nom nu l'envoyait chercher une
skill locale inexistante — observé, détour par `miaou__skills__read` ou abandon
de l'outil. La skill
(`servers/skills/bench/bench/SKILL.md`, servie en `skill://bench/SKILL.md`, avec
une annexe `dns.md` sur la lecture des résultats DNS, à lire avant d'en rapporter
un — « pour interpréter » laissait un modèle juger son résultat assez clair pour
s'en passer, et le rapporter sans dire d'où il était résolu) porte la
règle : signaler chaque usage d'un outil `bench` par la ligne
« *Banc d'essai bench — résultat **non contractuel**.* ». Tous les outils l'exigent
(`_meta["miaou/requiresSkill"]`, posé par `finalize_tools(requires_skill="bench")`).
Durable, et non un canari à retirer : la règle est vraie (résultat sans utilité en
production) et sert en même temps de témoin de bout en bout — le marqueur porte le
mot `bench`, donc sa présence dans une réponse atteste que la skill a été lue ET
rattachée au bon serveur.

Le texte se NOMME (« les outils `bench` ») au lieu de se désigner (« ce serveur ») :
agrégé par le proxy, il vit sous un titre de section parmi N, où un déictique n'a
plus d'antécédent stable. `bench` reste un segment littéral du nom d'outil en accès
direct comme derrière le double préfixe côté MIAOU (`miaou-proxy__bench__echo`). La
mention de l'effet réel (résolution DNS) évite par ailleurs qu'un modèle prudent
refuse `dns_lookup` en le croyant simulé.

Conséquence pour les tests : `mcp_bench` ne peut plus servir d'upstream muet. Les
tests qui vérifient « rien à agréger → rien à publier » montent sur un double
explicite, la garantie portant sur le proxy et non sur le silence d'un serveur.

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
├── __init__.py    # serveur MCP (MCPServer) + définition des outils (fetch_url/fetch_read/fetch_list/fetch_resource)
├── __main__.py    # point d'entrée `python -m mcp_web` / `uv run servers/mcp_web`
├── cache.py        # cache disque par checksum d'URL (texte, HTML brut, structure JSON)
├── pagemeta.py     # `_meta` de fetch_url : en-tête de page, favicon validée, cache par origine
├── structure.py    # extraction stdlib (html.parser) des headings/liens, sans dépendance tierce
└── search/         # recherche multi-moteurs (search, image_search) — section « Recherche » plus bas
    ├── __init__.py # config `search`, build_chain, SearchChain (repli, pauses, budget)
    ├── common.py   # EngineFailure, http_read, clean_snippet, resolve_api_key, constantes de pause
    ├── brave.py    # Brave Search (clef ; web et images)
    ├── ollama.py   # recherche web d'Ollama (clef ; web)
    └── ddg.py      # DuckDuckGo HTML (sans clef ; web), espacement et défi anti-bot, ENGINE unique
```

Quatre outils de téléchargement, plus deux de recherche décrits dans la section
« Recherche » ci-dessous. `"fetch": false` dans le bloc `config` de l'entrée retire
les quatre `fetch_*` d'un coup (`WebConfigError` si la valeur n'est pas un booléen) :
la description de `search` cesse alors de renvoyer vers `fetch_url`, et la ligne de
démarrage le signale. `fetch_url(url, max_bytes=5242880)` branch sur le `Content-Type` :

| Content-Type | Traitement | Résultat |
|---|---|---|
| `text/html` | html2text (script/style supprimés) | `TextResourceContents` `text/plain` |
| `text/*` | texte brut | `TextResourceContents` avec le vrai mime |
| tout le reste | base64 | `BlobResourceContents` |

Taille *téléchargée* bornée à `max_bytes` (défaut 5 Mo), troncature notée dans le texte.
Erreurs réseau retournées comme chaînes (pas de stack trace). Une `HTTPError` est
**fermée** avant d'être convertie (`_guarded_fetch`) : elle EST la réponse, socket
comprise, qui restait sinon ouverte jusqu'au GC — cyclique le plus souvent, l'exception
ayant traversé `to_thread`. Les tests la fermaient eux-mêmes, ce qui masquait le défaut.

**Décompression du corps selon `Content-Encoding` (`_decompress`, WEB9).** On ne
sollicite **aucun** encodage — urllib n'envoie pas d'`Accept-Encoding` et on n'en ajoute
pas — mais un serveur peut gzipper sans qu'on l'ait demandé : `python.org` le fait sur
`/downloads/release/` (mesuré le 2026-09-22), là où `docs.python.org`, `example.com` ou
Wikipédia servent du clair. Sans décompression le corps gzip partait tel quel en
`decode(errors="replace")`, et le modèle recevait un texte de remplacement qu'il lisait
comme étant la page — un défaut **silencieux**, sans erreur nulle part, que rien ne
distingue d'une page réellement illisible. `gzip`/`x-gzip` et `deflate` (avec le fallback
deflate brut, sans en-tête zlib, que tolèrent les navigateurs) sont traités ; un encodage
non géré (`br`, `zstd`) rend le corps **inchangé**, soit ce qui partait avant — un cas non
couvert ne doit pas faire échouer un fetch qui aboutissait. `zlib` est stdlib : rien à
ajouter à `requirements.txt` ni à `[project.dependencies]`.

Deux conséquences sur les bornes, qui tiennent au fait que le cap mord sur les octets
**compressés** :

- La troncature se décide dans `_fetch_bytes`, sur les octets tels qu'ils arrivent du
  réseau, et **jamais en aval** : après décompression le corps est légitimement plus gros
  que `max_bytes` sans avoir rien perdu, si bien qu'un `len(corps) > max_bytes` calculé
  plus loin annoncerait une troncature qui n'a pas eu lieu. `_guarded_fetch` relaie
  désormais le flag au lieu de le calculer, et son corps rendu peut dépasser `max_bytes`.
- Un flux gzip coupé au milieu ne se décompresse pas intégralement, d'où
  `zlib.decompressobj()` (qui rend ce qu'il a pu lire) plutôt que `gzip.decompress()` (qui
  lèverait sur la fin absente) : une page tronquée reste lisible jusqu'à sa coupure.

`max_bytes` borne donc le **transfert**, pas ce qui atteint le modèle : côté texte c'est
`MIAOU_WEB_READ_CAP` qui plafonne la sortie (paragraphe suivant), et `fetch_read` qui
pagine au-delà.

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

**`_meta` du résultat de `fetch_url` : ce que le client affiche, hors modèle (lot AI).**
`fetch_url` rend un `CallToolResult` complet — seule forme par laquelle MCPServer laisse un
outil poser le `_meta` de son résultat — et y range, sous la clé `miaou/web`
(`pagemeta.META_KEY`), ce qu'il faut à MIAOU pour afficher la source d'une citation :

| Champ | Source | Présent |
|---|---|---|
| `title` | `<title>` de l'en-tête, sinon `og:title` | HTML |
| `site_name` | `og:site_name` | HTML |
| `canonical_url` | URL finale après les redirections suivies par urllib (`geturl()`), http(s) seulement | tout succès |
| `favicon` | data-URL base64, type reconnu aux octets | HTML, si trouvée et sous le plafond |

Tous facultatifs, clé absente plutôt que vide ; aucun `_meta` sur un résultat d'erreur.
Rien de tout cela n'entre dans `content` : le modèle cite une URL et n'a pas besoin du
titre, et les octets d'une favicon y seraient payés à chaque tour. Préfixe `miaou/` pour
la même raison que `miaou/unauthorized_upstreams` côté proxy (`_meta` est un espace
partagé). `canonical_url` n'est PAS le `<link rel="canonical">` de la page : c'est l'adresse
réellement atteinte, ce qui répond à « où ai-je lu ça ? » sans croire la page sur parole.

L'en-tête est lu par `html.parser` (stdlib) sur le HTML coupé à `</head>` ou au premier
`<body>` (et à 256 Ko) : un `<title>` de `<svg>` dans le corps n'est jamais pris pour celui
de la page. Textes aplatis et bornés à 300 caractères.

La favicon est cherchée dans cet ordre : les `<link rel~="icon">` de la page (résolus
contre l'URL **finale** ; `apple-touch-icon` écarté, 180 px et presque toujours hors
plafond ; SVG écarté par type ou extension ; `data:` base64 accepté sans requête), puis
`/favicon.ico` de l'hôte final — trois tentatives au plus, timeout de 3 s chacune. Son
type est décidé **aux octets** (PNG, ICO, GIF, JPEG, WebP) et jamais au `Content-Type` : un
serveur sert volontiers une page d'erreur HTML en 200 sur `/favicon.ico`. SVG refusé en
toute circonstance (il porte du script). Plafond : data-URL de 16 384 caractères
(`FAVICON_MAX_CHARS`) ; au-delà, absente. Le téléchargement, lui, est borné plus large
(`FAVICON_DOWNLOAD_MAX`, 64 Ko) : un ICO multi-résolution dépasse souvent le plafond
AVANT réduction.

**Réduction d'un ICO (`shrink_ico`).** Un ICO porte souvent plusieurs images (16, 32,
48 px…) : on n'en garde qu'une, la plus petite d'au moins `FAVICON_TARGET_PX` (32 px, soit
16 px CSS en densité 2 — Retina, 4K à 200 %), à côté égal la plus profonde ; faute d'image
assez grande, la plus grande en dessous ; et si la retenue ne tient pas au plafond, la
suivante dans cet ordre. Une image PNG embarquée sort en `image/png` telle quelle ; une
image BMP est ré-emballée dans un ICO d'une seule entrée, octets de l'image inchangés
(seul l'offset du répertoire change). Répertoire incohérent (entrée hors du fichier, zéro
entrée) : favicon absente. Un PNG seul n'est jamais redimensionné (il faudrait une
bibliothèque d'image) : il passe s'il tient au plafond.

Mesuré le 2026-09-26 sur le vrai transport : docs.python.org (lien SVG écarté, puis
`favicon.ico` de 15 Ko à trois images) → ICO 32 px, 5 741 caractères ; Wikipédia → ICO
32 px, 1 049 ; GitHub, Le Monde, BBC → PNG 32 px, sous 1 300 ; Hacker News → PNG 256 px
non réduit, 10 030. Toute erreur rend une favicon absente, jamais un `fetch_url` en échec.

La sonde tourne **pendant** la conversion html2text (`asyncio.gather`), et son résultat est
gardé en mémoire **par origine** (`favicon_cache`, 256 origines, durée de vie
`MIAOU_WEB_CACHE_TTL_H`), échec compris : lire dix pages d'un site ne sonde qu'une fois, et
un site sans favicon valide ne coûte pas trois requêtes à chaque page.

Effet de bord du retour `CallToolResult` : `fetch_url` ne publie plus d'`outputSchema`, et
son résultat plus de `structuredContent` — lequel recopiait le contenu entier sur le fil,
sans lecteur (le proxy ne publie pas l'`outputSchema` de ses upstreams, MIAOU ne lit que
`content`). Un texte d'erreur reste un résultat ordinaire (`isError` faux), comme avant.

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
| `BRAVE_API_KEY` | — | Clef du moteur `brave`, si le bloc `config` n'en donne pas |
| `OLLAMA_API_KEY` | — | Clef du moteur `ollama`, si le bloc `config` n'en donne pas |

### Recherche (`search`, `image_search`)

Ces deux outils remplacent `mcp_ddg` et `mcp_brave`, dépréciés. Un seul outil par
type de recherche, qui choisit lui-même son moteur : `search` essaie les moteurs dans
l'ordre configuré et s'arrête au premier qui **répond**. `image_search` fait de même
parmi les moteurs qui savent chercher des images (aujourd'hui Brave seul).

**Config.** Bloc `config` de l'entrée `web` de `config.json`, clé `search` :
`order` (défaut `["brave", "ollama", "ddg"]`), et un bloc par moteur à clef
(`brave.api_key`, `ollama.api_key`, sinon `BRAVE_API_KEY` / `OLLAMA_API_KEY`). Le
bloc prime sur l'environnement pour la même raison que `mcp_brave` : plusieurs
entrées `web` peuvent porter des clefs différentes (`build(config)`, une instance par
entrée). Règles de `build_chain` :

- un moteur absent de `order` est désactivé ; `order: []` ou `"search": false`
  coupe la recherche (`"search": true` vaut l'absence de la clé) ;
- un moteur à clef sans clef est écarté, avec une ligne au démarrage ;
- si `order` cite des moteurs et qu'**aucun** n'est utilisable, la construction
  lève `SearchConfigError`, comme `mcp_brave` sans clef. Le proxy écarte alors
  l'upstream entier, outils `fetch_*` compris, avec la cause sur sa ligne
  « unavailable » ;
- un nom inconnu ou répété lève aussi `SearchConfigError` : une faute de frappe qui
  désactiverait un moteur en silence serait pire.

`build()` imprime sur stderr la chaîne active et les moteurs écartés
(`miaou-web : recherche via brave → ddg ; images via brave; ollama écarté (…)`), de
même que le lancement standalone. Le singleton du module, construit sans config,
lit l'ordre par défaut et les clefs de l'environnement ; il ne peut pas lever, `ddg`
n'ayant pas de clef. Recherche coupée, la ligne dit « recherche désactivée ».

`"fetch": false` et recherche coupée ensemble lèvent `WebConfigError` : une entrée
active sans aucun outil est une config à corriger, que le proxy signale sur la
ligne « unavailable » au démarrage — à la différence de `"disabled": true`, qui
coupe l'entrée exprès et en silence.

**Listage figé à la construction.** `search` n'est enregistré que si la chaîne a un
moteur web, `image_search` que si l'un sait chercher des images. C'est décidé sur la
config, jamais sur les pannes du moment : la liste d'outils ne bouge pas en cours de
session (`ToolCatalogCache` du proxy). Un moteur en panne laisse son outil listé, qui
rend l'échec. La description de `search` cite l'ordre actif, et l'espacement DDG
seulement si `ddg` est dans la chaîne.

**Repli.** Un résultat vide est une réponse et arrête la chaîne. Replier dessus ferait
finir sur DDG chaque requête sans résultat, alors que c'est le moteur à ménager. Un
échec (`EngineFailure`) fait passer au suivant. Le résultat est un
`TextResourceContents` `application/json`, URI `miaou://web-search/{query}` ou
`miaou://web-image_search/{query}` :

```json
{"engine": "ollama", "results": [{"title": "…", "url": "…", "snippet": "…"}],
 "fallback": [{"engine": "brave", "reason": "quota dépassé (HTTP 429)"}]}
```

`fallback` n'apparaît que si un moteur a été écarté pendant l'appel ; il dit pourquoi,
pour le modèle comme pour l'humain qui lit la trace. Si aucun moteur n'a répondu, un
texte énumère chacun et sa raison.

Le moteur qui a répondu est aussi posé pour le **client**, hors contenu :
`_meta["miaou/search"] = {"engine": "<nom>"}` (`SEARCH_META_KEY`). MIAOU peut ainsi
l'afficher sans deviner, depuis le JSON, quel résultat vient d'une recherche. La clé
est distincte de `miaou/web`, que MIAOU lit comme l'en-tête de page de `fetch_url` :
ses champs y sont tous facultatifs, donc un `{engine}` passerait pour une source
vide. Pas de `_meta` quand aucun moteur n'a répondu. Comme pour `fetch_url`, d'où
`_tool_result(..., meta_key=)` partagé, le `_meta` impose de rendre un
`CallToolResult` : pas d'`outputSchema` ni de `structuredContent`. Les résultats image ont la forme de l'ancien
`brave_image_search` (`title, page_url, image_url, thumbnail_url, source`).

**Normalisation.** Les trois moteurs rendent `{title, url, snippet}`.
`clean_snippet` retire les balises (Brave surligne en `<strong>`), décode les
entités, et coupe à `SNIPPET_MAX_CHARS` (400) avec « … ». C'est surtout pour Ollama,
dont `content` est du contenu de page (« des milliers de tokens » selon sa doc). Sans
cette coupe, le coût en tokens d'un appel dépendrait du moteur qui a répondu.
`max_results` est ramené dans [1, `MAX_RESULTS`] = [1, 10] pour tous les moteurs
(l'API Ollama plafonne d'ailleurs à 10).

**Pauses d'un appel à l'autre.** Selon sa cause, un échec met le moteur en pause
(`down_until`, `down_reason`). Pendant la pause, le moteur est sauté sans requête et
apparaît dans `fallback` avec le temps restant :

| Cause | Pause | Constante |
|---|---|---|
| Clef refusée | jusqu'au redémarrage | `KEY_REJECTED_COOLDOWN_S` (`inf`) |
| HTTP 429 | 10 min | `QUOTA_COOLDOWN_S` |
| Réseau, timeout, 5xx, autre 4xx, JSON illisible | 1 min | `TRANSIENT_COOLDOWN_S` |
| Défi anti-bot DDG | 2 h 30 | `ddg.ANOMALY_COOLDOWN_S` |
| Refus d'espacement DDG | aucune | — |

Seule la pause DDG est calibrée sur une mesure (le blocage du 2026-10-05). Les autres
sont choisies à l'aveugle, aucun quota n'ayant été mesuré.

« Clef refusée » se décide par moteur (`key_rejected` passé à `http_read`), sur des
réponses mesurées le 2026-10-05. **Ollama** répond 401, que la clef soit fausse ou
absente. **Brave** répond **422**, pas 401, avec
`{"error": {"code": "SUBSCRIPTION_TOKEN_INVALID", "meta": {"component":
"authentication"}}}`. Comme Brave sert aussi le 422 pour des paramètres invalides,
seul le composant `authentication` désigne la clef. Le corps de l'erreur est lu (au
plus 4 Ko) avant la fermeture de la `HTTPError`. NB : `mcp_brave`, déprécié, n'a
jamais vu ce 422 et rend « HTTP 422 » nu sur une clef invalide.

**Budget de temps.** Un appel `search` dispose de `SEARCH_BUDGET_S` (25 s) pour toute
la chaîne : il faut rester sous les 30 s de timeout MIAOU→MCP suggérés par défaut. Or
Brave et Ollama en timeout (10 s chacun), plus l'attente puis la requête DDG,
dépasseraient ce délai. Chaque moteur reçoit donc le temps restant, son timeout est
raboté d'autant (`min(ENGINE_TIMEOUT_S, restant)`), et en dessous de 2 s il n'est
même pas tenté. Le timeout urllib porte sur chaque opération socket : c'est une
marge, pas une garantie.

**Moteur DDG.** C'est le code de `mcp_ddg` repris tel quel (parser, défi
`anomaly-modal`, espacement `_MIN_INTERVAL_S` / `_MAX_WAIT_S`), avec deux différences.
D'abord, l'attente du créneau est aussi bornée par le budget restant : un appel
arrivé en fin de chaîne refuse sans requête s'il ne reste pas `_MIN_FETCH_S` (3 s) de
requête après l'attente. Ensuite, l'état (créneau, pause) est celui de l'**adresse
IP** : un seul `ddg.ENGINE` par processus, partagé par toutes les instances de
`mcp_web`. Il ne l'est **pas** avec `mcp_ddg`. Si les deux tournent, DuckDuckGo
reçoit deux flux espacés chacun de son côté, d'où l'avertissement de `mcp_ddg` au
démarrage. Le parser est dupliqué plutôt qu'importé : importer `mcp_ddg` construirait
son serveur, et `mcp_ddg` ne peut pas importer `mcp_web` (html2text absent de son
bloc PEP 723). La copie disparaîtra avec `mcp_ddg`.

**Ajouter un moteur** (ddgs, envisagé entre Ollama et DDG) : un module dans
`search/` avec `name`, `kinds`, `down_until`/`down_reason` et
`async search(kind, query, n, budget_s)`, qui rend une liste normalisée ou lève
`EngineFailure`. Puis son entrée dans `ENGINE_NAMES`/`DEFAULT_ORDER`, et dans
`_KEYED` s'il prend une clef. Pour ddgs en particulier, avant de l'intégrer : son
backend `duckduckgo` frappe le même DDG hors de notre espacement, et `primp` a sa
propre pile TLS, a priori hors de portée de `truststore` (non mesuré).

## `servers/mcp_ddg.py` — recherche DuckDuckGo (port 8769)

> **Déprécié** : remplacé par `search` de `mcp_web` (moteur `ddg`, mêmes protections),
> `disabled: true` dans `config.sample.json`. Il imprime un avertissement à chaque
> démarrage (`build()`, qui rend le singleton, et le lancement standalone). Actif à
> côté de `mcp_web`, il ne partage pas son espacement vers DuckDuckGo.

Un seul outil `ddg_search(query, max_results=5)`. POST sur l'endpoint HTML de DDG
(`html.duckduckgo.com/html/`), parsing stdlib uniquement (classes `result__a` /
`result__snippet`). Renvoie `TextResourceContents` `application/json` — tableau
`[{title, url, snippet}]`. Fragile si DDG change son markup.

L'autre mode de panne, plus fréquent que le markup : DDG remplace les résultats
par un défi anti-bot (HTTP 202, captcha « select the ducks », formulaire vers
`anomaly.js?cc=botnet`) dès qu'une IP sortante enchaîne quelques requêtes —
mesuré le 2026-10-05, curl compris, après une poignée d'appels en quelques
minutes. Le blocage a duré 2 h 15 à 2 h 30 (sonde d'une requête par quart
d'heure, qui ne l'a ni prolongé indéfiniment ni relancé une fois levé) ; seuil
et durée pour une autre IP inconnus. Le parser n'y trouvait aucun `result__a` et
l'outil rendait `[]`, indiscernable d'une recherche vide. La page est reconnue à
sa classe `anomaly-modal` (seulement quand aucun résultat n'a été extrait) et
l'outil rend un message explicite à la place. Le défi n'est pas contourné : la
seule issue est d'attendre, ou de passer par un autre moteur (`search` de `mcp_web`).

Pour ne pas le déclencher soi-même, les requêtes sortantes d'un processus sont
espacées d'au moins `_MIN_INTERVAL_S` (15 s) : un appel attend son créneau,
réservé sans verrou (aucun `await` entre lecture et écriture de `_next_slot`),
et il est refusé sans requête si l'attente dépasserait `_MAX_WAIT_S` (15 s).
Plafond imposé par le timeout MIAOU→MCP suggéré par défaut, 30 s, à ne pas
atteindre : `_MAX_WAIT_S + _FETCH_TIMEOUT_S` doit rester en dessous (test
dédié). Attendre plutôt que refuser d'emblée : un refus pousse le modèle à
relancer aussitôt. Les valeurs sont calibrées à l'aveugle, le seuil de DDG
n'ayant pas été mesuré (chaque essai coûte deux heures de blocage). L'espacement
ne protège qu'un processus : plusieurs instances derrière la même IP de sortie
(collègues sur le même réseau) ne se coordonnent pas, seul un proxy partagé les
couvre.

## `servers/mcp_brave.py` — recherche Brave Search (port 8770)

> **Déprécié** : remplacé par `search` / `image_search` de `mcp_web` (moteur `brave`),
> `disabled: true` dans `config.sample.json`. Il imprime un avertissement à chaque
> démarrage (`build()` et le lancement standalone).

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
> `config.sample.json` porte `disabled: true` sur son entrée `docs` ; le retirer
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
├── __init__.py    # serveur MCP (MCPServer) + définition des outils (docs__*)
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

1. Lancer le proxy (`uv run mcp_proxy`) avec `docs` activé dans `config.json`.
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

