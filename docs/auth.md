# Auth OAuth (campagne AB)

Deux mécanismes distincts et sans rapport entre eux : le proxy *vérifie* les jetons
de ses clients (entrante), et il en *obtient* auprès de serveurs tiers (sortante).

## Auth OAuth entrante (lot AB-1)

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

## `dev_auth_server.py` — Authorization Server de développement (lot AB-1.3)

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

## Auth OAuth sortante (lot AB-2) — stockage des jetons

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

## Le parcours d'autorisation

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

## Scopes : qui décide, et le 403 qui n'est pas une panne

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

## Le troisième état : « connu mais pas autorisé »

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

## Où l'on autorise, et à qui on le dit (lot AB-4)

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

## `HttpUpstream` : le transport vit dans sa propre tâche

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

