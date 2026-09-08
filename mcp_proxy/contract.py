"""Constantes et exceptions de contrat, partagées bas dans le paquet.

Ce que le serveur et l'auth sortante ont tous deux besoin de nommer. Posé ici
plutôt que dans l'un des deux pour que leur dépendance reste à sens unique :
`server` et `auth_out` importent d'ici, jamais l'un de l'autre.
"""

from __future__ import annotations

from .logging import _log


AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
"""Code applicatif du refus d'un outil dont l'upstream n'est pas autorisé.

Contrat avec le client (MIAOU, lot AB-3), sur le motif déjà éprouvé de
REF_UNKNOWN : le code voyage dans `error.data.code` d'une vraie erreur JSON-RPC
— `code` au niveau de l'erreur reste l'entier protocolaire, `data` est le slot
applicatif — et le client le teste par ÉGALITÉ de cette constante, jamais par
sous-chaîne dans le message. Un message est de la prose : il se traduit, se
reformule, et un test par sous-chaîne casse sans prévenir.
"""


def authorize_path(upstream_name: str) -> str:
    """Chemin à ouvrir pour (ré)autoriser un upstream. Source UNIQUE.

    Ses trois consommateurs — le contrat d'erreur, le rapport `status` et le
    `_meta` de `tools/list` — passent tous par ici : recomposer la chaîne
    ailleurs laisserait deux natures de lien pour une même action.

    **Relatif, jamais absolu.** Le proxy ne connaît que son adresse d'écoute
    (`build_callback_url` replie même `0.0.0.0` sur `127.0.0.1`) : un proxy
    atteint derrière un reverse proxy donnerait une URL injoignable. C'est au
    client de composer l'origine depuis l'URL qu'il a lui-même configurée — la
    seule valeur qui décrive comment il joint réellement le proxy.

    À ne pas confondre avec `UpstreamAuthorizer.last_authorization_url`, qui
    porte l'URL de l'AS pour une transaction ABANDONNÉE (le `state` et le PKCE
    challenge d'un parcours non interactif interrompu au démarrage) : bonne à
    afficher en diagnostic, mauvaise à suivre — la suivre mène à `/callback`
    sans `pending`, donc à une erreur. Le chemin rendu ici, lui, lance un
    parcours frais.
    """
    return f"/authorize/{upstream_name}"


class AuthorizationRequired(Exception):
    """Un upstream a besoin d'une autorisation qu'on ne peut pas demander ici.

    Levée quand le parcours interactif est INHIBÉ — au démarrage, notamment.
    Distincte d'une panne : un module introuvable ou un subprocess mort ne se
    répare pas, celui-ci se répare par un clic.
    """

    def __init__(self, upstream_name: str) -> None:
        super().__init__(
            f"L'upstream '{upstream_name}' exige une autorisation OAuth."
        )
        self.upstream_name = upstream_name


def is_disabled(block: dict, where: str) -> bool:
    """Neutralisation d'un bloc de config : `disabled`, ou l'ancien `_disabled`.

    La clé canonique est `disabled`. `_disabled` reste lu pour les configs
    existantes, mais son préfixe est trompeur : dans ce fichier, `_comment`
    et `_comment_auth` désignent des clés IGNORÉES, et le souligné laissait
    croire que celle-ci l'était aussi — alors qu'elle est le seul interrupteur
    du bloc. `disabled` l'emporte quand les deux sont présentes ; c'est la
    forme qu'on écrit, l'autre n'est qu'un vestige toléré.

    `where` nomme le bloc dans l'avertissement de migration (`auth`,
    `mcpServers.brave`…), qui est en ANGLAIS comme le reste de ce qui
    s'affiche au démarrage (listing des upstreams, états d'autorisation) —
    le français des traces d'auth est une exception à ne pas propager ici.
    Le message sort une fois par bloc au démarrage, et seulement pour une
    config qui porte encore l'ancienne clé : une config déjà migrée reste
    silencieuse. Sans lui, la tolérance serait invisible —
    ce qui marche sans rien dire ne se migre jamais, et l'orthographe morte
    survivrait dans les configs jusqu'au jour où on la retirerait pour de bon.
    """
    legacy = "_disabled" in block
    if "disabled" in block:
        if legacy:
            _log(
                f"config: {where} sets both 'disabled' and '_disabled' — "
                f"'disabled' wins, '_disabled' is ignored. Drop '_disabled'."
            )
        return bool(block["disabled"])
    if legacy:
        _log(
            f"config: {where} uses '_disabled', the former spelling of "
            f"'disabled'. Still honoured — rename it."
        )
        return bool(block["_disabled"])
    return False
