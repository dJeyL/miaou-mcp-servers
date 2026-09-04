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

