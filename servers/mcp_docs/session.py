"""Sessions, sanitization, matérialisation et contrat REF_UNKNOWN pour mcp_docs.

Session (docs server) : répertoire de travail côté serveur, keyé par
`session_id` = id de conversation MIAOU. C'est un **cache** — la source de
vérité reste toujours le navigateur (IndexedDB). Un `ref` (`att-N`, pièce
jointe de message ; ou `file-<id>`, fichier de bibliothèque d'espace, cf.
MIAOU lot Cbis) est matérialisé au premier appel portant `content_b64`, puis
réutilisable par `ref` seul tant que la session n'a pas expiré (TTL, sweep
opportuniste). Les deux familles de ref ne collisionnent jamais sur le disque
(préfixes distincts dans le nom de fichier matérialisé, cf. `ref_path`).
"""

from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Contrat REF_UNKNOWN partagé (proxy + tests)
# ---------------------------------------------------------------------------

# Marqueur stable en tête du message d'erreur. Le proxy (mcp_proxy.py) détecte
# ce préfixe dans le résultat isError du SDK MCP (qui avale toute exception
# levée par l'outil) et le convertit en erreur JSON-RPC data.code=REF_UNKNOWN.
REF_UNKNOWN_SENTINEL = "REF_UNKNOWN"

# Hors de la plage réservée JSON-RPC (-32768..-32000 inclus). Le client MIAOU
# ne dépend que de err.data.code == "REF_UNKNOWN" (string), pas de cet entier.
REF_UNKNOWN_ERROR_CODE = -31999

# att-N : pièce jointe de message (conversation-scopée, cf. MIAOU allocateAttId).
# file-<id> : fichier de bibliothèque d'espace (Space-scopé, cf. MIAOU lot Cbis,
# libraryRefFromId — id en base36 minuscules/chiffres).
_REF_RE = re.compile(r"^(att-\d+|file-[a-z0-9]+)$")
_SESSION_ID_FORBIDDEN = re.compile(r"[\\/]|\.\.")


class ToolError(Exception):
    """Erreur applicative renvoyée en isError par FastMCP (str(e) devient le texte)."""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


WORKDIR = Path(os.environ.get("MIAOU_DOCS_WORKDIR") or "./miaou-docs")
TTL_HOURS = _env_int("MIAOU_DOCS_TTL_H", 24)
MAX_FILE_MB = _env_int("MIAOU_DOCS_MAX_FILE_MB", 20)
MAX_SESSION_MB = _env_int("MIAOU_DOCS_MAX_SESSION_MB", 200)
MAX_UNZIP_MB = _env_int("MIAOU_DOCS_MAX_UNZIP_MB", 100)
READ_CAP = _env_int("MIAOU_DOCS_READ_CAP", 20000)
SEARCH_CAP = _env_int("MIAOU_DOCS_SEARCH_CAP", 50)


def validate_session_id(session_id: str) -> str:
    """Rejette séparateurs de chemin, '..' et chaîne vide avant tout usage filesystem."""
    if not session_id or _SESSION_ID_FORBIDDEN.search(session_id):
        raise ToolError(f"session_id invalide : {session_id!r}")
    return session_id


def validate_ref(ref: str) -> str:
    if not _REF_RE.match(ref):
        raise ToolError(f"ref invalide : {ref!r} (attendu att-<N> ou file-<id>)")
    return ref


def session_dir(session_id: str) -> Path:
    return WORKDIR / validate_session_id(session_id)


def ref_path(session_id: str, ref: str) -> Path:
    """Chemin du fichier matérialisé pour ce ref (nom stable, extension inconnue à ce stade)."""
    return session_dir(session_id) / f"{validate_ref(ref)}.bin"


def touch_session(session_id: str) -> None:
    d = session_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    os.utime(d, (now, now))


def sweep_expired_sessions() -> None:
    """Sweep TTL opportuniste : supprime les sessions non touchées depuis TTL_HOURS.

    Appelé en tête de chaque outil (pas de tâche périodique — le proxy et le
    mode standalone n'ont aucune machinerie de fond, cf. audit §1)."""
    if not WORKDIR.exists():
        return
    cutoff = time.time() - TTL_HOURS * 3600
    for entry in WORKDIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                import shutil

                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def session_disk_usage_mb(session_id: str) -> float:
    d = session_dir(session_id)
    if not d.exists():
        return 0.0
    total = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def materialize(session_id: str, ref: str, content_b64: str) -> Path:
    """Écrit le fichier pour (session_id, ref) si absent. Idempotent : un ref déjà
    matérialisé n'est pas ré-écrit (un rechargement de page MIAOU repousse
    content_b64 sans que ce soit une erreur)."""
    validate_session_id(session_id)
    validate_ref(ref)

    # Garde de taille AVANT décodage : longueur base64 → taille décodée ≈ ×3/4.
    approx_decoded_mb = (len(content_b64) * 3 / 4) / (1024 * 1024)
    if approx_decoded_mb > MAX_FILE_MB:
        raise ToolError(
            f"Fichier trop volumineux (~{approx_decoded_mb:.1f} Mo, max {MAX_FILE_MB} Mo)"
        )

    path = ref_path(session_id, ref)
    touch_session(session_id)
    if path.exists():
        return path

    data = base64.b64decode(content_b64)
    if len(data) / (1024 * 1024) > MAX_FILE_MB:
        raise ToolError(
            f"Fichier trop volumineux ({len(data) / (1024 * 1024):.1f} Mo, max {MAX_FILE_MB} Mo)"
        )

    usage = session_disk_usage_mb(session_id)
    if usage + len(data) / (1024 * 1024) > MAX_SESSION_MB:
        raise ToolError(f"Quota disque session dépassé (max {MAX_SESSION_MB} Mo)")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def resolve_ref(session_id: str, ref: str, content_b64: str | None) -> Path:
    """Résout un ref vers son fichier matérialisé, ou lève REF_UNKNOWN si absent
    et qu'aucun content_b64 n'a été fourni pour le matérialiser."""
    sweep_expired_sessions()
    validate_session_id(session_id)
    path = ref_path(session_id, ref)

    if content_b64 is not None:
        return materialize(session_id, ref, content_b64)

    if not path.exists():
        raise ToolError(f"{REF_UNKNOWN_SENTINEL}: ref '{ref}' inconnu pour la session '{session_id}'")

    touch_session(session_id)
    return path
