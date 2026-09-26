"""Métadonnées de page pour `fetch_url`, adressées au CLIENT et jamais au modèle.

`fetch_url` rend au modèle le texte de la page ; à côté, dans le `_meta` du
`CallToolResult` (clé `META_KEY`), il pose ce qu'un client affiche pour citer la
source : titre, nom de site, URL finale après redirections, favicon. Rien de
tout cela n'entre dans `content` — le modèle cite une URL, il n'a pas besoin du
titre, et les octets d'une favicon y seraient payés à chaque tour.

Contrat (tous les champs facultatifs, clé absente plutôt que vide) :
    {"title": str, "site_name": str, "canonical_url": str, "favicon": str}
`favicon` est une data-URL base64 dont le type a été reconnu AUX OCTETS (PNG,
ICO, GIF, JPEG, WebP ; SVG refusé : il porte du script), plafonnée à
`FAVICON_MAX_CHARS` caractères encodés — au-delà, absente. Un ICO à plusieurs
images est réduit à UNE (`shrink_ico`) : la plus petite d'au moins
`FAVICON_TARGET_PX`, pour rester nette en haute densité sans porter les 48 ou
256 px dont personne n'a l'usage.

La favicon coûte une ou plusieurs requêtes : elle est cherchée une fois par
origine et gardée en mémoire (`_FaviconCache`), échecs compris, pour que lire
dix pages d'un même site ne sonde pas dix fois son `/favicon.ico`."""

from __future__ import annotations

import base64
import binascii
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from html.parser import HTMLParser

from mcp_base import make_opener

META_KEY = "miaou/web"
"""Clé du `_meta` de `fetch_url`. Préfixe `miaou/` pour la même raison que
`miaou/unauthorized_upstreams` côté proxy : `_meta` est un espace partagé, une
clé nue collisionnerait avec une extension future du SDK ou d'un agrégateur."""

FAVICON_MAX_CHARS = 16384
FAVICON_TARGET_PX = 32
"""Côté visé d'une favicon affichée à 16 px CSS : 32 px couvrent un écran de
densité 2 (Retina, 4K à l'échelle 200 %) ; au-delà, du poids sans gain visible."""
FAVICON_DOWNLOAD_MAX = 64 * 1024
"""Octets lus au plus. Distinct du plafond de sortie : un ICO multi-résolution
dépasse souvent ce dernier AVANT réduction (15 Ko pour docs.python.org, dont
1,1 Ko pour son image 16 px), il faut donc pouvoir le lire en entier."""
FAVICON_TIMEOUT_S = 3
FAVICON_MAX_ATTEMPTS = 3
FAVICON_CACHE_MAX = 256
TEXT_MAX_CHARS = 300
HEAD_SCAN_MAX_CHARS = 256 * 1024

_WHITESPACE_RE = re.compile(r"\s+")
_HEAD_END_RE = re.compile(r"</head\s*>|<body[\s>]", re.IGNORECASE)
_DATA_URL_RE = re.compile(r"^data:([^;,]*)(;[^,]*)?,(.*)$", re.IGNORECASE | re.DOTALL)


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    text = _WHITESPACE_RE.sub(" ", value).strip()
    if not text:
        return None
    return text[:TEXT_MAX_CHARS]


class _HeadParser(HTMLParser):
    """Lit `<title>`, les `<meta>` Open Graph utiles et les `<link rel=icon>`.

    Ne regarde que l'en-tête : l'appelant coupe le HTML à `</head>` (ou au
    premier `<body>`), et un `<title>` croisé dans un `<svg>` du corps n'est
    donc jamais pris pour celui de la page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.og_title: str | None = None
        self.site_name: str | None = None
        self.icons: list[tuple[str, str]] = []
        self._in_title = False
        self._title_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title" and self.title is None:
            self._in_title = True
            self._title_chunks = []
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").strip().lower()
            if key == "og:site_name" and self.site_name is None:
                self.site_name = _clean_text(a.get("content"))
            elif key == "og:title" and self.og_title is None:
                self.og_title = _clean_text(a.get("content"))
        elif tag == "link":
            rels = a.get("rel", "").lower().split()
            href = a.get("href", "").strip()
            # `apple-touch-icon` écarté : 180 px, il dépasse le plafond presque
            # toujours et coûterait une requête pour rien.
            if "icon" in rels and href:
                self.icons.append((href, a.get("type", "").strip().lower()))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            self.title = _clean_text("".join(self._title_chunks))

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_chunks.append(data)


def extract_head_meta(html_text: str) -> dict:
    """Rend `{"title", "site_name", "icons"}` depuis l'en-tête d'une page.

    `title` : `<title>`, sinon `og:title`. `icons` : liste ordonnée de
    `(href, type)` des `<link rel~=icon>`, href non résolu. Un HTML malformé
    rend ce qui a pu être lu, jamais d'exception."""
    head = html_text[:HEAD_SCAN_MAX_CHARS]
    end = _HEAD_END_RE.search(head)
    if end:
        head = head[: end.start()]
    parser = _HeadParser()
    try:
        parser.feed(head)
        parser.close()
    except Exception:  # noqa: BLE001 — html.parser tolère beaucoup, pas tout
        pass
    return {
        "title": parser.title or parser.og_title,
        "site_name": parser.site_name,
        "icons": parser.icons,
    }


def sniff_image_mime(data: bytes) -> str | None:
    """Type d'image reconnu aux octets, parmi ceux qu'un client affiche sans
    risque. Le `Content-Type` annoncé n'est jamais cru : un serveur sert
    volontiers une page d'erreur HTML en 200 sur `/favicon.ico`."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _ico_entries(data: bytes) -> list[tuple[int, int, int, int, int]] | None:
    """Entrées d'un ICO : (côté en px, profondeur, offset, taille, rang dans le
    répertoire), ou None si
    le répertoire est incohérent (entrée hors du fichier, compte nul)."""
    if len(data) < 6:
        return None
    count = int.from_bytes(data[4:6], "little")
    if count == 0 or len(data) < 6 + 16 * count:
        return None
    entries = []
    for i in range(count):
        e = data[6 + 16 * i : 22 + 16 * i]
        # Un octet de dimension à 0 vaut 256 (convention du format).
        side = max(e[0] or 256, e[1] or 256)
        bits = int.from_bytes(e[6:8], "little")
        size = int.from_bytes(e[8:12], "little")
        offset = int.from_bytes(e[12:16], "little")
        if size == 0 or offset < 6 + 16 * count or offset + size > len(data):
            return None
        entries.append((side, bits, offset, size, i))
    return entries


def _ico_preference(entries: list[tuple[int, int, int, int, int]]) -> list[tuple[int, int, int, int, int]]:
    """Ordre d'essai : d'abord les images d'au moins `FAVICON_TARGET_PX`, de la
    plus petite à la plus grande ; puis les plus petites, de la plus grande à
    la plus petite. À côté égal, la profondeur la plus grande d'abord."""
    at_least = sorted((e for e in entries if e[0] >= FAVICON_TARGET_PX), key=lambda e: (e[0], -e[1]))
    below = sorted((e for e in entries if e[0] < FAVICON_TARGET_PX), key=lambda e: (-e[0], -e[1]))
    return at_least + below


def shrink_ico(data: bytes, max_bytes: int) -> tuple[str, bytes] | None:
    """Réduit un ICO à la meilleure image qui tient dans `max_bytes`.

    Une image PNG embarquée (format des ICO récents) sort en `image/png` telle
    quelle ; une image BMP est ré-emballée dans un ICO d'une seule entrée, ses
    octets inchangés. None si le répertoire est illisible ou si aucune image
    ne tient."""
    entries = _ico_entries(data)
    if entries is None:
        return None
    for _side, _bits, offset, size, rank in _ico_preference(entries):
        image = data[offset : offset + size]
        if image.startswith(b"\x89PNG\r\n\x1a\n"):
            if size <= max_bytes:
                return "image/png", image
            continue
        wrapped_size = 22 + size
        if wrapped_size > max_bytes:
            continue
        # En-tête (réservé, type 1 = icône, 1 entrée) + entrée d'origine dont
        # seul l'offset change : l'image suit immédiatement le répertoire.
        entry = data[6 + 16 * rank : 6 + 16 * rank + 12]
        header = b"\x00\x00\x01\x00\x01\x00" + entry + (22).to_bytes(4, "little")
        return "image/x-icon", header + image
    return None


def favicon_data_url(data: bytes) -> str | None:
    """Data-URL base64 d'une favicon validée, ou None (type non reconnu, vide,
    ICO illisible, ou data-URL au-delà de `FAVICON_MAX_CHARS`)."""
    if not data:
        return None
    mime = sniff_image_mime(data)
    if mime is None:
        return None
    if mime == "image/x-icon":
        shrunk = shrink_ico(data, _max_raw_for(mime))
        if shrunk is None:
            return None
        mime, data = shrunk
    url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    if len(url) > FAVICON_MAX_CHARS:
        return None
    return url


def _max_raw_for(mime: str) -> int:
    """Octets bruts max pour que la data-URL de ce type tienne au plafond."""
    prefix = len(f"data:{mime};base64,")
    return ((FAVICON_MAX_CHARS - prefix) // 4) * 3


def decode_data_url(href: str) -> bytes | None:
    """Octets d'une data-URL base64 (favicon inline dans la page), None sinon.
    Le type déclaré est ignoré : `favicon_data_url` re-décide aux octets."""
    m = _DATA_URL_RE.match(href.strip())
    if not m or "base64" not in (m.group(2) or "").lower():
        return None
    payload = m.group(3)
    # Borne avant décodage : une data-URL de plusieurs Mo n'a rien à faire ici.
    if len(payload) > (FAVICON_DOWNLOAD_MAX * 4) // 3 + 4:
        return None
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None


def favicon_candidates(page_url: str, icons: list[tuple[str, str]]) -> list[str]:
    """Adresses à essayer, dans l'ordre : les `<link rel=icon>` de la page
    (résolus contre l'URL FINALE, SVG écarté par type ou extension), puis
    `/favicon.ico` de l'hôte. Dédoublonnées, bornées à `FAVICON_MAX_ATTEMPTS`,
    le repli `/favicon.ico` étant toujours gardé s'il n'est pas déjà listé."""
    out: list[str] = []
    for href, typ in icons:
        if typ == "image/svg+xml":
            continue
        if href.lower().startswith("data:"):
            out.append(href)
            continue
        absolute = urllib.parse.urljoin(page_url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme.lower() not in ("http", "https"):
            continue
        if parts.path.lower().endswith(".svg"):
            continue
        out.append(absolute)
    fallback = urllib.parse.urljoin(page_url, "/favicon.ico")
    unique = list(dict.fromkeys(out))
    if fallback in unique:
        return unique[:FAVICON_MAX_ATTEMPTS]
    return unique[: FAVICON_MAX_ATTEMPTS - 1] + [fallback]


def origin_of(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _fetch_favicon_bytes(url: str, user_agent: str) -> bytes | None:
    """I/O bloquante (pour asyncio.to_thread). None sur toute erreur ou au-delà
    du plafond : une favicon ne fait jamais échouer un `fetch_url`."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with make_opener().open(req, timeout=FAVICON_TIMEOUT_S) as resp:
            data = resp.read(FAVICON_DOWNLOAD_MAX + 1)
    except urllib.error.HTTPError as e:
        # Une HTTPError porte la réponse ouverte : la fermer, sinon chaque
        # `/favicon.ico` en 404 laisse une connexion au GC.
        e.close()
        return None
    except Exception:  # noqa: BLE001 — best-effort par contrat
        return None
    if not isinstance(data, bytes) or len(data) > FAVICON_DOWNLOAD_MAX:
        return None
    return data


def resolve_favicon_blocking(page_url: str, icons: list[tuple[str, str]], user_agent: str) -> str | None:
    """Premier candidat qui donne une favicon valide, ou None."""
    for candidate in favicon_candidates(page_url, icons):
        if candidate.lower().startswith("data:"):
            data = decode_data_url(candidate)
        else:
            data = _fetch_favicon_bytes(candidate, user_agent)
        if data is None:
            continue
        url = favicon_data_url(data)
        if url is not None:
            return url
    return None


class _FaviconCache:
    """Favicon par origine, en mémoire, bornée en taille et en âge.

    Un échec est mis en cache comme un succès (valeur None) : c'est le cas le
    plus coûteux à re-sonder, puisqu'il épuise tous les candidats. Durée de vie
    alignée sur le cache disque des pages (`MIAOU_WEB_CACHE_TTL_H`)."""

    def __init__(self, max_entries: int, ttl_s: float) -> None:
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self._entries: OrderedDict[str, tuple[float, str | None]] = OrderedDict()

    def get(self, origin: str) -> tuple[bool, str | None]:
        """(trouvé, valeur) — `trouvé` distingue « pas de favicon » de « pas encore cherché »."""
        hit = self._entries.get(origin)
        if hit is None:
            return False, None
        stored_at, value = hit
        if time.time() - stored_at > self.ttl_s:
            del self._entries[origin]
            return False, None
        return True, value

    def put(self, origin: str, value: str | None) -> None:
        self._entries[origin] = (time.time(), value)
        self._entries.move_to_end(origin)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


def build_web_meta(
    *,
    canonical_url: str | None,
    head: dict | None = None,
    favicon: str | None = None,
) -> dict:
    """Assemble le contrat `META_KEY`, clés absentes plutôt que vides."""
    meta: dict[str, str] = {}
    if head:
        if head.get("title"):
            meta["title"] = head["title"]
        if head.get("site_name"):
            meta["site_name"] = head["site_name"]
    if canonical_url:
        meta["canonical_url"] = canonical_url
    if favicon:
        meta["favicon"] = favicon
    return meta
