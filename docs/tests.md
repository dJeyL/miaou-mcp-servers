# Tests

Les commandes canoniques sont dans `CLAUDE.md`. Ici : ce que chaque suite mocke,
et le script d'appel réel.

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

Script PEP 723 (dépendances : `mcp`, `truststore`), **pas un test pytest** : son nom ne commence
pas par `test_`, il n'est donc jamais collecté malgré sa place dans `tests/`. Il parle le
vrai transport streamable-http, comme MIAOU (`initialize`, `notifications/initialized`,
`tools/call`) — c'est le chemin que le stack in-process des tests unitaires ne couvre pas
(cf. mémoire « Vérifier le transport HTTP réel »). Le serveur visé doit déjà tourner.

```bash
uv run tests/live_call.py brave__brave_search '{"query": "blabla"}'   # proxy, port 8765
uv run tests/live_call.py --port 8769 ddg_search '{"query": "chat"}'  # serveur unitaire
uv run tests/live_call.py --list                                      # outils exposés
uv run tests/live_call.py --url http://host:8765/mcp echo '{"text": "hi"}'
uv run tests/live_call.py -H 'Authorization: Bearer xxx' --list   # header libre, répétable
```

`--port` défaut 8765 (le proxy), `--host` défaut `127.0.0.1`, `--url` prime sur les deux.
Arguments JSON optionnels. L'outil est vérifié contre `tools/list` avant l'appel (nom
inconnu → liste des disponibles, code 2). Rendu des trois familles de blocs : `text` brut,
`image`/`resource` binaire résumés (mime + taille base64, jamais le base64 lui-même),
`resource` texte avec son URI, plus `structuredContent` s'il existe. Codes de sortie : 0
succès, 1 `isError` ou échec de connexion, 2 erreur d'usage. Les `ExceptionGroup` d'anyio
sont aplatis avant affichage (`_flatten`) — sans ça, un serveur injoignable ne produit que
« unhandled errors in a TaskGroup », sans la cause.

`-H/--header` (répétable, forme `'Nom: valeur'`) passe des headers HTTP libres à
`streamablehttp_client` : `Authorization` pour viser un proxy en auth entrante sans dérouler
le parcours OAuth, mais aussi n'importe quel header applicatif d'un reverse proxy en amont.
La valeur n'est strippée qu'à gauche, un header sans `:` sort en code 2 avant toute connexion.

Le script appelle `enable_system_trust_store()` avant `asyncio.run`, comme
`MiaouMCPBase.main()` et `mcp_proxy.main()` — sans quoi viser une URL `https://` servie sous
AC d'entreprise interne échoue en `CERTIFICATE_VERIFY_FAILED` côté client alors même que les
serveurs, eux, joignent leurs upstreams : le banc d'essai diagnostiquerait un faux négatif.
Le helper y est **recopié** plutôt qu'importé de `servers/mcp_base.py` — c'est un client
autonome, et l'import tirerait FastMCP et starlette pour quatre lignes ; le prix est une
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

