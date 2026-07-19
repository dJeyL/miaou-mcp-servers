#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.28.1", "uvicorn", "starlette", "html2text"]
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
    Nom       : web
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
from typing import Annotated

import html2text
from mcp import types
from pydantic import Field

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


def _cache_and_cap(url: str, full_text: str, *, purge_html: bool = False) -> str:
    """Met le texte complet en cache (clé = SHA256 de l'URL) et renvoie une version
    plafonnée à READ_CAP, avec notice de pagination si tronquée. purge_html=True
    (chemin text/*) invalide .html/.json d'une URL qui était auparavant du HTML
    (WEB3) ; le chemin HTML (_render_html_blocking) garde False, store_html vient
    de (re)poser ces fichiers."""
    web_cache.store(url, full_text, purge_html=purge_html)
    cap = web_cache.READ_CAP
    if len(full_text) <= cap:
        return full_text
    return (
        full_text[:cap]
        + f"\n\n[Tronqué à {cap} caractères — appeler fetch_read(url, "
        f"char_start={cap}) pour la suite]"
    )


def _render_html_blocking(url: str, html_text: str, truncation_note: str) -> str:
    """Mise en cache du HTML brut, conversion html2text (CPU-bound sur plusieurs Mo)
    et mise en cache du texte rendu, groupées pour un seul asyncio.to_thread —
    l'ordre compte (store_html avant la conversion, _cache_and_cap après)."""
    web_cache.store_html(url, html_text)
    h = html2text.HTML2Text()
    h.ignore_images = True
    h.body_width = 0
    cleaned = h.handle(html_text).strip() + truncation_note
    return _cache_and_cap(url, cleaned)


def _load_structure_blocking(url: str, html_text: str) -> list[dict]:
    """Lecture du cache de structure, sinon extraction + mise en cache."""
    entries = web_cache.load_structure(url)
    if entries is None:
        entries = extract_structure(html_text)
        web_cache.store_structure(url, entries)
    return entries


def _fetch_bytes(req: urllib.request.Request, max_bytes: int) -> tuple[str, bytes]:
    """I/O bloquante isolée pour asyncio.to_thread (T2). Renvoie (content_type, corps)."""
    opener = make_opener()
    with opener.open(req, timeout=10) as resp:
        content_type = resp.headers.get("Content-Type", "application/octet-stream")
        raw = resp.read(max_bytes + 1)
        return content_type, raw


async def _guarded_fetch(
    url: str, max_bytes: int, cap: int
) -> tuple[str, bytes, bool] | str:
    """Gardes + téléchargement communs à fetch_url/fetch_resource (WEB4) : schéma
    http/https, clamp max_bytes vers [1, cap], requête + erreurs réseau en
    chaînes. Renvoie soit un message d'erreur (str), soit
    (content_type, corps_tronqué, truncated)."""
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return f"Schéma d'URL non autorisé ({scheme or '?'}) — http/https uniquement : {url}"
    if max_bytes < 1:
        return f"max_bytes doit être >= 1 (reçu {max_bytes})"
    max_bytes = min(max_bytes, cap)

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
    return content_type, raw, truncated


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
            max_bytes: Annotated[
                int,
                Field(
                    description=(
                        f"Taille maximale en octets à télécharger ; une valeur plus "
                        f"grande que le plafond ({_DEFAULT_MAX_BYTES}) y est silencieusement ramenée."
                    )
                ),
            ] = _DEFAULT_MAX_BYTES,
        ) -> str | types.EmbeddedResource:
            fetched = await _guarded_fetch(url, max_bytes, _DEFAULT_MAX_BYTES)
            if isinstance(fetched, str):
                return fetched
            content_type, raw, truncated = fetched
            max_bytes = min(max_bytes, _DEFAULT_MAX_BYTES)

            mime = content_type.split(";")[0].strip().lower()
            charset = _charset_from_content_type(content_type)
            truncation_note = f"\n\n[Tronqué à {max_bytes} octets]" if truncated else ""

            if mime == "text/html":
                try:
                    html_text = raw.decode(charset, errors="replace")
                except LookupError:
                    html_text = raw.decode("utf-8", errors="replace")
                text = await asyncio.to_thread(
                    _render_html_blocking, url, html_text, truncation_note
                )
                return types.EmbeddedResource(
                    type="resource",
                    resource=types.TextResourceContents(
                        uri=url,  # type: ignore[arg-type]
                        mimeType="text/plain",
                        text=text,
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
                        text=await asyncio.to_thread(_cache_and_cap, url, full_text, purge_html=True),
                    ),
                )
            else:
                await asyncio.to_thread(web_cache.purge, url)
                return types.EmbeddedResource(
                    type="resource",
                    resource=types.BlobResourceContents(
                        uri=url,  # type: ignore[arg-type]
                        mimeType=mime,
                        blob=base64.b64encode(raw).decode(),
                    ),
                )

        fetch_url.__doc__ = f"""Télécharge une URL et renvoie son contenu : HTML converti
        en texte, mimes textuels (text/*, JSON, XML, JavaScript, suffixes
        +json/+xml) renvoyés tels quels avec leur mime d'origine, binaire
        (image, etc.) encodé en base64. Téléchargement limité à max_bytes
        (défaut et plafond {_DEFAULT_MAX_BYTES} octets).

        Le texte renvoyé est en plus plafonné à {web_cache.READ_CAP} caractères ;
        le texte complet est conservé en cache — paginer avec
        fetch_read(url, char_start=...) sans retélécharger, et pour du HTML,
        extraire la structure de navigation (headings/liens) via fetch_list(url)."""
        self.mcp.tool(name="fetch_url")(fetch_url)

        async def fetch_read(
            url: str,
            char_start: int = 0,
            char_end: Annotated[
                int | None,
                Field(
                    description=(
                        f"Fin de plage en caractères (exclusive, optionnelle) ; ne lève "
                        f"pas le plafond de {web_cache.READ_CAP} caractères par appel, "
                        f"déplace seulement la fenêtre demandée."
                    )
                ),
            ] = None,
        ) -> str:
            try:
                full_text = await asyncio.to_thread(web_cache.load, url)
            except CacheMiss as e:
                return str(e)

            if char_start < 0:
                return f"char_start doit être >= 0 (reçu {char_start})"
            if char_end is not None and char_end < char_start:
                return f"char_end ({char_end}) < char_start ({char_start})"
            if char_start >= len(full_text) and full_text:
                return f"char_start ({char_start}) hors bornes (texte de {len(full_text)} caractères)"

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

        fetch_read.__doc__ = f"""Relit le texte déjà téléchargé par fetch_url sur cette URL,
        sans retélécharger. char_start (offset caractère, 0-indexé) et char_end
        (optionnel, exclusif) déplacent la fenêtre de lecture ; chaque appel
        reste plafonné à {web_cache.READ_CAP} caractères (char_end ne lève pas
        ce cap), la notice de fin indique l'offset suivant. Erreur si l'URL n'a
        jamais été récupérée via fetch_url, ou si le cache a expiré."""
        self.mcp.tool(name="fetch_read")(fetch_read)

        async def fetch_list(
            url: str,
            entry_start: int = 0,
            entry_end: int | None = None,
        ) -> str:
            try:
                html_text = await asyncio.to_thread(web_cache.load_html, url)
            except CacheMiss:
                if web_cache.entry_path(url).exists():
                    return f"Cette URL n'a pas renvoyé du HTML — fetch_list ne s'applique pas : {url}"
                return f"Aucun HTML en cache pour cette URL — appeler fetch_url d'abord : {url}"

            if entry_start < 0:
                return f"entry_start doit être >= 0 (reçu {entry_start})"
            if entry_end is not None and entry_end < entry_start:
                return f"entry_end ({entry_end}) < entry_start ({entry_start})"

            entries = await asyncio.to_thread(_load_structure_blocking, url, html_text)

            cap = web_cache.LIST_CAP
            requested_end = (
                entry_start + cap if entry_end is None else min(entry_end, entry_start + cap)
            )
            page = entries[entry_start:requested_end]
            total = len(entries)
            next_offset = entry_start + len(page)

            if not page:
                if entry_start >= total:
                    return f"entry_start ({entry_start}) hors bornes ({total} entrée(s) au total)."
                return f"Aucune entrée (headings/liens) dans la plage demandée ({total} entrée(s) au total)."

            lines = [_format_entry(entry_start + i, entry) for i, entry in enumerate(page)]
            note = ""
            if next_offset < total:
                note = (
                    f"\n\n[{next_offset}/{total} entrées — appeler fetch_list(url, "
                    f"entry_start={next_offset}) pour la suite]"
                )
            return "\n".join(lines) + note

        fetch_list.__doc__ = f"""Extrait la structure de navigation (headings h1-h6 et
        liens, dans l'ordre de la page) du HTML déjà téléchargé par fetch_url
        sur cette URL, sans retélécharger. Entrées numérotées (index 0-indexé,
        stable) ; entry_start/entry_end (exclusif) déplacent la fenêtre, chaque
        appel reste plafonné à {web_cache.LIST_CAP} entrées (entry_end ne lève
        pas ce cap). Uniquement pour une URL dont fetch_url a renvoyé du HTML ;
        erreur claire sinon, ou si l'URL n'a jamais été récupérée, ou si le
        cache a expiré."""
        self.mcp.tool(name="fetch_list")(fetch_list)

        async def fetch_resource(
            url: str,
            max_bytes: Annotated[
                int,
                Field(
                    description=(
                        f"Taille maximale en octets à transférer au client ; une valeur "
                        f"plus grande que le plafond ({web_cache.RESOURCE_MAX_BYTES}) y "
                        f"est silencieusement ramenée."
                    )
                ),
            ] = web_cache.RESOURCE_MAX_BYTES,
        ) -> list[types.ContentBlock] | str:
            fetched = await _guarded_fetch(url, max_bytes, web_cache.RESOURCE_MAX_BYTES)
            if isinstance(fetched, str):
                return fetched
            content_type, raw, truncated = fetched
            max_bytes = min(max_bytes, web_cache.RESOURCE_MAX_BYTES)

            mime = content_type.split(";")[0].strip().lower()
            blob = base64.b64encode(raw).decode()

            descripteur = f"Resource transférée au client : {mime}, {len(raw)} octets, depuis {url}."
            if truncated:
                descripteur += f" Tronqué à {max_bytes} octets."

            return [
                types.TextContent(type="text", text=descripteur),
                types.EmbeddedResource(
                    type="resource",
                    resource=types.BlobResourceContents(
                        uri=url,  # type: ignore[arg-type]
                        mimeType=mime,
                        blob=blob,
                    ),
                ),
            ]

        fetch_resource.__doc__ = f"""Télécharge une URL et transfère ses octets bruts au
        client (matérialisation en ressource `res_…`, hors contexte du modèle) —
        contrairement à fetch_url qui met le texte en contexte, ici seul un
        descripteur factuel (mime, taille, URL) est renvoyé au modèle. Le contenu
        est toujours transféré en binaire, même pour du texte/JSON, afin de rester
        exploitable tel quel côté client (ex. réinjection vers un outil de
        documents). Téléchargement limité à max_bytes (défaut et plafond
        {web_cache.RESOURCE_MAX_BYTES} octets)."""
        self.mcp.tool(name="fetch_resource")(fetch_resource)

        self.finalize_tools()


server = WebServer()
mcp = server.mcp  # exposé pour le proxy in-process

if __name__ == "__main__":
    server.main()
