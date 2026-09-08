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

`pyjwt[crypto]` est désormais importé directement, donc déclaré explicitement aux **deux**
endroits qui gouvernent un environnement : `requirements.txt` et `pyproject.toml`. Il y en
avait un troisième, le bloc PEP 723 en tête de `mcp_proxy.py` ; la mise en paquet du proxy
l'a supprimé avec le fichier plat, et c'est `[project.dependencies]` qui couvre désormais
le lancement `uv run mcp_proxy`.

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

**Le parcours est PROVOQUÉ, jamais espéré comme effet de bord d'un `start()`.**
`authorize()` se contentait de (re)démarrer l'upstream, en comptant sur le 401
du handshake pour déclencher l'OAuth. Sur un upstream dont `initialize` — voire
`tools/list` — passe sans jeton, aucune requête n'est refusée : `start()`
réussit, le flow n'est jamais amorcé, et `authorize()` retourne sans rien avoir
fait. On annonçait alors « autorisation accordée » alors qu'aucun jeton n'était
écrit et qu'aucun appel ne partait vers l'AS (payé en production le 2026-09-07 :
ni fichier de jetons, ni trace réseau, pour une page qui disait le contraire).

`authorize()` émet donc une requête à elle sur l'URL de l'upstream, à travers un
client httpx portant le provider en `auth`. Le 401 fait dérouler au SDK son
chemin **nominal** — découverte des métadonnées, enregistrement si besoin,
redirection, échange du code, écriture du jeton. Rien n'est réimplémenté :
mener le flow à la main dupliquerait la moitié du SDK, et deux chemins
d'autorisation divergeraient.

**Cette requête doit aller jusqu'à `tools/call`** (`_provoke_refusal`), et c'est
une mesure, pas une précaution : sur le déploiement d'entreprise qui a servi de
banc (2026-09-07), `initialize` répond **200** et seul `tools/call` renvoie le
401 porteur du `WWW-Authenticate`. Une requête d'amorçage arbitraire — un `ping`
— n'était jamais refusée, donc aucun parcours ne démarrait et la route
concluait « rien à autoriser ». Or `tools/call` ne s'envoie pas nu : le
transport streamable-http exige un `Mcp-Session-Id` obtenu à `initialize` et
rejoué ensuite, faute de quoi il est rejeté hors de toute question
d'autorisation. La séquence complète est donc déroulée — `initialize`,
`notifications/initialized`, `tools/call`.

**L'outil nommé doit EXISTER**, et c'est contre-intuitif. Un premier jet
appelait un nom inventé, sur la foi d'une mesure mal lue : un 401 obtenu sur
`tools/call` semblait prouver que le refus d'autorisation précédait la
résolution du nom. Il ne le prouvait pas — cette mesure portait sur un outil
réel. Reprise avec un nom inventé, elle rend `403 No matching resource found in
the API` : la passerelle (WSO2) route par ressource et rejette un nom inconnu
**avant** toute question d'autorisation. Un nom inventé n'y amorce donc jamais
rien.

La séquence lit donc `tools/list` et choisit un outil **réel** — mais en
**lecture seule** (`_pick_probe_tool` / `_looks_mutating`) : obtenir un jeton ne
doit pas créer un ticket ni envoyer un message. Les verbes d'écriture sont
comparés par segments après découpage sur séparateurs et casse, si bien que
`create_issue`, `createIssue` et `add-comment` sont écartés quand `list_updates`
reste. Aucun candidat sûr → on garde le nom de repli (`_AUTH_PROBE_TOOL`) :
une sonde qui échoue vaut mieux qu'une sonde qui écrit, et sur un serveur qui
refuse avant de résoudre, elle fonctionne.

La liste de verbes est **dupliquée** dans `tests/live_auth_probe.py`, script
autonome (PEP 723) que l'import du proxy alourdirait — même arbitrage que le
helper TLS de `live_call.py`, avec le même prix : toute évolution est à
répercuter.

`tests/live_auth_probe.py` (banc manuel, non collecté par pytest) pose la même
question hors du proxy : ce qu'un upstream répond sans jeton, requête par
requête. C'est lui qui a établi le 401 ci-dessus, après trois correctifs posés
sur des suppositions — un banc d'abord, des correctifs ensuite.

## Quand l'AS refuse la `redirect_uri` loopback

`build_callback_url` dérive `http://127.0.0.1:<port>/callback` de l'adresse
d'écoute. C'est la seule forme qu'un proxy local peut **servir** : le parcours
revient sur une route qu'il expose lui-même, pas sur un listener éphémère.
Beaucoup d'AS d'entreprise la refusent pourtant — loopback interdit, `http` en
clair interdit, ou URL non déclarée dans le client OAuth (WSO2, rencontré en
production le 2026-09-07).

`auth.redirect_uri` **gouverne désormais l'URL réellement annoncée**, et plus
seulement celle déclarée dans les credentials pré-provisionnés
(`build_client_info_override`). Les deux devaient déjà coïncider et rien ne
l'imposait : une divergence produisait un refus côté AS, loin de sa cause. Un
test les compare maintenant sur la même configuration.

Trois issues, par coût croissant, et **aucune n'est du code** :

1. **Changer la forme du loopback** : `http://localhost:<port>/callback` plutôt
   que `127.0.0.1`, ou l'inverse. Certains AS acceptent le nom et refusent l'IP
   littérale. Une ligne de configuration, rien d'autre — c'est ce que
   `auth.redirect_uri` rend possible.
2. **Faire déclarer l'URL côté AS.** La RFC 8252 §7.3 demande à un AS d'accepter
   le loopback pour un client natif, et d'en ignorer le port. Un refus signale
   en général un client enregistré comme « web » plutôt que « public/native »,
   ou une liste blanche incomplète. Se règle côté administration de l'AS.
3. **Déposer un jeton obtenu ailleurs.** Le parcours interactif n'est pas la
   seule voie : le fichier de jetons est un simple JSON, et un `access_token`
   fourni par l'équipe qui administre le service y est relu tel quel. Une entrée
   `{"<upstream>": {"tokens": {"access_token": "…", "token_type": "Bearer",
   "expires_in": 3600}, "expires_at": <epoch>}}` suffit :
   `has_usable_token()` la voit, l'upstream n'est plus marqué « à autoriser », et
   aucun parcours n'est tenté. Sans `refresh_token`, il faudra le renouveler à
   l'expiration — c'est le prix de cette voie, et il est explicite.

## Le renouvellement du jeton (lot AB-3)

Le refresh est **entièrement passif, et c'est le SDK qui le fait**.
`OAuthClientProvider` étant un `httpx.Auth`, httpx l'invoque sur chaque requête
sortante du transport ; en tête d'`async_auth_flow`, s'il voit un jeton expiré et
un refresh possible, il intercale lui-même un POST au token endpoint, met à jour
le jeton, l'écrit par `set_tokens()`, et laisse partir l'appel avec le nouveau
`Bearer`. Aucun appel explicite du proxy, aucune intervention utilisateur.

Deux conditions le neutralisent, et toutes deux ont été payées en production le
2026-09-07.

### Le token endpoint doit être connu, et la découverte ne suffit pas

`_refresh_token()` lit `context.oauth_metadata.token_endpoint`, et **se replie
sinon sur `urljoin(<base du serveur MCP>, "/token")`** — l'hôte du Jira, pas
celui de l'AS. Or `oauth_metadata` n'est peuplé que par la découverte
`/.well-known/...`, qui a lieu dans la branche 401 d'`async_auth_flow`, et qui :

- **échoue** si l'AS ne sert pas ses métadonnées aux trois chemins essayés
  (WSO2/Keycloak sur un realm : seuls les endpoints
  `.../protocol/openid-connect/*` existent) ;
- **n'est jamais persistée** — au redémarrage, `_initialize()` recharge jetons et
  client_info depuis le fichier, mais `oauth_metadata` repart à `None`.

Le POST de renouvellement part alors vers une URL inexistante, échoue, et
`_handle_refresh_response` appelle `clear_tokens()` : **le refresh token est
jeté**, un parcours interactif s'ouvre, et l'utilisateur reclique « Autoriser »
sans qu'un mot n'explique pourquoi (un `logger.warning` en DEBUG est la seule
trace côté SDK).

D'où `auth.authorization_endpoint` + `auth.token_endpoint` en config
(`build_oauth_metadata_override`), posés sur `provider().context.oauth_metadata`
à la construction. **Les deux ou rien** : un token endpoint seul laisserait le
parcours initial rediriger vers un AS découvert et rafraîchir auprès d'un autre —
deux AS pour une même identité est un mode de panne pire que l'absence de
configuration. `issuer` est dérivé de l'authorization endpoint s'il n'est pas
donné : le modèle l'exige, le SDK ne s'en sert pas ici.

Une découverte qui aboutit écrase ces valeurs, et c'est voulu : des métadonnées
fraîches valent mieux que des déclarées. Un échec, lui, ne les efface pas.

### Passif ne suffit pas : l'inactivité

Le refresh n'ayant lieu qu'au passage d'une requête, un upstream qu'on n'appelle
pas pendant assez longtemps voit son access token expirer, **puis son refresh
token**, sans qu'aucun des deux n'ait servi. Le prochain appel, des semaines plus
tard, exige une ré-autorisation manuelle.

`UpstreamAuthorizer.refresh_if_due()`, réveillé par une boucle du lifespan,
renouvelle quand l'échéance lue **en stockage** passe sous la marge. Sous un AS à
rotation — WSO2 le fait, c'est configurable — cela repousse indéfiniment
l'échéance du refresh token.

**Les deux constantes ne sont que des plafonds**, et c'est la correction du
2026-09-08. L'invariant à tenir est que plusieurs réveils tombent dans la fenêtre
« bientôt expiré » avant l'échéance, sinon un réveil manqué (machine en veille)
laisse passer l'expiration. Deux valeurs fixes ne peuvent pas le tenir face à un
AS quelconque : sur des jetons de **5 minutes** — mesuré sur WSO2 — une marge de
15 minutes rend tout jeton « bientôt expiré » dès son émission, et la boucle
renouvelle à chaque réveil, ce qui est exactement le rejeu qu'on veut éviter
devant un AS à rotation.

La marge est donc plafonnée à la **moitié de la durée de vie réellement émise**
(`_refresh_margin`), et la période de réveil dérivée du tiers de la plus courte
marge en vigueur (`_refresh_poll_interval`), recalculée à chaque tour — **sans
plancher fixe**. Un plancher de 30 s rendait la période plus longue que la
fenêtre qu'elle échantillonne dès que les jetons descendent sous la minute : sur
des jetons de **30 s**, que ce WSO2 émet aussi, la marge vaut 15 s pour un réveil
toutes les 30 s, donc une fenêtre sur deux sautée et un réveil qui arrive après
l'expiration. Posé pour « ne pas tourner en boucle serrée », il cassait
l'invariant qu'il devait protéger : c'est la fréquence des jetons courts qui
commande. Un test de propriété (`test_the_window_is_sampled_at_every_scale`)
vérifie l'invariant de 30 s à 24 h plutôt que sur des valeurs choisies. Cette
durée de vie ne se lit qu'**à l'émission** — relu plus tard, `expires_in` est ce
qu'il en RESTE — d'où sa mémorisation par `set_tokens()` sous la clé `lifetime`,
rendue par `observed_lifetime()`.

**La trace dit le GAIN, pas seulement le restant.** `expires_in` relu est ce
qu'il reste à courir : deux traces successives affichent donc des valeurs
différentes pour un AS qui émet toujours la même durée. Lues comme des durées de
jeton, elles ont fait conclure à tort que l'AS émettait des jetons de 30 s
(2026-09-08). Un renouvellement effectif se voit au **gain d'échéance**, jamais
au restant — d'où `+Ns, reste Ns, émis pour Ns`, et un avertissement explicite
quand le gain tombe sous la moitié de la durée émise : l'AS a bougé le jeton sans
en délivrer un neuf, et la boucle perdra la course.

**Trois cas, pas deux.** Entre « renouvelé » et « refusé » il y a l'échéance
**inchangée** : l'AS répond sans rien réémettre — il ne l'a pas jugé nécessaire —
et le seul écart restant est la dérive d'horloge entre les deux lectures. En deçà
de `_RENEWAL_NOOP_FRACTION` de la durée émise, ce n'est donc **pas** un
renouvellement (l'annoncer donnait « renouvelé (+0s) » aussitôt démenti par un
ATTENTION, sur un upstream où tout allait bien) et **pas** une anomalie : le
jeton court jusqu'à son terme, `authorization_pending` reste bas, et
`refresh_if_due` rend `False`. La trace est calme, et posée **une seule fois par
épisode** (`_renewal_noop_reported`, réarmé par le premier vrai renouvellement) :
tant que le jeton reste dans la fenêtre, la boucle repasse à chaque réveil — trois
fois par durée de vie — et répéter le non-événement noierait le reste.

**Un jeton relu n'est pas un jeton frais**, et `set_tokens()` doit s'en garder.
`get_tokens()` écrase `expires_in` par le RESTANT — nécessaire, le SDK ne
repassant pas par `update_token_expiry()` au chargement — mais le SDK garde cet
objet dégradé dans `context.current_tokens` et il lui arrive de le réécrire tel
quel. Pris pour une émission, il donnait `lifetime: 0` et un `expires_at` dans le
passé : le jeton était marqué expiré à l'instant où il venait d'être renouvelé,
et la trace annonçait « valide 0s ». Deux gardes, sans avoir à deviner d'où vient
l'objet : une durée de vie ne rétrécit jamais (on retient la plus longue vue), et
l'échéance d'un access token **inchangé** ne recule pas — un jeton réellement
différent repart, lui, de celle qu'il annonce.

**Un refresh qui échoue est MUET**, et c'est le piège principal de cette boucle.
Le SDK ne lève rien : il appelle `clear_tokens()` — qui vide le contexte **sans
toucher au fichier** —, repose `_initialized`, et laisse partir la requête **sans
en-tête `Authorization`**. Sur un upstream qui accepte `initialize` sans jeton
(ce Jira), la sonde répond donc 200, et le fichier garde un jeton d'apparence
intacte. Se fier à l'un ou à l'autre faisait annoncer « renouvelé » et lever
`authorization_pending` alors que rien n'était autorisé — l'échec ne ressortait
qu'au premier `tools/call`, avec sa stacktrace. Le témoin est donc
`provider.context.current_tokens`, seul endroit où le refus s'inscrit, et
seulement une fois `_initialized` posé : un contexte encore vierge est vide lui
aussi.

Le succès se juge sur l'**avancement de l'échéance DANS LE FUTUR**, jamais sur le
franchissement d'une marge ni sur le seul fait d'avancer. Partant d'un jeton déjà
expiré, l'échéance de départ est dans le passé et un `expires_in` de 0 la
dépasse : on journalisait « renouvelé (valide 0s) » sur un jeton mort. Exiger d'un jeton frais qu'il dépasse `_REFRESH_MARGIN_S` classait en
échec un renouvellement parfaitement réussi de 5 minutes : rien n'était
journalisé — d'où un « je ne vois aucune trace du refresh » parfaitement fondé —
et la boucle recommençait au réveil suivant.

Trois points de conception qui ne se devinent pas :

- **L'écriture passe par le provider partagé**, via une requête anodine sur
  l'upstream, jamais par un POST émis à la main. C'est le passage par
  `async_auth_flow`, sous le `anyio.Lock` d'OAuthContext, qui garde **un seul
  écrivain** sur le fichier de jetons. Un second chemin ferait voir un rejeu du
  refresh token à un AS à rotation, qui révoquerait toute la famille — exactement
  ce que la boucle existe pour éviter.
- **`interactive` reste faux.** Si le renouvellement échoue, le SDK enchaîne sur
  un parcours complet, que `_on_redirect` inhibe en levant
  `AuthorizationRequired` : la boucle marque l'autorisation comme due et s'arrête
  là. Une tâche de fond n'ouvre jamais un parcours que personne n'a demandé.
- **Un AS injoignable ne coûte pas l'autorisation.** Le jeton courant reste
  valable jusqu'à son terme et le prochain réveil réessaiera ; marquer
  l'autorisation comme due enverrait cliquer pour une panne réseau passagère.

Le déclencheur est l'échéance **en stockage**, pas `context.is_token_valid()` :
le contexte peut n'avoir jamais été sollicité depuis le démarrage, auquel cas son
verdict serait « valide » sur un jeton périmé.

Le boot tente aussi ce renouvellement, une fois, juste après l'amorçage du
drapeau — et pas seulement parce que c'est plus tôt. `has_usable_token()` répond
« utilisable » à un jeton **expiré porteur d'un refresh token** : utilisable au
sens où il se renouvelle sans l'utilisateur, pas au sens où il partirait tel
quel. Sans cette tentative, le proxy démarre donc en annonçant un upstream sans
réserve — aucune pastille côté MIAOU — dont le tout premier appel d'outil
échoue, en attendant le premier réveil de la boucle. Mesuré le 2026-09-08.
`refresh_if_due()` sortant sans requête quand l'échéance est loin, un démarrage
avec des jetons frais ne coûte rien.

### L'absence de `refresh_token` est dite à voix haute

Sous `--debug-auth`, `set_tokens()` — passage obligé de tout jeton obtenu, échange
initial comme renouvellement — annonce si un `refresh_token` accompagne le jeton.
Sans lui, rien ne pourra le renouveler et l'autorisation sera à refaire à la main.
Le savoir à l'obtention plutôt qu'à l'expiration, c'est la différence entre un
réglage à corriger côté AS (grant `refresh_token`, scope `offline_access`) et une
panne subie des heures plus tard.

## `--debug-auth` : rendre le parcours observable

Un parcours qui n'aboutit pas est **silencieux par construction** : le SDK avale
ses propres erreurs de découverte, et le proxy ne constate qu'une absence de
jeton. « L'upstream n'a rien demandé », « l'AS a refusé l'enregistrement » et
« la découverte a échoué » produisent alors le même symptôme muet — trois causes,
un seul signal, et le diagnostic se fait au jugé. C'est très exactement ce qui a
coûté plusieurs correctifs posés à l'aveugle.

`enable_auth_debug()` branche les loggers du SDK et du transport plutôt qu'un
traçage maison : ce sont eux qui voient les requêtes que le proxy **n'émet pas
lui-même** — découverte, enregistrement, échange du code. S'y ajoute une trace
explicite par étape de `_provoke_refusal`, dont la ligne qui manquait le plus :
« AUCUN 401 sur tools/call », qui nomme le cas où aucun parcours ne peut
s'amorcer.

**La liste couvre tout le cycle de vie, pas seulement le boot.** Première
version : `("mcp.client.auth", "httpx")` — on voyait les URL d'AS essayées au
démarrage puis plus rien, alors que le mode reste actif (relevé le 2026-09-08).
Deux causes, mesurées :

- **`mcp.client.auth` n'émet rien** dans le SDK installé : ni `getLogger`, ni
  appel `logger.*` dans le module. Tout ce qu'on voyait venait de `httpx`.
  Nommer un logger inexistant est **silencieux**, d'où une liste qui paraissait
  correcte.
- Le trafic d'**après** le boot passe par `mcp.client.streamable_http` — le
  transport des upstreams HTTP : connexion, session, envoi de messages,
  reconnexions SSE — qui journalise sous **son** nom et n'était pas couvert.

Deux pièges de nommage, vérifiés à la source plutôt que devinés :
`mcp.client.session` journalise sous `"client"` (pas sous son nom de module), et
`mcp.shared.session` appelle `logging.*` au niveau module — donc le **root
logger**, hors de portée d'une liste nommée. `httpx`, enfin, journalise ses
requêtes en **INFO**, pas en DEBUG : d'où un niveau posé sur chaque logger plutôt
qu'un filtrage par sévérité. `httpcore` reste volontairement **absent** — ses
lignes ne portent ni URL ni en-tête, elles noient le journal sans rien apprendre.

Les tests vérifient les noms **contre la source des modules du SDK**, jamais
contre une liste recopiée : recopier reproduirait exactement l'erreur à attraper.

**Les valeurs sensibles sont masquées** (`_redact_url`, filtre de logging) :
`code`, `access_token`, `refresh_token`, `client_secret`, `code_verifier`…
masqués par NOM, une valeur opaque ne se reconnaissant pas à sa forme. `state` et
`code_challenge` sont **conservés** — ils ne donnent aucun accès, et ce sont eux
qu'on lit pour corréler un callback à son parcours. Le masque est écrit `***` et
non `%2A%2A%2A` : un log illisible ne se lit pas.

**Le témoin d'un succès est le JETON**, jamais l'absence d'exception :
`has_usable_token()` tranche après le parcours. Un upstream qui répond sans rien
exiger n'a rien accordé — la route le dit (« Rien à autoriser »), au lieu de
confirmer une autorisation qui n'a pas eu lieu. Quand le jeton est bien là,
`authorize()` **rouvre la session** : celle en cours a été ouverte sans jeton,
donc elle ne porte aucun en-tête `Authorization` et continuerait à se faire
refuser.

**Un upstream « pas encore autorisé » n'est PAS retiré de la table de routage**,
contrairement à un upstream en panne. C'est l'unique exception à la règle du
lifespan, et elle a sa raison : une panne ne se répare pas toute seule, une
autorisation manquante se répare par un clic — et retirer l'upstream rendrait
`/authorize/{name}` incapable de le retrouver.

**`/authorize/{name}` attend son URL sur un ÉVÉNEMENT, jamais sur un délai.**
La route lance le parcours en tâche détachée (la réponse doit partir avant que
celui-ci n'attende le retour du navigateur sur `/callback`), puis attend
`UpstreamAuthorizer.redirect_ready` — armé **avant** le `start_soon`, sans quoi
un parcours rapide produirait son URL avant que l'attente n'existe.

**Trois signaleurs, et il faut les trois** — c'est la partie qui s'est révélée
fragile. `_on_redirect` signale dans ses DEUX branches : en mode interactif dès
que l'URL est connue (`authorize()` bloque juste après, sur le retour du
navigateur — attendre sa fin ferait tenir la route jusqu'à sa borne pour une
redirection déjà décidée), et en mode inhibé où il n'y aura jamais d'URL. Le
`finally` de la tâche couvre le reste : échec, ou parcours terminé sans
redirection.

**La route distingue TROIS issues, pas deux.** Rediriger, avoir abouti, avoir
échoué. Le troisième cas manquait et il n'est pas théorique : un `refresh_token`
encore valide, ou un AS qui accorde sans interaction, et le SDK obtient son
jeton sans jamais passer par `_on_redirect`, donc sans `pending`. Tester la
seule URL de redirection faisait alors répondre « le serveur d'autorisation n'a
pas répondu à temps » **une ligne de log après « Upstream autorisé »** — le
contraire de ce qui venait de se passer (payé en production le 2026-09-07).

Le témoin d'une autorisation acquise est l'état de l'upstream
(`authorization_pending` faux, `last_error` nulle), **jamais le chemin emprunté
pour y arriver** : `pending` est de toute façon remis à `None` par le `finally`
d'`authorize()`, donc il ne peut rien dire d'un parcours terminé, fût-il abouti
par redirection.

Elle attendait auparavant un `sleep(0.1)` fixe, et c'est un bug payé en
production : le parcours doit d'abord découvrir l'AS (`/.well-known/…`) et
parfois enregistrer un client, ce qui derrière un portail d'entreprise prend des
secondes. Passé le dixième de seconde, la route concluait « Le serveur
d'autorisation n'a pas pu être joint » alors qu'il répondait très bien —
diagnostic faux, et faux dans le sens qui décourage de réessayer. La borne
(`_AUTHORIZE_ROUTE_WAIT_S`) est volontairement généreuse : une borne trop large
coûte une page qui tarde, une borne trop courte coûte un mensonge.

Quand le parcours a **déjà** échoué, la page rend `last_error` plutôt qu'un
diagnostic réseau générique : un enregistrement refusé (`403
insufficient_scope`) ou un scope manquant est une erreur de configuration, et la
présenter comme une panne fait recliquer sur un lien qui ne peut rien réparer.
Les deux routes échappent tout ce qu'elles interpolent (`html.escape`) — elles
sont publiques, et `name` vient du chemin d'URL.

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

Le prédicat est unique : `upstream_is_live(upstream, authorizer)`, partagé par
le listing, le refus d'appel, le rapport de `status` et le `_meta` de
`tools/list` — des endroits qui doivent répondre la même chose, sous peine
d'annoncer un outil qu'on refuse ensuite pour une raison qu'on ne rapporte pas.
Un prédicat n'est unique que si tous ses consommateurs y passent : ajouter une
surface, c'est y brancher un consommateur de plus, jamais réécrire le filtre sur
place.

**Il répond sur DEUX conditions, et la seconde a manqué jusqu'au 2026-09-07.**
« Transport ouvert » ne vaut pas « autorisé ». Un upstream OAuth peut accepter
`initialize` ET `tools/list` sans jeton, et n'exiger l'autorisation qu'au
premier `tools/call` — c'est le cas d'un Jira d'entreprise, payé en production.
Jugé sur la seule session, il était déclaré vivant : ses outils listés sans
réserve, son `_meta` vide (donc aucune pastille côté MIAOU), `status` muet, et
son premier appel parti pour de bon vers un 401 au lieu d'être refusé avant
émission. Le drapeau `UpstreamAuthorizer.authorization_pending` porte la seconde
condition. L'`authorizer` est facultatif : un appelant qui n'en a pas retrouve
le comportement d'avant, à l'octet près.

**Ce qui arme le drapeau, et pourquoi ça ne peut pas être seulement `_on_redirect`.**
Celui-ci n'est atteint qu'au moment où une requête part réellement : sur un
upstream qui liste ses outils sans jeton, le premier événement révélateur est
donc l'échec que l'utilisateur subit — précisément ce que la surface `_meta`
existe pour éviter. Le lifespan amorce donc l'état au démarrage en interrogeant
le **stockage** : `UpstreamTokenStorage.has_usable_token()`, purement local
(lecture de fichier, aucune requête sortante, aucun retard au boot). Un jeton
expiré mais porteur d'un `refresh_token` compte comme utilisable — le SDK le
rafraîchit seul, et envoyer l'utilisateur cliquer lui ferait régler un problème
qui n'existe pas. Sonder l'upstream aurait coûté un aller-retour réseau par
upstream à chaque démarrage, pour une information déjà sur le disque.

**Un appel en vol survit à la mort de sa session — mais il faut le vouloir.**
La session vit dans `_serve()`, une tâche à elle (patron des cancel scopes
anyio). Une exception levée par le transport de CETTE tâche y est capturée,
rangée dans `_failure`, et `_serve` sort de ses contextes : le stream se ferme
sous les pieds de l'appelant **sans réponse ni erreur pour lui**. Un
`session.call_tool()` nu attend donc une réponse qui n'arrivera jamais, jusqu'à
son propre timeout — client suspendu, refus jamais rendu (payé en production le
2026-09-07 sur un upstream dont l'AS ne réclame son jeton qu'au premier appel
réel). `HttpUpstream.call_tool` fait donc courir l'appel CONTRE `_stopped`,
signalé par le `finally` de `_serve` dans tous les cas de fin de service ; la
première des deux qui vient l'emporte, et si c'est la mort du service on relève
`_failure`, lisible parce que le `except` la pose avant que le `finally` ne
signale.

**Le premier appel refuse comme les suivants.** Quand l'AS ne réclame
l'autorisation qu'à `tools/call`, le parcours OAuth du SDK démarre au milieu de
cet appel et `_on_redirect`, non interactif, lève `AuthorizationRequired`. Elle
traverse le transport sans être reconnue : l'appel restait suspendu jusqu'à son
timeout, et le refus n'arrivait qu'au tour SUIVANT, une fois l'état posé — un
tour entier perdu, pendant lequel MIAOU n'affiche rien. Le site d'appel la
convertit donc en `UpstreamNotAuthorized` (via `_unwrap_exception_group`, anyio
empaquetant ce qui traverse un task group).

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

