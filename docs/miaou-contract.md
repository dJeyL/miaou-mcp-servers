# Surface de contact avec MIAOU

Ce que MIAOU attend d'un serveur MCP, comment l'y connecter, et le contrat
partagé avec `mcp_docs`.

## Transport et configuration MIAOU

Les trois serveurs utilisent **streamable-http** (JSON-RPC 2.0, endpoint unique POST `/mcp`,
réponses JSON ou SSE `event:message`/`data:`). C'est le transport implémenté par MIAOU V2.

Pour connecter les serveurs depuis MIAOU → Paramètres → Serveurs MCP :

| Champ | bench | weather | web | ddg (déprécié) | brave (déprécié) | docs | proxy |
|---|---|---|---|---|---|---|---|
| Nom | `bench` | `weather` | `web` | `duckduckgo` | `brave` | `docs` | `proxy` |
| URL | `:8766/mcp` | `:8767/mcp` | `:8768/mcp` | `:8769/mcp` | `:8770/mcp` | `:8771/mcp` | `:8765/mcp` |
| Transport | `streamable-http` | idem | idem | idem | idem | idem | idem |

(préfixe `http://127.0.0.1` pour toutes les URLs)

En pratique : passer par le **proxy** expose tous les outils préfixés sur un seul port.
Les noms exposés côté proxy dépendent des clés dans `config.json` (`mcpServers`).


## MIAOU servi par le proxy (`/app/`, lot AO)

Quand `config.json` porte `miaou_dist` (dossier `dist/` d'un clone de MIAOU), le proxy
sert MIAOU à `http://<hôte>:<port>/app/` et redirige `/` vers `/app/` (détail :
`docs/proxy.md`, « MIAOU servi sous `/app/` »). Ce que ça engage côté MIAOU :

- `/app/` rend `miaou.html` ; tout autre fichier du dossier est servi sous `/app/<chemin>`.
  La portée naturelle d'un service worker posé dans `dist/` est donc `/app/`, qui
  n'englobe ni `/mcp` ni les routes OAuth.
- Le manifeste s'appelle **`manifest.webmanifest`**, à la racine du dossier : c'est le
  seul nom figé côté proxy (avertissement au démarrage s'il manque). Les icônes, elles,
  sont lues dans son `icons[].src` — les nommer et les ranger est l'affaire de MIAOU.
- Tout part en `Cache-Control: no-cache` avec `ETag`/`Last-Modified` : chaque
  chargement revalide, un `git pull` est vu au rechargement suivant.
- Types fixés par le proxy pour `.html`, `.js`, `.json`, `.webmanifest`, `.png`,
  `.svg`, `.ico` ; une autre extension dépend du `mimetypes` de la machine.
- Aucun fichier caché n'est servi, et un `dist/` qui contient `config.json` ou `.git`
  est refusé au démarrage.
- `/app/` reste public quand l'auth entrante du proxy est active.

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

Second usage : `search` et `image_search` de `mcp_web` posent
`_meta["miaou/search"] = {"engine": "<nom>"}` (`brave`, `ollama`, `ddg`), le moteur
qui a répondu dans la chaîne de repli. La clé est distincte de `miaou/web` pour ne pas
passer pour un en-tête de page vide. Elle est absente quand aucun moteur n'a répondu :
le résultat n'est alors qu'un texte d'échec. Le même `engine` figure dans le JSON
servi au modèle, mais c'est la clé `_meta` qui fait foi pour l'affichage.

Précédent neuf des deux côtés : jusque-là MIAOU ne lisait que le `_meta` de `tools/list`
(`miaou/unauthorized_upstreams`), et aucun serveur de ce dépôt n'en posait sur un appel. Le
proxy relaie ce `_meta` quel que soit le type d'upstream (cf. `docs/proxy.md`) ; vérifié sur
le vrai transport streamable-http, en inprocess comme derrière un upstream http.

Une clé n'en est jamais relayée : `io.modelcontextprotocol/serverInfo`, la signature dont
un upstream moderne (révision 2026-07-28) marque ses résultats. En ère moderne, le
`serverInfo` qu'un résultat du proxy porte est donc toujours celui du proxy
(`miaou-proxy`), posé par son propre SDK ; relayée, celle de l'upstream l'aurait
remplacé, le SDK ne posant la sienne que si la clé est absente. MIAOU l'ignore
aujourd'hui.

### `x-mcp-header` absent des schémas publiés par le proxy

Un upstream moderne peut annoter un paramètre `x-mcp-header`, qui demande au client de
le recopier dans un en-tête `Mcp-Param-*` ; un serveur 2.x refuse en 400 `-32020` un
`tools/call` moderne qui ne le fait pas. Le proxy retire l'annotation de tout ce qu'il
publie : c'est son propre SDK qui émet l'en-tête vers l'upstream. MIAOU n'a donc aucun
`Mcp-Param-*` à poser vers le proxy. Un serveur annotant `x-mcp-header` configuré EN
DIRECT dans MIAOU le reste, lui, exigeant.

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
   upstream inprocess sert une skill, ou qu'un upstream stdio/http négocié en moderne
   DÉCLARE l'extension — même avec un catalogue vide. Rien sinon. En legacy, `initialize` ne publie jamais
   `extensions` ; il annonce en revanche `resources` quand une skill est servie.
2. **URI préfixées du nom d'upstream** : `skill://bench/SKILL.md` de l'upstream `bench`
   devient `skill://bench/bench/SKILL.md`. Octets inchangés, empreintes valides ; le
   dernier segment reste le `name`.
3. **`skills/list`, `skills/get`** : entrées de tous les upstreams vivants, URI
   réécrites dans `uri` ET chaque `resources[].uri`, `resultType: "complete"`, sans
   pagination. `ttlMs: 300000` et `cacheScope: "public"` au plus large ; plus court et
   `private` si un upstream relayé le dit. URI non servie (ou d'annexe) sur `skills/get`
   → `-32602`. Une entrée d'upstream tiers est relayée telle quelle (seules celles dont
   les URI ne sont pas `skill://` sont écartées) : elle peut être invalide, à vérifier
   comme toute entrée (nom, dernier segment, `description`, empreintes), et `resources`
   peut valoir `"dynamic"`. Un upstream en panne est omis, les autres répondent.
4. **`resources/read`** d'une URI `skill://<upstream>/…` : contenu de l'upstream relayé
   tel quel (texte si UTF-8 valide, blob sinon), sous l'URI du client ; fichier non
   servi → `-32602`. Pour un upstream inprocess, « servi » veut dire déclaré par une
   entrée ; pour un upstream stdio/http, c'est lui qui juge — une skill servie mais non
   listée se lit donc par son URI. Panne de l'upstream : `-32603` lisible, jamais une
   erreur de code 0. Sur le fil moderne, l'en-tête `Mcp-Name` doit
   valoir `params.uri` (400 sinon) — `MCP_NAME_BEARING_METHODS` de MIAOU le porte déjà.
5. **`tools/list`** : un outil dont l'upstream déclare `_meta["miaou/requiresSkill"]` le
   garde, URI réécrite. La valeur est l'URI du `SKILL.md`, relative au serveur qui liste
   l'outil.
6. **`instructions`** : la section d'un upstream qui sert des skills se termine par un
   bloc généré, une ligne par skill servie, obligatoire ou facultative : `name`, URI
   préfixée, statut, `description` (absente ou coupée à 1 024 caractères pour un tiers).
   Forme exacte : `docs/proxy.md`. Le texte libre d'un upstream dont les skills sont
   relayées voit ses URI `skill://` préfixées comme les entrées ; celui d'un upstream
   legacy est publié tel quel. Le bloc est statique : un upstream mort en cours de vie y
   reste annoncé.
7. **Outil de repli `read_skill(uri)`**, nom nu, publié seulement si une skill est
   servie, marqué `_meta["miaou/skillsFallback"] = true` : un client qui lit les skills
   lui-même le masque par cette marque, pas par son nom. Texte rendu étiqueté du serveur
   d'origine. Aucune approbation côté serveur. Le repli d'un upstream dont les skills
   sont relayées (un proxy pris comme upstream) n'est pas republié ; celui d'un upstream
   legacy l'est, préfixé (`<up>__read_skill`), avec sa marque.

**Proxy chaîné.** Un proxy pris comme upstream d'un autre est un upstream http moderne qui
déclare l'extension : ses skills ressortent sous un préfixe double
(`skill://up/bench/bench/SKILL.md`), dernier segment toujours égal au `name`, empreintes
inchangées. Deux upstreams d'un même proxy peuvent ainsi servir deux skills de même
`name` sous la même carte serveur : la clé d'approbation (carte, `name`) de MIAOU les
confond.

Mesuré avec MIAOU actuel (qui ne parle pas l'extension) : rien de cassé, et le modèle
lit la skill de bench par `read_skill` puis applique sa règle. Le premier réflexe d'un
modèle était `miaou__skills__read` avec le nom `bench` — le message système de MIAOU
apprend à lire une skill par son slug. L'instruction de bench (« sa skill MCP `bench`,
par son URI ») et l'en-tête du bloc (« pas des skills locales : leur nom ne suffit
pas ») ont été écrits pour ça, et la description de `read_skill` le redit.

## Mise à jour du proxy (extension `miaou/self-update`, lot AT)

Publiée seulement si `self_update` vaut `true` dans la config ; sinon ni extension, ni
méthode (`-32601`). **Ère 2026-07-28 seulement** : `extensions["miaou/self-update"] = {}`
dans `server/discover` vaut découverte, et en legacy `initialize` ne publie jamais
`extensions`. Deux méthodes JSON-RPC hors outils, absentes de `tools/list` : le modèle ne
les voit pas. Mise en œuvre : `mcp_proxy/updater.py` (extension) et `selfupdate.py` (git).

**`miaou/update/status`** `{ refresh?: bool }` → `{ status: {…} }`. Lit l'état mis en
cache par la sonde du proxy (au démarrage, puis toutes les 5 min) ; `refresh: true` force
une sonde, sauf si la dernière date de moins de 30 s. Champs de `status` :

| Champ | Valeur |
|---|---|
| `state` | voir ci-dessous |
| `detail` | cause brute (sortie de git condensée) ou `null` |
| `remedy` | commande à lancer à la main, ou `null` (`repo_refused`, `auth_required`) |
| `branch`, `remote`, `remote_ref` | branche courante et ce qu'elle suit (`refs/heads/…`), lus dans la config de suivi |
| `head` | sha de HEAD |
| `target` | sha annoncé par le remote : **la valeur à renvoyer à `apply`** |
| `ahead`, `behind` | commits d'avance et de retard sur `target` |
| `dirty` | fichiers suivis modifiés (20 au plus), `[]` sinon |
| `update` | `null`, ou `{ kind, paths, subjects, subjects_total }` quand une mise à jour existe |
| `checked_at` | date ISO 8601 UTC de la sonde, `null` avant la première |
| `supervised` | `MIAOU_PROXY_SUPERVISED=1` présente : sans elle, `apply` est refusé |
| `applying` | une application est en cours |
| `served_build` | `build` de `<miaou_dist>/version.json`, relu à chaque appel ; `null` sans `miaou_dist` |

`state` : `checking` (première sonde pas encore rendue), `up_to_date`, `behind`
(mise à jour disponible), `dirty` (mise à jour disponible, bloquée par `dirty`),
`ahead`, `diverged`, `not_tracking` (HEAD détaché, branche sans upstream, branche
distante supprimée), `auth_required`, `fetch_failed` (dont timeout), `not_a_clone`,
`git_missing`, `repo_refused` (`safe.directory`), `git_error`. Un arbre sale sans mise à
jour reste `up_to_date`, avec `dirty` rempli.

`update.kind` : `miaou` (seuls des fichiers de `miaou_dist` changent), `proxy` (aucun),
`both`. `subjects` : sujets des nouveaux commits sans les merges, plus récents d'abord,
20 au plus ; `subjects_total` les compte tous. `paths` : nombre de fichiers touchés.

**`miaou/update/apply`** `{ target: <sha complet> }` → `{ applied: true, restart, kind,
before, target }`. Avance la branche **exactement** à `target`, en fast-forward, jamais
au-delà. `restart` vaut `false` pour `kind: miaou` : rien ne redémarre, `/app/` sert les
nouveaux fichiers aussitôt et `served_build` change ; l'état publié est relu tout de
suite. Sinon, la réponse partie, le proxy s'arrête en douceur et sort en 42 ; son
superviseur (`scripts/supervise.sh`) le relance par `uv run`. La session MCP meurt avec
lui : le client se reconnecte, puis relit l'état.

Refus : erreur JSON-RPC, code applicatif dans `error.data.code` (à tester par égalité),
message lisible, et selon le cas `data.before`, `data.target`, `data.remedy`.

| `data.code` | `error.code` | Cas |
|---|---|---|
| `UPDATE_NOT_SUPERVISED` | `-32600` | variable du superviseur absente |
| `UPDATE_INVALID_TARGET` | `-32602` | `target` n'est pas un sha complet |
| `UPDATE_IN_PROGRESS` | `-32600` | une autre application tourne |
| `UPDATE_RESTARTING` | `-32600` | une sortie est déjà demandée (`miaou/lifecycle/restart`, ou mise à jour qui redémarre) : le proxy s'arrête |
| `UPDATE_TOOLS_IN_FLIGHT` | `-32600` | appels d'outils en cours, tous clients confondus (jamais pour `kind: miaou`) |
| `UPDATE_STALE` | `-32600` | `target` déjà atteint, ou plus atteignable en avance rapide (branche réécrite, autre application) : relire l'état |
| `UPDATE_REFUSED` | `-32600` | arbre sale, remote injoignable, git a refusé avant de toucher l'arbre… (cause dans le message) |
| `UPDATE_ROLLED_BACK` | `-32603` | checkout interrompu, arbre remis à l'état d'avant |
| `UPDATE_ROLLBACK_FAILED` | `-32603` | retour arrière en échec : `data.remedy` donne la commande |

Tout refus laisse le proxy en marche et l'arbre dans son état d'avant (sauf le dernier).

## Cycle de vie du proxy (extension `miaou/lifecycle`, lot AV)

La config a-t-elle changé depuis le démarrage du proxy, et redémarrage à la demande.
**Publiée par défaut**, retirée par la clef de config `"lifecycle": false` (méthodes et
déclaration ensemble), **ère 2026-07-28 seulement** :
`extensions["miaou/lifecycle"] = {}` dans `server/discover`. Un client traite un proxy
qui ne la déclare pas comme un proxy antérieur à l'extension : ni état de config, ni
redémarrage proposé. Deux méthodes JSON-RPC hors
outils, absentes de `tools/list`. **Aucune garde d'origine** : MIAOU en `file://` s'en
sert comme MIAOU servi sous `/app/` ; le bloc `auth`, quand il est posé, est la seule
barrière (limite : toute page ouverte dans le navigateur peut demander un redémarrage).
Mise en œuvre : `mcp_proxy/lifecycle.py` (extension) et `configwatch.py` (config).

**`miaou/lifecycle/status`** `{}` → `{ status: {…} }`. Relit la config à chaque appel
(trois petits fichiers, une fusion, un hash) : « vérifier » = rappeler `status`, il n'y a
ni sonde ni cadence côté proxy.

| Champ | Valeur |
|---|---|
| `instance` | identité du processus, tirée une fois au démarrage (16 hexadécimaux) ; la même que celle de `miaou/secrets/status`. Un `instance` différent du dernier lu = un autre processus |
| `started_at` | date ISO 8601 UTC du démarrage |
| `restartable` | `MIAOU_PROXY_SUPERVISED=1` ET `MIAOU_PROXY_RESTART_BY_EXIT=1` : un superviseur relance sur 43. Faux : `restart` sera refusé, redémarrage à faire à la main |
| `restarting` | une sortie est demandée (ce `restart`, ou une mise à jour appliquée), l'arrêt est en cours |
| `config.state` | `unchanged`, `changed`, `invalid` |
| `config.changed` | chemins des clefs changées depuis le démarrage, triés, 20 au plus ; `[]` si `unchanged` |
| `config.changed_total` | leur nombre total |
| `config.detail` | cause brute si `invalid` (couche illisible, refus de config, port qui ne s'ouvre pas), sinon `null` |
| `config.layers` | fichiers lus, dans l'ordre, chemins tels que résolus ; `[]` si la chaîne ne se résout plus |
| `config.checked_at` | date ISO 8601 UTC de cette lecture |

**Ce qui compte comme changement** : le contenu fusionné des couches, AVANT substitution
des `${VAR}`, sans les clefs `_comment*`. Un fichier ré-enregistré à l'identique, ou
reformaté, ou dont seul un commentaire change, reste `unchanged`. Un `config.json` apparu
ou retiré depuis le démarrage compte (chaîne par défaut) ; un `--config` disparu rend
`invalid`. Une variable d'environnement changée n'est pas une modification de config.

**Chemins** : `mcpServers.<nom>` pour un serveur (ajouté, retiré ou modifié, sans plus de
détail), la clef racine pour le reste (`port`, `host`, `auth`, `miaou_dist`,
`self_update`…). Jamais de valeur. Un chemin reste listé même quand une option de la
ligne de commande le rend sans effet (`--port` pour `port`) : c'est un fait,
l'interprétation est au client — les chemins qui le couperont du proxy relancé (`port`,
`host`, `auth`, et `miaou_dist` quand la page est servie par ce proxy) sont à
annoncer AVANT le redémarrage.

**`invalid`** : la config modifiée ne permettrait pas au proxy de redémarrer. Elle est
validée par la fonction même du démarrage, avec les mêmes options de ligne de commande :
tout refus qui ferait sortir le proxy en 1 y est un `invalid`, avec le même message. Si
l'hôte ou le port effectifs changent, un `bind` d'essai s'y ajoute. Un `invalid` peut être
transitoire (fichier en cours d'écriture par un éditeur). Ce que la validation ne voit
pas : un module inprocess qui ne s'importe plus (l'upstream est écarté, le proxy démarre)
et un port pris entre l'essai et la relance.

**`miaou/lifecycle/restart`** `{ force?: bool }` → `{ restarting: true, instance }`
(`instance` du processus qui s'arrête). La réponse partie, le proxy s'arrête en douceur
(5 s pour les requêtes en cours) et sort en 43 ; `scripts/supervise.sh` le relance par
`uv run`, avec la config relue. Jamais d'exec sur place. Le nouveau processus se reconnaît
à un `instance` différent dans `status`. La session MCP meurt avec l'ancien, chez tous les
clients ; les secrets volatils sont à renvoyer (`instance` change aussi dans
`miaou/secrets/status`). Accepté que la config ait changé ou non. Un `restart` déjà
accepté, ou une sortie déjà demandée par une mise à jour, rend la même réponse sans rien
redemander (deux onglets, deux clics).

Refus, **dans cet ordre**, en erreur JSON-RPC `-32600`, code applicatif dans
`error.data.code` (à tester par égalité) :

| `data.code` | Cas | `data` en plus |
|---|---|---|
| `RESTART_NOT_SUPERVISED` | `restartable` faux | — |
| `RESTART_UPDATE_IN_PROGRESS` | une mise à jour (`miaou/update/apply`) est en cours d'application | — |
| `RESTART_CONFIG_INVALID` | la config relue à l'instant est `invalid` (`bind` d'essai refait) | `detail` : la cause, aussi dans le message |
| `RESTART_TOOLS_IN_FLIGHT` | appels d'outils en cours, tous clients confondus | `count` |

`force: true` ne lève que le dernier : les appels en vol sont coupés (un upstream bloqué,
dont l'appel ne finira jamais, est souvent ce que le redémarrage doit guérir). Le nombre à
afficher dans la confirmation est `data.count` du refus, jamais un compte local.
Réciproquement, une fois une sortie demandée, `miaou/update/apply` est refusé en
`UPDATE_RESTARTING`.

## Secrets volatils (extension `miaou/volatile-secrets`, lot AU)

Un mot de passe (ou une clef) que l'utilisateur saisit dans MIAOU, que le proxy tient
**en mémoire seulement**, et qu'il substitue au marqueur `{"volatile": "<clef>"}` du
`config` d'un upstream `inprocess` avant son `build(config)` (config : `docs/proxy.md`,
« Secrets volatils »). Mise en œuvre : `mcp_proxy/volatile.py` (config) et
`volatile_store.py` (extension).

**Découverte.** `extensions["miaou/volatile-secrets"] = {}` dans `server/discover`, **ère
2026-07-28 seulement**, publiée s'il reste au moins un upstream **consommateur** après le
démarrage (un upstream à clef dont `check_config` a passé) ; sinon ni extension, ni
méthode (`-32601`). Trois méthodes JSON-RPC hors outils, absentes de `tools/list`.

**Garde, sur les trois méthodes.** Acceptées seulement si l'en-tête `Host` vaut
`127.0.0.1:<port>` ou `localhost:<port>` (port d'écoute du proxy) **et** si `Origin`
vaut `http://<Host>` : MIAOU servi par ce proxy sous `/app/`, avec une carte qui vise la
même adresse. Tout le reste — `Origin: null` de `file://`, autre origine, autre nom que
celui de la page, DNS rebinding, `Origin` absent — est refusé en
`SECRET_ORIGIN_REFUSED`, dont le message donne l'adresse de `/app/` à ouvrir.

**Entrée d'une clef**, rendue par les trois méthodes :

```json
{ "key": "Mot de passe SSO", "held": true,
  "consumers": [ { "upstream": "api_prod", "state": "ready", "verdict": "accepted", "detail": null },
                 { "upstream": "logs", "state": "pending", "verdict": "untested", "detail": "non testé : …" } ] }
```

| Champ | Valeur |
|---|---|
| `key` | la clef, telle que la config la déclare (libellé à afficher) |
| `held` | au moins un consommateur est construit avec une valeur acceptée |
| `consumers[]` | upstreams qui attendent cette clef, dans l'ordre de la config (celui du test) |
| `state` | `pending` (non construit, ses appels refusés en `SECRET_REQUIRED`) ou `ready` |
| `verdict` | issue de la dernière saisie : `null` (aucune, ou après « Oublier »), `accepted`, `rejected` (identifiants refusés par le service), `indeterminate` (toute autre panne : annuaire injoignable, délai…), `incomplete` (config incomplète, rien contacté), `untested` (un consommateur précédent a refusé) |
| `detail` | cause lisible d'un verdict autre qu'`accepted`, valeur masquée (`***`), ou `null` |

Après un `rejected` sur un consommateur déjà `ready` (remplacement à chaud), il reste
`ready` : son instance précédente sert toujours.

**`miaou/secrets/status`** `{}` → `{ status: { instance, secrets: [entrée…] } }`.
`instance` : identifiant tiré au démarrage du proxy (16 hexadécimaux), le même que celui
de `miaou/lifecycle/status` ; un autre que le dernier lu veut dire que le proxy a
redémarré, donc oublié toutes les valeurs.

**`miaou/secrets/submit`** `{ key, value }` → `{ secret: entrée }`. Validation =
construction réussie, **séquentielle** dans l'ordre des consommateurs, **arrêtée au
premier refus** (une saisie fausse coûte un échec à l'annuaire, pas un par
consommateur) ; `indeterminate` et `incomplete` n'arrêtent pas. Gardes contre le
verrouillage de compte : une valeur **refusée** n'est jamais retestée tant que le proxy
tourne (verdict `rejected`, `detail` le dit) ; une valeur **identique** à celle qu'un
consommateur a acceptée ne le reconstruit pas (aucun appel sortant) — c'est ce qui rend
sans danger le renvoi simultané par plusieurs onglets (les dépôts d'une même clef sont
sérialisés). Un consommateur `ready` qui refuse la nouvelle valeur garde son instance.
Un appel d'outil en vol finit sur l'instance qui l'a reçu. Après chaque changement
d'instance, le proxy republie ses `instructions` et ses skills (visibles à la
prochaine connexion) : MIAOU doit reconnecter le serveur après une acceptation, faute
de `notifications/tools/list_changed`.

**`miaou/secrets/forget`** `{ key }` → `{ secret: entrée }`. Jette les instances
construites (le serveur garde ses identifiants dans son objet) et remet les
consommateurs en attente. Les valeurs refusées restent mémorisées comme telles.

Refus d'une requête (erreur JSON-RPC, `error.data.code` à tester par égalité, message
qui ne cite jamais la valeur) :

| `data.code` | `error.code` | Cas |
|---|---|---|
| `SECRET_ORIGIN_REFUSED` | `-32600` | garde `Host`/`Origin` |
| `SECRET_INVALID_PARAMS` | `-32602` | `key` absente ou vide ; `value` absente, vide, non textuelle ou de plus de 4096 caractères |
| `SECRET_UNKNOWN_KEY` | `-32602` | aucun consommateur n'attend cette clef |

**`tools/call` d'un upstream en attente** : erreur JSON-RPC `-32600`, `data = { code:
"SECRET_REQUIRED", upstream }` (sans la clef : un appel d'outil n'est pas gardé par
l'origine), message adressé au modèle (seul l'utilisateur peut agir, depuis MIAOU ; le
même appel fonctionnera après la saisie). Un appel arrivé pendant la validation est
refusé : vrai à l'instant de l'appel. Les outils d'un upstream en attente sont listés
**sans notice**, à l'octet près comme une fois construit (cache disque), ses
`instructions` aussi ; au tout premier démarrage d'une installation (cache vide), ils
n'apparaissent qu'après la première saisie acceptée. Aucun `_meta` de `tools/list` ne
signale l'attente : l'état se lit par l'extension.

**La valeur** n'est jamais écrite sur disque, journalisée, ni rendue par une méthode,
pas même masquée. Le proxy ne garde que des empreintes (HMAC, sel propre au processus),
et l'instance construite par le serveur la détient. Elle est perdue à tout redémarrage
du proxy (SIGUSR1, mise à jour, `miaou/lifecycle/restart`) : `instance` change, les consommateurs
repassent `pending`, et c'est au client de renvoyer une valeur qu'il détient comme
acceptée.

**Hors périmètre** : upstreams `stdio` et `http` ; proxy chaîné (un proxy qui agrège un
proxy porteur de secrets ne relaie pas l'extension) ; mot de passe qui expire en cours
de vie (le serveur rend alors une erreur d'outil ordinaire, l'utilisateur passe par
« Oublier »).

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

