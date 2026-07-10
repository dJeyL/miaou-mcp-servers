#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2", "uvicorn", "starlette", "html2text"]
# ///
"""
Serveur MCP fetch pour MIAOU.

Transport streamable-http (single endpoint POST, réponses en SSE). CORS ouvert
pour permettre au navigateur de l'atteindre directement depuis dist/miaou.html.

Outils exposés :
  - fetch_url(url, max_bytes=5242880) : télécharge une URL ; renvoie texte nettoyé
    pour HTML, texte brut pour text/* et les mimes textuels structurés
    (application/json, application/xml, application/javascript, suffixes +json/+xml),
    binaire base64 pour tout le reste. Le texte renvoyé est plafonné à
    MIAOU_WEB_READ_CAP caractères ; le texte complet (et le HTML brut si applicable)
    est mis en cache disque (clé = SHA256 de l'URL) pour pagination via fetch_read /
    extraction de structure via fetch_list.
  - fetch_read(url, char_start=0, char_end=None) : relit le texte déjà mis en cache
    par un appel fetch_url précédent, sans retélécharger, pour paginer au-delà du cap.
  - fetch_list(url, entry_start=0, entry_end=None) : extrait la structure de navigation
    (headings + liens, dans l'ordre d'apparition) du HTML déjà mis en cache par
    fetch_url, paginée par index d'entrée.

Variables d'environnement (toutes optionnelles, défauts constants) :
    MIAOU_WEB_WORKDIR      (défaut : "./miaou-web", relatif au répertoire de travail)
    MIAOU_WEB_CACHE_TTL_H  (défaut : 24, sweep opportuniste comme mcp_docs)
    MIAOU_WEB_READ_CAP     (défaut : 20000, en caractères, pour fetch_url/fetch_read)
    MIAOU_WEB_LIST_CAP     (défaut : 100, en nombre d'entrées, pour fetch_list)

Module éclaté en package (servers/mcp_web/) : cache.py (cache disque par checksum
d'URL), structure.py (extraction stdlib html.parser des headings/liens). Ce fichier
ne porte que le serveur FastMCP et ses outils.

Lancement (package, pas un script plat — `uv run servers/mcp_web.py` ne s'applique
pas ici, cd dans servers/ ou utiliser --directory) :
    uv run --directory servers python -m mcp_web                    # HTTP 127.0.0.1:8768
    uv run --directory servers python -m mcp_web --transport stdio  # stdin/stdout
    uv run --directory servers python -m mcp_web --host 0.0.0.0     # toutes interfaces

Dans MIAOU → Paramètres → Serveurs MCP → Ajouter :
    Nom       : fetch
    URL       : http://127.0.0.1:8768/mcp
    Transport : streamable-http   (deviné depuis /mcp)
    Activé    : oui
"""

from __future__ import annotations

import asyncio
import base64
import urllib.error
import urllib.parse
import urllib.request

import html2text
from mcp import types

from mcp_base import MiaouMCPBase, make_opener

from . import cache as web_cache
from .cache import CacheMiss
from .structure import extract_structure

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 Mo
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _charset_from_content_type(content_type: str) -> str:
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            return part[8:].strip().strip('"')
    return "utf-8"


_TEXTUAL_APPLICATION_MIMES = frozenset({
    "application/json",
    "application/xml",
    "application/javascript",
    "application/ld+json",
})


def _is_textual_mime(mime: str) -> bool:
    """True si le contenu doit être renvoyé en texte (TextResourceContents)
    plutôt qu'en blob base64 : text/*, plus les mimes application/* qui sont
    du texte structuré (JSON, XML, JS) et tout suffixe structuré +json / +xml."""
    return (
        mime.startswith("text/")
        or mime in _TEXTUAL_APPLICATION_MIMES
        or mime.endswith("+json")
        or mime.endswith("+xml")
    )


def _cache_and_cap(url: str, full_text: str) -> str:
    """Met le texte complet en cache (clé = SHA256 de l'URL) et renvoie une version
    plafonnée à READ_CAP, avec notice de pagination si tronquée."""
    web_cache.store(url, full_text)
    cap = web_cache.READ_CAP
    if len(full_text) <= cap:
        return full_text
    return (
        full_text[:cap]
        + f"\n\n[Tronqué à {cap} caractères — appeler fetch_read(url, "
        f"char_start={cap}) pour la suite]"
    )


def _fetch_bytes(req: urllib.request.Request, max_bytes: int) -> tuple[str, bytes]:
    """I/O bloquante isolée pour asyncio.to_thread (T2). Renvoie (content_type, corps)."""
    opener = make_opener()
    with opener.open(req, timeout=10) as resp:
        content_type = resp.headers.get("Content-Type", "application/octet-stream")
        raw = resp.read(max_bytes + 1)
        return content_type, raw


def _format_entry(index: int, entry: dict) -> str:
    if entry["type"] == "heading":
        prefix = "#" * entry["level"]
        return f"{index}. {prefix} {entry['text']}"
    return f"{index}. -> [{entry['text']}]({entry['url']})"


class WebServer(MiaouMCPBase):
    def __init__(self) -> None:
        super().__init__("miaou-web", default_port=8768)

        async def fetch_url(
            url: str,
            max_bytes: int = _DEFAULT_MAX_BYTES,
        ) -> str | types.EmbeddedResource:
            scheme = urllib.parse.urlsplit(url).scheme.lower()
            if scheme not in _ALLOWED_SCHEMES:
                return f"Schéma d'URL non autorisé ({scheme or '?'}) — http/https uniquement : {url}"
            if max_bytes < 1:
                return f"max_bytes doit être >= 1 (reçu {max_bytes})"

            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            try:
                content_type, raw = await asyncio.to_thread(_fetch_bytes, req, max_bytes)
            except urllib.error.HTTPError as e:
                return f"Erreur HTTP {e.code} ({e.reason}) — {url}"
            except urllib.error.URLError as e:
                return f"Erreur réseau ({e.reason}) — {url}"
            except TimeoutError:
                return f"Timeout (10 s) — {url}"
            except Exception as e:
                return f"Erreur inattendue ({type(e).__name__}: {e}) — {url}"

            truncated = len(raw) > max_bytes
            if truncated:
                raw = raw[:max_bytes]

            mime = content_type.split(";")[0].strip().lower()
            charset = _charset_from_content_type(content_type)
            truncation_note = f"\n\n[Tronqué à {max_bytes} octets]" if truncated else ""

            if mime == "text/html":
                try:
                    html_text = raw.decode(charset, errors="replace")
                except LookupError:
                    html_text = raw.decode("utf-8", errors="replace")
                web_cache.store_html(url, html_text)
                h = html2text.HTML2Text()
                h.ignore_images = True
                h.body_width = 0
                cleaned = h.handle(html_text).strip() + truncation_note
                return types.EmbeddedResource(
                    type="resource",
                    resource=types.TextResourceContents(
                        uri=url,  # type: ignore[arg-type]
                        mimeType="text/plain",
                        text=_cache_and_cap(url, cleaned),
                    ),
                )
            elif _is_textual_mime(mime):
                try:
                    text = raw.decode(charset, errors="replace")
                except LookupError:
                    text = raw.decode("utf-8", errors="replace")
                full_text = text + truncation_note
                return types.EmbeddedResource(
                    type="resource",
                    resource=types.TextResourceContents(
                        uri=url,  # type: ignore[arg-type]
                        mimeType=mime,
                        text=_cache_and_cap(url, full_text),
                    ),
                )
            else:
                return types.EmbeddedResource(
                    type="resource",
                    resource=types.BlobResourceContents(
                        uri=url,  # type: ignore[arg-type]
                        mimeType=mime,
                        blob=base64.b64encode(raw).decode(),
                    ),
                )

        fetch_url.__doc__ = f"""Télécharge une URL et renvoie son contenu : HTML converti
        en texte (html2text), text/* renvoyé tel quel, mimes textuels structurés
        (application/json, application/xml, application/javascript, et tout
        suffixe +json/+xml comme application/vnd.api+json ou image/svg+xml)
        renvoyés tels quels avec leur mime d'origine préservé, binaire (image,
        etc.) encodé en base64. Téléchargement limité à max_bytes (défaut 5 Mo).

        Le texte renvoyé (HTML converti, text/* ou mime textuel structuré) est
        en plus plafonné à {web_cache.READ_CAP} caractères ; le texte complet
        est conservé pour être relu avec fetch_read(url, char_start=...) sans
        retélécharger. Pour du HTML, la structure de navigation (headings/liens)
        est également extractible sans retélécharger via fetch_list(url)."""
        self.mcp.tool(name="fetch_url")(fetch_url)

        async def fetch_read(
            url: str,
            char_start: int = 0,
            char_end: int | None = None,
        ) -> str:
            try:
                full_text = web_cache.load(url)
            except CacheMiss as e:
                return str(e)

            if char_start < 0:
                return f"char_start doit être >= 0 (reçu {char_start})"
            if char_end is not None and char_end < char_start:
                return f"char_end ({char_end}) < char_start ({char_start})"

            cap = web_cache.READ_CAP
            requested_end = char_start + cap if char_end is None else min(char_end, char_start + cap)
            excerpt = full_text[char_start:requested_end]
            total = len(full_text)
            next_offset = char_start + len(excerpt)
            note = ""
            if next_offset < total:
                note = (
                    f"\n\n[{next_offset}/{total} caractères — appeler fetch_read(url, "
                    f"char_start={next_offset}) pour la suite]"
                )
            return excerpt + note

        fetch_read.__doc__ = f"""Relit un extrait du texte déjà téléchargé par un appel
        fetch_url précédent sur cette URL, sans retélécharger. char_start (offset
        caractère, 0-indexé) et char_end (optionnel, exclusif) permettent de
        paginer au-delà de la limite de fetch_url. Chaque appel reste plafonné à
        {web_cache.READ_CAP} caractères — char_end ne lève pas ce cap, il déplace
        la fenêtre ; la notice indique l'offset suivant à demander. Erreur si
        l'URL n'a jamais été récupérée via fetch_url, ou si le cache a expiré."""
        self.mcp.tool(name="fetch_read")(fetch_read)

        async def fetch_list(
            url: str,
            entry_start: int = 0,
            entry_end: int | None = None,
        ) -> str:
            try:
                html_text = web_cache.load_html(url)
            except CacheMiss:
                if web_cache.entry_path(url).exists():
                    return f"Cette URL n'a pas renvoyé du HTML — fetch_list ne s'applique pas : {url}"
                return f"Aucun HTML en cache pour cette URL — appeler fetch_url d'abord : {url}"

            if entry_start < 0:
                return f"entry_start doit être >= 0 (reçu {entry_start})"
            if entry_end is not None and entry_end < entry_start:
                return f"entry_end ({entry_end}) < entry_start ({entry_start})"

            entries = web_cache.load_structure(url)
            if entries is None:
                entries = extract_structure(html_text)
                web_cache.store_structure(url, entries)

            cap = web_cache.LIST_CAP
            requested_end = (
                entry_start + cap if entry_end is None else min(entry_end, entry_start + cap)
            )
            page = entries[entry_start:requested_end]
            total = len(entries)
            next_offset = entry_start + len(page)

            if not page:
                return f"Aucune entrée (headings/liens) entre {entry_start} et {total} au total."

            lines = [_format_entry(entry_start + i, entry) for i, entry in enumerate(page)]
            note = ""
            if next_offset < total:
                note = (
                    f"\n\n[{next_offset}/{total} entrées — appeler fetch_list(url, "
                    f"entry_start={next_offset}) pour la suite]"
                )
            return "\n".join(lines) + note

        fetch_list.__doc__ = f"""Extrait la structure de navigation (headings h1-h6 et
        liens, dans l'ordre d'apparition sur la page) du HTML déjà téléchargé par
        un appel fetch_url précédent sur cette URL, sans retélécharger. Chaque
        entrée est numérotée (index 0-indexé, stable d'un appel à l'autre) ;
        entry_start/entry_end (exclusif) paginent au-delà du cap de
        {web_cache.LIST_CAP} entrées par appel — entry_end ne lève pas ce cap, il
        déplace la fenêtre. Uniquement pertinent pour une URL dont fetch_url a
        renvoyé du HTML (text/html) ; erreur claire sinon, ou si l'URL n'a jamais
        été récupérée, ou si le cache a expiré."""
        self.mcp.tool(name="fetch_list")(fetch_list)


server = WebServer()
mcp = server.mcp  # exposé pour le proxy in-process

if __name__ == "__main__":
    server.main()
