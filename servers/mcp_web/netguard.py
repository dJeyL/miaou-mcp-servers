"""Openers de mcp_web : ce que le serveur accepte de suivre, et vers où.

Deux openers, bâtis sur la même base que `mcp_base.make_opener` (proxy-aware par
`ProxyHandler()` sans argument, donc `no_proxy` respecté) mais SANS les
gestionnaires par défaut de `build_opener` qui n'ont rien à faire ici :

  - `web_opener()` (fetch_url/fetch_resource) : http et https seulement. Le
    `build_opener` d'urllib embarque FTP, fichier et data:, et son gestionnaire
    de redirection suit `ftp://` : une page http qui redirige vers
    `ftp://hôte-interne/…` faisait ouvrir une connexion FTP depuis le serveur
    (mesuré). Ici le schéma d'une redirection est contrôlé et l'adresse
    ne l'est PAS : joindre une adresse de loopback sur ordre du modèle est un
    usage légitime d'un banc d'essai.
  - `favicon_opener()` (sonde de favicon) : même chose, plus le refus de toute
    cible dont l'hôte est une adresse IP ou `localhost` — à CHAQUE saut de
    redirection. La sonde est déclenchée par du contenu non fiable (un
    `<link rel=icon>` de la page lue), à l'insu du modèle : elle n'a aucune
    raison de toucher le réseau interne.

Le filtre d'hôte ne résout PAS les noms (pas de getaddrinfo) : en entreprise les
noms Internet ne se résolvent pas forcément, et résoudre ici ajouterait une
latence et un autre point de panne à une fonction best-effort. Trou résiduel
ACCEPTÉ : un NOM qui résout vers une adresse interne (nom d'intranet,
`*.nip.io`…) passe."""

from __future__ import annotations

import re
import socket
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

_HTTP_SCHEMES = frozenset({"http", "https"})

# Séparateurs de domaine que l'IDNA ramène à « . » (U+3002, U+FF0E, U+FF61).
_IDEOGRAPHIC_DOTS = str.maketrans({"。": ".", "．": ".", "｡": "."})

# Une étiquette numérique au sens d'inet_aton : décimal, octal (0…) ou hexa (0x…).
_NUMERIC_LABEL_RE = re.compile(r"(?:0[xX][0-9a-fA-F]*|[0-9]+)")


def _is_numeric_ipv4(host: str) -> bool:
    """Toutes les formes que la pile réseau lit comme une IPv4 : 1 à 4 étiquettes
    numériques (`127.1`, `2130706433`, `0x7f.0.0.1`, `017700000001`).

    Test volontairement plus large que la validité : une étiquette numérique ne
    peut pas être le dernier niveau d'un nom public (un TLD n'est jamais
    numérique), donc refuser `1.2.3` ou `0x` ne coûte rien, et ne pas dépendre
    de ce que la libc de la plateforme accepte garde le résultat identique sous
    macOS, Linux et Windows. `socket.inet_aton` est consulté en plus."""
    labels = host.split(".")
    if 1 <= len(labels) <= 4 and all(_NUMERIC_LABEL_RE.fullmatch(p) for p in labels):
        return True
    try:
        socket.inet_aton(host)
    except (OSError, ValueError):
        return False
    return True


def is_forbidden_probe_host(url: str) -> bool:
    """True si l'hôte de `url` est une adresse IP (toute forme, IPv6 entre
    crochets comprise), `localhost` ou `*.localhost` (RFC 6761), casse et point
    final ignorés — ou si l'URL n'a pas d'hôte lisible. Aucune résolution DNS."""
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return True
    if not host:
        return True
    # La pile réseau passe par l'IDNA : « １２７.0.0.1 » devient 127.0.0.1.
    host = unicodedata.normalize("NFKC", host).translate(_IDEOGRAPHIC_DOTS)
    host = host.lower().rstrip(".")
    if not host or ":" in host:  # IPv6 (les crochets sont déjà ôtés par hostname)
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    return _is_numeric_ipv4(host)


class SchemeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Suit une redirection vers http/https seulement (urllib suit aussi ftp)."""

    def _check(self, req, fp, code, msg, headers, newurl):
        scheme = urllib.parse.urlsplit(newurl).scheme.lower()
        if scheme not in _HTTP_SCHEMES:
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"redirection vers un schéma non autorisé ({scheme or '?'}) — http/https uniquement",
                headers, fp,
            )

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._check(req, fp, code, msg, headers, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ProbeRedirectHandler(SchemeRedirectHandler):
    """Idem, et refuse aussi une cible dont l'hôte est une IP ou localhost."""

    def _check(self, req, fp, code, msg, headers, newurl):
        super()._check(req, fp, code, msg, headers, newurl)
        if is_forbidden_probe_host(newurl):
            raise urllib.error.HTTPError(
                req.full_url, code,
                "redirection vers une adresse IP ou localhost refusée pour la sonde de favicon",
                headers, fp,
            )


def _opener(redirect_handler: urllib.request.HTTPRedirectHandler) -> urllib.request.OpenerDirector:
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        redirect_handler,
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


def web_opener() -> urllib.request.OpenerDirector:
    return _opener(SchemeRedirectHandler())


def favicon_opener() -> urllib.request.OpenerDirector:
    return _opener(ProbeRedirectHandler())
