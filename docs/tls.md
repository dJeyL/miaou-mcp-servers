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
comme httpx (`HttpUpstream`, et le client OAuth du SDK MCP) construisent leur contexte via
`ssl.create_default_context()`, donc via la classe patchée. **Aucun appel HTTP n'est
réécrit, et rien n'est passé explicitement à un client** — c'est la raison d'être du point
d'injection unique, et pourquoi il n'y a pas eu de migration `requests`→`httpx` ici : ce
dépôt n'a jamais utilisé `requests`.

Deux points d'appel, un par mode de lancement, et pas un de plus :

- **`MiaouMCPBase.main()`** — seul point traversé par les six lancements standalone.
- **`mcp_proxy.main()`**, en **tête**, avant `build_upstreams` (qui importe les modules de
  serveurs) et avant tout handshake TLS. L'ordre est contractuel : un contexte SSL déjà
  construit garde la classe d'origine et continue d'ignorer le magasin système.
  `test_main_enables_system_trust_store_before_building_upstreams` épingle l'ordre, pas
  seulement le fait que l'appel existe.

Le mode inprocess ne passe pas par `main()` des serveurs — d'où l'appel propre du proxy,
qui couvre alors tout le process, upstreams inprocess compris.

**Best-effort volontaire** : `truststore` absent (installation pip minimale) ou plateforme
non supportée renvoie `False` avec un avertissement sur stderr, sans lever. Sur un poste
sans AC interne, la vérification par bundle CA fonctionne déjà : faire échouer le démarrage
y serait une régression pure pour un bénéfice nul.

Un test épingle que le client httpx du SDK construit bien son contexte via la classe
injectée (`test_sdk_http_client_uses_injected_ssl_context`) — c'est une propriété d'une
**bibliothèque tierce**, exactement comme `test_mcp_sdk_http_client_still_trusts_env` :
un SDK qui câblerait un jour un `ssl_context` explicite ou un bundle certifi rendrait
l'injection silencieusement inopérante sur le seul type d'upstream qui a motivé le
changement.

`truststore` est déclaré aux trois endroits qui gouvernent un environnement
(blocs PEP 723, `requirements.txt`, `pyproject.toml`), blocs PEP 723 de **tous** les
serveurs compris — `mcp_bench` n'a pas de sortie HTTPS, mais son `main()` appelle le
helper comme les autres, et la dépendance manquante y produirait un avertissement au
lancement.

