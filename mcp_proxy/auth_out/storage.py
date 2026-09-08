"""Persistance des jetons OAuth obtenus auprès des upstreams.

`UpstreamTokenStorage` implémente l'interface de stockage attendue par le SDK
MCP, avec les gardes d'écriture qui empêchent d'écraser un jeton valide par un
jeton vide (cf. docs/auth.md).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ..logging import _log
from .debug import _auth_debug_enabled


_TOKENS_FILE_MODE = 0o600

def _default_tokens_path(config_path: str | Path) -> Path:
    """À côté de config.json, suffixé — pas dedans.

    config.json est ouvert et édité à la main ; un refresh token n'a rien à y
    faire. Fichier distinct, donc, et à ajouter au .gitignore.
    """
    cfg = Path(config_path)
    return cfg.with_name(f"{cfg.stem}-tokens.json")


def _write_secret_file(path: Path, payload: dict[str, Any]) -> None:
    """Écriture atomique, permissions restreintes posées À LA CRÉATION.

    Deux gardes, chacune pour une raison distincte :

    - `os.open(..., mode=0o600)` plutôt qu'un `chmod` après coup : entre le
      write et le chmod il existe une fenêtre pendant laquelle le refresh token
      est lisible par tout le monde. La fenêtre est courte, le fichier est
      durable.
    - Fichier temporaire dans le MÊME répertoire puis `os.replace` (atomique sur
      POSIX) : une écriture interrompue au milieu laisserait sinon un fichier de
      jetons tronqué, ce qui coûterait une ré-autorisation manuelle de tous les
      upstreams. Le même répertoire est nécessaire — `os.replace` n'est atomique
      qu'à l'intérieur d'un système de fichiers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _TOKENS_FILE_MODE)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class UpstreamTokenStorage:
    """Implémente le protocol mcp.client.auth.TokenStorage pour UN upstream.

    Un seul fichier pour tous les upstreams, une entrée par nom : le fichier est
    relu à chaque écriture pour ne pas écraser l'entrée d'un voisin.

    `client_info_override` porte les credentials pré-provisionnés de la config.
    Les rendre depuis get_client_info() suffit à court-circuiter la DCR côté SDK
    (`if not self.context.client_info:`), sans branche à ajouter nulle part :
    c'est le chemin prévu pour un AS qui n'enregistre pas dynamiquement.
    """

    def __init__(
        self,
        path: str | Path,
        upstream_name: str,
        client_info_override: Any = None,
    ) -> None:
        self._path = Path(path)
        self._name = upstream_name
        self._client_info_override = client_info_override

    # -- fichier ------------------------------------------------------------

    def _read_all(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            # Un fichier de jetons illisible ne doit pas empêcher le proxy de
            # démarrer : on repart d'une ardoise vide, ce qui coûte une
            # ré-autorisation — pas un crash au boot.
            _log(f"Fichier de jetons illisible ({e}) — ignoré.")
            return {}
        return data if isinstance(data, dict) else {}

    def _read_entry(self) -> dict[str, Any]:
        entry = self._read_all().get(self._name)
        return entry if isinstance(entry, dict) else {}

    def _update_entry(self, **fields: Any) -> None:
        data = self._read_all()
        entry = data.get(self._name)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry.update(fields)
        data[self._name] = entry
        _write_secret_file(self._path, data)

    # -- protocol TokenStorage ----------------------------------------------

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken

        entry = self._read_entry()
        raw = entry.get("tokens")
        if not raw:
            return None
        try:
            token = OAuthToken.model_validate(raw)
        except Exception as e:
            _log(f"Jetons stockés pour '{self._name}' illisibles ({e}) — ignorés.")
            return None

        # `expires_in` est une DURÉE, relative à l'instant d'émission : la
        # relire telle quelle après un redémarrage la ferait courir à nouveau
        # depuis maintenant. On persiste donc l'instant absolu d'expiration et
        # on recalcule la durée restante au chargement.
        #
        # Ce recalcul n'est pas un raffinement : le SDK charge les jetons dans
        # _initialize() SANS repasser par update_token_expiry(), donc
        # token_expiry_time reste None et is_token_valid() rend True pour un
        # jeton expiré depuis des heures. Le proxy l'enverrait, prendrait un
        # 401, et repartirait dans un parcours interactif au lieu de rafraîchir.
        expires_at = entry.get("expires_at")
        if expires_at is not None:
            remaining = int(expires_at - time.time())
            token.expires_in = max(remaining, 0)
        return token

    async def has_usable_token(self) -> bool:
        """« Un appel partirait-il authentifié, là, maintenant ? »

        PUREMENT LOCAL : lit le fichier de jetons, n'émet aucune requête. C'est
        ce qui permet de savoir qu'une autorisation manque AVANT le premier
        appel d'outil, sans sonder l'upstream ni retarder le démarrage.

        Un jeton expiré mais porteur d'un `refresh_token` compte comme
        utilisable : le SDK le rafraîchira tout seul au premier appel, sans
        parcours interactif. Le dire « à autoriser » enverrait l'utilisateur
        cliquer pour un problème qui se règle sans lui.
        """
        token = await self.get_tokens()
        if token is None or not token.access_token:
            return False
        if token.expires_in is not None and token.expires_in <= 0:
            return bool(token.refresh_token)
        return True

    def observed_lifetime(self) -> float | None:
        """Durée de vie à l'émission du dernier jeton écrit, ou None.

        Sert à calibrer la marge de renouvellement sur ce que l'AS émet
        réellement, plutôt que sur une constante qui suppose des jetons d'une
        heure. Lecture de fichier, aucun réseau.
        """
        value = self._read_entry().get("lifetime")
        return float(value) if isinstance(value, (int, float)) else None

    async def set_tokens(self, tokens: Any) -> None:
        payload = tokens.model_dump(exclude_none=True, mode="json")
        fields: dict[str, Any] = {"tokens": payload}
        if tokens.expires_in is not None:
            fields["expires_at"] = time.time() + tokens.expires_in
            # DURÉE DE VIE À L'ÉMISSION, mémorisée parce que c'est le seul
            # endroit où on la voit : relu plus tard, `expires_in` est ce qu'il
            # RESTE. C'est elle qui calibre la marge de renouvellement, laquelle
            # ne peut pas être une constante — un AS qui émet des jetons de 5
            # minutes rendrait « bientôt expiré » tout jeton dès son émission.
            #
            # MAIS ce qui arrive ici n'est pas toujours un jeton frais. Le SDK
            # garde en mémoire l'objet rendu par `get_tokens()`, dont on a
            # justement écrasé `expires_in` par le RESTANT, et il lui arrive de
            # le réécrire tel quel. Prendre cette valeur pour une durée de vie
            # donnait `lifetime: 0` sur un jeton relu périmé, donc un
            # `expires_at` dans le passé : le jeton se retrouvait marqué expiré
            # à l'instant même où il venait d'être renouvelé, et la trace
            # annonçait « valide 0s ». Mesuré le 2026-09-08.
            #
            # Une durée de vie ne RÉTRÉCIT jamais : on ne retient donc que la
            # plus longue vue, ce qui ignore les réécritures dégradées sans
            # avoir à deviner d'où vient l'objet.
            known = self.observed_lifetime() or 0
            fields["lifetime"] = max(known, tokens.expires_in)

            # Même cause, autre dégât : réécrit depuis un objet relu, cet
            # `expires_at` RECULE l'échéance au lieu de la porter. On ne la
            # laisse jamais reculer pour un access token inchangé — le seul
            # cas où une échéance doit se rapprocher est une révocation, qui
            # ne passe pas par ici. Un jeton VRAIMENT différent repart, lui,
            # de l'échéance qu'il annonce.
            previous = self._read_entry()
            same_token = (
                (previous.get("tokens") or {}).get("access_token")
                == tokens.access_token
            )
            if same_token and previous.get("expires_at") is not None:
                fields["expires_at"] = max(
                    float(previous["expires_at"]), fields["expires_at"]
                )
        else:
            fields["expires_at"] = None
        self._update_entry(**fields)

        # PASSAGE OBLIGÉ de tout jeton obtenu — échange initial comme
        # rafraîchissement. Le tracer ici, et pas dans le parcours, est ce qui
        # rend le cycle de vie observable sans rejouer une autorisation.
        #
        # L'absence de `refresh_token` est dite à voix haute : sans lui, le
        # jeton expirera sans que rien ne puisse le renouveler, et l'utilisateur
        # se reverra proposer « Autoriser » sans explication. Le savoir à
        # l'obtention plutôt qu'à l'expiration, c'est la différence entre un
        # réglage à corriger côté AS (grant `refresh_token`, scope
        # `offline_access`) et une panne subie des heures plus tard.
        if not _auth_debug_enabled():
            return
        if tokens.refresh_token:
            horizon = (f"expire dans {tokens.expires_in}s"
                       if tokens.expires_in is not None
                       else "sans expiration annoncée")
            _log(f"  [auth] {self._name} jeton enregistré ({horizon}), "
                 f"refresh_token présent — renouvellement automatique possible.")
        else:
            _log(f"  [auth] {self._name} jeton enregistré SANS refresh_token : "
                 f"il ne pourra pas être renouvelé, et l'autorisation devra "
                 f"être refaite à la main à son expiration. Vérifier le grant "
                 f"'refresh_token' du client et le scope 'offline_access'.")

    async def get_client_info(self) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull

        if self._client_info_override is not None:
            return self._client_info_override
        raw = self._read_entry().get("client_info")
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception as e:
            _log(f"Client info stockée pour '{self._name}' illisible ({e}) — ignorée.")
            return None

    async def set_client_info(self, client_info: Any) -> None:
        # Enregistrement dynamique mémorisé même quand la config fournit des
        # credentials : si l'override disparaît de la config, on retombe sur
        # l'enregistrement plutôt que d'en refaire un.
        self._update_entry(
            client_info=client_info.model_dump(exclude_none=True, mode="json")
        )
