"""Tests du `_meta` de fetch_url (servers/mcp_web/pagemeta.py) — lot AI.

Contrat adressé au client, hors modèle : `_meta["miaou/web"] = {title,
site_name, canonical_url, favicon}`, tous facultatifs. Aucun appel réseau : les
réponses sont routées par URL sur un `OpenerDirector.open` patché."""
import base64
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from mcp import types

import mcp_web
from mcp_web import cache as mcp_web_cache
from mcp_web import pagemeta
from mcp_web.pagemeta import (
    FAVICON_MAX_CHARS,
    META_KEY,
    _FaviconCache,
    decode_data_url,
    extract_head_meta,
    favicon_candidates,
    favicon_data_url,
    shrink_ico,
    sniff_image_mime,
)

_TM = mcp_web.server.mcp._tool_manager

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
ICO = (
    b"\x00\x00\x01\x00\x01\x00"
    + b"\x10\x10\x00\x00\x01\x00\x20\x00" + (40).to_bytes(4, "little") + (22).to_bytes(4, "little")
    + b"\x01" * 40
)
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_web_cache, "WORKDIR", tmp_path)
    mcp_web.favicon_cache.clear()


def _resp(body: bytes, content_type: str, final_url: str | None = None):
    headers = MagicMock()
    headers.get.side_effect = lambda k, d="": content_type if k == "Content-Type" else d
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read.side_effect = lambda n=-1: body if n < 0 else body[:n]
    mock.headers = headers
    mock.geturl.return_value = final_url
    return mock


class _Router:
    """Rend une réponse par URL demandée, 404 sinon ; compte les requêtes."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requested: list[str] = []

    def __call__(self, req, *args, **kwargs):
        import urllib.error

        url = req.full_url
        self.requested.append(url)
        if url not in self.routes:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return self.routes[url]


def _page(head: str = "", body: str = "<p>Corps</p>") -> bytes:
    return f"<html><head>{head}</head><body>{body}</body></html>".encode()


async def _call(url: str, router: _Router) -> types.CallToolResult:
    with patch("urllib.request.OpenerDirector.open", side_effect=router):
        result = await _TM.call_tool("fetch_url", {"url": url})
    assert isinstance(result, types.CallToolResult)
    return result


# ---------------------------------------------------------------------------
# Extraction de l'en-tête
# ---------------------------------------------------------------------------

def test_head_title_and_site_name():
    meta = extract_head_meta(
        '<html><head><title>  Une   page &amp; plus\n</title>'
        '<meta property="og:site_name" content="Le Site"></head></html>'
    )
    assert meta["title"] == "Une page & plus"
    assert meta["site_name"] == "Le Site"


def test_head_og_title_is_fallback_only():
    meta = extract_head_meta('<head><meta property="og:title" content="OG"></head>')
    assert meta["title"] == "OG"
    meta = extract_head_meta(
        '<head><meta property="og:title" content="OG"><title>Vrai</title></head>'
    )
    assert meta["title"] == "Vrai"


def test_head_ignores_title_inside_body():
    """Un <title> de <svg> dans le corps n'est pas le titre de la page."""
    meta = extract_head_meta("<html><head></head><body><svg><title>Icône</title></svg></body>")
    assert meta["title"] is None


def test_head_without_head_tag_stops_at_body():
    meta = extract_head_meta("<title>Haut</title><body><title>Bas</title></body>")
    assert meta["title"] == "Haut"


def test_head_text_is_capped():
    meta = extract_head_meta(f"<head><title>{'x' * 5000}</title></head>")
    assert len(meta["title"]) == pagemeta.TEXT_MAX_CHARS


def test_head_collects_icons_in_order_without_apple_touch():
    meta = extract_head_meta(
        '<head><link rel="apple-touch-icon" href="/big.png">'
        '<link rel="shortcut icon" href="/a.ico">'
        '<link rel="icon" type="image/svg+xml" href="/b.svg"></head>'
    )
    assert meta["icons"] == [("/a.ico", ""), ("/b.svg", "image/svg+xml")]


def test_head_malformed_html_does_not_raise():
    meta = extract_head_meta("<head><title>ok</title><meta property=")
    assert meta["title"] == "ok"


# ---------------------------------------------------------------------------
# Validation de la favicon
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "data,mime",
    [
        (PNG, "image/png"),
        (ICO, "image/x-icon"),
        (b"GIF89a" + b"\x00" * 10, "image/gif"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 10, "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
    ],
)
def test_sniff_accepts_known_images(data, mime):
    assert sniff_image_mime(data) == mime


@pytest.mark.parametrize("data", [SVG, b"<html>404</html>", b"", b"RIFF\x00\x00\x00\x00WAVE"])
def test_sniff_refuses_everything_else(data):
    assert sniff_image_mime(data) is None


def test_data_url_is_typed_from_bytes():
    assert favicon_data_url(PNG) == "data:image/png;base64," + base64.b64encode(PNG).decode()


def _ico(images: list[tuple[int, int, bytes]]) -> bytes:
    """ICO construit à la main : (côté, profondeur, octets de l'image)."""
    head = b"\x00\x00\x01\x00" + len(images).to_bytes(2, "little")
    offset = 6 + 16 * len(images)
    directory, blobs = b"", b""
    for side, bits, blob in images:
        dim = bytes([side % 256])
        directory += (
            dim + dim + b"\x00\x00" + (1).to_bytes(2, "little") + bits.to_bytes(2, "little")
            + len(blob).to_bytes(4, "little") + offset.to_bytes(4, "little")
        )
        blobs += blob
        offset += len(blob)
    return head + directory + blobs


def _bmp(tag: int, n: int) -> bytes:
    return bytes([tag]) * n


def test_ico_keeps_smallest_image_at_least_target():
    ico = _ico([(16, 32, _bmp(1, 100)), (32, 32, _bmp(2, 200)), (48, 32, _bmp(3, 300))])
    mime, data = shrink_ico(ico, 10_000)
    assert mime == "image/x-icon"
    assert data[4:6] == b"\x01\x00"          # une seule entrée
    assert data[6] == 32                       # celle de 32 px
    assert int.from_bytes(data[18:22], "little") == 22
    assert data[22:] == _bmp(2, 200)           # octets de l'image inchangés
    assert sniff_image_mime(data) == "image/x-icon"


def test_ico_prefers_depth_at_equal_size_and_256_is_encoded_as_zero():
    ico = _ico([(256, 32, _bmp(9, 50)), (64, 8, _bmp(4, 10)), (64, 32, _bmp(5, 20))])
    _, data = shrink_ico(ico, 10_000)
    assert data[22:] == _bmp(5, 20)
    ico = _ico([(256, 32, _bmp(9, 50)), (16, 32, _bmp(1, 10))])
    _, data = shrink_ico(ico, 10_000)
    assert data[6] == 0 and data[22:] == _bmp(9, 50)   # 256 px, seul >= cible


def test_ico_falls_back_below_target_then_to_what_fits():
    ico = _ico([(16, 32, _bmp(1, 100)), (24, 32, _bmp(2, 150))])
    _, data = shrink_ico(ico, 10_000)
    assert data[22:] == _bmp(2, 150)          # rien >= 32 : la plus grande en dessous
    ico = _ico([(16, 32, _bmp(1, 100)), (32, 32, _bmp(2, 5000))])
    _, data = shrink_ico(ico, 1000)
    assert data[22:] == _bmp(1, 100)          # la 32 px ne tient pas : repli sur 16
    assert shrink_ico(ico, 50) is None


def test_ico_embedded_png_comes_out_as_png():
    ico = _ico([(16, 32, _bmp(1, 100)), (32, 32, PNG)])
    assert shrink_ico(ico, 10_000) == ("image/png", PNG)
    assert favicon_data_url(ico) == favicon_data_url(PNG)


def test_ico_with_broken_directory_is_refused():
    ico = _ico([(32, 32, _bmp(2, 200))])
    broken = ico[:18] + (10_000).to_bytes(4, "little") + ico[22:]   # offset hors fichier
    assert shrink_ico(broken, 10_000) is None
    assert favicon_data_url(broken) is None
    assert favicon_data_url(b"\x00\x00\x01\x00\x00\x00") is None  # zéro entrée


def test_multi_resolution_ico_over_cap_is_reduced_under_it():
    """Le cas docs.python.org : l'ICO entier dépasse le plafond, l'image
    retenue le tient."""
    ico = _ico([(16, 32, _bmp(1, 1100)), (32, 32, _bmp(2, 4200)), (48, 32, _bmp(3, 9600))])
    assert len(ico) * 4 // 3 > FAVICON_MAX_CHARS
    url = favicon_data_url(ico)
    assert url is not None and len(url) <= FAVICON_MAX_CHARS
    assert base64.b64decode(url.split(",", 1)[1])[22:] == _bmp(2, 4200)


def test_data_url_absent_beyond_cap():
    prefix = len("data:image/png;base64,")
    raw_fits = ((FAVICON_MAX_CHARS - prefix) // 4) * 3
    fits = PNG + b"\x00" * (raw_fits - len(PNG))
    assert len(favicon_data_url(fits)) <= FAVICON_MAX_CHARS
    assert favicon_data_url(fits + b"\x00" * 3) is None


def test_svg_refused_even_as_inline_data_url():
    href = "data:image/svg+xml;base64," + base64.b64encode(SVG).decode()
    data = decode_data_url(href)
    assert data == SVG
    assert favicon_data_url(data) is None


def test_decode_data_url_requires_base64():
    assert decode_data_url("data:image/png,rawbytes") is None
    assert decode_data_url("https://example.com/x.png") is None


# ---------------------------------------------------------------------------
# Candidats
# ---------------------------------------------------------------------------

def test_candidates_resolve_against_final_url_then_fallback():
    got = favicon_candidates("https://www.site.test/a/page", [("icons/f.png", "")])
    assert got == ["https://www.site.test/a/icons/f.png", "https://www.site.test/favicon.ico"]


def test_candidates_skip_svg_and_foreign_schemes():
    got = favicon_candidates(
        "https://s.test/",
        [("/i.svg", ""), ("/j.png", "image/svg+xml"), ("javascript:alert(1)", "")],
    )
    assert got == ["https://s.test/favicon.ico"]


def test_candidates_are_bounded_and_keep_the_fallback():
    icons = [(f"/{i}.png", "") for i in range(10)]
    got = favicon_candidates("https://s.test/", icons)
    assert len(got) == pagemeta.FAVICON_MAX_ATTEMPTS
    assert got[-1] == "https://s.test/favicon.ico"


def test_candidates_do_not_duplicate_declared_favicon_ico():
    got = favicon_candidates("https://s.test/", [("/favicon.ico", "")])
    assert got == ["https://s.test/favicon.ico"]


# ---------------------------------------------------------------------------
# Cache par origine
# ---------------------------------------------------------------------------

def test_cache_distinguishes_miss_from_cached_failure():
    c = _FaviconCache(max_entries=4, ttl_s=60)
    assert c.get("https://a") == (False, None)
    c.put("https://a", None)
    assert c.get("https://a") == (True, None)


def test_cache_expires(monkeypatch):
    c = _FaviconCache(max_entries=4, ttl_s=60)
    now = [1000.0]
    monkeypatch.setattr(pagemeta.time, "time", lambda: now[0])
    c.put("https://a", "data:x")
    now[0] += 61
    assert c.get("https://a") == (False, None)


def test_cache_is_bounded_oldest_first():
    c = _FaviconCache(max_entries=2, ttl_s=60)
    for o in ("https://a", "https://b", "https://c"):
        c.put(o, o)
    assert c.get("https://a") == (False, None)
    assert c.get("https://c") == (True, "https://c")


# ---------------------------------------------------------------------------
# fetch_url de bout en bout
# ---------------------------------------------------------------------------

async def test_fetch_url_publishes_meta_without_touching_content():
    head = (
        "<title>Titre de page</title>"
        '<meta property="og:site_name" content="Nom du site">'
        '<link rel="icon" href="/static/f.png">'
    )
    router = _Router({
        "https://s.test/p": _resp(_page(head), "text/html; charset=utf-8", "https://s.test/final"),
        "https://s.test/static/f.png": _resp(PNG, "image/png"),
    })
    result = await _call("https://s.test/p", router)

    meta = result.meta[META_KEY]
    assert meta == {
        "title": "Titre de page",
        "site_name": "Nom du site",
        "canonical_url": "https://s.test/final",
        "favicon": favicon_data_url(PNG),
    }
    # Le modèle ne reçoit que le texte de la page : ni nom de site, ni octets.
    (block,) = result.content
    assert "Corps" in block.resource.text
    assert "Nom du site" not in block.resource.text
    assert "base64" not in block.resource.text
    assert result.isError is False


async def test_meta_reaches_the_wire_under_its_alias():
    router = _Router({"https://s.test/": _resp(_page("<title>T</title>"), "text/html", "https://s.test/")})
    result = await _call("https://s.test/", router)
    wire = json.loads(result.model_dump_json(by_alias=True, exclude_none=True))
    assert wire["_meta"][META_KEY]["title"] == "T"


async def test_favicon_falls_back_to_favicon_ico_and_skips_svg_link():
    router = _Router({
        "https://s.test/": _resp(
            _page('<link rel="icon" href="/f.svg">'), "text/html", "https://s.test/"
        ),
        "https://s.test/favicon.ico": _resp(ICO, "image/vnd.microsoft.icon"),
    })
    result = await _call("https://s.test/", router)
    assert result.meta[META_KEY]["favicon"].startswith("data:image/x-icon;base64,")
    assert "https://s.test/f.svg" not in router.requested


async def test_favicon_absent_when_too_big_or_not_an_image():
    big = PNG + b"\x00" * FAVICON_MAX_CHARS
    router = _Router({
        "https://s.test/": _resp(
            _page('<link rel="icon" href="/big.png">'), "text/html", "https://s.test/"
        ),
        "https://s.test/big.png": _resp(big, "image/png"),
        # Page d'erreur servie en 200 sous un Content-Type d'image : refusée aux octets.
        "https://s.test/favicon.ico": _resp(b"<html>nope</html>", "image/x-icon"),
    })
    result = await _call("https://s.test/", router)
    assert "favicon" not in result.meta[META_KEY]


async def test_favicon_is_probed_once_per_origin():
    router = _Router({
        "https://s.test/a": _resp(_page(), "text/html", "https://s.test/a"),
        "https://s.test/b": _resp(_page(), "text/html", "https://s.test/b"),
    })
    await _call("https://s.test/a", router)
    await _call("https://s.test/b", router)
    # Échec (404) mis en cache lui aussi : une seule sonde pour les deux pages.
    assert router.requested.count("https://s.test/favicon.ico") == 1


async def test_favicon_origin_is_the_final_one_after_redirect():
    router = _Router({
        "http://old.test/": _resp(_page(), "text/html", "https://new.test/"),
        "https://new.test/favicon.ico": _resp(PNG, "image/png"),
    })
    result = await _call("http://old.test/", router)
    assert result.meta[META_KEY]["canonical_url"] == "https://new.test/"
    assert "favicon" in result.meta[META_KEY]
    assert "http://old.test/favicon.ico" not in router.requested


async def test_inline_data_favicon_needs_no_request():
    href = "data:image/png;base64," + base64.b64encode(PNG).decode()
    router = _Router({
        "https://s.test/": _resp(_page(f'<link rel="icon" href="{href}">'), "text/html", "https://s.test/"),
    })
    result = await _call("https://s.test/", router)
    assert result.meta[META_KEY]["favicon"] == favicon_data_url(PNG)
    assert router.requested == ["https://s.test/"]


async def test_non_html_carries_canonical_url_only():
    router = _Router({
        "https://s.test/d.json": _resp(b'{"a": 1}', "application/json", "https://s.test/d2.json"),
    })
    result = await _call("https://s.test/d.json", router)
    assert result.meta == {META_KEY: {"canonical_url": "https://s.test/d2.json"}}
    assert router.requested == ["https://s.test/d.json"]


async def test_error_result_has_no_meta():
    result = await _call("https://s.test/missing", _Router({}))
    assert result.meta is None
    assert "404" in result.content[0].text
    assert result.isError is False


async def test_final_url_outside_http_is_dropped():
    router = _Router({"https://s.test/": _resp(_page(), "text/html", "file:///etc/passwd")})
    result = await _call("https://s.test/", router)
    assert "canonical_url" not in (result.meta or {}).get(META_KEY, {})


def test_fetch_url_no_longer_publishes_an_output_schema():
    """Effet de bord assumé du retour CallToolResult : plus de structuredContent
    qui recopiait tout le contenu sur le fil."""
    tool = next(t for t in _TM.list_tools() if t.name == "fetch_url")
    assert tool.fn_metadata.output_schema is None
