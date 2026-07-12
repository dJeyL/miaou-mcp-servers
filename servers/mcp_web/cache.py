"""Cache disque par checksum d'URL pour mcp_web.

Contrairement à mcp_docs (session_id = conversation MIAOU, contrat REF_UNKNOWN),
ce cache n'est pas rattaché à une session : la clé est le SHA256 de l'URL
demandée à `fetch_url`. Une page déjà téléchargée reste paginable via
`fetch_read` sans retélécharger, jusqu'à expiration du TTL (sweep opportuniste,
même pattern que mcp_docs.session).

Trois fichiers possibles par URL, mêmes clé et TTL :
  - <hash>.txt  : texte rendu (html2text ou text/*), relu par fetch_read
  - <hash>.html : HTML brut, source pour fetch_list (structure headings/liens)
  - <hash>.json : structure déjà extraite par fetch_list, relue par fetch_list
    lui-même pour paginer sans reparser"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


class CacheMiss(Exception):
    """Levée quand l'URL demandée n'a pas (ou plus) d'entrée en cache."""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Variable d'environnement {name} invalide : {raw!r} (entier attendu)")


WORKDIR = Path(os.environ.get("MIAOU_WEB_WORKDIR") or "./miaou-web")
TTL_HOURS = _env_int("MIAOU_WEB_CACHE_TTL_H", 24)
READ_CAP = _env_int("MIAOU_WEB_READ_CAP", 20000)
LIST_CAP = _env_int("MIAOU_WEB_LIST_CAP", 100)
RESOURCE_MAX_BYTES = _env_int("MIAOU_WEB_RESOURCE_MAX_BYTES", 5 * 1024 * 1024)


def url_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def entry_path(url: str) -> Path:
    return WORKDIR / f"{url_key(url)}.txt"


def html_path(url: str) -> Path:
    return WORKDIR / f"{url_key(url)}.html"


def structure_path(url: str) -> Path:
    return WORKDIR / f"{url_key(url)}.json"


def _touch_all(url: str) -> None:
    """Rafraîchit le mtime des trois fichiers existants de la clé (W6) : un
    fetch_read répété touchait seulement .txt, laissant .html/.json expirer
    pendant que le texte restait vivant — fetch_list échouait alors avec un
    message trompeur ("appeler fetch_url d'abord") sur une URL bien connue."""
    now = time.time()
    for path in (entry_path(url), html_path(url), structure_path(url)):
        if path.exists():
            os.utime(path, (now, now))


def sweep_expired() -> None:
    """Sweep TTL opportuniste : supprime les entrées non touchées depuis TTL_HOURS.

    Appelé en tête de chaque outil (pas de tâche périodique — cf. mcp_docs.session)."""
    if not WORKDIR.exists():
        return
    cutoff = time.time() - TTL_HOURS * 3600
    for entry in WORKDIR.iterdir():
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


def store(url: str, text: str) -> Path:
    sweep_expired()
    WORKDIR.mkdir(parents=True, exist_ok=True)
    path = entry_path(url)
    path.write_text(text, encoding="utf-8")
    return path


def load(url: str) -> str:
    sweep_expired()
    path = entry_path(url)
    if not path.exists():
        raise CacheMiss(
            f"Aucun contenu en cache pour cette URL — appeler fetch_url d'abord : {url}"
        )
    _touch_all(url)
    return path.read_text(encoding="utf-8")


def store_html(url: str, html_text: str) -> Path:
    sweep_expired()
    WORKDIR.mkdir(parents=True, exist_ok=True)
    path = html_path(url)
    path.write_text(html_text, encoding="utf-8")
    # Invalide la structure déjà extraite (fetch_list) : un re-fetch peut avoir
    # changé le HTML, la resservir serait silencieusement périmée.
    structure_path(url).unlink(missing_ok=True)
    return path


def load_html(url: str) -> str:
    sweep_expired()
    path = html_path(url)
    if not path.exists():
        raise CacheMiss(
            f"Aucun HTML en cache pour cette URL — appeler fetch_url d'abord : {url}"
        )
    _touch_all(url)
    return path.read_text(encoding="utf-8")


def store_structure(url: str, entries: list[dict]) -> Path:
    path = structure_path(url)
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def load_structure(url: str) -> list[dict] | None:
    sweep_expired()
    path = structure_path(url)
    if not path.exists():
        return None
    _touch_all(url)
    return json.loads(path.read_text(encoding="utf-8"))
