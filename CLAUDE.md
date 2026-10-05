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
├── mcp_proxy/            # serveur proxy (package, point d'entrée principal)
│   ├── __init__.py       # ré-exporte la surface publique historique
│   ├── __main__.py       # `python -m mcp_proxy`
│   ├── contract.py       # constantes/exceptions partagées (casse le cycle server↔auth_out)
│   ├── logging.py        # `_log` format uvicorn
│   ├── upstream.py       # InProcess / Stdio / Http (ère négociée par `Client` ; skills : inprocess seulement)
│   ├── netproxy.py       # override --proxy / --noproxy
│   ├── config.py         # load_config, build_upstreams
│   ├── server.py         # build_proxy_server, catalogue, instructions, relais du `_meta` d'outil
│   ├── skills.py         # extension Skills agrégée : URI préfixées, bloc des instructions, repli read_skill
│   ├── auth_in.py        # Resource Server OAuth (AB-1)
│   ├── auth_out/         # client OAuth d'upstreams tiers (AB-2/AB-3), package
│   │   ├── debug.py      # --debug-auth, masquage (porte _AUTH_DEBUG)
│   │   ├── probe.py      # choix de l'outil de sonde, lecture du refus
│   │   ├── storage.py    # UpstreamTokenStorage, gardes d'écriture
│   │   └── authorizer.py # parcours, renouvellement, routes Starlette
│   ├── app.py            # build_app (Starlette)
│   └── entry.py          # CLI, main()
├── dev_auth_server.py    # serveur d'autorisation OAuth de DÉVELOPPEMENT (jamais en prod)
├── servers/
│   ├── mcp_base.py       # classe de base partagée (MiaouMCPBase + make_opener + extension Skills)
│   ├── mcp_bench.py      # banc d'essai général (port 8766)
│   ├── mcp_weather.py    # météo réelle via wttr.in (port 8767)
│   ├── mcp_web/          # téléchargement d'URL et recherche multi-moteurs (port 8768), package — `_meta` client sur fetch_url, `search/` = un module par moteur
│   ├── mcp_ddg.py        # recherche DuckDuckGo HTML (port 8769) — DÉPRÉCIÉ, désactivé par défaut
│   ├── mcp_brave.py      # recherche Brave Search API (port 8770) — DÉPRÉCIÉ, désactivé par défaut
│   ├── skills/           # skills des serveurs mono-fichier : skills/<serveur>/<skill>/SKILL.md
│   └── mcp_docs/         # extraction PDF/Office/Zip (port 8771), package — OBSOLÈTE, désactivé par défaut
├── docs/                 # domaines détaillés, lus à la demande (voir index en fin de fichier)
├── tests/
│   ├── live_call.py      # appel manuel d'un outil sur un serveur lancé (non collecté)
│   ├── live_auth_probe.py  # ce qu'un upstream répond SANS jeton (non collecté)
│   ├── live_discovery_probe.py  # ce que la découverte OAuth du SDK 2.x conclurait (non collecté)
│   ├── proxy_client.py   # harnais : parler au Server du proxy en JSON-RPC legacy (non collecté)
│   ├── skills_fixture_server.py  # upstream de test qui sert des skills : build(config), ou stdio en script (non collecté)
│   ├── era_fixture_server.py  # upstream 2.x de test (stdio en subprocess, app http via build()) (non collecté)
│   ├── fabricated_skills_server.py  # upstream 2.x de test aux entrées de skills fabriquées (non collecté)
│   ├── legacy_stdio_fixture.py  # faux upstream stdio qui ne parle que `initialize` (non collecté)
│   ├── test_base.py
│   ├── test_bench.py
│   ├── test_weather.py
│   ├── test_web.py
│   ├── test_web_structure.py
│   ├── test_web_pagemeta.py  # `_meta` de fetch_url : titre, site, URL finale, favicon (lot AI)
│   ├── test_web_search.py  # search/image_search : chaîne de repli, pauses, budget, config
│   ├── test_ddg.py
│   ├── test_brave.py
│   ├── test_docs.py
│   ├── test_proxy.py
│   ├── test_skills.py    # extension Skills de mcp_base (vecteurs d'empreinte de la spec)
│   ├── test_proxy_skills.py  # skills servies par le proxy : URI, `_meta`, bloc, repli
│   ├── test_proxy_era.py  # ère négociée avec les upstreams stdio/http (vrais handshakes)
│   ├── test_proxy_remote_skills.py  # skills relayées d'upstreams stdio/http (vrais handshakes)
│   ├── test_proxy_auth.py  # auth OAuth entrante (lot AB-1)
│   ├── test_proxy_outbound_auth.py  # auth OAuth sortante (lot AB-2)
│   └── test_dev_auth_server.py  # serveur d'autorisation de développement (lot AB-1.3)
├── config.sample.json    # template de config pour le proxy
├── config.json           # (gitignored) config active du proxy
├── requirements.txt      # pour les utilisateurs sans uv
├── pyproject.toml        # métadonnées projet + config pytest (asyncio_mode=auto) + groupe dev
│                         # + [project.scripts] mcp_proxy et le build-system qui le rend
│                         #   installable — c'est ce qui garde `uv run mcp_proxy` court
├── uv.lock               # lock uv, versionné
└── .gitignore
```

## Les serveurs en un coup d'œil

Six serveurs de banc d'essai plus un proxy qui les agrège. Le détail de chacun
(outils, contrats, variables d'environnement, décisions) est dans
**`docs/servers.md`** — à lire quand on touche au serveur concerné, pas avant.

| Serveur | Port | Rôle | Outils |
|---|---|---|---|
| `mcp_bench.py` | 8766 | Banc d'essai général : exerce les chemins de résultat de MIAOU (texte, image, resource) | `echo`, `add`, `sleep`, `dns_lookup`, `reverse_dns`, `get_image`, `get_json_resource` |
| `mcp_weather.py` | 8767 | Météo réelle via wttr.in | `get_weather` (`astronomy`, `hourly`, `extract`) |
| `mcp_web/` | 8768 | Téléchargement d'URL, cache disque par checksum, pagination ; recherche multi-moteurs (Brave → Ollama → DDG, ordre en config) | `fetch_url`, `fetch_read`, `fetch_list`, `fetch_resource`, `search`, `image_search` (si un moteur sait chercher des images) |
| `mcp_ddg.py` | 8769 | Recherche DuckDuckGo (HTML scrapé) — **déprécié** | `ddg_search` |
| `mcp_brave.py` | 8770 | Recherche Brave Search API (clef requise) — **déprécié** | `brave_search`, `brave_image_search` |
| `mcp_docs/` | 8771 | Extraction PDF/Office/Zip — **obsolète, désactivé par défaut** | `list`, `read`, `search`, `extract`, `drop_session` |
| `mcp_proxy/` | 8765 | Agrège tout sur un port, préfixe les outils (`bench__echo`…) et les skills | (+ `status` si auth sortante, `read_skill` si une skill est servie) |

Deux points qu'on ne devine pas depuis le tableau :

- **`mcp_docs` est obsolète mais conservé** — MIAOU ouvre ces cinq formats lui-même.
  Il reste pour le travail **hors connexion** (l'ouverture native télécharge ses
  moteurs depuis un CDN). Ne pas « faire le ménage » dans ce package au motif qu'il
  ne sert plus par défaut.
- **`mcp_ddg` et `mcp_brave` sont dépréciés** — remplacés par `search`/`image_search`
  de `mcp_web`, désactivés dans `config.sample.json`, conservés le temps de la
  transition (avertissement à chaque démarrage). `mcp_ddg` actif à côté de `mcp_web`
  ne partage pas son espacement vers DuckDuckGo.
- **Pas d'outil sans config fonctionnelle** — `mcp_brave` refuse de s'initialiser sans
  clef, et `mcp_web` refuse de même si `config.search.order` ne cite que des moteurs
  non configurés (ce qui emporte aussi ses `fetch_*`). Côté proxy, l'upstream est
  retiré de la table de routage et les autres démarrent normalement.

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
BRAVE_API_KEY=<key> uv run servers/mcp_brave.py      # HTTP 127.0.0.1:8770 (déprécié, comme mcp_ddg)

# mcp_web et mcp_docs sont des packages (pas des scripts plats) — lancement différent :
uv run --directory servers python -m mcp_web         # HTTP 127.0.0.1:8768
uv run --directory servers python -m mcp_docs        # HTTP 127.0.0.1:8771

# Proxy (agrège tout sur un seul port)
cp config.sample.json config.json     # puis éditer config.json (clefs de config.search de web, etc.)
uv run mcp_proxy                        # port défini dans config.json
uv run mcp_proxy --port 8765            # override port
uv run mcp_proxy --config autre.json
uv run mcp_proxy --proxy 10.0.0.1:3128  # force le proxy réseau vu par les upstreams
uv run mcp_proxy --noproxy              # force l'absence de proxy vu par les upstreams
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
python -m mcp_proxy [options]     # depuis la racine (package, plus un script plat)
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
  renverrait 403 avant même la couche CORS si la protection est active. Depuis le
  SDK 2.x, le réglage se passe à `streamable_http_app()` (`_make_app`), plus au
  constructeur — et l'omettre RÉACTIVE la protection, l'hôte par défaut étant
  `127.0.0.1` (mesuré : 403). Le proxy, lui, laisse `security_settings` à `None`
  dans son gestionnaire de sessions, ce qui la désactive.

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

Une `urllib.error.HTTPError` attrapée pour être convertie en message **se ferme**
(`e.close()`) : elle est aussi la réponse, socket comprise, qui resterait sinon ouverte
jusqu'au GC. Les tests vérifient `err.fp.closed` après l'appel plutôt que de la fermer
eux-mêmes — c'est ce geste, dans chaque test, qui a masqué la fuite dans tous les serveurs.

## Tests

```bash
# Avec uv — commande canonique, ne dépend pas de pyproject.toml/uv.lock
uv run --with pytest --with pytest-asyncio --with html2text --with pymupdf \
  --with python-docx --with openpyxl --with python-pptx --with truststore --with pyyaml pytest tests/

# Avec uv — alternative via pyproject.toml (groupe dev) + uv.lock, équivalente
# depuis que [project.dependencies] couvre l'union runtime (DD7)
uv run --group dev pytest tests/

# Avec pip (après pip install -r requirements.txt)
pytest tests/
```

`pyproject.toml` porte `asyncio_mode = "auto"` (pytest-asyncio) — les deux commandes
uv ci-dessus s'appuient dessus, la commande canonique n'a juste pas besoin du lock.

Ce que chaque suite mocke (et pourquoi aucun test ne fait d'appel réseau réel), plus
les bancs manuels — `tests/live_call.py`, qui parle le vrai transport
streamable-http, `tests/live_auth_probe.py`, qui mesure ce qu'un upstream
répond sans jeton, et `tests/live_discovery_probe.py`, qui rejoue la découverte
OAuth du SDK 2.x : `docs/tests.md`.

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
`MCPServer` (SDK MCP 2.x, ex-`FastMCP`) génère le schéma JSON automatiquement depuis la
signature Python et la docstring. Aucune déclaration manuelle dans un registre.

**Un refus adressé au modèle se lève en `ToolError` du SDK**
(`mcp.server.mcpserver.exceptions`, ou une sous-classe — celle de `mcp_docs` en est
une). En 2.x, `MCPServer` ne transmet le texte d'une exception d'outil QUE pour celle-là :
toute autre exception est traitée en plantage et rendue en « Error executing tool <nom> »
nu, sans un mot du motif — en silence, puisque l'appel reste un `isError` ordinaire. Une
`MCPError` levée par un outil sort, elle, en erreur JSON-RPC (`data` intact) : c'est le
canal d'un contrat machine comme REF_UNKNOWN, jamais d'un refus que le modèle doit lire.

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


## Ajouter une skill à un serveur

Une skill (format Agent Skills) est un dossier contenant un `SKILL.md` à frontmatter
YAML (`name` = nom du dossier, `description` non vide), plus d'éventuelles annexes
citées par chemin relatif. Elle se pose dans le `skills_dir` du serveur :
`servers/skills/<serveur>/<skill>/` pour un serveur mono-fichier,
`servers/mcp_<serveur>/skills/<skill>/` pour un package. Le serveur passe
`skills_dir=` à `MiaouMCPBase`, déclare `pyyaml` dans son bloc PEP 723, et, si ses
outils l'exigent, appelle `self.finalize_tools(requires_skill=...)`. Une skill invalide
fait échouer le démarrage avec sa cause. Le texte libre du serveur (instructions,
docstrings) nomme la skill comme « skill MCP » à lire par son URI, sans jamais écrire
cette URI : le proxy la préfixe, et génère lui-même la ligne qui la donne. Détail :
`docs/servers.md` (section « Skills d'un serveur ») et `docs/proxy.md`.

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
  variables d'environnement, décisions de conception. Skills d'un serveur
  (`skills_dir` et où le poser, validation au démarrage, ensemble de fichiers figé
  mais contenu relu, `SkillFileResource` et les octets intacts, `requires_skill`,
  PyYAML paresseux, base importée par `from mcp_base` sous peine de skills invisibles au proxy, jamais d'URI de skill dans le texte libre). `mcp_bench` (chemins de
  résultat D8/D9, seul serveur à publier des `instructions` et une skill exigée
  par ses outils — consigne durable qui se nomme au lieu de se désigner, règle
  « non contractuel » portée par la skill, et qui lui interdit de servir d'upstream muet
  dans les tests ; `sleep` et son `_SLEEP_CAP`, dont le clamp teste NaN à part parce
  qu'il traverse `max`/`min` et qu'`asyncio.sleep(NaN)` ne termine jamais),
  `mcp_weather` (`astronomy`/`hourly` séparés et pourquoi, `extract`
  et le nom de ressource `weather-<lieu>-<yyyymmdd>.json`), `mcp_web` (cache par
  checksum d'URL, caps
  `READ_CAP`/`LIST_CAP`, `fetch_resource` et le canal bytes→client, décompression
  `Content-Encoding` non sollicitée — `_decompress`, WEB9 — et la troncature décidée
  sur les octets reçus qu'elle impose ; `_meta["miaou/web"]` de `fetch_url` — titre,
  `site_name`, URL finale, favicon reconnue aux octets et plafonnée, ICO réduit à
  une image de 32 px (`shrink_ico`), cache par origine —, la `HTTPError` fermée
  par `_guarded_fetch`, et le retour `CallToolResult` qui l'impose ; recherche
  `search`/`image_search` — config `search` et `build_chain` (`order`, clefs, refus
  `SearchConfigError` si aucun moteur cité n'est utilisable), listage figé à la
  construction, vide = réponse, `fallback`, `_meta["miaou/search"]` = moteur
  pour le client, `clean_snippet` et `MAX_RESULTS` = 10,
  pauses par cause dont la clef refusée de Brave en 422 mesurée, budget
  `SEARCH_BUDGET_S` raboté par moteur, `ddg.ENGINE` unique par processus et non
  partagé avec `mcp_ddg`, ajout d'un moteur et réserves sur ddgs), `mcp_ddg`
  — déprécié — (défi anti-bot reconnu à `anomaly-modal` au lieu d'un `[]` muet, blocage d'IP
  de plus de 2 h mesuré, espacement `_MIN_INTERVAL_S`/`_MAX_WAIT_S` borné par
  les 30 s de timeout MIAOU, sans effet entre instances), `mcp_brave` — déprécié — (`resolve_api_key`, refus d'init sans clef), `mcp_docs` (obsolète mais
  conservé pour le hors-connexion : sessions, pagination, `search`, `extract` hors
  `READ_CAP`, sécurité archives, locales des headings docx).
- **`docs/proxy.md`** — `mcp_proxy/` hors auth : les trois types d'upstream
  (inprocess/stdio/http), `build_upstreams`/`build_proxy_server`/`build_app` et le
  wrapper ASGI qui évite le 307 sur `/mcp`, l'override `--proxy`/`--noproxy` et ses
  trois chemins d'application, le format de `config.json`, le pattern
  `build(config)` pour plusieurs instances d'un même module, ce que `call_tool`
  relaie d'un upstream stdio/http (`relay_call_result` : `content`, `isError`,
  `_meta` sans le `serverInfo` de l'upstream, pas `structuredContent`), ce qu'il rend en erreur depuis le SDK 2.x
  (`isError` + `str(e)` comme en 1.x — la `MCPError` d'un upstream http déballée
  du task group de `call_tool` —, sauf les deux contrats en `MCPError` :
  AUTHORIZATION_REQUIRED — réservé à un upstream qui a un authorizer, un upstream
  sans OAuth et sans session étant « injoignable » — et une `MCPError` d'upstream
  INPROCESS), la panne d'un upstream isolée dans `tools/list` (un stdio mort ne fait
  plus tomber les autres), les schémas
  d'entrée complétés par `_object_schema` (un seul outil sans `type: object`
  ferait rejeter tout `tools/list`) et purgés de `x-mcp-header`
  (`_strip_param_headers`, sinon le proxy exige `Mcp-Param-*` de son client), le gestionnaire de sessions
  (`MAX_REQUEST_BODY_BYTES`, `SESSION_IDLE_TIMEOUT_S`), le client httpx2 de
  `HttpUpstream._build_http_client` sur lequel portent les tests `trust_env`, et
  `aggregate_instructions` (consigne de portée serveur : les trois captures par type
  d'upstream, la section titrée par le préfixe d'outil, l'écriture différée après
  `start()`, le préambule non préfixé que le client re-préfixant doit réécrire).
  Ère des upstreams stdio/http : `Client(<transport>, mode, cache=None)` du SDK
  (sonde `server/discover`, repli sur `initialize` y compris sur délai de 10 s et
  sur 4xx nu, panne jamais prise pour un verdict d'ère), borne inchangée, session
  legacy identique à une requête près, `protocol_version`/`capabilities` sur
  `Upstream`, `tools/list` amorcé au démarrage d'un moderne (`Mcp-Param-*` ne
  part que pour un outil listé sur la session), aucune capacité d'upstream
  republiée, clé `protocol_era` (`auto`/`legacy`) d'une entrée stdio/http, ère au
  journal de démarrage, ligne « skills non relayées » réservée au legacy forcé par
  la config.
  Extension Skills agrégée : `prefix_skill_uri`/`resolve_skill_uri`,
  `install_skills` au lifespan et seulement si une skill est servie (capacités
  calculées à chaque `server/discover`), stdio/http relayés sous la condition
  unique `serves_skills` (ère moderne ET extension déclarée ; un legacy ne reçoit
  aucune requête), `send_request` brut et `read_resource` (`Mcp-Name` par le SDK),
  requêtes bornées, garde unique `_on_session` de `HttpUpstream`, erreurs relayées
  (`_remote_failure`, jamais de code 0), upstream en panne omis des listages,
  extension publiée dès qu'un upstream distant la DÉCLARE (listage vide compris),
  l'upstream distant seul juge de `resources/read` (pas de liste blanche), entrées
  non préfixables écartées (`_unprefixable`) et le reste relayé tel quel, bloc
  dégradé (description bornée à 1 024), indices de cache les plus restrictifs,
  texte libre d'un upstream relayé aux URI préfixées, repli `miaou/skillsFallback`
  d'un upstream relayé non republié (`published_tools`), compte des skills sur la
  ligne de démarrage et une ligne par upstream pour une skill exigée non servie,
  bloc statique qui ment pendant la panne d'un upstream, surface (instructions et
  skills) recalculée après `authorize()` (`publish_surface`),
  `relay_tool_meta` et `miaou/requiresSkill` réécrit, `_meta` dans
  `ToolCatalogCache`, bloc généré des instructions (forme, en-tête « pas des skills
  locales » et la mesure qui l'a imposé), repli `read_skill` et sa marque
  `miaou/skillsFallback`.
- **`docs/auth.md`** — campagne AB, les deux sens sans rapport entre eux. Entrante
  (AB-1 : le proxy est Resource Server, `JwtTokenVerifier`, validation d'audience
  RFC 8707 non désactivable, `dev_auth_server.py`). Sortante (AB-2 : le proxy est
  client OAuth d'un tiers, `UpstreamTokenStorage` et ses gardes d'écriture, parcours
  `/authorize/{name}` + `/callback` et le recalcul de la surface publiée qui suit
  (`on_authorized`), scopes et le 403 qui n'est pas une panne, le
  troisième état « connu mais pas autorisé » et les DEUX conditions de
  `upstream_is_live` — « transport ouvert » ne vaut pas « autorisé » —,
  `has_usable_token` qui l'amorce au boot sans requête, contrat
  `AUTHORIZATION_REQUIRED`, `authorize_path` et le `_meta` de `tools/list`,
  l'attente sur événement de `/authorize/{name}`, `_provoke_refusal` qui déroule
  la séquence jusqu'à `tools/call` (seul refusé sur un déploiement
  d'entreprise), `--debug-auth`, son masquage et la LISTE de loggers qui doit
  couvrir l'après-boot (`mcp.client.streamable_http`, et `httpx2` depuis le
  SDK 2.x ; nommer un logger muet est silencieux), le filtre qui écarte du
  journal du SDK le refus VOLONTAIRE de `_on_redirect` (`_ExpectedRefusalFilter`),
  `HttpUpstream` et la contrainte anyio des cancel scopes (garde `_on_session`, et
  sa limite : une `AuthorizationRequired` en cours de session rend « Connection
  closed » à l'appel en vol, le contrat n'arrivant qu'au suivant), la sonde d'ère qui
  reçoit le premier 401 sans rien changer — `AuthorizationRequired` la traverse —
  et `_provoke_refusal`/`refresh_if_due` restés en JSON-RPC legacy). Durcissements du SDK
  2.x, éprouvés contre `dev_auth_server.py` seulement : `iss` (RFC 9207) relayé
  par `/callback` et comparé à l'issuer effectif — d'où `auth.issuer` à déclarer
  sur un realm —, issuer d'AS comparé à l'octet près (et la PRM construite depuis
  les chaînes de la config, sans le slash qu'ajoutait `AnyHttpUrl`),
  `offline_access` + `prompt=consent`, PRM en 5xx fatale. Renouvellement (AB-3 : le refresh du SDK est passif et
  vise `<hôte-du-serveur-MCP>/token` quand la découverte échoue — d'où
  `build_oauth_metadata_override` et les endpoints déclarés ENSEMBLE en config —,
  `refresh_if_due` et la boucle du lifespan qui couvre l'INACTIVITÉ, tentative au
  boot parce que « utilisable » inclut un jeton expiré à rafraîchir, écriture par
  le provider partagé pour rester écrivain unique, marge et période CALIBRÉES sur
  la durée de vie émise — `observed_lifetime`, les constantes ne sont que des
  plafonds —, succès jugé sur l'avancement de l'échéance, et le TROISIÈME cas
  d'une échéance inchangée : ni renouvellement ni anomalie, trace calme une
  fois par épisode).
- **`docs/miaou-contract.md`** — surface de contact avec MIAOU : transport
  streamable-http et table de configuration des cartes serveur, séquence attendue
  (`initialize` → `tools/list` → `tools/call`) et les trois familles de blocs de
  résultat, le champ `instructions` de l'`InitializeResult` que MIAOU lit et
  injecte dans le system prompt (et le préambule qu'il re-préfixe, étant seul à
  connaître son slug), le `_meta` d'un résultat `tools/call` adressé au client
  (`miaou/web` de `fetch_url`, premier usage ; `miaou/search` = moteur de
  `search`/`image_search` ; jamais le `serverInfo` d'un upstream),
  `x-mcp-header` absent des schémas du proxy (aucun `Mcp-Param-*` à poser), les skills servies par le proxy (capacités par ère,
  et dès qu'un upstream stdio/http moderne déclare l'extension, URI préfixées,
  `skills/*` aux indices de cache les plus restrictifs et aux entrées tierces
  relayées telles quelles, `resources/read` et `Mcp-Name`, `miaou/requiresSkill`,
  bloc des instructions, repli `read_skill` à masquer par sa marque, préfixe double
  d'un proxy chaîné et la collision d'approbations qu'il rend possible, et le réflexe
  `miaou__skills__read` mesuré avec MIAOU actuel), et le contrat `mcp_docs` ↔ dispatcher (détection de capability par
  `ref`+`content_b64`, `session_id`, idempotence de la matérialisation, REF_UNKNOWN
  levée en `MCPError` par l'outil et donc rejouable en autonome comme derrière le
  proxy, taille d'un `content_b64` et `MAX_REQUEST_BODY_BYTES` couplé au plafond
  de MIAOU, sessions jamais expirées d'inactivité, formats de `ref` acceptés).
- **`docs/tls.md`** — `enable_system_trust_store()` : pourquoi une AC d'entreprise
  interne échoue en `CERTIFICATE_VERIFY_FAILED` alors que le navigateur l'accepte,
  l'injection `truststore` qui remplace la classe `ssl.SSLContext`, les trois points
  d'appel (l'ordre contractuel dans `mcp_proxy.main()`, la copie assumée dans
  `tests/live_call.py`), le best-effort assumé, et httpx2 (SDK 2.x) qui consulte le
  magasin système de lui-même — l'injection ne sert plus qu'à urllib.
- **`docs/tests.md`** — ce que chaque suite mocke (aucun appel réseau réel, aucune
  clef requise), le harnais `tests/proxy_client.py` (`Client(mode="legacy")`, le
  chemin JSON-RPC de MIAOU, jamais le mode par défaut), les patchs qui visent
  `httpx2` et non plus `httpx`, l'isolation filesystem par `tmp_path`, les deux pièges de fixture
  du mock de réponse de `test_web.py` (queue de remplissage compressible, garde
  verte des deux côtés), les vrais handshakes de `test_proxy_era.py` (fixtures
  stdio des deux ères, app http sur `httpx2.ASGITransport`, middleware qui imite
  un serveur 1.x, identité legacy comparée au mode `legacy`), l'opener qui route par URL de `test_web_pagemeta.py`
  (favicon, sonde unique par origine) et le cache de favicons vidé en fixture,
  et les bancs manuels
  non collectés : `tests/live_call.py`, qui parle le vrai transport
  streamable-http comme MIAOU (`-H/--header`, truststore, affiche le `_meta`, et
  `--modern`/`--method` pour l'ère 2026-07-28 et une méthode JSON-RPC quelconque), et
  `tests/live_auth_probe.py`, qui mesure ce qu'un upstream répond SANS jeton
  (séquence complète avec `Mcp-Session-Id`, `--tool`/`--args`, et le filtre
  `_looks_mutating` qui interdit d'appeler un outil d'écriture pour sonder), et
  `tests/live_discovery_probe.py`, qui rejoue la découverte OAuth du SDK 2.x avec
  ses propres fonctions (version épinglée) et rend un verdict par durcissement.

## Règle d'or

En cas d'ambiguïté sur un point non couvert ici : **signaler plutôt que deviner**.
