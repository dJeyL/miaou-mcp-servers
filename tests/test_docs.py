"""Tests pour servers/mcp_docs/ (D1 — sessions/transport/REF_UNKNOWN, D2 — formats,
D3 — sécurité archives)."""
import base64
import shutil
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_HAS_ZIP_CLI = shutil.which("zip") is not None
_requires_zip_cli = pytest.mark.skipif(
    not _HAS_ZIP_CLI, reason="binaire 'zip' absent (stdlib zipfile ne sait pas chiffrer en écriture)"
)

_ROOT = Path(__file__).parent.parent
_SERVERS = _ROOT / "servers"
for p in (_ROOT, _SERVERS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import mcp_docs
from mcp_docs import REF_UNKNOWN_ERROR_CODE, REF_UNKNOWN_SENTINEL, ToolError
from mcp_docs import session as docs_session
from mcp_docs.formats import detect_kind, list_document, read_document, zip_read_member
from mcp_docs.session import (
    materialize,
    resolve_ref,
    session_dir,
    sweep_expired_sessions,
    validate_ref,
    validate_session_id,
)


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(docs_session, "WORKDIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Sanitization session_id / ref
# ---------------------------------------------------------------------------

def test_validate_session_id_rejects_empty():
    with pytest.raises(ToolError):
        validate_session_id("")


def test_validate_session_id_rejects_path_separator():
    with pytest.raises(ToolError):
        validate_session_id("foo/bar")
    with pytest.raises(ToolError):
        validate_session_id("foo\\bar")


def test_validate_session_id_rejects_dotdot():
    with pytest.raises(ToolError):
        validate_session_id("../etc")
    with pytest.raises(ToolError):
        validate_session_id("foo..bar")


def test_validate_session_id_accepts_normal_id():
    assert validate_session_id("conv-123") == "conv-123"


def test_validate_ref_accepts_att_n():
    assert validate_ref("att-1") == "att-1"
    assert validate_ref("att-42") == "att-42"


def test_validate_ref_rejects_nested_path():
    """La syntaxe ref#path du brief est explicitement écartée (audit §2) —
    l'adressage de membre d'archive passe par le paramètre `path` séparé."""
    with pytest.raises(ToolError):
        validate_ref("att-3#membre.docx")


def test_validate_ref_rejects_garbage():
    with pytest.raises(ToolError):
        validate_ref("not-a-ref")


# ---------------------------------------------------------------------------
# Matérialisation idempotente
# ---------------------------------------------------------------------------

def test_materialize_writes_file(workdir):
    content = base64.b64encode(b"hello world").decode()
    path = materialize("conv-1", "att-1", content)
    assert path.exists()
    assert path.read_bytes() == b"hello world"


def test_materialize_is_idempotent(workdir):
    """Un rechargement de page MIAOU repousse content_b64 pour un ref déjà connu —
    ré-écriture silencieuse, pas une erreur."""
    content = base64.b64encode(b"hello world").decode()
    path1 = materialize("conv-1", "att-1", content)
    path1.write_bytes(b"already there, must not be overwritten")

    other_content = base64.b64encode(b"different bytes").decode()
    path2 = materialize("conv-1", "att-1", other_content)

    assert path1 == path2
    assert path2.read_bytes() == b"already there, must not be overwritten"


def test_materialize_rejects_oversized_file(workdir):
    with patch.object(docs_session, "MAX_FILE_MB", 1):
        big = base64.b64encode(b"x" * (2 * 1024 * 1024)).decode()
        with pytest.raises(ToolError, match="volumineux"):
            materialize("conv-1", "att-1", big)


def test_materialize_rejects_session_quota(workdir):
    with patch.object(docs_session, "MAX_SESSION_MB", 1), patch.object(docs_session, "MAX_FILE_MB", 10):
        # Premier fichier ~0.9 Mo passe.
        first = base64.b64encode(b"x" * int(0.9 * 1024 * 1024)).decode()
        materialize("conv-1", "att-1", first)

        # Deuxième fichier ferait dépasser le quota de session (1 Mo).
        second = base64.b64encode(b"y" * int(0.5 * 1024 * 1024)).decode()
        with pytest.raises(ToolError, match="Quota"):
            materialize("conv-1", "att-2", second)


def test_materialize_rejects_bad_session_id(workdir):
    content = base64.b64encode(b"data").decode()
    with pytest.raises(ToolError):
        materialize("../escape", "att-1", content)


# ---------------------------------------------------------------------------
# resolve_ref — REF_UNKNOWN
# ---------------------------------------------------------------------------

def test_resolve_ref_unknown_without_content_raises_sentinel(workdir):
    with pytest.raises(ToolError) as exc_info:
        resolve_ref("conv-1", "att-99", None)
    assert str(exc_info.value).startswith(REF_UNKNOWN_SENTINEL)


def test_resolve_ref_materializes_when_content_provided(workdir):
    content = base64.b64encode(b"payload").decode()
    path = resolve_ref("conv-1", "att-1", content)
    assert path.read_bytes() == b"payload"


def test_resolve_ref_known_ref_without_content_succeeds(workdir):
    content = base64.b64encode(b"payload").decode()
    resolve_ref("conv-1", "att-1", content)
    path = resolve_ref("conv-1", "att-1", None)
    assert path.read_bytes() == b"payload"


# ---------------------------------------------------------------------------
# TTL sweep (horloge mockée)
# ---------------------------------------------------------------------------

def test_sweep_removes_expired_session(workdir):
    old_dir = session_dir("conv-old")
    old_dir.mkdir(parents=True)
    old_time = time.time() - 25 * 3600  # TTL défaut 24h dépassé
    import os
    os.utime(old_dir, (old_time, old_time))

    with patch.object(docs_session, "TTL_HOURS", 24):
        sweep_expired_sessions()

    assert not old_dir.exists()


def test_sweep_keeps_recent_session(workdir):
    recent_dir = session_dir("conv-recent")
    recent_dir.mkdir(parents=True)

    with patch.object(docs_session, "TTL_HOURS", 24):
        sweep_expired_sessions()

    assert recent_dir.exists()


def test_sweep_touch_on_access_prevents_expiry(workdir):
    """touch_session doit repousser mtime — un accès récent empêche le sweep."""
    d = session_dir("conv-1")
    d.mkdir(parents=True)
    old_time = time.time() - 25 * 3600
    import os
    os.utime(d, (old_time, old_time))

    docs_session.touch_session("conv-1")

    with patch.object(docs_session, "TTL_HOURS", 24):
        sweep_expired_sessions()

    assert d.exists()


# ---------------------------------------------------------------------------
# docs__drop_session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drop_session_removes_directory(workdir):
    from mcp_docs import server as docs_server

    d = session_dir("conv-1")
    d.mkdir(parents=True)
    tm = docs_server.mcp._tool_manager
    result = await tm.call_tool("drop_session", {"session_id": "conv-1"})
    assert not d.exists()
    assert "conv-1" in result


@pytest.mark.asyncio
async def test_drop_session_idempotent_when_absent(workdir):
    from mcp_docs import server as docs_server

    tm = docs_server.mcp._tool_manager
    result = await tm.call_tool("drop_session", {"session_id": "conv-never-existed"})
    assert "conv-never-existed" in result


# ---------------------------------------------------------------------------
# REF_UNKNOWN à travers le stack proxy (modèle test_proxy.py)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ref_unknown_through_proxy_raises_jsonrpc_error(workdir):
    """Le sentinel textuel REF_UNKNOWN doit ressortir en erreur JSON-RPC
    data.code == 'REF_UNKNOWN', pas en isError textuel (contrat client, audit §3)."""
    import mcp.types as types
    from mcp.shared.exceptions import McpError

    from mcp_proxy import InProcessUpstream, build_proxy_server

    upstream = InProcessUpstream("mcp_docs")
    await upstream.start()

    upstreams = {"docs": upstream}
    tool_map: dict = {}
    server = build_proxy_server(upstreams, tool_map)

    list_handler = server.request_handlers[types.ListToolsRequest]
    await list_handler(types.ListToolsRequest(method="tools/list", params=None))

    call_handler = server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(
            name="docs__list",
            arguments={"ref": "att-99", "session_id": "conv-1"},
        ),
    )

    with pytest.raises(McpError) as exc_info:
        await call_handler(request)

    assert exc_info.value.error.data["code"] == "REF_UNKNOWN"
    assert exc_info.value.error.code == REF_UNKNOWN_ERROR_CODE


@pytest.mark.asyncio
async def test_non_ref_unknown_error_stays_iserror_through_proxy(workdir):
    """Une erreur applicative ordinaire (session_id manquant) ne doit pas être
    convertie en erreur JSON-RPC — seul le sentinel REF_UNKNOWN déclenche McpError."""
    import mcp.types as types

    from mcp_proxy import InProcessUpstream, build_proxy_server

    upstream = InProcessUpstream("mcp_docs")
    await upstream.start()

    upstreams = {"docs": upstream}
    tool_map: dict = {}
    server = build_proxy_server(upstreams, tool_map)

    list_handler = server.request_handlers[types.ListToolsRequest]
    await list_handler(types.ListToolsRequest(method="tools/list", params=None))

    call_handler = server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(
            name="docs__list",
            arguments={"ref": "att-1"},
        ),
    )

    result = await call_handler(request)
    assert result.root.isError


# ---------------------------------------------------------------------------
# Détection de type
# ---------------------------------------------------------------------------

def test_detect_kind_by_filename_extension(tmp_path):
    p = tmp_path / "whatever.bin"
    p.write_bytes(b"not really a pdf")
    assert detect_kind(p, filename="report.pdf") == "pdf"
    assert detect_kind(p, filename="sheet.xlsx") == "xlsx"


def test_detect_kind_pdf_by_magic_bytes(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"%PDF-1.4\n...")
    assert detect_kind(p) == "pdf"


def test_detect_kind_unknown_for_garbage(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"not a document at all")
    assert detect_kind(p) == "inconnu"


def test_detect_kind_plain_zip_by_magic_bytes(tmp_path):
    import zipfile

    p = tmp_path / "f.bin"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "hi")
    assert detect_kind(p) == "zip"


# ---------------------------------------------------------------------------
# PDF (pymupdf) — fixtures générées à la volée
# ---------------------------------------------------------------------------

def _make_pdf(tmp_path, pages_text, toc=None):
    import fitz

    doc = fitz.open()
    for text in pages_text:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    if toc:
        doc.set_toc(toc)
    path = tmp_path / "doc.pdf"
    doc.save(str(path))
    doc.close()
    return path


def test_pdf_list_reports_page_count_and_outline(tmp_path):
    path = _make_pdf(tmp_path, ["Page one", "Page two"], toc=[[1, "Chapter 1", 1], [1, "Chapter 2", 2]])
    result = list_document("pdf", path)
    assert "2 page" in result
    assert "Chapter 1" in result
    assert "Chapter 2" in result


def test_pdf_list_no_outline(tmp_path):
    path = _make_pdf(tmp_path, ["Solo page"])
    result = list_document("pdf", path)
    assert "pas de sommaire" in result


def test_pdf_read_without_selector_returns_first_page_only(tmp_path):
    path = _make_pdf(tmp_path, ["First page text", "Second page text"])
    result = read_document("pdf", path, None)
    assert "First page text" in result
    assert "Second page text" not in result
    assert "Tronqué" in result


def test_pdf_read_with_range_selector(tmp_path):
    path = _make_pdf(tmp_path, ["Page A", "Page B", "Page C"])
    result = read_document("pdf", path, "2-3")
    assert "Page B" in result
    assert "Page C" in result
    assert "Page A" not in result


def test_pdf_read_scanned_page_reports_no_ocr_note(tmp_path):
    path = _make_pdf(tmp_path, [None])  # page sans texte, simulateur de scan
    result = read_document("pdf", path, "1")
    assert "OCR" in result


# ---------------------------------------------------------------------------
# XLSX (openpyxl)
# ---------------------------------------------------------------------------

def _make_xlsx(tmp_path, sheets):
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    path = tmp_path / "sheet.xlsx"
    wb.save(str(path))
    return path


def test_xlsx_list_reports_sheets_and_dimensions(tmp_path):
    path = _make_xlsx(tmp_path, {"Data": [["a", "b"], ["1", "2"]], "Empty": [["x"]]})
    result = list_document("xlsx", path)
    assert "Data" in result
    assert "Empty" in result


def test_xlsx_read_default_first_sheet(tmp_path):
    path = _make_xlsx(tmp_path, {"S1": [["h1", "h2"], ["v1", "v2"]], "S2": [["z"]]})
    result = read_document("xlsx", path, None)
    assert "h1" in result and "v1" in result
    assert "S1" in result


def test_xlsx_read_max_rows_default_truncates(tmp_path):
    rows = [[str(i)] for i in range(250)]
    path = _make_xlsx(tmp_path, {"Big": rows})
    result = read_document("xlsx", path, "Big")
    assert "Tronqué à 200 lignes" in result


def test_xlsx_read_unknown_sheet_raises(tmp_path):
    path = _make_xlsx(tmp_path, {"S1": [["a"]]})
    with pytest.raises(ToolError, match="Feuille inconnue"):
        read_document("xlsx", path, "Nope!A1:B2")


# ---------------------------------------------------------------------------
# DOCX (python-docx)
# ---------------------------------------------------------------------------

def _make_docx(tmp_path, structure, tables=None):
    """structure: liste de (level_or_None, text) ; level=None -> paragraphe normal.
    tables (optionnel) : liste de tables, chaque table = liste de lignes,
    chaque ligne = liste de textes de cellule."""
    import docx

    d = docx.Document()
    for level, text in structure:
        if level is None:
            d.add_paragraph(text)
        else:
            d.add_heading(text, level=level)
    for rows in tables or []:
        n_rows = len(rows)
        n_cols = len(rows[0]) if rows else 0
        table = d.add_table(rows=n_rows, cols=n_cols)
        for r, row_values in enumerate(rows):
            for c, value in enumerate(row_values):
                table.cell(r, c).text = value
    path = tmp_path / "doc.docx"
    d.save(str(path))
    return path


def test_docx_list_reports_headings(tmp_path):
    path = _make_docx(tmp_path, [(1, "Title"), (None, "Body text"), (2, "Subtitle")])
    result = list_document("docx", path)
    assert "Title" in result
    assert "Subtitle" in result


def test_docx_list_no_headings(tmp_path):
    path = _make_docx(tmp_path, [(None, "Just a paragraph")])
    result = list_document("docx", path)
    assert "pas de heading" in result


def test_docx_read_by_heading_selector(tmp_path):
    path = _make_docx(
        tmp_path,
        [
            (1, "Intro"),
            (None, "Intro text"),
            (1, "Body"),
            (None, "Body text"),
            (1, "Conclusion"),
            (None, "Conclusion text"),
        ],
    )
    result = read_document("docx", path, "Body")
    assert "Body text" in result
    assert "Intro text" not in result
    assert "Conclusion text" not in result


def test_docx_read_unknown_heading_raises(tmp_path):
    path = _make_docx(tmp_path, [(1, "Intro"), (None, "Text")])
    with pytest.raises(ToolError, match="introuvable"):
        read_document("docx", path, "Nonexistent")


def test_docx_list_reports_tables(tmp_path):
    path = _make_docx(tmp_path, [], tables=[[["a", "b"], ["c", "d"]]])
    result = list_document("docx", path)
    assert "Tableaux (1)" in result
    assert "2 ligne(s) x 2 colonne(s)" in result


def test_docx_read_tabular_document_not_empty(tmp_path):
    """Document sans aucun paragraphe non-vide, tout le contenu en table
    (formulaire questions/réponses) : `read` sans selector doit rendre le
    contenu, pas une chaîne vide."""
    path = _make_docx(
        tmp_path, [],
        tables=[[["1", "Combien de branches ?"], ["2", "Pourquoi le logo est bleu ?"]]],
    )
    result = read_document("docx", path, None)
    assert "Combien de branches ?" in result
    assert "Pourquoi le logo est bleu ?" in result


def test_docx_read_paragraphs_and_tables_both_present(tmp_path):
    path = _make_docx(
        tmp_path, [(1, "Title"), (None, "Intro paragraph")],
        tables=[[["k", "v"]]],
    )
    result = read_document("docx", path, None)
    assert "Intro paragraph" in result
    assert "k | v" in result


def test_docx_read_by_heading_selector_ignores_tables(tmp_path):
    """Le mode selector-heading reste borné aux paragraphes : une table présente
    ailleurs dans le document n'est pas mélangée à une section ciblée."""
    path = _make_docx(
        tmp_path,
        [(1, "Body"), (None, "Body text")],
        tables=[[["unrelated", "table"]]],
    )
    result = read_document("docx", path, "Body")
    assert "Body text" in result
    assert "unrelated" not in result


# ---------------------------------------------------------------------------
# PPTX (python-pptx)
# ---------------------------------------------------------------------------

def _make_pptx(tmp_path, slide_titles):
    from pptx import Presentation

    prs = Presentation()
    for title in slide_titles:
        slide = prs.slides.add_slide(prs.slide_layouts[0])
        slide.shapes.title.text = title
    path = tmp_path / "deck.pptx"
    prs.save(str(path))
    return path


def test_pptx_list_reports_slide_count_and_titles(tmp_path):
    path = _make_pptx(tmp_path, ["Intro", "Details", "Thanks"])
    result = list_document("pptx", path)
    assert "3 slide" in result
    assert "Intro" in result
    assert "Details" in result
    assert "Thanks" in result


def test_pptx_read_without_selector_first_slide_only(tmp_path):
    path = _make_pptx(tmp_path, ["Intro", "Details"])
    result = read_document("pptx", path, None)
    assert "Intro" in result
    assert "Tronqué" in result


def test_pptx_read_with_range(tmp_path):
    path = _make_pptx(tmp_path, ["One", "Two", "Three"])
    result = read_document("pptx", path, "2-3")
    assert "Two" in result
    assert "Three" in result
    assert "One" not in result


# ---------------------------------------------------------------------------
# ZIP (stdlib zipfile)
# ---------------------------------------------------------------------------

def _make_zip(tmp_path, members):
    import zipfile

    path = tmp_path / "archive.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name, content in members.items():
            z.writestr(name, content)
    return path


def test_zip_list_reports_entries(tmp_path):
    path = _make_zip(tmp_path, {"a.txt": "hello", "dir/b.txt": "world"})
    result = list_document("zip", path)
    assert "a.txt" in result
    assert "dir/b.txt" in result


def test_zip_read_member_returns_text_content(tmp_path):
    path = _make_zip(tmp_path, {"a.txt": "hello world"})
    result = zip_read_member(path, "a.txt")
    assert "hello world" in result


def test_zip_read_member_unknown_raises(tmp_path):
    path = _make_zip(tmp_path, {"a.txt": "hello"})
    with pytest.raises(ToolError, match="introuvable"):
        zip_read_member(path, "missing.txt")


def test_zip_read_binary_member_reports_note_instead_of_garbage(tmp_path):
    path = _make_zip(tmp_path, {})
    import zipfile

    with zipfile.ZipFile(path, "a") as z:
        z.writestr("blob.bin", b"\xff\xfe\x00\x01binary")
    result = zip_read_member(path, "blob.bin")
    assert "binaire" in result


def test_read_document_zip_without_path_raises():
    with pytest.raises(ToolError, match="path"):
        read_document("zip", Path("/nonexistent"), None)


# ---------------------------------------------------------------------------
# docx/xlsx/pdf/pptx détectés par sniff interne d'un zip (Office = zip signé)
# ---------------------------------------------------------------------------

def test_detect_kind_docx_sniffed_from_zip_signature(tmp_path):
    path = _make_docx(tmp_path, [(None, "hi")])
    assert detect_kind(path) == "docx"


def test_detect_kind_xlsx_sniffed_from_zip_signature(tmp_path):
    path = _make_xlsx(tmp_path, {"S1": [["a"]]})
    assert detect_kind(path) == "xlsx"


def test_detect_kind_pptx_sniffed_from_zip_signature(tmp_path):
    path = _make_pptx(tmp_path, ["Slide"])
    assert detect_kind(path) == "pptx"


# ---------------------------------------------------------------------------
# Bout en bout via les outils MCP (list/read réels, pas seulement formats.py)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_tool_end_to_end_pdf(workdir):
    from mcp_docs import server as docs_server

    path = _make_pdf(workdir, ["irrelevant, materialisation manuelle ci-dessous"])
    # Matérialise directement dans le cache de session pour éviter de repasser par b64 ici.
    dest = session_dir("conv-1") / "att-1.bin"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(path.read_bytes())

    tm = docs_server.mcp._tool_manager
    result = await tm.call_tool("list", {"ref": "att-1", "session_id": "conv-1"})
    assert "PDF" in result


@pytest.mark.asyncio
async def test_read_tool_requires_path_for_zip(workdir):
    from mcp.server.fastmcp.exceptions import ToolError as FastMCPToolError

    from mcp_docs import server as docs_server

    path = _make_zip(workdir, {"a.txt": "hi"})
    dest = session_dir("conv-1") / "att-1.bin"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(path.read_bytes())

    tm = docs_server.mcp._tool_manager
    with pytest.raises(FastMCPToolError, match="path"):
        await tm.call_tool("read", {"ref": "att-1", "session_id": "conv-1"})


# ---------------------------------------------------------------------------
# D3 — Sécurité archives : zip-slip, tailles, chiffrement, imbrication
# ---------------------------------------------------------------------------

def _make_raw_zip(tmp_path, entries, name="raw.zip"):
    """entries: dict[str, bytes|str]. writestr accepte str ou bytes."""
    import zipfile

    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        for member_name, content in entries.items():
            z.writestr(member_name, content)
    return path


def test_zip_read_member_rejects_relative_traversal(tmp_path):
    path = _make_raw_zip(tmp_path, {"../../etc/passwd": "pwned"})
    with pytest.raises(ToolError, match="traversal"):
        zip_read_member(path, "../../etc/passwd")


def test_zip_read_member_rejects_absolute_path(tmp_path):
    path = _make_raw_zip(tmp_path, {"/absolute/path.txt": "pwned"})
    with pytest.raises(ToolError, match="traversal"):
        zip_read_member(path, "/absolute/path.txt")


def test_zip_list_flags_traversal_entries_without_blocking_list(tmp_path):
    """list reste possible sur une archive contenant des entrées suspectes —
    seule l'extraction (read) est bloquée."""
    path = _make_raw_zip(tmp_path, {"../evil.txt": "x", "safe.txt": "ok"})
    result = list_document("zip", path)
    assert "chemin suspect" in result
    assert "safe.txt" in result


def _make_encrypted_zip(tmp_path):
    """stdlib zipfile ne sait pas écrire de zip chiffré — dépend du binaire 'zip'."""
    import subprocess

    src = tmp_path / "secret.txt"
    src.write_text("top secret")
    archive = tmp_path / "encrypted.zip"
    subprocess.run(
        ["zip", "-P", "testpass", "-j", str(archive), str(src)],
        check=True,
        capture_output=True,
    )
    return archive


@_requires_zip_cli
def test_zip_read_member_rejects_encrypted(tmp_path):
    import zipfile

    archive = _make_encrypted_zip(tmp_path)
    with zipfile.ZipFile(archive) as z:
        member_name = z.namelist()[0]

    with pytest.raises(ToolError, match="chiffré"):
        zip_read_member(archive, member_name)


@_requires_zip_cli
def test_zip_list_flags_encrypted_entries(tmp_path):
    archive = _make_encrypted_zip(tmp_path)
    result = list_document("zip", archive)
    assert "chiffré" in result


def test_zip_read_member_rejects_oversized_declared_header(tmp_path):
    """La garde sur ZipInfo.file_size (en-tête) rejette avant même d'ouvrir le flux."""
    path = _make_raw_zip(tmp_path, {"big.txt": "x" * 1000})
    with patch("mcp_docs.formats.MAX_UNZIP_MB", 0):
        with pytest.raises(ToolError, match="volumineux"):
            zip_read_member(path, "big.txt")


def test_zip_read_member_normal_stream_within_limit_succeeds(tmp_path):
    """Garde-fou : un membre de taille normale, sous la limite, reste lisible
    (la garde en flux ne doit pas produire de faux positif)."""
    path = _make_raw_zip(tmp_path, {"ok.txt": "A" * 1000})
    result = zip_read_member(path, "ok.txt")
    assert "A" * 100 in result


def test_zip_read_member_nested_archive_reported_not_extracted(tmp_path):
    import zipfile

    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("a.txt", "hi")

    outer = _make_raw_zip(tmp_path, {})
    with zipfile.ZipFile(outer, "a") as z:
        z.write(inner, arcname="nested.zip")

    result = zip_read_member(outer, "nested.zip")
    assert "imbriquée" in result


def test_zip_list_flags_nested_archive_candidates(tmp_path):
    import zipfile

    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("a.txt", "hi")

    outer = _make_raw_zip(tmp_path, {})
    with zipfile.ZipFile(outer, "a") as z:
        z.write(inner, arcname="nested.zip")

    result = list_document("zip", outer)
    assert "archive imbriquée potentielle" in result


def test_zip_list_reports_total_declared_size_over_limit(tmp_path):
    path = _make_raw_zip(tmp_path, {"a.txt": "x" * 1000})
    with patch("mcp_docs.formats.MAX_UNZIP_MB", 0):
        result = list_document("zip", path)
    assert "Taille décompressée totale" in result
