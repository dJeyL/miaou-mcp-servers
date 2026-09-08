"""Mode `--debug-auth` : rendre le parcours OAuth sortant observable.

Module bas du sous-paquet — tout le reste en dépend, il ne dépend de rien
d'interne hormis le log. C'est ici que vit `_AUTH_DEBUG`, dont le module de
résidence fait partie du contrat (voir son docstring).
"""

from __future__ import annotations

import sys
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..logging import _log


_SENSITIVE_QUERY_KEYS = (
    "code", "access_token", "refresh_token", "id_token", "client_secret",
    "code_verifier", "assertion", "password",
)


def _redact_url(url: Any) -> str:
    """URL lisible, valeurs sensibles remplacées.

    `state` et `code_challenge` sont CONSERVÉS : ils ne donnent aucun accès et
    ce sont eux qu'on lit pour comprendre un parcours (corrélation d'un
    callback, présence de PKCE).
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parts = urlsplit(str(url))
    except Exception:  # pragma: no cover - une URL illisible se log telle quelle
        return str(url)
    if not parts.query:
        return str(url)
    pairs = [
        (k, "***" if k.lower() in _SENSITIVE_QUERY_KEYS else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    # `safe="*"` : sans lui le masque ressort en %2A%2A%2A, illisible pour qui
    # lit un log en diagonale — et un log qu'on ne lit pas ne sert à rien.
    return urlunsplit(parts._replace(query=urlencode(pairs, safe="*")))


_AUTH_DEBUG = False
"""Drapeau du mode debug de l'auth sortante.

**Son module de résidence fait partie de son contrat.** `_auth_debug_enabled()`
lit le global de CE module ; un `monkeypatch.setattr` visant une réexportation
(`mcp_proxy._AUTH_DEBUG`) rebinderait le nom du paquet sans rien changer à
ce que lisent les fonctions d'ici. Un test écrit ainsi ne verrait pas le mode
debug s'activer — et comme le drapeau à `False` ne lève rien, il passerait en
vérifiant l'absence de trace qu'il croit avoir demandée.

Pour cette raison, ce nom n'est PAS réexporté par `__init__.py` : mieux vaut un
`AttributeError` franc au patch qu'un test vert qui ne teste rien. Qui patche
vise `mcp_proxy.auth_out`.
"""


def _auth_debug_enabled() -> bool:
    return _AUTH_DEBUG


def enable_auth_debug() -> None:
    """Rend le parcours OAuth sortant observable (option `--debug-auth`).

    Un parcours qui n'aboutit pas est SILENCIEUX par construction : le SDK
    avale ses propres erreurs de découverte, et le proxy ne voit qu'une absence
    de jeton. Impossible alors de distinguer « l'upstream n'a rien demandé » de
    « l'AS a refusé l'enregistrement » ou de « la découverte a échoué » — trois
    causes, un seul symptôme, et le diagnostic se fait au jugé. Ce mode expose
    les requêtes réellement émises, seule information qui les sépare.

    Branché sur les loggers du SDK MCP et de httpx plutôt que sur un traçage
    maison : ce sont eux qui voient les requêtes, y compris celles que le proxy
    n'émet pas lui-même (découverte, enregistrement, échange de jeton).
    """
    import logging

    class _RedactingFilter(logging.Filter):
        """Masque les valeurs sensibles des URL présentes dans un message."""

        def filter(self, record: logging.LogRecord) -> bool:
            try:
                message = record.getMessage()
            except Exception:  # pragma: no cover
                return True
            if "?" in message and any(
                k in message for k in _SENSITIVE_QUERY_KEYS
            ):
                import re

                record.msg = re.sub(
                    r"(https?://\S+)",
                    lambda m: _redact_url(m.group(1)),
                    message,
                )
                record.args = ()
            return True

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("DEBUG:    %(name)s %(message)s"))
    handler.addFilter(_RedactingFilter())
    handler.setLevel(logging.DEBUG)

    # `httpcore` est volontairement ABSENT : ses lignes ne portent ni URL ni
    # en-tête (`send_request_headers.started request=<Request [b'POST']>`), donc
    # elles noient le journal sans rien apprendre — mesuré, pas supposé.
    #
    # LA LISTE COUVRE TOUT LE CYCLE DE VIE, PAS SEULEMENT LE BOOT. Première
    # version : `("mcp.client.auth", "httpx")`. On voyait alors les URL d'AS
    # essayées au démarrage puis plus rien, alors que le mode est censé rester
    # actif — incohérence relevée le 2026-09-08. Deux causes, mesurées :
    #
    # - `mcp.client.auth` N'ÉMET RIEN dans le SDK installé (aucun `getLogger`,
    #   aucun appel `logger.*` dans le module). Le nommer ne coûte rien mais
    #   n'apportait rien non plus : tout ce qu'on voyait venait de `httpx`.
    # - Le trafic d'APRÈS le boot passe par `mcp.client.streamable_http` (le
    #   transport des upstreams HTTP : connexion, session, envoi de messages,
    #   reconnexions SSE), qui journalise sous SON nom et n'était pas couvert.
    #
    # `httpx` journalise ses requêtes en INFO, pas en DEBUG — d'où un niveau
    # posé sur chaque logger plutôt qu'un filtrage par sévérité : on veut la
    # ligne `HTTP Request: POST … "200 OK"` de chaque requête, quel que soit
    # son niveau d'origine.
    #
    # Les NOMS sont ceux que les modules posent réellement, vérifiés à la
    # source : `mcp.client.session` journalise sous `"client"` (et non sous son
    # nom de module), et `mcp.shared.session` appelle `logging.*` au niveau
    # module — donc le root logger, hors de portée d'une liste nommée. Nommer
    # un logger qui n'existe pas est silencieux : c'est ainsi que la première
    # version a pu paraître correcte.
    for name in (
        "mcp.client.auth",
        "mcp.client.streamable_http",
        "client",
        "httpx",
    ):
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False

    global _AUTH_DEBUG
    _AUTH_DEBUG = True

    _log("Mode debug du parcours OAuth actif (--debug-auth).")
    _log("  Les jetons et codes d'autorisation sont masqués dans ces traces.")
