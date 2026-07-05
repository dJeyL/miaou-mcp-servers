#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2", "uvicorn", "starlette", "pymupdf", "python-docx", "openpyxl", "python-pptx"]
# ///
"""
Serveur MCP d'extraction de documents pour MIAOU (PDF, Office, Zip).

Transport streamable-http (single endpoint POST, réponses en SSE). CORS ouvert
pour permettre au navigateur de l'atteindre directement depuis dist/miaou.html.

Extraction uniquement — aucune génération ni modification de document (non-goal
explicite). Lecture paginée : `docs__list` renvoie la structure sans contenu,
`docs__read` renvoie un extrait borné avec troncature signalée.

Cycle de vie des fichiers : chaque conversation MIAOU a une session
(`session_id` = id de conversation), qui est un cache sur disque
(`<workdir>/<session_id>/`). Les octets voyagent en base64 dans l'appel MCP
normal (`content_b64`), injectés par le dispatcher MIAOU seulement au premier
appel pour un `ref` donné (matérialisation idempotente) ; les appels suivants
ne portent que `ref`. Un `ref` inconnu sans `content_b64` déclenche l'erreur
machine REF_UNKNOWN (voir `REF_UNKNOWN_SENTINEL` ci-dessous), convertie par le
proxy (mcp_proxy.py) en erreur JSON-RPC ; le client MIAOU rejoue alors une fois
avec le contenu inliné. Ce rejeu ne fonctionne que derrière le proxy — en
standalone (FastMCP pur) l'appel échoue simplement en isError.

Variables d'environnement (toutes optionnelles, défauts constants) :
    MIAOU_DOCS_WORKDIR         (défaut : "./miaou-docs", relatif au répertoire de travail)
    MIAOU_DOCS_TTL_H           (défaut : 24)
    MIAOU_DOCS_MAX_FILE_MB     (défaut : 20)
    MIAOU_DOCS_MAX_SESSION_MB  (défaut : 200)
    MIAOU_DOCS_MAX_UNZIP_MB    (défaut : 100)
    MIAOU_DOCS_READ_CAP        (défaut : 20000, en caractères)

Module éclaté en package (servers/mcp_docs/) : session.py (sessions, sanitization,
matérialisation, contrat REF_UNKNOWN), formats.py (détection de type + parsers
PDF/docx/xlsx/pptx/zip). Ce fichier ne porte que le serveur FastMCP et ses outils.

Lancement (package, pas un script plat — `uv run servers/mcp_docs.py` ne s'applique
pas ici, cd dans servers/ ou utiliser --directory) :
    uv run --directory servers python -m mcp_docs                    # HTTP 127.0.0.1:8771
    uv run --directory servers python -m mcp_docs --transport stdio  # stdin/stdout
    uv run --directory servers python -m mcp_docs --host 0.0.0.0     # toutes interfaces

Dans MIAOU → Paramètres → Serveurs MCP → Ajouter :
    Nom       : docs
    URL       : http://127.0.0.1:8771/mcp
    Transport : streamable-http   (deviné depuis /mcp)
    Activé    : oui
"""

from __future__ import annotations

from mcp_base import MiaouMCPBase

from .formats import detect_kind, list_document, read_document, zip_read_member
from .session import (
    REF_UNKNOWN_ERROR_CODE,
    REF_UNKNOWN_SENTINEL,
    ToolError,
    resolve_ref,
    session_dir,
    validate_session_id,
)

__all__ = [
    "REF_UNKNOWN_ERROR_CODE",
    "REF_UNKNOWN_SENTINEL",
    "ToolError",
    "mcp",
    "server",
]


class DocsServer(MiaouMCPBase):
    def __init__(self) -> None:
        super().__init__("miaou-docs", default_port=8771)

        @self.mcp.tool()
        async def drop_session(session_id: str) -> str:
            """Supprime le répertoire de cache d'une session (nettoyage à la suppression
            d'une conversation MIAOU). Idempotent : silencieux si la session n'existe pas."""
            validate_session_id(session_id)
            d = session_dir(session_id)
            if d.exists():
                import shutil

                shutil.rmtree(d, ignore_errors=True)
            return f"Session '{session_id}' supprimée."

        @self.mcp.tool(name="list")
        async def list_(
            ref: str,
            session_id: str | None = None,
            content_b64: str | None = None,
            filename: str | None = None,
        ) -> str:
            """Liste la structure d'un document (pages/feuilles/slides/entrées zip)
            sans en renvoyer le contenu. `ref` identifie l'attachement (att-N) ;
            `content_b64` matérialise le fichier au premier appel ; `filename`
            (optionnel) aide à la détection de type par extension, sinon magic
            bytes. Un membre d'archive listé ici est lisible via `read(path=...)`
            (extraction en tant que document imbriqué : D3)."""
            if session_id is None:
                raise ToolError("session_id requis (appel hors MIAOU ?)")
            doc_path = resolve_ref(session_id, ref, content_b64)
            kind = detect_kind(doc_path, filename)
            return list_document(kind, doc_path)

        @self.mcp.tool(name="read")
        async def read(
            ref: str,
            session_id: str | None = None,
            content_b64: str | None = None,
            path: str | None = None,
            selector: str | None = None,
            filename: str | None = None,
        ) -> str:
            """Lit un extrait borné d'un document (plage de pages/lignes/slides/
            paragraphes selon le format). Réponse plafonnée à MIAOU_DOCS_READ_CAP
            caractères, troncature signalée explicitement. Sans selector sur un
            document multi-unités, renvoie la première unité + notice — jamais
            le document entier. `path` lit un membre de zip par chemin (texte
            brut uniquement en v1 ; un membre lui-même document structuré
            (docx/xlsx/pptx/pdf imbriqué) sera extractible via ref+path en D3)."""
            if session_id is None:
                raise ToolError("session_id requis (appel hors MIAOU ?)")
            doc_path = resolve_ref(session_id, ref, content_b64)
            kind = detect_kind(doc_path, filename)

            if path is not None:
                if kind != "zip":
                    raise ToolError(f"'{ref}' n'est pas une archive : path ne s'applique pas")
                return zip_read_member(doc_path, path)

            return read_document(kind, doc_path, selector)


server = DocsServer()
mcp = server.mcp  # exposé pour le proxy in-process

if __name__ == "__main__":
    server.main()
