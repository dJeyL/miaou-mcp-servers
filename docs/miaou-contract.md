# Surface de contact avec MIAOU

Ce que MIAOU attend d'un serveur MCP, comment l'y connecter, et le contrat
partagé avec `mcp_docs`.

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


## Ce que MIAOU attend d'un serveur MCP

1. `initialize` (handshake JSON-RPC) → capte `Mcp-Session-Id`
2. `notifications/initialized`
3. `tools/list` → liste les outils, les préfixe du nom de serveur, les met en cache
4. Pour chaque appel : `tools/call { name, arguments }` → `{ content: [...blocks], isError }`

Blocs de résultat : `text` (D9), `image`/`resource` binaire (D8.1), `resource` texte (D8.2).
Si `isError: true`, MIAOU marque l'ack en rouge dans le thread.

La session ouverte par `initialize` ne s'expire pas d'inactivité :
`SESSION_IDLE_TIMEOUT_S = None` (`servers/mcp_base.py`, repris par le proxy) neutralise
les 30 min par défaut du SDK 2.x. MIAOU sait ré-initialiser une session tuée (un 404 sur
un `Mcp-Session-Id` connu), mais ces serveurs tournent en local : on évite l'aller-retour
de reconnexion après chaque pause.

### `_meta` d'un résultat `tools/call` (lot AI)

Un résultat peut porter, à côté de `content`, un `_meta` adressé au **client** et jamais
servi au modèle. Premier usage : `fetch_url` de `mcp_web` pose
`_meta["miaou/web"] = {title, site_name, canonical_url, favicon}`, tous facultatifs, pour
que MIAOU affiche la source d'une citation (libellé, favicon) sans rien payer en contexte.
Détail des champs et de leur validation : `docs/servers.md`, section `mcp_web`. `favicon`
est une data-URL dont le type a été reconnu aux octets (PNG, ICO, GIF, JPEG, WebP ; jamais
SVG), plafonnée à 16 384 caractères : le client la revalide quand même avant de la poser.

Précédent neuf des deux côtés : jusque-là MIAOU ne lisait que le `_meta` de `tools/list`
(`miaou/unauthorized_upstreams`), et aucun serveur de ce dépôt n'en posait sur un appel. Le
proxy relaie ce `_meta` quel que soit le type d'upstream (cf. `docs/proxy.md`) ; vérifié sur
le vrai transport streamable-http, en inprocess comme derrière un upstream http.

### `instructions` de l'`InitializeResult`

Le proxy publie une consigne de portée serveur dans le champ `instructions` de
l'`InitializeResult` (cf. `docs/proxy.md`) : c'est le seul emplacement du
protocole pour une consigne qui vaut pour un serveur entier, les seuls champs
relayés au modèle par outil étant `name`/`description`/`inputSchema`. MIAOU lit
ce champ sur la réponse à `initialize` — la même dont il capte
`Mcp-Session-Id` — et l'injecte dans le system prompt, rattaché à son serveur
d'origine.

Le préambule écrit par le proxy énonce `<serveur>__<outil>`, forme vraie pour
un client qui lui parle en direct. MIAOU re-préfixe du slug de la carte serveur
(`miaou-proxy__bench__echo` ici, `proxy__…` chez un collègue), et est seul à
connaître ce slug — raison pour laquelle le proxy ne le porte pas en config :
l'y mettre dupliquerait une donnée qui vit côté client, avec dérive garantie au
premier renommage de carte serveur.

Vérifié de bout en bout : sur un `gemma4:e4b` local, la ligne témoin de
`mcp_bench` apparaît après `dns_lookup` et `reverse_dns`, et pas après
`get_weather`. Le marqueur portant le mot `bench`, cette asymétrie atteste que
le champ est lu ET rattaché au bon serveur malgré le double préfixe.

## Skills servies par le proxy (extension `io.modelcontextprotocol/skills`)

Un serveur peut servir des skills (format Agent Skills, transport de l'extension
SEP-2640), et en exiger une avant l'appel de ses outils. `mcp_bench` en sert une,
obligatoire pour tous ses outils. Ce que le proxy publie (détail de mise en œuvre :
`docs/proxy.md`) :

1. **Capacités, ère 2026-07-28.** `extensions["io.modelcontextprotocol/skills"] = {}`
   (pas de `directoryRead`) et `resources`, dans `server/discover`, dès qu'au moins un
   upstream sert une skill. Rien sinon. En legacy, `initialize` ne publie jamais
   `extensions` ; il annonce en revanche `resources` quand une skill est servie.
2. **URI préfixées du nom d'upstream** : `skill://bench/SKILL.md` de l'upstream `bench`
   devient `skill://bench/bench/SKILL.md`. Octets inchangés, empreintes valides ; le
   dernier segment reste le `name`.
3. **`skills/list`, `skills/get`** : entrées de tous les upstreams vivants, URI
   réécrites dans `uri` ET chaque `resources[].uri`, `resultType: "complete"`,
   `ttlMs: 300000`, `cacheScope: "public"`, sans pagination. URI non servie (ou
   d'annexe) sur `skills/get` → `-32602`.
4. **`resources/read`** d'une URI `skill://<upstream>/…` : contenu de l'upstream relayé
   tel quel (texte si UTF-8 valide, blob sinon), sous l'URI du client ; fichier non
   déclaré par une entrée → `-32602`. Sur le fil moderne, l'en-tête `Mcp-Name` doit
   valoir `params.uri` (400 sinon) — `MCP_NAME_BEARING_METHODS` de MIAOU le porte déjà.
5. **`tools/list`** : un outil dont l'upstream déclare `_meta["miaou/requiresSkill"]` le
   garde, URI réécrite. La valeur est l'URI du `SKILL.md`, relative au serveur qui liste
   l'outil.
6. **`instructions`** : la section d'un upstream qui sert des skills se termine par un
   bloc généré, une ligne par skill servie, obligatoire ou facultative : `name`, URI
   préfixée, statut, `description`. Forme exacte : `docs/proxy.md`.
7. **Outil de repli `read_skill(uri)`**, nom nu, publié seulement si une skill est
   servie, marqué `_meta["miaou/skillsFallback"] = true` : un client qui lit les skills
   lui-même le masque par cette marque, pas par son nom. Texte rendu étiqueté du serveur
   d'origine. Aucune approbation côté serveur.

Mesuré avec MIAOU actuel (qui ne parle pas l'extension) : rien de cassé, et le modèle
lit la skill de bench par `read_skill` puis applique sa règle. Le premier réflexe d'un
modèle était `miaou__skills__read` avec le nom `bench` — le message système de MIAOU
apprend à lire une skill par son slug. L'instruction de bench (« sa skill MCP `bench`,
par son URI ») et l'en-tête du bloc (« pas des skills locales : leur nom ne suffit
pas ») ont été écrits pour ça, et la description de `read_skill` le redit.

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
  Mécanisme : l'outil lève lui-même
  `MCPError(REF_UNKNOWN_ERROR_CODE, "REF_UNKNOWN: …", data={"code": "REF_UNKNOWN"})`
  (`mcp_docs/session.py`). Depuis le SDK MCP 2.x, une `MCPError` levée par un outil
  traverse `MCPServer` en erreur JSON-RPC, `code`/`message`/`data` intacts — le rejeu
  fonctionne donc **en autonome (port 8771 direct) comme derrière le proxy**. (En 1.x, le
  SDK avalait toute exception d'outil en `isError` : il fallait un sentinel dans le texte
  et un wrapper du proxy qui le repêchait, d'où un rejeu réservé au proxy. Les deux ont
  disparu avec la migration.)
  **Côté proxy** : `handle_call_tool` laisse traverser une `MCPError` venue d'un upstream
  **inprocess** — c'est un refus voulu par l'outil. Celle d'un upstream **distant**
  (stdio, http) est la réponse d'erreur de ce serveur-là, que le proxy aplatit en
  `isError` textuel, comme il l'a toujours fait. Le proxy n'importe toujours pas
  `mcp_docs` : un proxy configuré sans `docs` ne charge ni lui ni ses libs de parsing.
- **Taille d'un `content_b64`** : le fichier ENTIER voyage en base64 dans les arguments,
  jusqu'au plafond `MAX_INLINE_BYTES` de MIAOU (64 Mo, environ 85,4 Mo une fois encodé).
  Le SDK 2.x refuse par défaut tout corps de requête au-delà de 4 Mio (413 avant tout
  parsing) : serveurs autonomes et proxy le relèvent à `MAX_REQUEST_BODY_BYTES` (96 Mio,
  `servers/mcp_base.py`). **Couplé à MIAOU** : si son plafond monte, celui-ci suit.
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

