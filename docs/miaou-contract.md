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
  Mécanisme : l'outil lève `ToolError(f"{REF_UNKNOWN_SENTINEL}: ...")` (constante
  `mcp_docs.REF_UNKNOWN_SENTINEL`) ; le proxy (`mcp_proxy._wrap_ref_unknown_sentinel`)
  détecte ce sentinel dans le résultat `isError` (le SDK MCP avale toute exception de
  l'outil en `CallToolResult(isError=True)`, y compris `McpError` — voir le commentaire
  du post-wrapper dans `mcp_proxy.py`) et lève `McpError(data={'code': 'REF_UNKNOWN'})`,
  que `_handle_request` du SDK convertit en erreur JSON-RPC. **Ce rejeu ne fonctionne que
  derrière le proxy** : en standalone (FastMCP pur, port 8771 direct), l'appel échoue en
  isError textuel sans déclencher le rejeu — documenté ici, pas une régression à corriger.
  **Déclaration du contrat, côté serveur** : le proxy ne connaît aucun sentinel en propre
  et n'importe pas `mcp_docs` — un upstream inprocess *déclare* le contrat en exposant
  `REF_UNKNOWN_SENTINEL` (str) et `REF_UNKNOWN_ERROR_CODE` (int) au niveau module ;
  `InProcessUpstream.start()` les lit par `getattr`, et le wrapper ne convertit le sentinel
  que pour les outils routés vers un upstream qui les déclare. Conséquences : un proxy
  configuré sans `docs` n'importe ni `mcp_docs` ni ses libs de parsing, et un message
  d'erreur d'un autre serveur qui contiendrait « REF_UNKNOWN » n'est pas converti (le match
  reste par sous-chaîne : FastMCP préfixe le message, il ne peut pas être ancré en tête).
  Les upstreams **stdio** sont hors périmètre : le proxy ne peut pas lire de constante dans
  un subprocess, un serveur stdio devrait lever l'erreur JSON-RPC lui-même.
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

