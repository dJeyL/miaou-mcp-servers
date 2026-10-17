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
(une ligne au journal par cause, pas une par requête) et rend `-32603` sur `skills/get` —
c'est le sort d'une skill dont un fichier a été supprimé à chaud, même une annexe : la
relecture de l'ensemble figé échoue sur le fichier disparu (`SkillError` « illisible »), et
la skill entière disparaît du listage. Les fichiers de l'ensemble figé restent pourtant
« servis » (`Skills.served_uris`) : lire le fichier disparu rend `-32603` « indisponible »,
lire un autre fichier de la skill réussit. Un lien symbolique dont la cible résolue
(`Path.resolve()`) sort du dossier de la skill fait échouer le démarrage (`SkillError` qui
nomme le fichier : copier le fichier dans la skill ou retirer le lien) ; un lien qui reste
dans le dossier est servi. Serveur seul, `resources/list` (celui du SDK, qui liste les
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

Les six outils autres que `sleep` ont un `asyncio.sleep(2)` intentionnel pour simuler
la latence réseau et vérifier que le patienteur animé et les acks MCP (`mcp_call`)
s'affichent correctement pendant le round-trip.

`dns_lookup` et `reverse_dns` rendent toute entrée irrésoluble en texte « Échec de
résolution… » et non en erreur nue : outre `OSError`, un label idna de plus de 63
caractères lève `UnicodeError` et un octet NUL dans `gethostbyaddr` un `TypeError`, que
le SDK 2.x rendrait en « Error executing tool » sans motif. `dns_lookup` refuse en outre
tout nom contenant un NUL sans le résoudre : certains OS résolvent le préfixe avant le
NUL, et le texte rendu citerait un nom qui n'est pas celui résolu.

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
client. Il est tronqué à 60 caractères, et un lieu sans aucun caractère ASCII (`日本`,
`Москва`) prend `lieu-<8 hex>` (condensé SHA-256 du lieu) : un simple `lieu` donnerait le
même nom à deux villes le même jour. Le lieu est encodé en pourcentage (seule la virgule
reste lisible) dans l'URL wttr.in comme dans l'URI de la ressource non-extract, et un
`city` vide ou blanc est refusé (`ToolError`) : wttr.in répondrait pour le lieu déduit de
l'IP du serveur, présenté comme une météo demandée. La date est celle du premier jour du bulletin (`weather[0].date`) quand elle
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
├── cache.py        # cache disque par checksum d'URL (texte, HTML brut, structure JSON, alias d'URL finale)
├── pagemeta.py     # `_meta` de fetch_url : en-tête de page, favicon validée, cache par origine, filtre de `canonical_url`
├── netguard.py     # openers http(s) seulement ; la sonde de favicon refuse IP/localhost à chaque saut
├── structure.py    # extraction stdlib (html.parser) des headings/liens, sans dépendance tierce
└── search/         # recherche multi-moteurs (search, image_search) — section « Recherche » plus bas
    ├── __init__.py # config `search`, build_chain, SearchChain (repli, pauses, budget)
    ├── common.py   # EngineFailure, http_read, clean_snippet, is_result_url, resolve_api_key, constantes de pause
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

Taille *utile* bornée à `max_bytes` (défaut 5 Mo), **décompression comprise** (ci-dessous),
troncature notée dans le texte. Erreurs réseau retournées comme chaînes (pas de stack
trace) ; une URL qu'`urlsplit` refuse (`http://[::1`) rend « URL invalide », et un charset
annoncé par le serveur que Python ne sait pas lire comme un charset (`idna`, `punycode` :
`UnicodeError`, pas `LookupError`) retombe sur UTF-8 (`_decode`). Une `HTTPError` est
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

**Le plafond porte sur ce qu'on rend, décompression comprise.** Le serveur
impose lui-même son encodage : un gzip de quelques Mo se décompresse en gigaoctets (mesuré :
200 Mo de zéros = 194 Ko compressés, ratio ~1000:1, soit ~5 Go pour le plafond de 5 Mo). Le
paramètre `max_bytes` de `fetch_url` et de `fetch_resource` compte donc les **octets utiles
reçus**, après décompression. `_decompress` vide le flux par morceaux bornés
(`decompressobj.decompress(données, max_length)`, `_inflate`) et ne tient jamais plus de
`max_bytes + 1` octets décompressés ; la lecture du flux compressé est elle-même bornée à
`max_bytes + _INFLATE_SLACK` (1 Ko : un gzip d'un contenu peu compressible dépasse un peu sa
taille). La troncature est annoncée exactement comme pour un corps non compressé (« [Tronqué
à N octets] » pour `fetch_url`, « Tronqué à N octets. » dans le descripteur de
`fetch_resource`), qu'elle vienne du plafond décompressé ou d'un flux compressé coupé. Un
contenu d'exactement `max_bytes` octets n'est pas tronqué.

Deux autres points tiennent à la nature du flux :

- La troncature se décide dans `_fetch_bytes`, sur ce que le corps contient réellement, et
  **jamais en aval** : `_guarded_fetch` relaie le flag au lieu de le calculer.
- Un flux gzip coupé au milieu ne se décompresse pas intégralement, d'où
  `zlib.decompressobj()` (qui rend ce qu'il a pu lire) plutôt que `gzip.decompress()` (qui
  lèverait sur la fin absente) : une page tronquée reste lisible jusqu'à sa coupure.

**Redirections : http/https seulement.** `urllib.request.build_opener` embarque FTP,
fichier et `data:`, et son gestionnaire de redirection suit `ftp://` : une page http qui
redirigeait vers `ftp://hôte-interne/…` faisait ouvrir une connexion FTP depuis le serveur
(mesuré sur un serveur loopback). `netguard.web_opener()` — `ProxyHandler()` sans argument
comme `make_opener`, donc `no_proxy` respecté, mais ni FTP ni fichier ni data — et son
`SchemeRedirectHandler` refusent toute redirection hors http/https (« redirection vers un
schéma non autorisé »). **L'adresse, elle, n'est pas restreinte** : joindre `127.0.0.1` ou un
nom d'intranet sur ordre du modèle est un usage légitime d'un banc d'essai.

`max_bytes` ne dit donc rien de ce qui atteint le modèle côté texte : c'est
`MIAOU_WEB_READ_CAP` qui plafonne la sortie (paragraphe suivant), et `fetch_read` qui
pagine au-delà.

Le texte produit (HTML converti ou `text/*`) est en plus plafonné en sortie à
`MIAOU_WEB_READ_CAP` caractères (défaut 20000, cf. `servers/mcp_web/cache.py`) — une page
HTML de plusieurs Mo convertie par html2text peut sinon saturer la fenêtre de contexte de
l'appelant malgré `max_bytes`. Le texte complet (et le HTML brut, si `text/html`) est écrit
sur disque (`servers/mcp_web/cache.py`), clé = SHA256 de l'URL demandée — pas de session_id ni de
contrat REF_UNKNOWN comme mcp_docs : ce cache est indépendant de toute conversation MIAOU,
une URL déjà récupérée reste paginable même depuis un autre client. Sweep TTL opportuniste
(`MIAOU_WEB_CACHE_TTL_H`, défaut 24h), même pattern que le TTL de session de `mcp_docs`.
Le sweep (`sweep_expired`) ne supprime **que** les noms que le module écrit —
`<sha256 en 64 hexa>.(txt|html|json|alias)` — jamais un autre fichier : `MIAOU_WEB_WORKDIR`
est libre et peut pointer un dossier qui n'est pas le nôtre (mesuré : un `notes.txt`
de 25 h y disparaissait au premier outil).

**Alias d'URL finale.** MIAOU cite `canonical_url`, l'URL finale après
redirections ; le modèle peut rappeler `fetch_read(<finale>)`, alors que le cache est rangé
sous l'URL demandée. `fetch_url` pose donc, sous la clé de l'URL finale, un petit fichier
`<hash>.alias` qui contient l'URL demandée (`store_alias`), et `fetch_read`/`fetch_list`
passent par `resolve` : une entrée propre de l'URL l'emporte sur son alias, sinon la cible
de l'alias est lue (et l'alias rafraîchi comme les trois autres fichiers). Reproduit avant
correctif : `CacheMiss` sur l'URL finale. Limites assumées : un alias est écrasé par le
fetch suivant qui aboutit à la même URL finale par un autre chemin (le dernier gagne) ; si
l'URL demandée est re-fetchée et mène ailleurs, l'alias ancien désigne alors son nouveau
contenu ; un alias dont la cible a expiré ou a été purgée (re-fetch binaire) ne résout plus.
Pas de plafond de volume du cache (reporté : borné par le TTL).

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
| `canonical_url` | URL finale après les redirections suivies par urllib (`geturl()`), http(s) seulement, **omise si le client la rejetterait** (voir ci-dessous) | tout succès |
| `favicon` | data-URL base64, type reconnu aux octets | HTML, si trouvée et sous le plafond |

Tous facultatifs, clé absente plutôt que vide ; aucun `_meta` sur un résultat d'erreur.
Rien de tout cela n'entre dans `content` : le modèle cite une URL et n'a pas besoin du
titre, et les octets d'une favicon y seraient payés à chaque tour. Préfixe `miaou/` pour
la même raison que `miaou/unauthorized_upstreams` côté proxy (`_meta` est un espace
partagé). `canonical_url` n'est PAS le `<link rel="canonical">` de la page : c'est l'adresse
réellement atteinte, ce qui répond à « où ai-je lu ça ? » sans croire la page sur parole
(ni `<link rel=canonical>` ni `og:url` ne sont lus : verrouillé par
`test_canonical_url_is_the_final_url_never_the_pages_own_claim`).

**Grammaire client de `canonical_url`.** MIAOU garde la clef si elle fait au plus 2048
unités UTF-16 et vérifie `/^https?:\/\/[^\s]+$/i` (`webMetaFromResult`, `src/js/utils.js`),
et ne pose la note `[web_ref:URL]` que si l'URL tient dans
`/\[web_ref:(https?:\/\/[^\s\]]+)\]/` (`WEB_REF_MARKER_RE`) : ni blanc ni `]`. Le serveur
applique la même règle (`client_accepts_canonical_url`, `pagemeta.py`) : au-delà de 2048
unités UTF-16, ou avec un caractère <= 0x20, un blanc Unicode (`\s` de JS : NBSP, U+2028,
U+FEFF…) ou `]`, `canonical_url` est **omis** du `_meta` (les autres champs restent). Le
client retombe alors sur l'URL demandée pour sa note de source, comme il le faisait en
écartant la clef.

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

**La sonde refuse les adresses internes.** Un `<link rel=icon>` d'une page non
fiable déclenchait un GET vers `http://127.0.0.1:…/admin`, `169.254.169.254` ou le proxy
lui-même, à l'insu du modèle (mesuré : les trois URL étaient demandées). La sonde
(`_probe_bytes`, opener `netguard.favicon_opener()`) refuse donc toute URL dont l'hôte est
une **adresse IP** — toute forme que la pile réseau lit comme telle, pas seulement celles
d'`ipaddress` : `127.1`, `2130706433`, `0x7f.0.0.1`, `017700000001` (formes `inet_aton`),
IPv6 entre crochets dont `[::ffff:127.0.0.1]`, chiffres pleine chasse et points
idéographiques que l'IDNA ramène à de l'ASCII — ainsi que `localhost` et `*.localhost`
(RFC 6761), casse et point final ignorés. Le test est syntaxique et ne dépend pas de la libc
(`is_forbidden_probe_host` : étiquettes toutes numériques, plus `socket.inet_aton` en
renfort) ; aucun `getaddrinfo`, parce qu'en entreprise les noms Internet ne se résolvent pas
forcément. La règle s'applique à l'URL candidate ET à **chaque saut de redirection**
(`ProbeRedirectHandler`). Conséquence : une page servie par une adresse IP (banc local) n'a
jamais de favicon. **Trou résiduel ACCEPTÉ** : un *nom* qui résout vers une adresse interne
(nom d'intranet, `*.nip.io`…) passe. `fetch_url` lui-même n'est pas restreint.

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
un site sans favicon valide ne coûte pas trois requêtes à chaque page. Un échec
**transitoire** (timeout, erreur réseau, 5xx, 408, 429 sur un candidat, `probe_favicon`) n'est
gardé que `FAVICON_FAILURE_TTL_S` (5 min) : il ne fige pas « pas de favicon » pour 24 h ; un
échec définitif (404, corps qui n'est pas une image, trop gros, hôte refusé) garde la durée
entière. Un `href` d'icône que `urljoin` refuse (`http://[::1`) écarte ce seul candidat.

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
re-télécharge. Borné à `max_bytes` octets utiles, décompression comprise (défaut 5 Mo,
`MIAOU_WEB_RESOURCE_MAX_BYTES`), troncature notée dans le descripteur. Erreurs réseau et schéma non http/https retournés comme
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

Ces deux outils ont remplacé les serveurs `mcp_ddg` et `mcp_brave` (supprimés le
2026-10-17). Un seul outil par
type de recherche, qui choisit lui-même son moteur : `search` essaie les moteurs dans
l'ordre configuré et s'arrête au premier qui **répond**. `image_search` fait de même
parmi les moteurs qui savent chercher des images (aujourd'hui Brave seul).

**Config.** Bloc `config` de l'entrée `web` de `config.json`, clé `search` :
`order` (défaut `["brave", "ollama", "ddg"]`), et un bloc par moteur à clef
(`brave.api_key`, `ollama.api_key`, sinon `BRAVE_API_KEY` / `OLLAMA_API_KEY`). Le
bloc prime sur l'environnement parce que `os.environ` est partagé par tout le
processus : plusieurs entrées `web` peuvent porter des clefs différentes (`build(config)`, une instance par
entrée). Règles de `build_chain` :

- un moteur absent de `order` est désactivé ; `order: []` ou `"search": false`
  coupe la recherche (`"search": true` vaut l'absence de la clé) ;
- un moteur à clef sans clef est écarté, avec une ligne au démarrage ;
- si `order` cite des moteurs et qu'**aucun** n'est utilisable, la construction
  lève `SearchConfigError` (pas d'outil sans config fonctionnelle). Le proxy écarte alors
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
(l'API Ollama plafonne d'ailleurs à 10). Les URL de résultat passent par
`common.is_result_url` : chaîne non vide, schéma absent ou http/https — un
`javascript:`, `data:` ou `file:`, un entier ou un objet sont écartés (pour les images,
`image_url` invalide écarte le résultat, `page_url`/`thumbnail_url` invalides deviennent
`""`). Un lien SANS schéma passe volontairement : les liens de redirection de DuckDuckGo
ont la forme `//duckduckgo.com/l/?uddg=…` (de mémoire, non remesuré faute d'appel réseau
autorisé) et un filtre « http(s) seulement » viderait ce moteur.

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
plus 4 Ko) avant la fermeture de la `HTTPError`.

**Budget de temps.** Un appel `search` dispose de `SEARCH_BUDGET_S` (25 s) pour toute
la chaîne : il faut rester sous les 30 s de timeout MIAOU→MCP suggérés par défaut. Or
Brave et Ollama en timeout (10 s chacun), plus l'attente puis la requête DDG,
dépasseraient ce délai. Chaque moteur reçoit donc le temps restant, son timeout est
raboté d'autant (`min(ENGINE_TIMEOUT_S, restant)`), et en dessous de 2 s il n'est
même pas tenté. Le timeout urllib porte sur chaque opération socket : c'est une
marge, pas une garantie.

**Moteur DDG.** POST sur l'endpoint HTML de DuckDuckGo (`html.duckduckgo.com/html/`),
parsing stdlib (classes `result__a` / `result__snippet`) : fragile si DDG change son
markup. L'autre mode de panne, plus fréquent : DDG remplace les résultats par un défi
anti-bot (HTTP 202, captcha « select the ducks », formulaire vers `anomaly.js?cc=botnet`)
dès qu'une IP sortante enchaîne quelques requêtes — mesuré le 2026-10-05, curl compris,
après une poignée d'appels en quelques minutes. Le blocage a duré 2 h 15 à 2 h 30 (sonde
d'une requête par quart d'heure, qui ne l'a ni prolongé ni relancé une fois levé) ; seuil
et durée pour une autre IP inconnus. Sans garde, le parser n'y trouve aucun `result__a`
et rend `[]`, indiscernable d'une recherche vide : la page est reconnue à sa classe
`anomaly-modal` (seulement quand aucun résultat n'a été extrait) et le moteur échoue en
`EngineFailure` avec sa pause. Le défi n'est pas contourné.

Pour ne pas le déclencher soi-même, les requêtes sortantes sont espacées d'au moins
`_MIN_INTERVAL_S` (15 s) : un appel attend son créneau, réservé sans verrou (aucun
`await` entre lecture et écriture du créneau), et refuse sans requête si l'attente
dépasserait `_MAX_WAIT_S` (15 s) ou si, après l'attente, il ne reste pas `_MIN_FETCH_S`
(3 s) de budget pour la requête. Attendre plutôt que refuser d'emblée : un refus pousse le
modèle à relancer aussitôt. Valeurs calibrées à l'aveugle, le seuil de DDG n'ayant pas été
mesuré (chaque essai coûte deux heures de blocage). L'état (créneau, pause) est celui de
l'**adresse IP** : un seul `ddg.ENGINE` par processus, partagé par toutes les instances de
`mcp_web` ; plusieurs processus derrière la même IP de sortie (collègues sur le même
réseau) ne se coordonnent pas, seul un proxy partagé les couvre.

**Ajouter un moteur** (ddgs, envisagé entre Ollama et DDG) : un module dans
`search/` avec `name`, `kinds`, `down_until`/`down_reason` et
`async search(kind, query, n, budget_s)`, qui rend une liste normalisée ou lève
`EngineFailure`. Puis son entrée dans `ENGINE_NAMES`/`DEFAULT_ORDER`, et dans
`_KEYED` s'il prend une clef. Pour ddgs en particulier, avant de l'intégrer : son
backend `duckduckgo` frappe le même DDG hors de notre espacement, et `primp` a sa
propre pile TLS, a priori hors de portée de `truststore` (non mesuré).

## `servers/mcp_secret_demo.py` — démonstration des secrets volatils (port 8772)

Serveur jouet du lot AU : de quoi essayer de bout en bout, sans service réel, un mot de
passe saisi dans MIAOU et tenu en mémoire par le proxy (`docs/proxy.md`, « Secrets
volatils » ; contrat : `docs/miaou-contract.md`). Entrée `secret_demo` de
`config.defaults.json`, désactivée.

Il se comporte comme un serveur qui s'authentifie au démarrage, et c'est le contrat
qu'un serveur à secret volatil doit tenir :

- **`check_config(config)`**, crochet facultatif lu par le proxy : complétude seulement
  (`login` non vide, un `password` par instance), sans rien contacter, marqueurs
  `{"volatile": …}` encore en place (un objet, donc « présent »). Lève
  `ConfigIncomplete` (`mcp_base`) : le proxy écarte l'upstream au démarrage, qui n'est
  alors pas consommateur du secret. `build` l'appelle en premier.
- **`build(config)`** reçoit la config résolue (une chaîne à la place du marqueur), et
  **s'authentifie avant de construire** : la valeur attendue (`expected`, sinon
  `MIAOU_SECRET_DEMO_EXPECTED`, sinon `miaou`) passe ; `injoignable` simule un annuaire
  en panne (`ConnectionError`, verdict indéterminé : l'utilisateur n'a rien à
  ressaisir) ; tout le reste lève `CredentialsRejected` (`mcp_base`), seul échec que
  MIAOU présente comme une saisie ratée. Avec `instances` (liste de sous-blocs, même
  clef), chacune est testée, et la première refusée fait tout refuser. `delay_s`
  (borné à [0, 10] s, NaN ramené à 0) simule un annuaire lent. Synchrone, comme un vrai
  appel bloquant : le proxy appelle `build` hors de sa boucle d'événements.
- `--help` (`-h`) est servi avant toute authentification : le bloc `__main__` ne
  demande `MIAOU_SECRET_DEMO_PASSWORD` qu'ensuite.
- Jamais la valeur dans un message d'exception, une description d'outil ou les
  `instructions` (le proxy masque quand même, et n'écrit pas dans son cache une
  instance qui la cite).

Un outil, `whoami()` : le login et le nombre d'instances, jamais le mot de passe.
`instructions` publiées, pour voir celles d'un upstream en attente servies depuis le
cache. Une skill **facultative**, `secret-demo` (`servers/skills/secret_demo/`, publiée
`skill://secret_demo/secret-demo/SKILL.md`), pour la même raison : resservie du cache
pendant l'attente, lecture refusée en `SECRET_REQUIRED`. Aucun outil ne l'exige (pas de
`requires_skill`) : un client qui appelle `whoami` n'a rien à lire d'abord — le verify
Playwright de MIAOU l'appelle par un modèle stubé. Son texte ne cite pas la valeur, sans
quoi rien du serveur n'irait au cache, et ne dit rien de la conduite à tenir sans mot de
passe : c'est précisément quand elle n'est pas lisible (lecture en `SECRET_REQUIRED`). `skills_dir` est passé par le constructeur, donc
par `build(config)`. Pas de singleton `mcp` : un secret volatil exige `build(config)`. En autonome, le
mot de passe vient de `MIAOU_SECRET_DEMO_PASSWORD` (login : `MIAOU_SECRET_DEMO_LOGIN`,
défaut `demo`).

## `servers/mcp_docs/` — extraction de documents (port 8771)

> **Obsolète, désactivé par défaut.** MIAOU ouvre désormais ces cinq formats
> lui-même (zip, PDF, Excel, Word, PowerPoint), sans serveur. Ce serveur reste en
> place et intact pour un seul usage, qui reste réel : le travail **hors
> connexion**, l'ouverture native de MIAOU téléchargeant ses moteurs depuis un CDN.
> `config.defaults.json` porte `disabled: true` sur son entrée `docs` ; le retirer
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

