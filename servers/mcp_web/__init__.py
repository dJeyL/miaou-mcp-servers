#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.2,<3", "uvicorn", "starlette", "html2text", "truststore"]
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

fetch_url pose en plus, hors du contenu servi au modèle, un `_meta` destiné au
client (clé `miaou/web` : titre, nom de site, URL finale, favicon) — cf. pagemeta.py.

  - search(query, max_results=5) : recherche web multi-moteurs, repli d'un moteur
    au suivant (Brave → Ollama → DuckDuckGo par défaut) — cf. search/.
  - image_search(query, max_results=5) : recherche d'images, listée seulement si un
    moteur de la chaîne sait en chercher (Brave).

Config (bloc `config` de l'entrée config.json) : clé `search`, ordre des moteurs
et clefs d'API, sinon BRAVE_API_KEY / OLLAMA_API_KEY dans l'environnement, ou
`false` pour couper la recherche ; clé `fetch`, `false` pour retirer les fetch_*.

Variables d'environnement (toutes optionnelles, défauts constants) :
    MIAOU_WEB_WORKDIR      (défaut : "./miaou-web", relatif au répertoire de travail)
    MIAOU_WEB_CACHE_TTL_H  (défaut : 24, sweep opportuniste comme mcp_docs)
    MIAOU_WEB_READ_CAP     (défaut : 20000, en caractères, pour fetch_url/fetch_read)
    MIAOU_WEB_LIST_CAP     (défaut : 100, en nombre d'entrées, pour fetch_list)

Module éclaté en package (servers/mcp_web/) : cache.py (cache disque par checksum
d'URL), structure.py (extraction stdlib html.parser des headings/liens), pagemeta.py
(métadonnées de page et favicon du `_meta` de fetch_url), search/ (moteurs de
recherche et chaîne de repli). Ce fichier
ne porte que le serveur MCP et ses outils.

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
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Annotated

import html2text
from mcp import types
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from mcp_base import MiaouMCPBase, make_opener

from . import cache as web_cache
from .cache import CacheMiss
from .pagemeta import (
    FAVICON_CACHE_MAX,
    META_KEY,
    _FaviconCache,
    build_web_meta,
    extract_head_meta,
    origin_of,
    resolve_favicon_blocking,
)
from .search import MAX_RESULTS, SEARCH_META_KEY, build_chain
from .search import ddg as search_ddg
from .search.common import SNIPPET_MAX_CHARS
from .structure import extract_structure

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 Mo
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

favicon_cache = _FaviconCache(FAVICON_CACHE_MAX, web_cache.TTL_HOURS * 3600)
"""Favicon par origine, partagée par tous les appels du processus (cf. pagemeta)."""


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


def _decompress(raw: bytes, encoding: str) -> bytes:
    """Décompresse un corps selon son Content-Encoding, en tolérant une fin
    manquante (WEB9).

    On ne sollicite AUCUN encodage (pas d'Accept-Encoding dans la requête) :
    urllib n'en envoie pas et on ne décompresse donc que ce qu'un serveur
    impose de lui-même. python.org le fait sur /downloads/release/, mesuré le
    2026-09-22 — et le corps gzip partait alors en decode(errors="replace"),
    d'où un texte de remplacement que le modèle lisait comme du contenu.

    `raw` est déjà tronqué à max_bytes+1 OCTETS COMPRESSÉS, donc le flux est
    coupé en plein milieu dès que le cap mord : `decompress()` lèverait sur la
    fin absente. Les objets de décompression, eux, rendent ce qu'ils ont pu
    lire — c'est exactement le comportement voulu, la troncature étant déjà
    signalée par ailleurs.

    Un encodage inconnu (ou un corps illisible) rend `raw` inchangé : ce qui
    était déjà envoyé avant cette fonction. Un cas non couvert ne doit pas
    faire échouer un fetch qui aboutissait."""
    enc = encoding.strip().lower()
    try:
        if enc == "gzip" or enc == "x-gzip":
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
        if enc == "deflate":
            try:
                return zlib.decompressobj().decompress(raw)
            except zlib.error:
                # deflate « brut », sans en-tête zlib : toléré par les navigateurs.
                return zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw)
    except (zlib.error, OSError):
        return raw
    return raw


def _fetch_bytes(
    req: urllib.request.Request, max_bytes: int
) -> tuple[str, bytes, bool, str | None]:
    """I/O bloquante isolée pour asyncio.to_thread (T2). Renvoie
    (content_type, corps, truncated, url_finale).

    `url_finale` est l'adresse atteinte après les redirections suivies par
    urllib (`geturl()`), None si la réponse n'en donne pas une chaîne http(s).

    La troncature est décidée ICI, sur les octets tels qu'ils arrivent du
    réseau, et jamais en aval : après `_decompress` le corps est plus GROS que
    `max_bytes` sans avoir rien perdu, si bien qu'un `len(corps) > max_bytes`
    calculé plus loin signalerait une troncature qui n'a pas eu lieu."""
    opener = make_opener()
    with opener.open(req, timeout=10) as resp:
        content_type = resp.headers.get("Content-Type", "application/octet-stream")
        encoding = resp.headers.get("Content-Encoding") or ""
        raw = resp.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        if encoding:
            raw = _decompress(raw, encoding)
        return content_type, raw, truncated, _final_url(resp)


def _final_url(resp) -> str | None:
    try:
        final = resp.geturl()
    except Exception:  # noqa: BLE001 — l'URL finale est un bonus, jamais une panne
        return None
    if not isinstance(final, str):
        return None
    if urllib.parse.urlsplit(final).scheme.lower() not in _ALLOWED_SCHEMES:
        return None
    return final


async def _guarded_fetch(
    url: str, max_bytes: int, cap: int
) -> tuple[str, bytes, bool, str | None] | str:
    """Gardes + téléchargement communs à fetch_url/fetch_resource (WEB4) : schéma
    http/https, clamp max_bytes vers [1, cap], requête + erreurs réseau en
    chaînes. Renvoie soit un message d'erreur (str), soit
    (content_type, corps_tronqué, truncated, url_finale).

    La troncature et la décompression appartiennent à `_fetch_bytes` (WEB9) :
    le corps rendu ici peut dépasser `max_bytes` — c'est le cas nominal d'une
    réponse compressée non tronquée."""
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return f"Schéma d'URL non autorisé ({scheme or '?'}) — http/https uniquement : {url}"
    if max_bytes < 1:
        return f"max_bytes doit être >= 1 (reçu {max_bytes})"
    max_bytes = min(max_bytes, cap)

    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        content_type, raw, truncated, final_url = await asyncio.to_thread(
            _fetch_bytes, req, max_bytes
        )
    except urllib.error.HTTPError as e:
        # Une HTTPError EST la réponse, socket comprise (`e.fp`) : sans ce
        # close, elle reste ouverte jusqu'au passage du GC — cyclique, le plus
        # souvent, l'exception ayant traversé to_thread et son futur.
        e.close()
        return f"Erreur HTTP {e.code} ({e.reason}) — {url}"
    except urllib.error.URLError as e:
        return f"Erreur réseau ({e.reason}) — {url}"
    except TimeoutError:
        return f"Timeout (10 s) — {url}"
    except Exception as e:
        return f"Erreur inattendue ({type(e).__name__}: {e}) — {url}"

    return content_type, raw, truncated, final_url


async def _favicon_for(page_url: str, icons: list[tuple[str, str]]) -> str | None:
    """Favicon de l'origine de `page_url`, depuis le cache ou sondée une fois."""
    origin = origin_of(page_url)
    found, value = favicon_cache.get(origin)
    if found:
        return value
    value = await asyncio.to_thread(resolve_favicon_blocking, page_url, icons, _UA)
    favicon_cache.put(origin, value)
    return value


def _tool_result(
    block: str | types.EmbeddedResource,
    web_meta: dict | None = None,
    meta_key: str = META_KEY,
) -> types.CallToolResult:
    """Résultat de fetch_url (ou de search, sous `meta_key`), `_meta` compris
    quand il y a quelque chose à dire.

    `**{"_meta": ...}` et non `meta=...`, comme côté proxy : pydantic ne
    sérialise sous l'alias que si le champ a été peuplé PAR l'alias. Un texte
    d'erreur reste un résultat ordinaire (isError faux), comme avant que
    fetch_url rende un CallToolResult."""
    content: list[types.ContentBlock] = (
        [types.TextContent(type="text", text=block)] if isinstance(block, str) else [block]
    )
    if not web_meta:
        return types.CallToolResult(content=content)
    return types.CallToolResult(content=content, **{"_meta": {meta_key: web_meta}})


def _format_entry(index: int, entry: dict) -> str:
    if entry["type"] == "heading":
        prefix = "#" * entry["level"]
        return f"{index}. {prefix} {entry['text']}"
    return f"{index}. -> [{entry['text']}]({entry['url']})"


def _search_result(query: str, kind: str, outcome: dict) -> types.CallToolResult:
    """Résultat de search/image_search : le JSON du moteur qui a répondu, avec
    `_meta["miaou/search"] = {"engine"}` pour le client, ou un message qui nomme
    chaque moteur écarté et pourquoi (sans `_meta` : aucun moteur à afficher).
    CallToolResult pour la même raison que fetch_url : seule forme qui laisse
    poser le `_meta` d'un résultat."""
    if outcome["engine"] is None:
        reasons = "; ".join(f"{f['engine']} : {f['reason']}" for f in outcome["fallback"])
        return _tool_result(f"Aucun moteur de recherche n'a répondu — {reasons}")
    return _tool_result(
        types.EmbeddedResource(
            type="resource",
            resource=types.TextResourceContents(
                uri=f"miaou://web-{kind}/{urllib.parse.quote(query)}",  # type: ignore[arg-type]
                mimeType="application/json",
                text=json.dumps(outcome, ensure_ascii=False),
            ),
        ),
        {"engine": outcome["engine"]},
        SEARCH_META_KEY,
    )


class WebConfigError(ValueError):
    """Config de mcp_web invalide hors du bloc `search` (cf. SearchConfigError)."""


class WebServer(MiaouMCPBase):
    def __init__(self, config: dict | None = None) -> None:
        super().__init__("miaou-web", default_port=8768, config=config)
        fetch = self.config.get("fetch", True)
        if not isinstance(fetch, bool):
            raise WebConfigError("config « fetch » : booléen attendu")
        self.fetch_enabled = fetch
        self.search_chain = build_chain(self.config)
        if not self.fetch_enabled and not self.search_chain.engines:
            # Une entrée active sans aucun outil est une config à corriger, à
            # dire au boot ; `"disabled": true` est le moyen de la couper.
            raise WebConfigError(
                "aucun outil : fetch et recherche désactivés par la config "
                "(« disabled »: true sur l'entrée pour la couper)"
            )
        if self.fetch_enabled:
            self._register_fetch_tools()
        self._register_search_tools()
        self.finalize_tools()

    def _register_fetch_tools(self) -> None:
        """fetch_url, fetch_read, fetch_list, fetch_resource : tous ou aucun
        (`config.fetch`), les trois derniers n'ayant de sens qu'avec le premier
        ou à côté de lui."""

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
        ) -> types.CallToolResult:
            # Retour CallToolResult, seule forme par laquelle MCPServer laisse un
            # outil poser le `_meta` de son résultat. Effet de bord assumé :
            # plus d'outputSchema ni de structuredContent, qui recopiait le
            # contenu entier sur le fil (et que le proxy ne publie pas).
            fetched = await _guarded_fetch(url, max_bytes, _DEFAULT_MAX_BYTES)
            if isinstance(fetched, str):
                return _tool_result(fetched)
            content_type, raw, truncated, final_url = fetched
            max_bytes = min(max_bytes, _DEFAULT_MAX_BYTES)

            mime = content_type.split(";")[0].strip().lower()
            charset = _charset_from_content_type(content_type)
            truncation_note = f"\n\n[Tronqué à {max_bytes} octets]" if truncated else ""

            if mime == "text/html":
                try:
                    html_text = raw.decode(charset, errors="replace")
                except LookupError:
                    html_text = raw.decode("utf-8", errors="replace")
                head = await asyncio.to_thread(extract_head_meta, html_text)
                # Favicon sondée PENDANT la conversion html2text, pas après :
                # sur une grosse page, la sonde ne rallonge alors rien.
                text, favicon = await asyncio.gather(
                    asyncio.to_thread(
                        _render_html_blocking, url, html_text, truncation_note
                    ),
                    _favicon_for(final_url or url, head["icons"]),
                )
                return _tool_result(
                    types.EmbeddedResource(
                        type="resource",
                        resource=types.TextResourceContents(
                            uri=url,  # type: ignore[arg-type]
                            mimeType="text/plain",
                            text=text,
                        ),
                    ),
                    build_web_meta(canonical_url=final_url, head=head, favicon=favicon),
                )
            elif _is_textual_mime(mime):
                try:
                    text = raw.decode(charset, errors="replace")
                except LookupError:
                    text = raw.decode("utf-8", errors="replace")
                full_text = text + truncation_note
                return _tool_result(
                    types.EmbeddedResource(
                        type="resource",
                        resource=types.TextResourceContents(
                            uri=url,  # type: ignore[arg-type]
                            mimeType=mime,
                            text=await asyncio.to_thread(_cache_and_cap, url, full_text, purge_html=True),
                        ),
                    ),
                    build_web_meta(canonical_url=final_url),
                )
            else:
                await asyncio.to_thread(web_cache.purge, url)
                return _tool_result(
                    types.EmbeddedResource(
                        type="resource",
                        resource=types.BlobResourceContents(
                            uri=url,  # type: ignore[arg-type]
                            mimeType=mime,
                            blob=base64.b64encode(raw).decode(),
                        ),
                    ),
                    build_web_meta(canonical_url=final_url),
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
            content_type, raw, truncated, _final = fetched
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

    def _register_search_tools(self) -> None:
        """`search` si la chaîne a au moins un moteur web, `image_search` si
        l'un d'eux sait chercher des images — décidé ICI, sur la config, jamais
        sur les pannes du moment : la liste d'outils ne bouge pas en cours de
        session. Un outil visible est un outil configuré."""
        chain = self.search_chain
        web_names = chain.names("web")
        image_names = chain.names("images")
        fallback_doc = (
            "Moteurs, essayés dans cet ordre : {order}. Un moteur en échec ou en pause fait "
            "passer au suivant ; `engine` nomme celui qui a répondu, `fallback` (présent "
            "seulement s'il y en a) liste les moteurs écartés et pourquoi, et un message les "
            "énumère si aucun n'a répondu. Un résultat vide est une réponse : il ne fait pas "
            "passer au moteur suivant."
        )

        if web_names:
            async def search(
                query: str,
                max_results: Annotated[
                    int,
                    Field(description=f"Nombre maximal de résultats, silencieusement ramené dans [1, {MAX_RESULTS}]."),
                ] = 5,
            ) -> types.CallToolResult:
                n = max(1, min(max_results, MAX_RESULTS))
                return _search_result(query, "search", await chain.run("web", query, n))

            doc = (
                f"Recherche web. Renvoie un objet JSON {{engine, results: [{{title, url, snippet}}], "
                f"fallback?}} ; snippet est un extrait plafonné à {SNIPPET_MAX_CHARS} caractères"
                + (" — lire la page avec fetch_url" if self.fetch_enabled else "")
                + f". max_results borné à [1, {MAX_RESULTS}]. "
                + fallback_doc.format(order=" → ".join(web_names))
            )
            if "ddg" in web_names:
                doc += (
                    f" Le moteur ddg espace ses requêtes d'au moins {search_ddg._MIN_INTERVAL_S:.0f} s : "
                    "lancer les recherches une par une plutôt qu'en rafale."
                )
            search.__doc__ = doc
            self.mcp.tool(name="search")(search)

        if image_names:
            async def image_search(
                query: str,
                max_results: Annotated[
                    int,
                    Field(description=f"Nombre maximal de résultats, silencieusement ramené dans [1, {MAX_RESULTS}]."),
                ] = 5,
            ) -> types.CallToolResult:
                n = max(1, min(max_results, MAX_RESULTS))
                return _search_result(query, "image_search", await chain.run("images", query, n))

            image_search.__doc__ = (
                f"Recherche d'images. Renvoie un objet JSON {{engine, results: [{{title, page_url, "
                f"image_url, thumbnail_url, source}}], fallback?}} — index d'URLs seulement, pas les "
                f"données binaires. max_results borné à [1, {MAX_RESULTS}]. "
                + fallback_doc.format(order=" → ".join(image_names))
            )
            self.mcp.tool(name="image_search")(image_search)

    def announce_search(self) -> None:
        """Ligne de démarrage : chaîne de moteurs active et moteurs écartés,
        et les fetch_* s'ils sont coupés par la config."""
        line = self.search_chain.summary()
        if not self.fetch_enabled:
            line += "; fetch_* désactivés (config fetch: false)"
        print(f"{self.mcp.name} : {line}", file=sys.stderr)


def build(config: dict | None = None) -> MCPServer:
    """Factory appelée par InProcessUpstream.start() du proxy : une instance
    par entrée config.json, chacune avec sa config `search` (clefs, ordre) et
    `fetch`. Lève SearchConfigError sur une config `search` invalide,
    WebConfigError sur une clé `fetch` qui n'est pas un booléen ou si fetch
    et recherche sont tous deux coupés."""
    web = WebServer(config)
    web.announce_search()
    return web.mcp


# Singleton de compatibilité (import direct, mode standalone, tests) : config
# vide, donc ordre par défaut et clefs lues dans l'environnement.
server = WebServer()
mcp = server.mcp  # exposé pour le proxy in-process

if __name__ == "__main__":
    server.announce_search()
    server.main()
