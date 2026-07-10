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
`docs__read` renvoie un extrait borné avec troncature signalée, `docs__search`
cherche du texte et renvoie les occurrences groupées par unité avec extraits
de contexte.

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
    MIAOU_DOCS_SEARCH_CAP      (défaut : 50, nombre de snippets)

Module éclaté en package (servers/mcp_docs/) : session.py (sessions, sanitization,
matérialisation, contrat REF_UNKNOWN), formats.py (détection de type + parsers
PDF/docx/xlsx/pptx/zip), search.py (logique pure de recherche : fold, parsing de
requête, matching, snippets). Ce fichier ne porte que le serveur FastMCP et ses outils.

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

import asyncio
import shutil

from mcp_base import MiaouMCPBase

from .formats import (
    TextRange,
    detect_kind,
    list_document,
    read_document,
    search_document,
    zip_list_member,
    zip_read_member,
    zip_search,
)
from .search import parse_query, render_results
from .session import (
    READ_CAP,
    REF_UNKNOWN_ERROR_CODE,
    REF_UNKNOWN_SENTINEL,
    SEARCH_CAP,
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


def _build_range(
    char_start: int | None,
    char_end: int | None,
    line_start: int | None,
    line_end: int | None,
) -> TextRange | None:
    """Valide les paramètres de plage de `read` et construit un TextRange, ou None
    si aucune plage n'est demandée. Char et ligne sont mutuellement exclusifs ;
    `*_end` sans `*_start` est une erreur (le début est obligatoire)."""
    has_char = char_start is not None or char_end is not None
    has_line = line_start is not None or line_end is not None

    if not has_char and not has_line:
        return None
    if has_char and has_line:
        raise ToolError(
            "char_start/char_end et line_start/line_end sont exclusifs — "
            "choisir un seul mode de plage"
        )

    if has_char:
        if char_start is None:
            raise ToolError("char_end fourni sans char_start (le début de plage est obligatoire)")
        if char_start < 0:
            raise ToolError(f"char_start doit être >= 0 (reçu {char_start})")
        if char_end is not None and char_end < char_start:
            raise ToolError(f"char_end ({char_end}) < char_start ({char_start})")
        return TextRange(char_start=char_start, char_end=char_end)

    # has_line
    if line_start is None:
        raise ToolError("line_end fourni sans line_start (le début de plage est obligatoire)")
    if line_start < 1:
        raise ToolError(f"line_start doit être >= 1 (1-indexé, reçu {line_start})")
    if line_end is not None and line_end < line_start:
        raise ToolError(f"line_end ({line_end}) < line_start ({line_start})")
    return TextRange(line_start=line_start, line_end=line_end)


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
                shutil.rmtree(d, ignore_errors=True)
            return f"Session '{session_id}' supprimée."

        @self.mcp.tool(name="list")
        async def list_(
            ref: str,
            session_id: str | None = None,
            content_b64: str | None = None,
            path: str | None = None,
            filename: str | None = None,
        ) -> str:
            """Liste la structure d'un document (pages/feuilles/slides/entrées zip)
            sans en renvoyer le contenu. `ref` identifie l'attachement (att-N) ;
            `content_b64` matérialise le fichier au premier appel ; `filename`
            (optionnel) aide à la détection de type par extension, sinon magic
            bytes. `path` liste la structure d'un membre d'archive qui est
            lui-même un document structuré (docx/xlsx/pptx/pdf), un seul niveau
            d'imbrication supporté ; sans `path`, un membre d'archive listé ici
            reste lisible tel quel via `read(path=...)`."""
            if session_id is None:
                raise ToolError("session_id requis (appel hors MIAOU ?)")
            doc_path = resolve_ref(session_id, ref, content_b64)
            kind = detect_kind(doc_path, filename)

            if path is not None:
                if kind != "zip":
                    raise ToolError(f"'{ref}' n'est pas une archive : path ne s'applique pas")
                return await asyncio.to_thread(zip_list_member, doc_path, path)

            return await asyncio.to_thread(list_document, kind, doc_path)

        async def read(
            ref: str,
            session_id: str | None = None,
            content_b64: str | None = None,
            path: str | None = None,
            selector: str | None = None,
            char_start: int | None = None,
            char_end: int | None = None,
            line_start: int | None = None,
            line_end: int | None = None,
            filename: str | None = None,
        ) -> str:
            if session_id is None:
                raise ToolError("session_id requis (appel hors MIAOU ?)")

            rng = _build_range(char_start, char_end, line_start, line_end)

            doc_path = resolve_ref(session_id, ref, content_b64)
            kind = detect_kind(doc_path, filename)

            if path is not None:
                if kind != "zip":
                    raise ToolError(f"'{ref}' n'est pas une archive : path ne s'applique pas")
                return await asyncio.to_thread(zip_read_member, doc_path, path, selector, rng)

            return await asyncio.to_thread(read_document, kind, doc_path, selector, rng)

        read.__doc__ = f"""Lit un extrait borné d'un document (plage de pages/lignes/slides/
        paragraphes selon le format). Réponse plafonnée à {READ_CAP}
        caractères, troncature signalée explicitement. Sans selector sur un
        document multi-unités, renvoie la première unité + notice — jamais
        le document entier. `path` lit un membre de zip par chemin (texte
        brut, ou extrait structuré via `selector` si le membre est lui-même
        un docx/xlsx/pptx/pdf — un seul niveau d'imbrication supporté).

        Pour dépasser la limite de {READ_CAP} caractères sur une unité
        volumineuse, paginer avec une plage sur le texte extrait :
        `char_start`/`char_end` (offset caractère, `char_start` obligatoire,
        `char_end` optionnel) OU `line_start`/`line_end` (numéro de ligne
        1-indexé, `line_start` obligatoire, `line_end` optionnel inclusif).
        Les deux modes sont exclusifs entre eux. La plage porte sur le texte
        que `read` produirait pour l'unité sélectionnée (donc combinable avec
        `selector` : « lignes 500-800 de la page 3 »). Chaque appel reste
        plafonné à {READ_CAP} caractères — la plage déplace la fenêtre, la
        notice indique l'offset suivant à demander. Non applicable à un xlsx
        (grille : utiliser un selector 'Feuille!A1:C10')."""
        self.mcp.tool(name="read")(read)

        async def search(
            ref: str,
            query: str,
            session_id: str | None = None,
            content_b64: str | None = None,
            path: str | None = None,
            filename: str | None = None,
        ) -> str:
            if session_id is None:
                raise ToolError("session_id requis (appel hors MIAOU ?)")
            doc_path = resolve_ref(session_id, ref, content_b64)
            kind = detect_kind(doc_path, filename)
            parsed_query = parse_query(query)

            if kind == "zip":
                unit_results = await asyncio.to_thread(
                    zip_search, doc_path, parsed_query, member_path=path
                )
            elif path is not None:
                raise ToolError(f"'{ref}' n'est pas une archive : path ne s'applique pas")
            else:
                unit_results = await asyncio.to_thread(search_document, kind, doc_path, parsed_query)

            return render_results(kind, query, unit_results, SEARCH_CAP)

        search.__doc__ = f"""Cherche du texte dans un document et renvoie les occurrences
        groupées par unité (page/feuille/slide/membre zip), avec un
        extrait de contexte par occurrence. Chaque unité est étiquetée par
        un label directement réutilisable comme selector de `read` (ou
        comme `path` pour un membre de zip) — un hit peut être relu en
        détail avec un second appel ciblé.

        Syntaxe de la requête : des termes séparés par des espaces sont
        tous requis (ET implicite) ; `"une phrase entre guillemets"`
        cherche cette séquence exacte. Pas d'opérateurs OU/NON/parenthèses
        — composer plusieurs appels ou filtrer les résultats soi-même si
        besoin. Insensible à la casse et aux accents. Une phrase exacte ne
        matche pas si elle est à cheval sur deux unités (page/section/…).

        Réponse plafonnée à {SEARCH_CAP} occurrences au total,
        troncature signalée explicitement.

        Dans une archive zip, seuls les membres texte brut sont cherchés ;
        les membres reconnus comme documents structurés (docx/xlsx/pptx/pdf),
        chiffrés ou binaires ne sont pas balayés (signalé en fin de
        résultat). Sans `path`, tous les membres texte de l'archive sont
        cherchés ; avec `path`, la recherche est restreinte à ce membre."""
        self.mcp.tool(name="search")(search)


server = DocsServer()
mcp = server.mcp  # exposé pour le proxy in-process

if __name__ == "__main__":
    server.main()
