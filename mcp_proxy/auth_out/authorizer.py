"""Le client OAuth lui-même : parcours d'autorisation et renouvellement.

`UpstreamAuthorizer` porte l'état d'un upstream tiers — jeton, transaction en
cours, échéance de renouvellement — et les routes Starlette qui déroulent le
parcours (`/authorize/{name}`, `/callback`).
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from starlette.routing import Route

from ..contract import AuthorizationRequired, authorize_path
from ..logging import _log
from ..upstream import HttpUpstream, Upstream
from .debug import _auth_debug_enabled, _redact_url
from .probe import _AUTH_PROBE_TOOL, _MCP_PROTOCOL_VERSION, _pick_probe_tool
from .storage import UpstreamTokenStorage, _default_tokens_path


def build_client_info_override(auth: dict[str, Any] | None) -> Any:
    """Credentials pré-provisionnés → OAuthClientInformationFull, ou None.

    Pour un AS qui ne fait pas d'enregistrement dynamique (GitHub, notamment),
    où client_id/client_secret sont créés à la main.
    """
    if not auth:
        return None
    client_id = auth.get("client_id")
    if not client_id:
        return None

    from mcp.shared.auth import OAuthClientInformationFull

    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=auth.get("client_secret"),
        redirect_uris=[auth["redirect_uri"]] if auth.get("redirect_uri") else None,
        scope=auth.get("scope"),
        token_endpoint_auth_method=auth.get(
            "token_endpoint_auth_method",
            "client_secret_post" if auth.get("client_secret") else "none",
        ),
    )


def build_oauth_metadata_override(auth: dict[str, Any] | None) -> Any:
    """Endpoints de l'AS déclarés en config → OAuthMetadata, ou None.

    POURQUOI CE N'EST PAS UN CONFORT. Le SDK ne connaît le token endpoint que
    par `context.oauth_metadata`, peuplé UNIQUEMENT par la découverte
    `/.well-known/...` qui a lieu dans la branche 401 d'`async_auth_flow`. Deux
    conséquences, toutes deux mesurées le 2026-09-07 sur un WSO2 :

    - cette découverte ÉCHOUE quand l'AS ne sert pas ses métadonnées aux trois
      chemins que le SDK essaie (ici : realms/wso2, où seuls les endpoints
      `protocol/openid-connect/*` existent) ;
    - même réussie, elle n'est JAMAIS persistée : au redémarrage du proxy,
      `_initialize()` recharge les jetons et le client_info depuis le fichier,
      mais `oauth_metadata` repart à None.

    Or `_refresh_token()` du SDK se replie alors sur
    `urljoin(base_url_du_SERVEUR_MCP, "/token")` — l'hôte du Jira, pas celui de
    l'AS. Le POST de rafraîchissement part vers une URL inexistante, échoue, et
    `_handle_refresh_response` appelle `clear_tokens()` : le refresh token est
    jeté, un parcours interactif s'ouvre, et l'utilisateur reclique. Le tout
    sans un mot, un `logger.warning` en DEBUG étant la seule trace.

    Déclarer les endpoints rend donc le rafraîchissement possible là où la
    découverte ne peut pas aboutir. `issuer` n'a pas d'usage propre ici — le
    SDK ne le vérifie pas — mais le modèle l'exige : on le dérive de
    l'authorization endpoint quand il n'est pas donné.
    """
    if not auth:
        return None
    token_endpoint = auth.get("token_endpoint")
    authorization_endpoint = auth.get("authorization_endpoint")
    if not token_endpoint or not authorization_endpoint:
        # Les deux ou rien : un token endpoint seul laisserait le parcours
        # initial rediriger vers une URL découverte, donc potentiellement vers
        # un autre AS que celui qui rafraîchit. Deux AS pour une même identité
        # est un mode de panne bien pire que l'absence de configuration.
        return None

    from mcp.shared.auth import OAuthMetadata

    issuer = auth.get("issuer")
    if not issuer:
        from urllib.parse import urlparse

        parsed = urlparse(authorization_endpoint)
        issuer = f"{parsed.scheme}://{parsed.netloc}"

    return OAuthMetadata(
        issuer=issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        registration_endpoint=auth.get("registration_endpoint"),
        scopes_supported=auth["scope"].split() if auth.get("scope") else None,
    )


class PendingAuthorization:
    """Rendez-vous entre le navigateur (route /callback) et le parcours OAuth.

    Le SDK attend un `callback_handler` qui BLOQUE puis rend `(code, state)`.
    L'attente est bornée : sans borne, un upstream jamais autorisé retiendrait
    indéfiniment la tâche qui l'attend. Le `timeout` d'OAuthContext ne couvre
    pas ce handler — il est passé au client httpx, pas à notre attente — donc
    la borne est ici, explicitement.

    Le `state` n'est pas vérifié ici : le SDK le compare lui-même en
    `secrets.compare_digest`. En rajouter une deuxième vérification donnerait
    deux prédicats qui peuvent diverger.
    """

    def __init__(self, upstream_name: str, timeout: float) -> None:
        import anyio

        self.upstream_name = upstream_name
        self.timeout = timeout
        self.authorization_url: str | None = None
        # `state` attendu, extrait de l'URL d'autorisation : sert uniquement à
        # router le callback vers le bon upstream (cf. build_callback_route).
        self.state: str | None = None
        self._event = anyio.Event()
        self._code: str | None = None
        self._state: str | None = None
        self._error: str | None = None

    def resolve(self, code: str | None, state: str | None, error: str | None = None) -> None:
        """Appelée depuis la route /callback. Idempotente : un rechargement de
        l'onglet ne doit pas écraser un résultat déjà reçu."""
        if self._event.is_set():
            return
        self._code, self._state, self._error = code, state, error
        self._event.set()

    async def wait(self) -> tuple[str, str | None]:
        import anyio

        with anyio.move_on_after(self.timeout) as scope:
            await self._event.wait()
        if scope.cancelled_caught:
            raise TimeoutError(
                f"Aucune autorisation reçue pour '{self.upstream_name}' sous "
                f"{self.timeout:.0f}s. Relancer l'autorisation quand tu es prêt."
            )
        if self._error:
            raise RuntimeError(
                f"Autorisation refusée pour '{self.upstream_name}' : {self._error}"
            )
        if not self._code:
            raise RuntimeError(
                f"Callback sans code d'autorisation pour '{self.upstream_name}'."
            )
        return self._code, self._state


_AUTHORIZATION_WAIT_S = 300.0

_AUTHORIZE_ROUTE_WAIT_S = 20.0

_REFRESH_POLL_INTERVAL_S = 300.0
_REFRESH_MARGIN_S = 900.0


_RENEWAL_NOOP_FRACTION = 0.05


def _refresh_poll_interval(authorizers: dict[str, Any] | None) -> float:
    """Période de réveil : assez courte pour qu'aucune fenêtre ne soit sautée.

    Un tiers de la marge la plus courte parmi les upstreams, pour que trois
    réveils au moins tombent dans la fenêtre de chacun. Plafonné à
    `_REFRESH_POLL_INTERVAL_S` : un AS généreux n'a pas à être sondé plus
    souvent.

    PAS DE PLANCHER FIXE. Un plancher de 30 s (première version) rendait la
    période PLUS LONGUE que la fenêtre qu'elle doit échantillonner dès que les
    jetons descendent sous la minute : sur des jetons de 30 s — WSO2 en émet,
    mesuré le 2026-09-08 — la marge vaut 15 s pour un réveil toutes les 30 s,
    donc une fenêtre sur deux sautée. La boucle se réveillait après
    l'expiration et trouvait un jeton mort. Le plancher, posé pour « ne pas
    tourner en boucle serrée », cassait exactement l'invariant qu'il devait
    protéger : c'est la fréquence des jetons courts qui commande, et un AS qui
    en émet toutes les 30 s impose de le sonder souvent.
    """
    margins = []
    for authorizer in (authorizers or {}).values():
        storage = getattr(authorizer, "_storage", None)
        observed = getattr(storage, "observed_lifetime", None)
        lifetime = observed() if callable(observed) else None
        if lifetime:
            margins.append(min(_REFRESH_MARGIN_S, lifetime / 2))
    if not margins:
        return _REFRESH_POLL_INTERVAL_S
    # Borne basse à la seconde, uniquement pour qu'une durée de vie aberrante
    # (0, valeur négative) ne produise pas une boucle sans attente.
    return max(1.0, min(_REFRESH_POLL_INTERVAL_S, min(margins) / 3))


def format_authorization_notice(upstream_name: str, url: str) -> list[str]:
    """Le lien, en évidence, sur stderr.

    Le lien copiable est le mécanisme de référence, pas un repli : mcp_proxy est
    une CLI, et confier l'ouverture à l'OS ne garantit ni le bon navigateur ni
    le bon profil (celui où la session tierce est ouverte). --open reste un
    confort explicitement demandé.
    """
    return [
        "",
        f"  Autorisation requise pour l'upstream '{upstream_name}'.",
        "  Ouvrir ce lien dans le navigateur où tu es connecté :",
        "",
        f"    {url}",
        "",
    ]


class UpstreamAuthorizer:
    """Porte l'état d'autorisation d'UN upstream, et fabrique son httpx.Auth.

    Un seul OAuthClientProvider par upstream, construit une fois et réutilisé :
    c'est ce qui rend effectif le verrou d'OAuthContext (un anyio.Lock pris pour
    tout async_auth_flow), donc l'écrivain unique du refresh. En construire un
    par requête rendrait le verrou inopérant et ferait voir un rejeu à un AS à
    rotation, qui révoquerait toute la famille de jetons.
    """

    def __init__(
        self,
        name: str,
        server_url: str,
        storage: Any,
        callback_url: str,
        scope: str | None = None,
        open_browser: bool = False,
        wait_timeout: float = _AUTHORIZATION_WAIT_S,
        oauth_metadata: Any = None,
    ) -> None:
        self.name = name
        self.server_url = server_url
        self.callback_url = callback_url
        self.scope = scope
        # Endpoints déclarés en config, s'il y en a. Posés sur le contexte du
        # provider à sa construction : le SDK ne les découvre qu'en branche
        # 401 et ne les persiste jamais, donc sans ça le refresh d'un proxy
        # redémarré vise `<hôte-du-serveur-MCP>/token`. Cf.
        # build_oauth_metadata_override.
        self.oauth_metadata = oauth_metadata
        self.open_browser = open_browser
        self.wait_timeout = wait_timeout
        self._storage = storage
        self._provider: Any = None
        self.pending: PendingAuthorization | None = None
        # Dernière URL d'autorisation connue, pour la rendre à qui la demande
        # sans relancer un parcours.
        self.last_authorization_url: str | None = None
        # Une autorisation a été RÉCLAMÉE par l'AS et pas encore accordée.
        #
        # Distinct de « pas de session » : un upstream peut très bien accepter
        # `initialize` ET `tools/list` sans jeton, et n'exiger l'autorisation
        # qu'au premier `tools/call` — c'est le cas d'un Jira derrière un
        # portail d'entreprise, observé en production. `_session` est alors POSÉE et
        # `upstream_is_live` rend True, alors que l'upstream refusera tout
        # appel. Sans ce drapeau, les trois surfaces (listing `_meta`, refus
        # d'appel, rapport `status`) concluent toutes « il va bien ».
        #
        # Il est posé par `_on_redirect` quand le parcours est inhibé, c'est-à-
        # dire au seul moment où l'on apprend de l'AS lui-même qu'un jeton
        # manque, et levé par un parcours mené à son terme.
        self.authorization_pending = False
        # Un AS qui rend la même échéance le fait à CHAQUE réveil tant que le
        # jeton reste dans la fenêtre de renouvellement — trois fois par durée
        # de vie avec la période actuelle. Journaliser le non-événement une
        # seule fois par épisode : la trace sert à comprendre pourquoi le jeton
        # ne bouge pas, pas à la répéter jusqu'à noyer le reste.
        self._renewal_noop_reported = False
        # Dernier échec de parcours, pour que `status` dise POURQUOI. Sans lui,
        # un scope insuffisant se présente comme une autorisation manquante, et
        # l'exploitant reclique indéfiniment sur un lien qui ne peut pas
        # réparer sa configuration.
        self.last_error: str | None = None
        # Signalé dès qu'un parcours interactif a produit son URL de
        # redirection, OU qu'il s'est terminé sans en produire. C'est ce que
        # /authorize/{name} attend pour répondre : une attente sur ÉVÉNEMENT,
        # jamais un délai fixe (cf. la route). None hors parcours — la route
        # l'arme elle-même avant de lancer sa tâche.
        self.redirect_ready: Any = None
        # Le parcours interactif est INHIBÉ par défaut, et c'est le point
        # important : le start() d'un upstream tourne dans le lifespan, AVANT
        # qu'uvicorn n'ouvre le port. Y attendre un clic sur /callback serait un
        # interblocage — le proxy attendrait une redirection vers une route
        # qu'il ne sert pas encore. On refuse donc au démarrage, et le parcours
        # ne s'ouvre que sur demande explicite, port déjà ouvert.
        self.interactive = False

    async def _on_redirect(self, url: str) -> None:
        from urllib.parse import parse_qs, urlparse

        if _auth_debug_enabled():
            # CE QUE L'AS REÇOIT, et non ce qu'on croit lui envoyer. Un refus de
            # `redirect_uri` se règle en comparant CETTE valeur, caractère par
            # caractère, à celle enregistrée dans le client OAuth — schéma,
            # hôte, port et chemin compris. La lire ailleurs (config, log de
            # démarrage) ne prouve rien : seule celle-ci part réellement.
            query = parse_qs(urlparse(url).query)
            _log(f"  [auth] {self.name} redirection vers l'AS :")
            _log(f"  [auth]   endpoint     : {urlparse(url)._replace(query='').geturl()}")
            for key in ("redirect_uri", "client_id", "scope", "response_type"):
                value = (query.get(key) or [None])[0]
                if value is not None:
                    _log(f"  [auth]   {key:<12} : {value}")
            _log(f"  [auth]   Comparer redirect_uri à celle enregistrée dans "
                 f"le client OAuth de l'AS — l'égalité doit être EXACTE.")

        if not self.interactive:
            # Mémorisé pour que `status` puisse le rendre, mais on ne bloque
            # pas : le parcours s'arrête ici et l'upstream reste « connu mais
            # non autorisé ».
            self.last_authorization_url = url
            # C'est ICI, et nulle part ailleurs, qu'on apprend de l'AS qu'un
            # jeton manque. Le noter durablement est ce qui permet aux surfaces
            # de le dire sans avoir à re-provoquer l'échec.
            self.authorization_pending = True
            if self.redirect_ready is not None:
                self.redirect_ready.set()
            raise AuthorizationRequired(self.name)

        pending = PendingAuthorization(self.name, self.wait_timeout)
        pending.authorization_url = url
        pending.state = (parse_qs(urlparse(url).query).get("state") or [None])[0]
        self.pending = pending
        # Signalé ICI, pas au retour d'`authorize()` : celle-ci bloque juste
        # après, en attente du retour du navigateur sur /callback. Attendre sa
        # fin ferait tenir la route jusqu'à sa borne alors que l'URL vers
        # laquelle rediriger est déjà connue.
        if self.redirect_ready is not None:
            self.redirect_ready.set()
        for line in format_authorization_notice(self.name, url):
            print(line, file=sys.stderr, flush=True)
        if self.open_browser:
            import webbrowser

            # Confort seulement : un échec d'ouverture ne doit pas casser le
            # parcours, le lien reste affiché.
            try:
                webbrowser.open(url)
            except Exception as e:  # pragma: no cover
                _log(f"Ouverture du navigateur impossible ({e}) — utiliser le lien.")

    async def _on_callback(self) -> tuple[str, str | None]:
        if self.pending is None:  # pragma: no cover
            raise RuntimeError("Callback attendu sans autorisation en cours.")
        try:
            return await self.pending.wait()
        finally:
            self.pending = None

    async def _provoke_refusal(self, client: Any) -> None:
        """Émet la séquence MCP jusqu'à obtenir le refus qui amorce l'OAuth.

        `initialize` NE SUFFIT PAS, et c'est tout l'objet de cette méthode : sur
        un déploiement d'entreprise (passerelle devant un Jira, mesuré le
        2026-09-07), `initialize` répond 200 et seul `tools/call` renvoie le 401
        porteur du `WWW-Authenticate`. Une requête d'amorçage arbitraire — un
        `ping`, la version précédente — n'était donc jamais refusée, et aucun
        parcours ne démarrait.

        Or `tools/call` ne s'envoie pas nu : le transport streamable-http exige
        un `Mcp-Session-Id` obtenu à `initialize` et rejoué ensuite. On déroule
        donc la vraie séquence. Les réponses ne sont pas lues — httpx exécute le
        flow d'authentification AVANT de nous rendre la main, et c'est ce
        passage, pas le résultat, qui nous intéresse.

        Le nom d'outil est sans importance : le refus d'autorisation précède la
        résolution du nom. En inventer un est même préférable — appeler un outil
        réel pour obtenir un jeton exécuterait une action non demandée, et rien
        ne garantit qu'elle soit sans effet de bord.
        """
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

        def _trace(step: str, response: Any) -> None:
            """Ce que l'étape a VRAIMENT envoyé et obtenu.

            Les en-têtes de la REQUÊTE en font partie, et c'est le point : les
            loggers de `httpcore` ne les montrent pas (`send_request_headers.
            started request=<Request [b'POST']>`, mesuré), donc ils sont
            invisibles sans ça. Or la seule différence possible entre une
            requête du proxy et celle d'un banc externe qui obtient, lui, un
            résultat différent, se trouve là — au premier rang, l'en-tête
            `Authorization` que le provider OAuth ajoute quand il croit détenir
            un jeton, et qui fait répondre 403 là où l'absence de jeton donne
            401.

            Les valeurs ne sont JAMAIS journalisées : seuls les noms d'en-tête,
            plus la forme du jeton (son schéma et sa longueur) quand il y en a
            un. C'est assez pour conclure, et ça ne fuite rien.
            """
            if not _auth_debug_enabled():
                return
            sent = response.request.headers
            names = ", ".join(sorted(k.lower() for k in sent.keys()))
            _log(f"  [auth] {self.name} {step} -> HTTP {response.status_code}")
            _log(f"  [auth]   URL demandée : {_redact_url(response.request.url)}")
            _log(f"  [auth]   en-têtes envoyés : {names}")
            authorization = sent.get("authorization")
            if authorization:
                scheme, _, value = authorization.partition(" ")
                _log(f"  [auth]   ATTENTION : Authorization envoyé "
                     f"({scheme}, {len(value)} caractères) — un jeton refusé "
                     f"donne 403 là où l'absence de jeton donne 401.")
            challenge = response.headers.get("www-authenticate")
            if challenge:
                _log(f"  [auth]   www-authenticate : {challenge}")
            body = (response.text or "").strip()
            if response.status_code >= 400 and body:
                _log(f"  [auth]   corps : {body[:200]}")

        response = await client.post(
            self.server_url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "miaou-proxy", "version": "1"},
                },
            },
        )
        _trace("initialize", response)

        session_id = response.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
            # Le serveur attend la notification avant de servir la suite.
            await client.post(
                self.server_url,
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
        elif _auth_debug_enabled():
            _log(f"  [auth] {self.name} initialize n'a renvoyé aucun "
                 f"Mcp-Session-Id — tools/call partira sans session.")

        # `tools/list` d'abord : il faut le NOM d'un outil réel. Une passerelle
        # qui route par ressource (WSO2, mesuré) rejette un nom inconnu avant
        # d'évaluer l'autorisation, donc un nom inventé n'obtient jamais le 401
        # qu'on cherche — il obtient un 403 de routage.
        listed = await client.post(
            self.server_url,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        _trace("tools/list", listed)

        probe_tool = _pick_probe_tool(listed.text or "") or _AUTH_PROBE_TOOL
        if _auth_debug_enabled():
            origin = ("lu dans tools/list" if probe_tool != _AUTH_PROBE_TOOL
                      else "REPLI — aucun outil de lecture trouvé")
            _log(f"  [auth]   outil de sonde : {probe_tool} ({origin})")

        response = await client.post(
            self.server_url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": probe_tool, "arguments": {}},
            },
        )
        _trace("tools/call", response)
        if _auth_debug_enabled():
            self._explain_probe_outcome(response)

    def _explain_probe_outcome(self, response: Any) -> None:
        """Conclut, plutôt que de laisser conclure.

        Ce mode s'utilise sur un poste distant, souvent sans copier-coller
        possible : un journal qu'il faut recopier à la main pour être interprété
        ailleurs n'est pas un outil de diagnostic. On nomme donc le cas observé
        ET l'action qui en découle.
        """
        status = response.status_code
        if status == 401:
            if response.headers.get("www-authenticate"):
                _log(f"  [auth] {self.name} : VERDICT — refus exploitable, le "
                     f"parcours OAuth peut s'amorcer.")
            else:
                _log(f"  [auth] {self.name} : VERDICT — 401 SANS "
                     f"www-authenticate. Le client n'a aucun serveur "
                     f"d'autorisation à découvrir ; à corriger côté serveur.")
            return

        if status == 403 and response.request.headers.get("authorization"):
            _log(f"  [auth] {self.name} : VERDICT — 403 alors qu'un jeton a été "
                 f"envoyé. Ce jeton est refusé (expiré, scopes insuffisants, "
                 f"mauvais public). Le supprimer du fichier de jetons force un "
                 f"parcours neuf ; s'il revient, le refus est côté droits.")
            return

        if status == 403:
            _log(f"  [auth] {self.name} : VERDICT — 403 sans jeton envoyé. Ce "
                 f"n'est pas une autorisation manquante au sens OAuth : une "
                 f"passerelle ou le serveur refuse la requête elle-même. "
                 f"Comparer avec `tests/live_auth_probe.py`, qui pose la même "
                 f"question depuis un autre poste.")
            return

        _log(f"  [auth] {self.name} : VERDICT — ni 401 ni 403 (HTTP {status}). "
             f"Rien à quoi accrocher un parcours OAuth par ce chemin.")

    async def authorize(self, upstream: Upstream) -> None:
        """Déroule le parcours interactif, puis (re)démarre l'upstream.

        À n'appeler qu'une fois le port ouvert : c'est la condition qui rend le
        /callback atteignable. Le drapeau `interactive` n'est levé que pour la
        durée du parcours, pour qu'un échec ultérieur au démarrage ne rouvre
        pas un parcours à l'insu de tout le monde.

        **Le parcours est PROVOQUÉ, jamais espéré comme effet de bord d'un
        `start()`.** C'est la correction du 2026-09-07 : `start()` ouvre une
        session, et sur un upstream dont `initialize` (voire `tools/list`)
        passe sans jeton, il réussit sans qu'aucune requête ne soit refusée —
        donc sans déclencher l'OAuth. `authorize()` retournait alors sans rien
        avoir fait, on annonçait « autorisation accordée », et aucun jeton
        n'était jamais écrit : sur le terrain, ni fichier de jetons ni le
        moindre appel à l'AS, pour une page qui disait le contraire.

        On émet donc une requête à nous sur l'URL de l'upstream, à travers un
        client httpx portant le provider en `auth`. Le 401 attendu fait dérouler
        au SDK son chemin NOMINAL — découverte des métadonnées, enregistrement
        si besoin, redirection, échange du code, écriture du jeton. Rien n'est
        réimplémenté ici : mener le flow à la main dupliquerait la moitié du
        SDK, et deux chemins d'autorisation finiraient par diverger.

        Si l'upstream répond sans exiger d'autorisation, il n'y avait rien à
        accorder — pas une erreur, mais pas non plus un jeton : c'est
        `has_usable_token` qui tranche ensuite, jamais le seul fait d'être
        arrivé ici sans exception.
        """
        import httpx

        self.interactive = True
        try:
            async with httpx.AsyncClient(
                auth=self.provider(), timeout=self.wait_timeout, follow_redirects=False
            ) as client:
                await self._provoke_refusal(client)
        except AuthorizationRequired:
            # Ne peut pas arriver ici (`interactive` est vrai), mais si le
            # parcours est inhibé pour une raison qu'on n'a pas prévue, ne
            # surtout pas le présenter comme un succès.
            raise
        except Exception:
            # L'autorisation reste due : un parcours qui échoue ne doit pas
            # faire croire aux surfaces que le problème est réglé.
            raise
        finally:
            self.interactive = False
            self.pending = None

        # Le témoin est le JETON, jamais l'absence d'exception. Un upstream qui
        # répond 200 sans rien exiger n'a pas « accordé » quoi que ce soit.
        if await self._storage.has_usable_token():
            self.authorization_pending = False
            # La session courante a été ouverte SANS jeton : la rouvrir est ce
            # qui la fait porter l'en-tête Authorization.
            await upstream.stop()
            await upstream.start()

    async def refresh_if_due(self) -> bool:
        """Renouvelle le jeton s'il expire bientôt. Rend True s'il l'a fait.

        POURQUOI CETTE MÉTHODE EXISTE. Le refresh du SDK est passif : il se
        déclenche dans `async_auth_flow`, donc uniquement quand une requête
        traverse le provider. Un upstream qu'on n'appelle pas pendant une
        semaine voit son access token expirer, puis son refresh token, sans
        qu'aucun des deux n'ait servi — et le prochain appel exige une
        ré-autorisation manuelle. Rafraîchir d'avance transforme l'inactivité
        en non-événement, et sous un AS à rotation (WSO2 le fait, c'est
        configurable) cela repousse indéfiniment l'échéance du refresh token.

        ÉCRITURE PAR LE PROVIDER PARTAGÉ, jamais en direct. Le renouvellement
        passe par `async_auth_flow`, sous le `anyio.Lock` d'OAuthContext, comme
        le ferait un appel d'outil : c'est ce qui garde UN SEUL écrivain sur le
        fichier de jetons. Émettre le POST nous-mêmes doublerait le chemin de
        refresh, et un AS à rotation verrait deux usages du même refresh token
        — il révoquerait alors toute la famille, ce que cette boucle est
        justement censée éviter.

        Le déclencheur est l'échéance lue en STOCKAGE, pas
        `context.is_token_valid()` : le contexte peut très bien ne rien savoir
        (provider jamais sollicité depuis le démarrage), auquel cas son verdict
        serait « valide » sur un jeton périmé.
        """
        token = await self._storage.get_tokens()
        if token is None or not token.refresh_token:
            # Rien à renouveler : soit aucun jeton, soit un jeton sans moyen de
            # l'être. Dans les deux cas c'est à l'utilisateur d'autoriser, et
            # les surfaces le disent déjà.
            return False
        if token.expires_in is None:
            # Sans échéance annoncée, rien ne permet de dire « bientôt » : on
            # laisse le refresh passif faire son travail au premier 401.
            return False
        if token.expires_in > self._refresh_margin(token):
            return False

        # L'instant d'expiration AVANT la tentative : c'est son avancement qui
        # dira si un renouvellement a réellement eu lieu (cf. plus bas).
        deadline_before = time.time() + token.expires_in

        import httpx

        # Une requête quelconque suffit : c'est son PASSAGE par le provider qui
        # déclenche le refresh, pas ce qu'elle demande. On vise donc l'upstream
        # avec la plus inoffensive qui soit — et son résultat ne nous intéresse
        # pas, seulement l'effet de bord sur le stockage.
        #
        # `interactive` reste FAUX : si le renouvellement échoue (refresh token
        # révoqué ou expiré), le SDK enchaîne sur un parcours complet, que
        # `_on_redirect` inhibe en levant AuthorizationRequired. C'est le
        # comportement voulu — une boucle de fond ne doit jamais ouvrir un
        # parcours interactif que personne n'a demandé ; elle marque
        # l'autorisation comme due et s'arrête là.
        try:
            async with httpx.AsyncClient(
                auth=self.provider(), timeout=self.wait_timeout,
                follow_redirects=False,
            ) as client:
                await client.post(
                    self.server_url,
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": _MCP_PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {"name": "miaou-proxy", "version": "1"},
                        },
                    },
                )
        except AuthorizationRequired:
            _log(f"Renouvellement du jeton de '{self.name}' impossible — "
                 f"autorisation à refaire : {authorize_path(self.name)}")
            self.authorization_pending = True
            return False
        except Exception as e:
            # Un AS injoignable n'est pas une autorisation perdue : le jeton
            # courant reste valable jusqu'à son terme, et le prochain réveil
            # réessaiera. Ne PAS marquer l'autorisation comme due ici, ce
            # serait envoyer cliquer pour une panne réseau passagère.
            _log(f"Renouvellement du jeton de '{self.name}' échoué ({e}) — "
                 f"nouvelle tentative au prochain réveil.")
            return False

        # Le témoin est le STOCKAGE, pas l'absence d'exception : la requête
        # peut très bien avoir abouti sans qu'aucun refresh n'ait eu lieu.
        #
        # Et le critère est que l'ÉCHÉANCE AIT AVANCÉ, jamais qu'elle dépasse
        # une marge. Comparer à `_REFRESH_MARGIN_S` (première version) exigeait
        # d'un jeton frais qu'il vive plus de 15 minutes : sur un AS qui en
        # émet de 5 minutes — WSO2, mesuré le 2026-09-08 — un renouvellement
        # parfaitement réussi était classé en échec. Rien n'était journalisé,
        # et la boucle recommençait à chaque réveil sur un jeton qu'elle venait
        # de renouveler, ce qui est exactement le rejeu qu'on veut éviter
        # devant un AS à rotation.
        # Et l'échéance doit avoir avancé DANS LE FUTUR. « Avoir avancé » seul
        # ne suffit pas : partant d'un jeton déjà expiré, `deadline_before` est
        # dans le passé, et un `expires_in` de 0 le dépasse — on journalisait
        # alors « renouvelé (valide 0s) » et on levait `authorization_pending`
        # sur un jeton mort. Le proxy annonçait l'upstream comme autorisé, et
        # le premier appel d'outil échouait. Mesuré le 2026-09-08.
        # LE CONTEXTE DU PROVIDER D'ABORD, et pas seulement le fichier. Quand
        # un refresh échoue, le SDK ne lève RIEN : il appelle `clear_tokens()`
        # — qui vide le contexte sans toucher au fichier —, repose
        # `_initialized` et laisse partir la requête SANS en-tête
        # `Authorization`. Sur un upstream qui accepte `initialize` sans jeton
        # (ce Jira, mesuré), la sonde répond donc 200 et l'échec est
        # totalement muet ; le fichier, lui, garde un jeton d'apparence
        # intacte. Se fier à lui faisait annoncer « renouvelé » et lever
        # `authorization_pending` alors que rien n'était autorisé — l'échec
        # ressortait au premier `tools/call`. Mesuré le 2026-09-08.
        #
        # La garde ne vaut QUE si le provider a effectivement chargé les
        # jetons (`_initialized`) : un contexte encore vierge est vide lui
        # aussi, et le confondre avec un refus ferait annoncer une
        # autorisation due à chaque premier passage.
        context = getattr(self._provider, "context", None)
        initialized = getattr(self._provider, "_initialized", False)
        if (
            initialized
            and context is not None
            and getattr(context, "current_tokens", None) is None
        ):
            _log(f"Renouvellement du jeton de '{self.name}' refusé par l'AS — "
                 f"autorisation à refaire : {authorize_path(self.name)}")
            self.authorization_pending = True
            return False

        renewed = await self._storage.get_tokens()
        if renewed is None or renewed.expires_in is None:
            return False
        if renewed.expires_in <= 0:
            # « Avoir avancé » ne suffit pas : partant d'un jeton déjà expiré,
            # `deadline_before` est dans le passé et un `expires_in` de 0 le
            # dépasse. On journalisait « renouvelé (valide 0s) » sur un jeton
            # mort, et le premier appel d'outil échouait.
            _log(f"Renouvellement du jeton de '{self.name}' sans effet — le "
                 f"jeton reste expiré. Autoriser : {authorize_path(self.name)}")
            self.authorization_pending = True
            return False
        deadline_after = time.time() + renewed.expires_in
        if deadline_after <= deadline_before:
            return False

        # CE QUI EST AFFICHÉ EST LE RESTANT, PAS LA DURÉE ÉMISE. `expires_in`
        # relu est ce qu'il reste à courir (cf. get_tokens), donc deux traces
        # successives montrent des valeurs différentes pour un AS qui émet
        # toujours la même durée — on lit « valide 30s » puis « valide 60s »
        # sans que l'AS ait rien changé. Journaliser les deux, plus le gain,
        # est ce qui permet de conclure sans deviner : un renouvellement
        # effectif se voit au GAIN, pas au restant. Mesuré le 2026-09-08, où
        # cette ambiguïté m'a fait conclure à tort que l'AS émettait des
        # jetons de 30 s.
        gained = int(deadline_after - deadline_before)
        issued = self._storage.observed_lifetime() if hasattr(
            self._storage, "observed_lifetime") else None

        # TROIS CAS, PAS DEUX. Entre « renouvelé » et « refusé » il y a
        # l'échéance qui n'a pas bougé : l'AS a répondu sans rien réémettre.
        # La version précédente l'annonçait comme un renouvellement puis se
        # contredisait par un ATTENTION — « renouvelé (+0s) » suivi de « l'AS
        # n'a pas délivré un jeton neuf ». Or rien ne va mal : le jeton court
        # toujours, et le prochain réveil réessaiera.
        if issued and gained < issued * _RENEWAL_NOOP_FRACTION:
            if not self._renewal_noop_reported:
                _log(f"Jeton de '{self.name}' non renouvelé — l'AS a rendu la "
                     f"même échéance (reste {int(renewed.expires_in)}s sur "
                     f"{int(issued)}s). Nouvelle tentative au prochain réveil.")
                self._renewal_noop_reported = True
            # L'autorisation n'est pas due pour autant : le jeton reste bon.
            self.authorization_pending = False
            return False

        detail = f"+{gained}s, reste {int(renewed.expires_in)}s"
        if issued:
            detail += f", émis pour {int(issued)}s"
        _log(f"Jeton de '{self.name}' renouvelé ({detail}).")

        # Un gain réel mais très inférieur à la durée émise n'est pas un
        # renouvellement complet : le jeton a bougé sans repartir à neuf, et la
        # boucle reviendra très vite. Le dire, plutôt que de laisser croire que
        # tout va bien jusqu'à ce que l'appel échoue. Le seuil bas est traité
        # au-dessus : ici on sait déjà que l'échéance a réellement avancé.
        if issued and gained < issued / 2:
            _log(f"  ATTENTION : gain de {gained}s pour un jeton émis pour "
                 f"{int(issued)}s — l'AS n'a pas délivré un jeton neuf. "
                 f"Vérifier la rotation des refresh tokens côté serveur.")
        # Réarmement : le prochain épisode de non-renouvellement sera dit.
        self._renewal_noop_reported = False
        self.authorization_pending = False
        return True

    def _refresh_margin(self, token: Any) -> float:
        """Combien de temps AVANT l'échéance on renouvelle.

        Plafonnée à une FRACTION de la durée de vie du jeton, sans quoi une
        marge fixe plus large que cette durée rend tout jeton « bientôt
        expiré » dès son émission : la boucle renouvellerait à chaque réveil,
        indéfiniment. Un AS d'entreprise émet couramment des jetons de 5
        minutes (WSO2, mesuré le 2026-09-08) là où la marge vaut 15 minutes.

        La durée de vie n'est pas lisible sur un jeton déjà entamé —
        `expires_in` est ce qu'il en RESTE. On retient donc la plus longue
        échéance vue pour cet upstream, mémorisée à l'écriture par le stockage,
        et on retombe sur `expires_in` tant qu'on n'a rien vu de mieux.
        """
        lifetime = getattr(self._storage, "observed_lifetime", None)
        if callable(lifetime):
            lifetime = lifetime()
        if not lifetime:
            lifetime = token.expires_in or 0
        # La moitié de la durée de vie laisse un réveil entier pour réessayer
        # après un échec, sans renouveler dès l'émission.
        return min(_REFRESH_MARGIN_S, lifetime / 2)

    def provider(self) -> Any:
        """Construit le provider AU PREMIER APPEL, puis le rend tel quel."""
        if self._provider is not None:
            return self._provider

        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata

        metadata = OAuthClientMetadata(
            client_name="miaou-proxy",
            redirect_uris=[self.callback_url],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=self.scope,
        )
        self._provider = OAuthClientProvider(
            server_url=self.server_url,
            client_metadata=metadata,
            storage=self._storage,
            redirect_handler=self._on_redirect,
            callback_handler=self._on_callback,
        )
        if self.oauth_metadata is not None:
            # Posé sur le CONTEXTE, pas passé au constructeur : le SDK n'a pas
            # de paramètre pour ça, il n'attend ces métadonnées que de sa
            # découverte. Les y installer d'avance fait exactement ce que la
            # découverte aurait fait si elle avait abouti — `_refresh_token()`
            # et `_perform_authorization()` lisent tous deux
            # `context.oauth_metadata` sans se soucier de son origine.
            #
            # Ne PAS mettre `auth_server_url` à jour au passage : il ne sert
            # qu'à construire les URLs de découverte, laquelle n'aura plus
            # lieu d'être pour les endpoints qu'on vient de fournir.
            self._provider.context.oauth_metadata = self.oauth_metadata
        return self._provider


def build_callback_url(host: str, port: int, path: str = "/callback") -> str:
    """URL de redirection annoncée à l'AS.

    Servie par le proxy en fonctionnement normal, pas par un listener éphémère :
    c'est la seule forme compatible avec un parcours déclenché depuis MIAOU.
    Port FIXE — RFC 8252 §7.3 demande à l'AS d'ignorer le port d'un redirect
    loopback, mais en pratique certains le comparent strictement.
    """
    advertised = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return f"http://{advertised}:{port}{path}"


_CALLBACK_PAGE = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 34rem; margin: 4rem auto;
        padding: 0 1rem; line-height: 1.5; }}
.ok {{ color: #16794a; }} .ko {{ color: #a4302a; }}
code {{ background: #f0f0f0; padding: .1rem .3rem; border-radius: .2rem; }}
</style></head>
<body><h1 class="{cls}">{title}</h1><p>{message}</p></body></html>"""


def render_callback_page(upstream_name: str | None, error: str | None) -> str:
    """Page rendue au navigateur au retour de l'AS.

    Rendue AUSSI en cas d'erreur : un onglet blanc après un refus laisserait
    croire à une panne du proxy, alors que le refus vient de l'utilisateur.
    """
    who = f" pour <code>{upstream_name}</code>" if upstream_name else ""
    if error:
        return _CALLBACK_PAGE.format(
            cls="ko",
            title="Autorisation refusée",
            message=f"Le serveur d'autorisation a répondu <code>{error}</code>{who}. "
            "Le proxy n'a reçu aucun jeton — tu peux fermer cet onglet et relancer.",
        )
    return _CALLBACK_PAGE.format(
        cls="ok",
        title="Autorisation reçue",
        message=f"Le proxy a reçu le code d'autorisation{who}. "
        "Tu peux fermer cet onglet.",
    )


def build_callback_route(authorizers: dict[str, UpstreamAuthorizer]) -> Any:
    """Route /callback, PUBLIQUE — et c'est voulu.

    Seul /mcp est enveloppé par RequireAuthMiddleware : le navigateur qui revient
    de l'AS tiers ne porte aucun jeton du proxy, exiger le nôtre ici fermerait la
    boucle, exactement comme l'envelopper les routes /.well-known l'aurait fait
    pour l'auth entrante (piège déjà payé en AB-1).
    """
    from starlette.responses import HTMLResponse

    async def handle_callback(request: Any) -> Any:
        params = request.query_params
        code = params.get("code")
        state = params.get("state")
        error = params.get("error")

        # Le state ne sert PAS à valider ici (le SDK le compare lui-même en
        # compare_digest) : il sert à savoir QUEL upstream attend, quand
        # plusieurs partagent la route. Comparaison sur le paramètre `state`
        # extrait de l'URL d'autorisation, jamais par sous-chaîne — un state
        # est du texte arbitraire, il peut apparaître ailleurs dans l'URL.
        waiting = [a for a in authorizers.values() if a.pending is not None]
        target = None
        if state:
            for auth in waiting:
                if auth.pending.state == state:
                    target = auth
                    break
        # Un seul upstream en attente : pas d'ambiguïté à lever, on lui remet le
        # callback même sans state (un AS qui ne le renvoie pas fera échouer la
        # comparaison du SDK, ce qui est le bon endroit pour ce refus).
        if target is None and len(waiting) == 1:
            target = waiting[0]

        if target is None:
            return HTMLResponse(
                render_callback_page(None, "aucune autorisation en attente"),
                status_code=400,
            )

        target.pending.resolve(code, state, error)
        return HTMLResponse(
            render_callback_page(target.name, error),
            status_code=400 if error else 200,
        )

    return Route("/callback", handle_callback, methods=["GET"])


def build_authorize_route(
    authorizers: dict[str, UpstreamAuthorizer],
    upstreams: dict[str, Upstream],
) -> Any:
    """Route `/authorize/{name}` — déclenche le parcours, port déjà ouvert.

    PUBLIQUE, comme /callback et pour la même raison (cf. build_callback_route).
    C'est ici, et pas dans le lifespan, que le parcours interactif peut vivre :
    le port écoute, donc la redirection vers /callback est atteignable.

    Le parcours est lancé en tâche de fond et la réponse part tout de suite : le
    navigateur ne doit pas rester suspendu pendant qu'on attend son propre
    retour sur /callback.
    """
    import anyio
    from html import escape
    from starlette.responses import HTMLResponse

    async def handle_authorize(request: Any) -> Any:
        name = request.path_params["name"]
        authorizer = authorizers.get(name)
        if authorizer is None or name not in upstreams:
            return HTMLResponse(
                _CALLBACK_PAGE.format(
                    cls="ko",
                    title="Upstream inconnu",
                    # `name` vient du chemin d'URL, donc d'un tiers : la route
                    # est PUBLIQUE. Échappé, sans exception.
                    message=f"Aucun upstream OAuth nommé <code>{escape(name)}</code>.",
                ),
                status_code=404,
            )

        if authorizer.pending is not None:
            url = authorizer.pending.authorization_url or ""
            return HTMLResponse(
                _CALLBACK_PAGE.format(
                    cls="ok",
                    title="Autorisation déjà en cours",
                    message=(
                        f'Suivre <a href="{escape(url, quote=True)}">ce lien</a> '
                        f"pour la terminer."
                    ),
                )
            )

        # Armé AVANT de lancer la tâche : le parcours peut produire son URL
        # immédiatement, et un événement créé après coup manquerait le signal.
        authorizer.redirect_ready = anyio.Event()
        authorizer.last_error = None

        async def _run() -> None:
            try:
                await authorizer.authorize(upstreams[name])
                authorizer.last_error = None
                _log(f"Upstream '{name}' autorisé.")
            except Exception as e:
                authorizer.last_error = str(e)
                _log(f"Autorisation de '{name}' échouée : {e}")
            finally:
                # Toujours réveiller la route, y compris en échec : sinon elle
                # attendrait sa borne entière pour une erreur déjà connue.
                authorizer.redirect_ready.set()

        # Tâche détachée : la réponse doit partir avant que le parcours
        # n'attende le retour du navigateur sur /callback.
        request.app.state.task_group.start_soon(_run)

        # Attente sur ÉVÉNEMENT, jamais un délai fixe. Le parcours doit d'abord
        # découvrir l'AS (`/.well-known/...`), et éventuellement enregistrer un
        # client : derrière un portail d'entreprise, c'est un aller-retour
        # réseau que 100 ms ne couvrent pas. Le chronomètre concluait alors
        # « serveur d'autorisation injoignable » alors qu'il répondait très
        # bien, simplement plus lentement que la borne — diagnostic faux, et
        # faux dans le sens qui décourage de réessayer.
        with anyio.move_on_after(_AUTHORIZE_ROUTE_WAIT_S):
            await authorizer.redirect_ready.wait()

        url = authorizer.pending.authorization_url if authorizer.pending else None
        if url:
            from starlette.responses import RedirectResponse

            return RedirectResponse(url, status_code=302)

        # TROISIÈME issue, et il faut la traiter AVANT de conclure à l'échec :
        # le parcours peut ABOUTIR SANS jamais rediriger. Un `refresh_token`
        # encore valide en stockage, ou un AS qui accorde sans interaction, et
        # le SDK obtient son jeton sans passer par `_on_redirect` — donc sans
        # `pending`. Tester la seule URL de redirection faisait alors répondre
        # « le serveur d'autorisation n'a pas répondu à temps » une seconde
        # après un « Upstream autorisé » dans le log : le contraire de ce qui
        # venait de se passer. Payé en production le 2026-09-07.
        #
        # Le témoin est l'état de l'upstream, pas le chemin qu'il a emprunté
        # pour y arriver : `authorize()` réussi pose `authorization_pending` à
        # faux. Noter que `pending` est de toute façon remis à None par le
        # `finally` d'`authorize()`, donc il ne pouvait rien dire d'un parcours
        # terminé — même abouti par redirection.
        if not authorizer.authorization_pending and authorizer.last_error is None:
            return HTMLResponse(
                _CALLBACK_PAGE.format(
                    cls="ok",
                    title="Autorisation accordée",
                    message=(
                        f"L'upstream <code>{escape(name)}</code> est autorisé. "
                        f"Retourner à MIAOU et relancer la demande."
                    ),
                )
            )

        # Ni redirection, ni jeton, ni erreur : l'upstream a répondu sans rien
        # exiger. Le dire tel quel — annoncer une autorisation accordée serait
        # le faux positif payé le 2026-09-07, où la page confirmait un succès
        # pendant qu'aucun jeton n'était écrit et qu'aucun appel ne partait
        # vers l'AS.
        if authorizer.last_error is None:
            return HTMLResponse(
                _CALLBACK_PAGE.format(
                    cls="ko",
                    title="Rien à autoriser",
                    message=(
                        f"L'upstream <code>{escape(name)}</code> a répondu sans "
                        f"demander d'autorisation, et aucun jeton n'a été "
                        f"obtenu. Si ses outils refusent malgré tout, "
                        f"l'autorisation se joue ailleurs que sur ce parcours "
                        f"— voir la sortie du proxy."
                    ),
                ),
                status_code=409,
            )

        # `last_error` dit POURQUOI quand le parcours a échoué (scope refusé,
        # enregistrement rejeté). Sans elle, une erreur de configuration se
        # présentait comme une panne réseau, et l'exploitant recliquait sur un
        # lien qui ne pouvait pas la réparer.
        detail = authorizer.last_error
        message = (
            f"Le parcours d'autorisation a échoué : <code>{escape(str(detail))}</code>"
            if detail
            else "Le serveur d'autorisation n'a pas répondu à temps. "
            "Voir la sortie du proxy."
        )
        return HTMLResponse(
            _CALLBACK_PAGE.format(
                cls="ko",
                title="Autorisation impossible",
                message=message,
            ),
            status_code=502,
        )

    return Route("/authorize/{name}", handle_authorize, methods=["GET"])


def build_upstream_authorizers(
    cfg: dict[str, Any],
    upstreams: dict[str, Upstream],
    tokens_path: str | Path,
    callback_url: str,
    open_browser: bool = False,
) -> dict[str, UpstreamAuthorizer]:
    """Un UpstreamAuthorizer par upstream http portant une clé `auth`.

    Câble aussi le provider dans l'HttpUpstream correspondant : c'est le seul
    endroit où l'auth entre dans le transport, par le paramètre httpx.Auth.
    """
    authorizers: dict[str, UpstreamAuthorizer] = {}
    for name, srv in cfg.get("mcpServers", {}).items():
        if srv.get("_disabled") or name not in upstreams:
            continue
        auth = srv.get("auth")
        if not isinstance(auth, dict) or auth.get("_disabled"):
            continue
        upstream = upstreams[name]
        if not isinstance(upstream, HttpUpstream):
            raise ValueError(
                f"Serveur '{name}' : la clé 'auth' n'a de sens que sur un "
                f"upstream de type 'http'."
            )
        storage = UpstreamTokenStorage(
            tokens_path, name, client_info_override=build_client_info_override(auth)
        )
        # `auth.redirect_uri` gouverne l'URL RÉELLEMENT annoncée à l'AS, pas
        # seulement celle déclarée au client pré-provisionné : les deux
        # devaient déjà coïncider, et rien ne l'imposait. L'URL dérivée de
        # l'adresse d'écoute (`http://127.0.0.1:port/callback`) reste le défaut,
        # mais elle est refusée par les AS d'entreprise qui n'admettent ni le
        # loopback ni le `http` en clair, ou qui exigent une URL déclarée
        # d'avance — cas rencontré en production (WSO2) le 2026-09-07. C'est
        # alors au déploiement de router cette URL vers `/callback` du proxy.
        upstream_callback = auth.get("redirect_uri") or callback_url
        oauth_metadata = build_oauth_metadata_override(auth)
        authorizer = UpstreamAuthorizer(
            name=name,
            server_url=srv["url"],
            storage=storage,
            callback_url=upstream_callback,
            scope=auth.get("scope"),
            open_browser=open_browser,
            oauth_metadata=oauth_metadata,
        )
        if oauth_metadata is not None:
            _log(f"  {name:<12} endpoints OAuth déclarés : "
                 f"token={oauth_metadata.token_endpoint}")
        if auth.get("redirect_uri"):
            _log(f"  {name:<12} redirection OAuth : {upstream_callback} "
                 f"(depuis la config)")
        authorizers[name] = authorizer
        upstream._auth = authorizer.provider()
    return authorizers
