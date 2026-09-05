# CLAUDE.md — miaou-mcp-servers

Instructions pour travailler dans ce dépôt.

## Ce qu'est le projet

Six serveurs MCP de développement (bench, weather, web, ddg, brave, docs) et un serveur
proxy qui les agrège, extraits du dépôt [MIAOU](https://github.com/dJeyL/miaou), un client de
chat web pour API OpenAI-compatible (single-file HTML). Ils servent à tester l'agrégation
MCP de MIAOU : connexion, invocation d'outils, rendu des résultats non-text.

Ils n'ont pas de rôle en production — ce sont des outils de banc d'essai, destinés à
tourner localement pendant le développement de MIAOU.

## Structure du projet

```
miaou-mcp-servers/
├── mcp_proxy.py          # serveur proxy (racine, point d'entrée principal)
├── dev_auth_server.py    # serveur d'autorisation OAuth de DÉVELOPPEMENT (jamais en prod)
├── servers/
│   ├── mcp_base.py       # classe de base partagée (MiaouMCPBase + make_opener)
│   ├── mcp_bench.py      # banc d'essai général (port 8766)
│   ├── mcp_weather.py    # météo réelle via wttr.in (port 8767)
│   ├── mcp_web/          # téléchargement d'URL (port 8768), package
│   ├── mcp_ddg.py        # recherche DuckDuckGo HTML (port 8769)
│   ├── mcp_brave.py      # recherche Brave Search API (port 8770)
│   └── mcp_docs/         # extraction PDF/Office/Zip (port 8771), package — OBSOLÈTE, désactivé par défaut
├── docs/                 # domaines détaillés, lus à la demande (voir index en fin de fichier)
├── tests/
│   ├── live_call.py      # appel manuel d'un outil sur un serveur lancé (non collecté)
│   ├── test_base.py
│   ├── test_bench.py
│   ├── test_weather.py
│   ├── test_web.py
│   ├── test_web_structure.py
│   ├── test_ddg.py
│   ├── test_brave.py
│   ├── test_docs.py
│   ├── test_proxy.py
│   ├── test_proxy_auth.py  # auth OAuth entrante (lot AB-1)
│   ├── test_proxy_outbound_auth.py  # auth OAuth sortante (lot AB-2)
│   └── test_dev_auth_server.py  # serveur d'autorisation de développement (lot AB-1.3)
├── config.sample.json    # template de config pour le proxy
├── config.json           # (gitignored) config active du proxy
├── requirements.txt      # pour les utilisateurs sans uv
├── pyproject.toml        # métadonnées projet + config pytest (asyncio_mode=auto) + groupe dev
├── uv.lock               # lock uv, versionné
└── .gitignore
```

## Les serveurs en un coup d'œil

Six serveurs de banc d'essai plus un proxy qui les agrège. Le détail de chacun
(outils, contrats, variables d'environnement, décisions) est dans
**`docs/servers.md`** — à lire quand on touche au serveur concerné, pas avant.

| Serveur | Port | Rôle | Outils |
|---|---|---|---|
| `mcp_bench.py` | 8766 | Banc d'essai général : exerce les chemins de résultat de MIAOU (texte, image, resource) | `echo`, `add`, `dns_lookup`, `reverse_dns`, `get_image`, `get_json_resource` |
| `mcp_weather.py` | 8767 | Météo réelle via wttr.in | `get_weather` (`astronomy`, `hourly`, `extract`) |
| `mcp_web/` | 8768 | Téléchargement d'URL, cache disque par checksum, pagination | `fetch_url`, `fetch_read`, `fetch_list`, `fetch_resource` |
| `mcp_ddg.py` | 8769 | Recherche DuckDuckGo (HTML scrapé) | `ddg_search` |
| `mcp_brave.py` | 8770 | Recherche Brave Search API (clef requise) | `brave_search`, `brave_image_search` |
| `mcp_docs/` | 8771 | Extraction PDF/Office/Zip — **obsolète, désactivé par défaut** | `list`, `read`, `search`, `extract`, `drop_session` |
| `mcp_proxy.py` | 8765 | Agrège tout sur un port, préfixe les outils (`bench__echo`…) | (+ `status` si auth sortante) |

Deux points qu'on ne devine pas depuis le tableau :

- **`mcp_docs` est obsolète mais conservé** — MIAOU ouvre ces cinq formats lui-même.
  Il reste pour le travail **hors connexion** (l'ouverture native télécharge ses
  moteurs depuis un CDN). Ne pas « faire le ménage » dans ce package au motif qu'il
  ne sert plus par défaut.
- **`mcp_brave` refuse de s'initialiser sans clef** — plutôt qu'exposer deux outils
  qui échoueraient à chaque appel. Côté proxy, l'upstream est retiré de la table de
  routage et les autres démarrent normalement.

## Lancement

### Avec uv (recommandé)

Les scripts utilisent PEP 723 (bloc `# ///` en tête). Les dépendances sont installées
automatiquement par `uv run` dans un venv isolé.

```bash
# Serveurs unitaires
uv run servers/mcp_bench.py                          # HTTP 127.0.0.1:8766
uv run servers/mcp_bench.py --transport stdio        # mode stdio
uv run servers/mcp_bench.py --host 0.0.0.0           # toutes interfaces

uv run servers/mcp_weather.py                        # HTTP 127.0.0.1:8767
uv run servers/mcp_ddg.py                            # HTTP 127.0.0.1:8769
BRAVE_API_KEY=<key> uv run servers/mcp_brave.py      # HTTP 127.0.0.1:8770

# mcp_web et mcp_docs sont des packages (pas des scripts plats) — lancement différent :
uv run --directory servers python -m mcp_web         # HTTP 127.0.0.1:8768
uv run --directory servers python -m mcp_docs        # HTTP 127.0.0.1:8771

# Proxy (agrège tout sur un seul port)
cp config.sample.json config.json     # puis éditer config.json (BRAVE_API_KEY, etc.)
uv run mcp_proxy.py                   # port défini dans config.json
uv run mcp_proxy.py --port 8765       # override port
uv run mcp_proxy.py --config autre.json
uv run mcp_proxy.py --proxy 10.0.0.1:3128   # force le proxy réseau vu par les upstreams
uv run mcp_proxy.py --noproxy               # force l'absence de proxy vu par les upstreams
```

### Avec pip

```bash
pip install -r requirements.txt
python servers/mcp_bench.py [options]
python servers/mcp_weather.py [options]
python servers/mcp_ddg.py [options]
BRAVE_API_KEY=<key> python servers/mcp_brave.py [options]
python -m mcp_web [options]     # depuis servers/ (package, pas un script plat)
python -m mcp_docs [options]    # depuis servers/ (package, pas un script plat)
python mcp_proxy.py [options]
```


## Architecture commune des serveurs

```python
class MyServer(MiaouMCPBase):
    def __init__(self):
        super().__init__("nom-du-serveur", default_port=9000)

        @self.mcp.tool()
        async def mon_outil(arg: str) -> ...: ...

server = MyServer()
mcp = server.mcp  # exposé pour InProcessUpstream du proxy

if __name__ == "__main__":
    server.main()
```

Deux points à ne pas toucher sans bonne raison :

- **`enable_dns_rebinding_protection=False`** : MIAOU peut être servi en `file://`
  (ouverture directe de `dist/miaou.html`), qui envoie `Origin: null`. Le SDK MCP
  renverrait 403 avant même la couche CORS si la protection est active.

- **`expose_headers=["Mcp-Session-Id"]`** dans le middleware CORS : MIAOU lit ce
  header après `initialize` pour maintenir la session. Sans lui, le navigateur masque
  le header et chaque appel repart sans session → erreur 404 ou réinitialisation.

Tout appel réseau ou bloquant à l'intérieur d'un outil `async` (urllib dans
weather/ddg/brave/web, résolution DNS dans bench, parsing de document dans
`mcp_docs`) est enveloppé dans `asyncio.to_thread(...)` — un outil async qui appelle
directement une fonction bloquante gèlerait l'event loop pendant tout le round-trip,
sérialisant les appels concurrents. Isoler l'I/O bloquante dans une fonction
synchrone dédiée (ex. `_fetch_bytes`, `_fetch_ddg_html`, `_fetch_brave_bytes`) puis
l'appeler via `await asyncio.to_thread(...)` est le pattern à suivre pour tout
nouvel outil qui ferait de l'I/O.

## Tests

```bash
# Avec uv — commande canonique, ne dépend pas de pyproject.toml/uv.lock
uv run --with pytest --with pytest-asyncio --with html2text --with pymupdf \
  --with python-docx --with openpyxl --with python-pptx --with truststore pytest tests/

# Avec uv — alternative via pyproject.toml (groupe dev) + uv.lock, équivalente
# depuis que [project.dependencies] couvre l'union runtime (DD7)
uv run --group dev pytest tests/

# Avec pip (après pip install -r requirements.txt)
pytest tests/
```

`pyproject.toml` porte `asyncio_mode = "auto"` (pytest-asyncio) — les deux commandes
uv ci-dessus s'appuient dessus, la commande canonique n'a juste pas besoin du lock.

Ce que chaque suite mocke (et pourquoi aucun test ne fait d'appel réseau réel), plus
`tests/live_call.py` qui parle le vrai transport streamable-http : `docs/tests.md`.

## Posture sécurité

CORS ouvert (`allow_origins=["*"]`), réseau local uniquement. Délibéré : c'est du
banc d'essai. En production, ces serveurs seraient derrière un proxy (Caddy, nginx)
qui porte les tokens côté serveur — cf. brief D6 de MIAOU.

Le proxy sait néanmoins exiger une autorisation OAuth de ses clients, et en obtenir
auprès de serveurs tiers (campagne AB) — **désactivé par défaut** dans les deux sens :
sans clé `auth` dans `config.json`, le comportement est celui d'avant le lot, à
l'octet près. Détail : `docs/auth.md`.

## Ajouter un outil

Décorer une fonction avec `@self.mcp.tool()` dans le `__init__` du serveur concerné.
FastMCP génère le schéma JSON automatiquement depuis la signature Python et la docstring.
Aucune déclaration manuelle dans un registre.

La docstring ne documente que l'outil dans son ensemble (clé `description` du schéma
`tools/list`) — un paramètre nu (`text: str`) n'a jamais de clé `description` dans son
propre schéma, quelle que soit la qualité de la docstring. Pour qu'un paramètre précis
porte sa propre description, l'annoter avec `Annotated[type, Field(description="...")]`
(import `from pydantic import Field`, `from typing import Annotated`) — fonctionne aussi
sous `from __future__ import annotations`. Réservé aux paramètres dont le nom seul ne
suffit pas (contrainte fine documentée seulement dans la docstring globale : exclusivité
avec un autre paramètre, cap non levé par une plage, clamp silencieux, format dépendant du
type de document) — ne pas annoter systématiquement tous les paramètres, l'info doit
migrer d'un endroit à l'autre, pas se dupliquer. Exemples appliqués : `mcp_docs.read`
(`selector`, `char_start`/`char_end` vs `line_start`/`line_end`), `mcp_bench.reverse_dns.ip`
(accepte aussi un hostname), `mcp_web.fetch_url.max_bytes` (clamp silencieux au plafond).

Chaque serveur appelle `self.finalize_tools()` (défini dans `mcp_base.py`) en dernière
ligne de son `__init__`, après l'enregistrement de tous les outils — à conserver en
ajoutant un outil ou un serveur. Cet appel normalise ce que `tools/list` expose :
`inspect.cleandoc` sur les descriptions (une docstring assignée via
`func.__doc__ = f"""..."""` partirait sinon sur le wire avec l'indentation source de
chaque ligne de continuation), et suppression des clés `"title"` auto-générées par
Pydantic dans les schémas de paramètres (bruit pur, le payload `tools/list` est renvoyé
au modèle à chaque requête).

Les descriptions d'outils sont volontairement compactes, mais chaque garantie
comportementale qui y reste est contractuelle (valeurs de caps interpolées, « la plage
déplace la fenêtre, ne lève pas le cap », exclusivité char/ligne, exclusion xlsx de la
pagination, labels de `search` réutilisables comme selectors, niveau unique d'imbrication
zip) : ne pas les couper pour gagner des tokens, le modèle appelant les prend au pied de
la lettre.

### Faire apparaître une valeur d'environnement résolue dans une docstring d'outil

Si la description d'un outil doit citer la valeur *active* d'une constante dérivée d'une
variable d'environnement (ex. `MIAOU_DOCS_READ_CAP`) plutôt que le nom de la variable,
`f"""..."""` en position de docstring ne fonctionne pas : Python n'assigne `__doc__`
qu'à partir d'un littéral de chaîne reconnu syntaxiquement, jamais depuis une expression
(f-string comprise) — la fonction se retrouve avec `__doc__ is None`, silencieusement.

Pattern à appliquer : définir la fonction sans décorateur, assigner `func.__doc__ = f"""..."""`
explicitement, puis appliquer le décorateur a posteriori en appel direct :

```python
async def read(...) -> str:
    ...

read.__doc__ = f"""Réponse plafonnée à {READ_CAP} caractères, ..."""
self.mcp.tool(name="read")(read)
```

Voir `servers/mcp_docs/__init__.py` (outils `read`/`search`) pour l'exemple appliqué.


## Domaines détaillés (`docs/`)

À lire à la demande, selon la zone touchée — pas systématiquement. CLAUDE.md garde
ce qui sert à *toute* tâche (forme du projet, lancement, conventions d'écriture d'un
outil) ; le reste est ici.

**Toute modification d'un `docs/*.md` déclenche la question : « la ligne d'index
ci-dessous le décrit-elle encore correctement ? »** La ligne résume en quelques
mots-clés/noms de fonctions le contenu du fichier ; si le lot change un fait qu'elle
cite (constante renommée, contrat déplacé, outil ajouté), la relire et la corriger
dans le même lot. Sans ce déclencheur, une ligne d'index reste fausse pendant des
lots — piège déjà payé côté MIAOU.

- **`docs/servers.md`** — les six serveurs en détail : outils exposés, contrats,
  variables d'environnement, décisions de conception. `mcp_bench` (chemins de
  résultat D8/D9), `mcp_weather` (`astronomy`/`hourly` séparés et pourquoi, `extract`
  et le nom de ressource `weather-<lieu>-<yyyymmdd>.json`), `mcp_web` (cache par
  checksum d'URL, caps
  `READ_CAP`/`LIST_CAP`, `fetch_resource` et le canal bytes→client), `mcp_ddg`,
  `mcp_brave` (`resolve_api_key`, refus d'init sans clef), `mcp_docs` (obsolète mais
  conservé pour le hors-connexion : sessions, pagination, `search`, `extract` hors
  `READ_CAP`, sécurité archives, locales des headings docx).
- **`docs/proxy.md`** — `mcp_proxy.py` hors auth : les trois types d'upstream
  (inprocess/stdio/http), `build_upstreams`/`build_proxy_server`/`build_app` et le
  wrapper ASGI qui évite le 307 sur `/mcp`, l'override `--proxy`/`--noproxy` et ses
  trois chemins d'application, le format de `config.json`, et le pattern
  `build(config)` pour plusieurs instances d'un même module.
- **`docs/auth.md`** — campagne AB, les deux sens sans rapport entre eux. Entrante
  (AB-1 : le proxy est Resource Server, `JwtTokenVerifier`, validation d'audience
  RFC 8707 non désactivable, `dev_auth_server.py`). Sortante (AB-2 : le proxy est
  client OAuth d'un tiers, `UpstreamTokenStorage` et ses gardes d'écriture, parcours
  `/authorize/{name}` + `/callback`, scopes et le 403 qui n'est pas une panne, le
  troisième état « connu mais pas autorisé », contrat `AUTHORIZATION_REQUIRED`,
  `authorize_path` et le `_meta` de `tools/list`, `HttpUpstream` et la contrainte
  anyio des cancel scopes).
- **`docs/miaou-contract.md`** — surface de contact avec MIAOU : transport
  streamable-http et table de configuration des cartes serveur, séquence attendue
  (`initialize` → `tools/list` → `tools/call`) et les trois familles de blocs de
  résultat, et le contrat `mcp_docs` ↔ dispatcher (détection de capability par
  `ref`+`content_b64`, `session_id`, idempotence de la matérialisation, REF_UNKNOWN
  et son rejeu qui ne marche que derrière le proxy, formats de `ref` acceptés).
- **`docs/tls.md`** — `enable_system_trust_store()` : pourquoi une AC d'entreprise
  interne échoue en `CERTIFICATE_VERIFY_FAILED` alors que le navigateur l'accepte,
  l'injection `truststore` qui remplace la classe `ssl.SSLContext`, les deux points
  d'appel (et l'ordre contractuel dans `mcp_proxy.main()`), le best-effort assumé.
- **`docs/tests.md`** — ce que chaque suite mocke (aucun appel réseau réel, aucune
  clef requise), l'isolation filesystem par `tmp_path`, et `tests/live_call.py` qui
  parle le vrai transport streamable-http comme MIAOU.

## Règle d'or

En cas d'ambiguïté sur un point non couvert ici : **signaler plutôt que deviner**.
