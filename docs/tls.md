# Vérification TLS : magasin de confiance système (truststore)


Un upstream HTTPS dont le certificat est signé par une **AC d'entreprise interne**
échouait en `CERTIFICATE_VERIFY_FAILED` (« certificate is not trusted »), alors que le
même hôte s'ouvre sans erreur dans un navigateur : l'AC est bien installée, mais dans le
magasin du **système** (schannel sous Windows, Keychain sous macOS, ca-certificates sous
Linux), que Python ne consulte pas — il s'en tient au bundle CA figé de certifi/OpenSSL.
Côté proxy, le symptôme est un upstream marqué `unavailable` au démarrage dont les outils
disparaissent de `tools/list`.

`enable_system_trust_store()` (`servers/mcp_base.py`) appelle
`truststore.inject_into_ssl()`, qui **remplace la classe `ssl.SSLContext` elle-même**.
C'est ce qui rend cet appel unique suffisant pour toute la sortie HTTPS du process,
quelle que soit la bibliothèque : urllib (`make_opener()`, dans weather/ddg/brave/web)
construit son contexte via `ssl.create_default_context()`, donc via la classe patchée.
Depuis le SDK MCP 2.x, le client HTTP des upstreams `http` et du parcours OAuth est
`httpx2`, qui **consulte de lui-même le magasin système** (`truststore.SSLContext` par
défaut, sauf si `SSL_CERT_FILE` ou `SSL_CERT_DIR` sont posés, qu'il préfère alors) :
l'injection ne lui est plus nécessaire, et ne le gêne pas. **Aucun appel HTTP n'est
réécrit, et rien n'est passé explicitement à un client** — c'est la raison d'être du point
d'injection unique, et pourquoi il n'y a pas eu de migration `requests`→`httpx` ici : ce
dépôt n'a jamais utilisé `requests`.

Trois points d'appel, un par mode de lancement, et pas un de plus :

- **`MiaouMCPBase.main()`** — seul point traversé par les six lancements standalone.
- **`mcp_proxy.main()`**, en **tête**, avant `build_upstreams` (qui importe les modules de
  serveurs) et avant tout handshake TLS. L'ordre est contractuel : un contexte SSL déjà
  construit garde la classe d'origine et continue d'ignorer le magasin système.
  `test_main_enables_system_trust_store_before_building_upstreams` épingle l'ordre, pas
  seulement le fait que l'appel existe.
- **`tests/live_call.py`**, avant `asyncio.run`. Seul appelant qui **recopie** le helper au
  lieu de l'importer : c'est un client autonome à bloc PEP 723, et importer `mcp_base`
  y tirerait le SDK serveur et starlette pour quatre lignes. La contrepartie est explicite —
  toute évolution du helper d'origine est à répercuter dans cette copie. Sans elle, viser
  une URL `https://` sous AC interne échoue côté client seul, ce qui se lit à tort comme
  une panne du serveur.

Le mode inprocess ne passe pas par `main()` des serveurs — d'où l'appel propre du proxy,
qui couvre alors tout le process, upstreams inprocess compris.

**Best-effort volontaire** : `truststore` absent (installation pip minimale) ou plateforme
non supportée renvoie `False` avec un avertissement sur stderr, sans lever. Sur un poste
sans AC interne, la vérification par bundle CA fonctionne déjà : faire échouer le démarrage
y serait une régression pure pour un bénéfice nul.

Un test épingle que le client d'un upstream `http` vérifie bien contre le magasin système
(`test_http_upstream_client_checks_the_system_trust_store`), sur le client que
`HttpUpstream._build_http_client` construit RÉELLEMENT — c'est une propriété d'une
**bibliothèque tierce** (httpx2), exactement comme `trust_env` (cf. `docs/proxy.md`) : un
défaut qui changerait un jour rendrait la vérification silencieusement fausse sur le seul
type d'upstream qui a motivé le changement. Avant la migration, ce test visait le client
que construisait le SDK : resté vert, il n'aurait plus rien prouvé.

`truststore` est déclaré aux trois endroits qui gouvernent un environnement
(blocs PEP 723, `requirements.txt`, `pyproject.toml`), blocs PEP 723 de **tous** les
serveurs compris — `mcp_bench` n'a pas de sortie HTTPS, mais son `main()` appelle le
helper comme les autres, et la dépendance manquante y produirait un avertissement au
lancement.

