# Tests

Les commandes canoniques sont dans `CLAUDE.md`. Ici : ce que chaque suite mocke,
et le script d'appel réel.

Les tests de bench mockent `asyncio.sleep` pour éviter les délais de 2 s.
Les tests de weather, fetch, ddg et brave mockent `urllib.request.OpenerDirector.open`.
Les tests de brave mockent aussi `os.environ` — aucun appel réseau réel, aucune clef requise.
`test_web_search.py` (recherche multi-moteurs de `mcp_web`) mocke le même `open` avec un
routeur par fragment d'URL (`_Router`) : chaque moteur reçoit sa réponse, les requêtes
sont comptées par moteur pour prouver qu'un moteur en pause ou après une réponse vide
n'est pas interrogé, et les `HTTPError` levées sont gardées pour vérifier leur
fermeture. L'état DDG étant celui du processus (`ddg.ENGINE`), une fixture le remet à
zéro et coupe l'espacement ; une autre retire `BRAVE_API_KEY`/`OLLAMA_API_KEY` de
l'environnement, sans quoi la chaîne construite dépendrait du shell. Le corps du 422
de Brave sur clef invalide y est celui mesuré le 2026-10-05.
Les tests du proxy mockent les upstreams ou utilisent InProcessUpstream sur mcp_bench réel.
Ils parlent au `Server` du proxy par `tests/proxy_client.py` (`list_tools`, `call_tool`) :
un `Client(server, mode="legacy")` du SDK, donc le vrai chemin JSON-RPC de la poignée de
main `initialize` — celui de MIAOU. **Jamais le mode par défaut** : `auto` négocie la
révision 2026-07-28 et dispatche EN DIRECT, sans sérialisation, si bien qu'un test vert y
prouverait le mauvais chemin. Ce passage par le fil rend aussi les tests plus forts qu'un
appel au handler : un `_meta` mal aliasé est jeté à la relecture côté client (le SDK 2.x
ignore les champs inconnus), une erreur protocolaire arrive en `MCPError` avec son `data`.
Le harnais déballe l'`ExceptionGroup` d'anyio dont le transport en mémoire enveloppe
l'erreur (`_single_cause`). Les tests des serveurs, eux, appellent l'outil par
`_tool_manager.call_tool(nom, args, None)` : le contexte est obligatoire en 2.x, `None`
suffit à un outil qui n'en déclare pas.
`tests/test_proxy_era.py` fait, lui, de VRAIS handshakes avec des upstreams stdio et
http, pour la négociation d'ère : sans réseau, un subprocess `sys.executable` pour stdio
(`tests/era_fixture_server.py`, un `MCPServer` 2.x avec instructions, une extension et un
outil annoté `x-mcp-header` ; `tests/legacy_stdio_fixture.py`, faux serveur en
bibliothèque standard qui ne parle que `initialize` et journalise chaque message reçu —
un vrai serveur 1.x exigerait un téléchargement) ; pour http, `_build_http_client`
remplacé par un client `httpx2.ASGITransport` sur l'app streamable-http du même
`MCPServer` (le flux GET SSE de l'ère legacy y passe, fermeture comprise), avec un hook
de requête qui enregistre ce qui part. L'upstream http legacy est ce serveur 2.x derrière
un middleware qui répond à toute requête `MCP-Protocol-Version: 2026-07-28` ce que répond
un serveur 1.28.1 (400, `-32600`, `id: "server-error"`). L'identité legacy se vérifie en
comparant la session du mode `auto` à celle du mode `legacy`, message par message. Un
second middleware répond à tout `tools/call` une erreur JSON-RPC (400 avec corps) : la
seule façon de voir l'erreur d'un upstream http traverser le VRAI `HttpUpstream.call_tool`
et son task group — un upstream `MagicMock`, comme dans `test_proxy.py`, ne l'a jamais
vue enveloppée.
`tests/test_proxy_remote_skills.py` reprend cet outillage pour les skills relayées :
`tests/skills_fixture_server.py`, lancé en script, sert l'extension Skills de `mcp_base`
sur stdio (`python skills_fixture_server.py <skills_dir> [<requires_skill>]`), et son
`build(config)` donne l'app http. La skill de fixture porte CRLF, BOM, emoji et un
fichier binaire : les octets lus à travers le proxy sont comparés à ceux du disque, et
aux empreintes que l'upstream publie. Le legacy est tenu par ce qui PART : aucune
méthode `skills/*` ni `resources/*` dans ce qu'enregistre le hook httpx2 (2.x forcé en
legacy, qui répondrait s'il était interrogé ; 2.x derrière le middleware 1.x) ni dans le
journal de `legacy_stdio_fixture.py`. Un middleware refuse `skills/get` en 400 avec
corps, ou ne répond jamais (borne de la requête) ; la mort d'un upstream stdio se
provoque en tuant son subprocess (`pgrep -P`), sans passer par `stop()`.
Les formes que nos serveurs n'émettent jamais viennent de `tests/fabricated_skills_server.py`
(un `MCPServer` dont l'extension Skills rend des entrées FABRIQUÉES) : deux pages dont la
seconde répète son curseur et dit `private`, `"dynamic"`, URI hors `skill://`, fichier
sans `uri`, `name` ou `description` absents, description trop longue, entrée qui n'est
pas un objet, skill servie non listée ; `build("empty")`, extension déclarée et listage
vide. Ce qu'ils couvrent est jugé sur NOTRE lecture de la spec, aucun upstream tiers ne
servant encore l'extension.
`tests/test_proxy_webapp.py` (MIAOU servi sous `/app/`) monte un `dist/` factice dans
`tmp_path` et l'interroge à trois niveaux : les purs de `webapp.py`, l'app de
`build_app` sur `httpx2.ASGITransport` (wrapper ASGI compris), et un **vrai uvicorn**
sur un port éphémère (`port=0`, lancé dans un thread, port relu sur sa socket),
interrogé par `http.client` — le seul niveau qui montre les en-têtes réellement émis
et la revalidation après un `git pull` simulé (fichier réécrit, mtime avancé). Le cas
Windows (registre qui type `.js` en `text/plain`) se simule en remplaçant
**`starlette.responses.guess_type`** : Starlette importe la fonction par son nom, et
remplacer `mimetypes.guess_type` laisse le test vert même sans la table de types —
vérifié en retirant la table.
Les tests qui interceptent le client HTTP du SDK patchent **`httpx2`** (`AsyncClient`,
`MockTransport`, `Response`) : le SDK 2.x n'emploie plus `httpx`, et un patch resté sur
l'ancien nom ne lève rien — il ne patche plus rien, et le test part sur le réseau.
Les tests de docs monkeypatchent `mcp_docs.session.WORKDIR` (fixture `tmp_path`) pour
isoler le filesystem par test, exercent chaque format (fixtures PDF/xlsx/docx/pptx/zip
générées à la volée par les libs elles-mêmes) via `formats.py` directement, et vérifient
REF_UNKNOWN à travers le stack proxy réel (InProcessUpstream sur mcp_docs + build_proxy_server),
pas seulement l'appel direct à l'outil.
Les tests de mcp_web monkeypatchent `mcp_web.cache.WORKDIR` (fixture `tmp_path`, autouse)
pour la même raison ; `tests/test_web_structure.py` exerce `mcp_web.structure.extract_structure`
en isolation (pas de HTTP, pas de cache) sur des fragments HTML construits à la main.
`tests/test_web_pagemeta.py` couvre le `_meta` de `fetch_url` : les purs de `pagemeta.py`
en isolation, puis `fetch_url` de bout en bout derrière un opener patché qui **route par
URL** (`_Router`) — la page, sa favicon et `/favicon.ico` répondent chacune leur corps, et
les URL demandées sont comptées (sonde unique par origine, pas de requête pour une favicon
`data:`). Le cache de favicons vivant dans le processus, les deux fichiers de tests web le
vident en fixture autouse : sans quoi un test hériterait de la sonde ratée du précédent.
`fetch_url` rendant un `CallToolResult`, `test_web.py` passe par `_fetch_url`, qui en
extrait le bloc unique et vérifie au passage `isError` faux.

Le mock de réponse de `test_web.py` (`_make_mock_resp`) porte un `Content-Encoding`
optionnel, ce qui fait traverser `_decompress` (WEB9) au vrai chemin `fetch_url` /
`fetch_resource` — le stub est l'opener, pas `_fetch_bytes`. Deux pièges de fixture y
sont posés, parce qu'aucun des deux ne fait échouer un test naïf :

- une queue de remplissage **répétée** (`b"x" * 40000`) se gzippe en ~90 octets et ne
  franchit donc jamais `max_bytes` : le test de troncature mid-stream passerait sans avoir
  rien tronqué. D'où `os.urandom`, incompressible.
- les deux tests de troncature restent **verts sur le code d'avant** (la décompression
  n'est pas ce qu'ils gardent) ; seuls les cinq tests de décompression y tombent. Vérifié
  en neutralisant `_decompress` — un test de non-régression qui passe des deux côtés ne
  prouve rien.

### `tests/live_call.py` — appel réel d'un outil

Script PEP 723 (dépendances : `mcp` 2.x, `truststore`), **pas un test pytest** : son nom ne commence
pas par `test_`, il n'est donc jamais collecté malgré sa place dans `tests/`. Il parle le
vrai transport streamable-http, comme MIAOU (`initialize`, `notifications/initialized`,
`tools/call`) — c'est le chemin que le stack in-process des tests unitaires ne couvre pas
(cf. mémoire « Vérifier le transport HTTP réel »). Le serveur visé doit déjà tourner.

```bash
uv run tests/live_call.py web__search '{"query": "blabla"}'           # proxy, port 8765
uv run tests/live_call.py --port 8768 search '{"query": "chat"}'      # serveur unitaire
uv run tests/live_call.py --list                                      # outils exposés
uv run tests/live_call.py --url http://host:8765/mcp echo '{"text": "hi"}'
uv run tests/live_call.py -H 'Authorization: Bearer xxx' --list   # header libre, répétable
uv run tests/live_call.py --modern --list                            # ère 2026-07-28, extensions affichées
uv run tests/live_call.py --method skills/list                       # requête JSON-RPC quelconque
uv run tests/live_call.py --modern --method resources/read --params '{"uri": "skill://bench/bench/SKILL.md"}'
```

`--modern` négocie la révision 2026-07-28 (`Client(mode="auto")`, qui sonde
`server/discover`) au lieu de la poignée de main `initialize` de MIAOU, et affiche la
révision retenue et les `extensions` publiées : c'est la seule ère où elles le sont. Sans
lui, le script parle comme MIAOU actuel (révision affichée aussi). `--method` envoie une
requête JSON-RPC quelconque (`skills/list`, `skills/get`, `resources/read`…) avec
`--params`, et affiche le résultat brut ; une erreur JSON-RPC sort avec son code et son
message, en code 1. `--list` affiche aussi le `_meta` de chaque outil qui en porte
(`miaou/requiresSkill`, `miaou/skillsFallback`).

`--port` défaut 8765 (le proxy), `--host` défaut `127.0.0.1`, `--url` prime sur les deux.
Arguments JSON optionnels. L'outil est vérifié contre `tools/list` avant l'appel (nom
inconnu → liste des disponibles, code 2). Rendu des trois familles de blocs : `text` brut,
`image`/`resource` binaire résumés (mime + taille base64, jamais le base64 lui-même),
`resource` texte avec son URI, plus `structuredContent` et `_meta` s'ils existent (c'est le
seul moyen de voir que le `_meta` d'un appel traverse le fil). Codes de sortie : 0
succès, 1 `isError` ou échec de connexion, 2 erreur d'usage. Les `ExceptionGroup` d'anyio
sont aplatis avant affichage (`_flatten`) — sans ça, un serveur injoignable ne produit que
« unhandled errors in a TaskGroup », sans la cause.

`-H/--header` (répétable, forme `'Nom: valeur'`) passe des headers HTTP libres à
client `httpx2` passé à `streamable_http_client` : `Authorization` pour viser un proxy en auth entrante sans dérouler
le parcours OAuth, mais aussi n'importe quel header applicatif d'un reverse proxy en amont.
La valeur n'est strippée qu'à gauche, un header sans `:` sort en code 2 avant toute connexion.

Le script appelle `enable_system_trust_store()` avant `asyncio.run`, comme
`MiaouMCPBase.main()` et `mcp_proxy.main()` — sans quoi viser une URL `https://` servie sous
AC d'entreprise interne échoue en `CERTIFICATE_VERIFY_FAILED` côté client alors même que les
serveurs, eux, joignent leurs upstreams : le banc d'essai diagnostiquerait un faux négatif.
Le helper y est **recopié** plutôt qu'importé de `servers/mcp_base.py` — c'est un client
autonome, et l'import tirerait le SDK serveur et starlette pour quatre lignes ; le prix est une
duplication à répercuter (cf. `docs/tls.md`).

### `tests/live_auth_probe.py` — ce qu'un upstream répond SANS jeton

Même statut que son voisin (PEP 723, non collecté), et une seule question : **cet upstream
refuse-t-il quelque chose, et par quel canal ?** Tout le parcours OAuth sortant repose sur un
401 porteur d'un `WWW-Authenticate` ; sans lui le SDK n'a aucun AS à découvrir et rien ne
démarre. Le script pose la question hors du proxy, sans le SDK, sans rien qui puisse masquer
la réponse : code HTTP et en-têtes d'authentification, requête par requête.

```bash
uv run tests/live_auth_probe.py https://jira.exemple/mcp
uv run tests/live_auth_probe.py https://jira.exemple/mcp --tool jira_list_projects --args '{"results": 1}'
uv run tests/live_auth_probe.py https://jira.exemple/mcp -H 'X-Tenant: acme'
```

Il déroule la vraie séquence — `initialize`, `notifications/initialized`, `tools/call` — en
rejouant le `Mcp-Session-Id` : une requête hors session est rejetée pour une raison qui n'a
rien à voir avec l'autorisation, et la mesure ne dirait rien (défaut de sa première version).
**Aucun jeton n'est envoyé, rien n'est écrit** : lançable sur un serveur de production.

Deux gardes sur le choix de l'outil appelé. `--tool` le désigne explicitement, ce qui vaut
mieux derrière un proxy qui agrège : le premier outil de `tools/list` peut venir d'un AUTRE
upstream, et le diagnostic porterait sur celui-là. À défaut, la découverte automatique écarte
tout nom évoquant une **écriture** (`_looks_mutating`, comparaison par segments après
découpage sur séparateurs et casse — `create_issue`, `createIssue`, `add-comment` sortent,
`list_updates` reste) : sonder une autorisation ne doit jamais créer un ticket. Si tous les
outils sont écartés, aucun appel n'est fait.

C'est ce script qui a établi le 401 sur `tools/call` d'un Jira d'entreprise (2026-09-07),
après **trois** correctifs posés sur des suppositions successives, tous publiés, aucun
efficace — chacun corrigeait un défaut réel sans toucher la cause. La leçon tient en une
ligne : sur un comportement distant qu'on ne peut pas reproduire, écrire le banc AVANT le
correctif.

### `tests/live_discovery_probe.py` — ce que la découverte OAuth du SDK 2.x conclurait

Même statut (PEP 723, non collecté, aucun jeton, aucune écriture), et une question tournée
vers la migration au SDK MCP 2.x : **ce que publie cet upstream fait-il échouer la découverte
OAuth durcie de la 2.x ?** Quatre durcissements dépendent de l'autre bout et ne se tranchent
pas en lisant du code : une PRM en 5xx/429 devient fatale, l'`issuer` des métadonnées d'AS
se compare à l'octet près, `offline_access` découvert entraîne `prompt=consent`, et le
paramètre `iss` de la redirection (RFC 9207) se compare à l'`issuer` des métadonnées
EFFECTIVES — y compris celui que l'override de config dérive de l'authorization endpoint.

```bash
uv run tests/live_discovery_probe.py https://jira.exemple/mcp
uv run tests/live_discovery_probe.py https://jira.exemple/mcp \
    --authorization-endpoint https://sso.exemple/realms/r/protocol/openid-connect/auth
uv run tests/live_discovery_probe.py https://jira.exemple/mcp --oidc https://sso.exemple/realms/r
```

La découverte est rejouée avec les fonctions du SDK lui-même (URL candidates, lecture des
réponses, `validate_metadata_issuer`), jamais recopiées, d'où une version **épinglée**
(`mcp==2.2.0`) : le script mesure la version visée. La seule recopie est la dérivation
d'`issuer` de `build_oauth_metadata_override`, une ligne, à répercuter si elle change.
Codes de sortie : 0 rien de bloquant, 1 au moins un risque, 2 upstream injoignable ou
erreur d'usage — un hôte injoignable ne conclut à aucun risque.

Éprouvé le 2026-10-02 contre le proxy (auth entrante) et `dev_auth_server.py` : il a relevé
que la PRM servie par le proxy en 1.x annonce `http://127.0.0.1:8787/` (slash final ajouté
par la normalisation pydantic de la 1.x) quand l'AS se déclare `http://127.0.0.1:8787` — un
couple qu'un client 2.x refuse.

