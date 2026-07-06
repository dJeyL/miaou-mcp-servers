"""Détection de type et extraction paginée par format (PDF/docx/xlsx/pptx/zip).

`list_document` renvoie la structure sans contenu ; `read_document` renvoie un
extrait borné (`session.READ_CAP` caractères), troncature toujours signalée.
Aucun format ne renvoie jamais le document entier en un seul appel.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .session import MAX_UNZIP_MB, READ_CAP, ToolError

# ---------------------------------------------------------------------------
# Détection de type — extension du nom d'origine si fourni, sinon magic bytes
# ---------------------------------------------------------------------------

_OFFICE_ZIP_SIGNATURES = {
    "word/": "docx",
    "xl/": "xlsx",
    "ppt/": "pptx",
}


def _sniff_zip_kind(path: Path) -> str:
    """Distingue docx/xlsx/pptx d'un zip brut par ses dossiers internes caractéristiques."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except zipfile.BadZipFile:
        return "zip"
    for prefix, kind in _OFFICE_ZIP_SIGNATURES.items():
        if any(n.startswith(prefix) for n in names):
            return kind
    return "zip"


def detect_kind(path: Path, filename: str | None = None) -> str:
    """Renvoie l'un de : pdf, docx, xlsx, pptx, zip, inconnu.

    Le contrat client (dispatcher MIAOU) ne transmet pas le nom de fichier
    d'origine à ce jour — `filename` reste optionnel pour un usage futur ou
    manuel. En son absence, la détection retombe sur les magic bytes."""
    if filename:
        ext = Path(filename).suffix.lower().lstrip(".")
        if ext in ("pdf", "docx", "xlsx", "pptx", "zip"):
            return ext

    with open(path, "rb") as f:
        head = f.read(8)

    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return _sniff_zip_kind(path)
    return "inconnu"


# ---------------------------------------------------------------------------
# Cap de troncature partagé par tous les formats
# ---------------------------------------------------------------------------

def _cap_text(text: str, cap: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def _truncation_notice(continue_hint: str) -> str:
    return f"\n\n[Tronqué à {READ_CAP} caractères — {continue_hint}]"


def _parse_range(selector: str, total: int) -> tuple[int, int]:
    """Parse 'N' ou 'N-M' (1-indexé, inclusif), borné à [1, total]."""
    if "-" in selector:
        start_s, end_s = selector.split("-", 1)
        start, end = int(start_s), int(end_s)
    else:
        start = end = int(selector)
    start = max(1, start)
    end = min(total, end)
    if start > end:
        raise ToolError(f"selector invalide : '{selector}' (document de {total} unité(s))")
    return start, end


# ---------------------------------------------------------------------------
# PDF (pymupdf)
# ---------------------------------------------------------------------------

def pdf_list(path: Path) -> str:
    import fitz

    with fitz.open(path) as doc:
        toc = doc.get_toc()
        lines = [f"PDF — {doc.page_count} page(s)"]
        if toc:
            lines.append("Sommaire :")
            for level, title, page in toc:
                lines.append(f"{'  ' * (level - 1)}- p.{page} {title}")
        else:
            lines.append("(pas de sommaire)")
        return "\n".join(lines)


def pdf_read(path: Path, selector: str | None) -> str:
    import fitz

    with fitz.open(path) as doc:
        if selector:
            start, end = _parse_range(selector, doc.page_count)
        else:
            start, end = 1, 1

        parts = []
        empty_pages = []
        for page_num in range(start, end + 1):
            text = doc[page_num - 1].get_text().strip()
            if not text:
                empty_pages.append(page_num)
            parts.append(f"--- Page {page_num} ---\n{text}")
        body = "\n\n".join(parts)

        notice = ""
        if empty_pages:
            notice += (
                f"\n\n[Pages sans texte extractible (probablement scannées, "
                f"pas d'OCR en v1) : {', '.join(map(str, empty_pages))}]"
            )
        if not selector and doc.page_count > 1:
            notice += _truncation_notice(f"selector='2-{doc.page_count}' pour la suite")

        capped, truncated = _cap_text(body, READ_CAP)
        if truncated and not notice:
            notice = _truncation_notice("relire avec un selector plus étroit")
        return capped + notice


# ---------------------------------------------------------------------------
# XLSX (openpyxl)
# ---------------------------------------------------------------------------

MAX_XLSX_ROWS_DEFAULT = 200


def xlsx_list(path: Path) -> str:
    import openpyxl

    # Fichier ouvert et passé en objet binaire, pas en chemin : openpyxl valide
    # l'extension du *chemin* (rejette tout sauf .xlsx/.xlsm/...), alors que le
    # fichier matérialisé par mcp_docs garde un nom stable `att-N.bin` (type
    # réel détecté par magic bytes, cf. session.ref_path). En mode read_only,
    # les feuilles restent en lecture différée : tout accès doit rester dans
    # le bloc `with`, sinon le fichier sous-jacent est déjà refermé.
    with open(path, "rb") as f:
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        try:
            lines = ["XLSX — feuilles :"]
            for name in wb.sheetnames:
                ws = wb[name]
                dim = ws.calculate_dimension()
                lines.append(f"- {name} ({dim}, {ws.max_row} ligne(s) x {ws.max_column} colonne(s))")
            return "\n".join(lines)
        finally:
            wb.close()


def xlsx_read(path: Path, selector: str | None) -> str:
    """selector : 'NomFeuille' ou 'NomFeuille!A1:C10'. Sans plage, cap à
    MAX_XLSX_ROWS_DEFAULT lignes depuis le début de la feuille."""
    import openpyxl

    with open(path, "rb") as f:
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        try:
            if not selector:
                sheet_name = wb.sheetnames[0]
                cell_range = None
            elif "!" in selector:
                sheet_name, cell_range = selector.split("!", 1)
            else:
                sheet_name, cell_range = selector, None

            if sheet_name not in wb.sheetnames:
                raise ToolError(f"Feuille inconnue : '{sheet_name}' (disponibles : {wb.sheetnames})")
            ws = wb[sheet_name]

            truncated_rows = False
            if cell_range:
                rows = list(ws[cell_range])
            else:
                rows = []
                for i, row in enumerate(ws.iter_rows(), start=1):
                    if i > MAX_XLSX_ROWS_DEFAULT:
                        truncated_rows = True
                        break
                    rows.append(row)

            lines = [f"Feuille '{sheet_name}' :"]
            for row in rows:
                values = [str(c.value) if c.value is not None else "" for c in row]
                lines.append(" | ".join(values))
            body = "\n".join(lines)

            notice = ""
            if truncated_rows:
                notice = (
                    f"\n\n[Tronqué à {MAX_XLSX_ROWS_DEFAULT} lignes — préciser un "
                    f"selector '{sheet_name}!A1:...' pour la suite]"
                )
            capped, capped_flag = _cap_text(body, READ_CAP)
            if capped_flag and not notice:
                notice = _truncation_notice("relire avec un selector plus étroit")
            return capped + notice
        finally:
            wb.close()


# ---------------------------------------------------------------------------
# DOCX (python-docx)
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^Heading (\d+)$")


def _table_text(table) -> str:
    """Concatène le texte de toutes les cellules d'une table, ligne par ligne."""
    lines = []
    for row in table.rows:
        cells = [c.text.strip() for c in row.cells]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def docx_list(path: Path) -> str:
    import docx

    d = docx.Document(str(path))
    lines = ["DOCX — sections :"]
    found = False
    for para in d.paragraphs:
        m = _HEADING_RE.match(para.style.name or "" if para.style else "")
        if m and para.text.strip():
            level = int(m.group(1))
            lines.append(f"{'  ' * (level - 1)}- {para.text.strip()}")
            found = True
    if not found:
        lines.append("(pas de heading — document sans structure de titres)")
    if d.tables:
        lines.append(f"\nTableaux ({len(d.tables)}) :")
        for i, table in enumerate(d.tables):
            lines.append(f"- table {i + 1} : {len(table.rows)} ligne(s) x {len(table.columns)} colonne(s)")
    return "\n".join(lines)


def docx_read(path: Path, selector: str | None) -> str:
    """selector : titre exact d'un heading (lit jusqu'au prochain heading de
    même niveau ou supérieur, paragraphes uniquement) ; sans selector, les N
    premiers paragraphes PUIS le texte de toutes les tables (un document
    purement tabulaire — formulaire questions/réponses en table, par exemple —
    n'a aucun paragraphe non-vide : sans les tables ici, `read` renverrait une
    chaîne vide alors que `list` a bien signalé leur présence)."""
    import docx

    d = docx.Document(str(path))
    paragraphs = d.paragraphs

    if selector:
        start_idx = None
        start_level = None
        for i, para in enumerate(paragraphs):
            m = _HEADING_RE.match(para.style.name or "" if para.style else "")
            if m and para.text.strip() == selector:
                start_idx = i
                start_level = int(m.group(1))
                break
        if start_idx is None:
            raise ToolError(f"Heading introuvable : '{selector}'")

        end_idx = len(paragraphs)
        for i in range(start_idx + 1, len(paragraphs)):
            style = paragraphs[i].style
            m = _HEADING_RE.match(style.name or "" if style else "")
            if m and int(m.group(1)) <= start_level:
                end_idx = i
                break
        selected = paragraphs[start_idx:end_idx]
        body = "\n".join(p.text for p in selected)
        notice = ""
    else:
        selected = paragraphs[:50]
        para_notice = _truncation_notice("selector=<titre d'un heading>") if len(paragraphs) > 50 else ""
        parts = [p.text for p in selected]
        if d.tables:
            parts.append("\n--- Tableaux ---")
            parts.extend(_table_text(t) for t in d.tables)
        body = "\n".join(parts)
        notice = para_notice

    capped, truncated = _cap_text(body, READ_CAP)
    if truncated and not notice:
        notice = _truncation_notice("relire avec un selector plus étroit")
    return capped + notice


# ---------------------------------------------------------------------------
# PPTX (python-pptx)
# ---------------------------------------------------------------------------

def pptx_list(path: Path) -> str:
    from pptx import Presentation

    prs = Presentation(str(path))
    lines = [f"PPTX — {len(prs.slides)} slide(s) :"]
    for i, slide in enumerate(prs.slides, start=1):
        title = slide.shapes.title.text if slide.shapes.title else "(sans titre)"
        lines.append(f"{i}. {title}")
    return "\n".join(lines)


def pptx_read(path: Path, selector: str | None) -> str:
    from pptx import Presentation

    prs = Presentation(str(path))
    total = len(prs.slides)

    if selector:
        start, end = _parse_range(selector, total)
    else:
        start, end = 1, 1

    parts = []
    for slide_num in range(start, end + 1):
        slide = prs.slides[slide_num - 1]
        texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame]
        parts.append(f"--- Slide {slide_num} ---\n" + "\n".join(t for t in texts if t.strip()))
    body = "\n\n".join(parts)

    notice = ""
    if not selector and total > 1:
        notice = _truncation_notice(f"selector='2-{total}' pour la suite")
    capped, truncated = _cap_text(body, READ_CAP)
    if truncated and not notice:
        notice = _truncation_notice("relire avec un selector plus étroit")
    return capped + notice


# ---------------------------------------------------------------------------
# ZIP (stdlib zipfile) — arbre d'entrées et lecture de membre
#
# Gardes de sécurité (brief D4) : zip-slip (chemin résolu, rejet absolu/`..`),
# tailles totale/par-entrée contrôlées en flux (les headers mentent — on ne se
# fie pas à ZipInfo.file_size seul), rejet des membres chiffrés et des archives
# imbriquées (listées, jamais extraites). Menace : utilisateur unique, mais les
# gardes restent non négociables (coût trivial, échec = dommage filesystem).
# ---------------------------------------------------------------------------

_ZIP_READ_CHUNK = 64 * 1024


def _is_zip_slip(member_path: str) -> bool:
    """Rejette chemin absolu ou toute composante '..' (avant même resolve())."""
    if member_path.startswith("/") or member_path.startswith("\\"):
        return True
    normalized = Path(member_path)
    return ".." in normalized.parts


def _is_encrypted(info: zipfile.ZipInfo) -> bool:
    return bool(info.flag_bits & 0x1)


def _is_nested_archive(head: bytes) -> bool:
    return head.startswith(b"PK\x03\x04")


def zip_list(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        lines = ["ZIP — entrées :"]
        total_declared = 0
        for info in infos:
            kind = "dir" if info.is_dir() else "file"
            flags = []
            if _is_zip_slip(info.filename):
                flags.append("chemin suspect, non extractible")
            if not info.is_dir() and _is_encrypted(info):
                flags.append("chiffré, non extractible")
            if not info.is_dir() and info.filename.lower().endswith(
                (".zip", ".docx", ".xlsx", ".pptx")
            ):
                flags.append("archive imbriquée potentielle, non extractible en v1")
            flag_note = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"- {info.filename} ({info.file_size} octets, {kind}){flag_note}")
            total_declared += info.file_size

        total_mb = total_declared / (1024 * 1024)
        if total_mb > MAX_UNZIP_MB:
            lines.append(
                f"\n[Taille décompressée totale déclarée : {total_mb:.1f} Mo, "
                f"dépasse la limite de {MAX_UNZIP_MB} Mo — l'extraction de membres "
                f"individuels reste bornée par membre, mais l'archive entière ne "
                f"pourrait pas être extraite intégralement]"
            )
        return "\n".join(lines)


def zip_read_member(path: Path, member_path: str) -> str:
    """Extrait un membre du zip en mémoire, en flux et avec garde de taille
    cumulative (pas de confiance dans ZipInfo.file_size). Rejette zip-slip,
    membres chiffrés et archives imbriquées (une archive dans l'archive reste
    listée par zip_list mais n'est pas elle-même extractible en v1)."""
    if _is_zip_slip(member_path):
        raise ToolError(f"Chemin de membre invalide (traversal) : '{member_path}'")

    with zipfile.ZipFile(path) as z:
        try:
            info = z.getinfo(member_path)
        except KeyError:
            raise ToolError(f"Membre introuvable dans l'archive : '{member_path}'")
        if info.is_dir():
            raise ToolError(f"'{member_path}' est un répertoire, pas un fichier")
        if _is_encrypted(info):
            raise ToolError(f"Membre chiffré, non extractible : '{member_path}'")

        # Garde rapide sur la taille déclarée (header), puis contrôle réel en
        # flux ci-dessous — les deux sont nécessaires, le header seul ment.
        if info.file_size / (1024 * 1024) > MAX_UNZIP_MB:
            raise ToolError(
                f"Membre trop volumineux d'après l'en-tête "
                f"({info.file_size / (1024 * 1024):.1f} Mo, max {MAX_UNZIP_MB} Mo)"
            )

        chunks = []
        total = 0
        try:
            with z.open(info) as member_f:
                while True:
                    chunk = member_f.read(_ZIP_READ_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total / (1024 * 1024) > MAX_UNZIP_MB:
                        raise ToolError(
                            f"Membre trop volumineux en flux réel (>{MAX_UNZIP_MB} Mo, "
                            f"en-tête mensonger) : '{member_path}'"
                        )
                    chunks.append(chunk)
        except zipfile.BadZipFile as e:
            raise ToolError(f"Archive corrompue ou en-tête incohérent pour '{member_path}' : {e}")
        data = b"".join(chunks)

    if _is_nested_archive(data[:4]):
        return (
            f"[Archive imbriquée détectée dans '{member_path}' — non extractible "
            f"en v1, listée uniquement via zip_list]"
        )

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"[Membre binaire non affichable en l'état : '{member_path}', {len(data)} octets]"

    capped, truncated = _cap_text(text, READ_CAP)
    notice = _truncation_notice("relire avec un selector plus étroit") if truncated else ""
    return capped + notice


# ---------------------------------------------------------------------------
# Dispatch — table kind → fonction
# ---------------------------------------------------------------------------

_LIST_DISPATCH = {
    "pdf": pdf_list,
    "docx": docx_list,
    "xlsx": xlsx_list,
    "pptx": pptx_list,
    "zip": zip_list,
}

_READ_DISPATCH = {
    "pdf": pdf_read,
    "docx": docx_read,
    "xlsx": xlsx_read,
    "pptx": pptx_read,
}


def list_document(kind: str, path: Path) -> str:
    fn = _LIST_DISPATCH.get(kind)
    if fn is None:
        raise ToolError(f"Type de document non supporté ou non reconnu (détecté : '{kind}')")
    return fn(path)


def read_document(kind: str, path: Path, selector: str | None) -> str:
    if kind == "zip":
        raise ToolError("Zip : préciser 'path' pour lire un membre (voir docs__list pour l'arbre)")
    fn = _READ_DISPATCH.get(kind)
    if fn is None:
        raise ToolError(f"Type de document non supporté ou non reconnu (détecté : '{kind}')")
    return fn(path, selector)
