"""Tests unitaires pour servers/mcp_web/ (package)."""
import base64
import os
import sys
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from mcp import types
from mcp_web import server as fetch_server
from mcp_web import cache as mcp_web_cache
from mcp_web import _is_textual_mime

_TM = fetch_server.mcp._tool_manager


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_web_cache, "WORKDIR", tmp_path)


def _make_mock_resp(body: bytes, content_type: str = "text/html; charset=utf-8"):
    headers = MagicMock()
    headers.get.side_effect = (
        lambda key, default="": content_type if key == "Content-Type" else default
    )
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read.return_value = body
    mock.headers = headers
    return mock


@pytest.mark.asyncio
async def test_fetch_html_returns_text_resource():
    html = b"<html><body><p>Hello world</p></body></html>"
    mock_resp = _make_mock_resp(html, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com"})
    assert isinstance(result, types.EmbeddedResource)
    assert result.resource.mimeType == "text/plain"
    assert "Hello world" in result.resource.text


@pytest.mark.asyncio
async def test_fetch_html_strips_scripts():
    html = b"<html><body><script>alert(1)</script><p>Contenu</p></body></html>"
    mock_resp = _make_mock_resp(html)
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com"})
    assert "alert" not in result.resource.text
    assert "Contenu" in result.resource.text


@pytest.mark.asyncio
async def test_fetch_text_plain_returns_raw():
    body = b"texte brut ici"
    mock_resp = _make_mock_resp(body, "text/plain; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com/data.txt"})
    assert isinstance(result, types.EmbeddedResource)
    assert result.resource.mimeType == "text/plain"
    assert "texte brut ici" in result.resource.text


@pytest.mark.asyncio
async def test_fetch_json_preserves_mime():
    body = b'{"ok": true}'
    mock_resp = _make_mock_resp(body, "application/json")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://api.example.com/data"})
    assert isinstance(result, types.EmbeddedResource)
    assert isinstance(result.resource, types.TextResourceContents)
    assert result.resource.mimeType == "application/json"
    assert '{"ok": true}' in result.resource.text


@pytest.mark.asyncio
async def test_fetch_json_suffix_mime_returns_text():
    body = b'{"data": []}'
    mock_resp = _make_mock_resp(body, "application/vnd.api+json")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://api.example.com/vnd"})
    assert isinstance(result.resource, types.TextResourceContents)
    assert result.resource.mimeType == "application/vnd.api+json"
    assert '{"data": []}' in result.resource.text


@pytest.mark.asyncio
async def test_fetch_xml_returns_text():
    body = b"<root><item>1</item></root>"
    mock_resp = _make_mock_resp(body, "application/xml")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com/data.xml"})
    assert isinstance(result.resource, types.TextResourceContents)
    assert result.resource.mimeType == "application/xml"
    assert "<item>1</item>" in result.resource.text


@pytest.mark.asyncio
async def test_fetch_svg_xml_suffix_returns_text_not_blob():
    body = b"<svg><circle r='5'/></svg>"
    mock_resp = _make_mock_resp(body, "image/svg+xml")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com/icon.svg"})
    assert isinstance(result.resource, types.TextResourceContents)
    assert result.resource.mimeType == "image/svg+xml"
    assert "<circle" in result.resource.text


@pytest.mark.parametrize(
    "mime,expected",
    [
        ("text/plain", True),
        ("application/json", True),
        ("application/ld+json", True),
        ("application/vnd.api+json", True),
        ("image/svg+xml", True),
        ("image/png", False),
        ("application/octet-stream", False),
        ("application/pdf", False),
    ],
)
def test_is_textual_mime(mime, expected):
    assert _is_textual_mime(mime) is expected


@pytest.mark.asyncio
async def test_fetch_binary_returns_blob():
    body = bytes(range(256))
    mock_resp = _make_mock_resp(body, "image/png")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com/image.png"})
    assert isinstance(result, types.EmbeddedResource)
    assert isinstance(result.resource, types.BlobResourceContents)
    assert result.resource.mimeType == "image/png"
    assert base64.b64decode(result.resource.blob) == body


@pytest.mark.asyncio
async def test_fetch_truncation_adds_note():
    body = b"A" * 20
    mock_resp = _make_mock_resp(body, "text/plain")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com", "max_bytes": 10})
    assert "Tronqué" in result.resource.text
    assert "10" in result.resource.text


@pytest.mark.asyncio
async def test_fetch_http_error_returns_string():
    err = urllib.error.HTTPError("http://example.com", 404, "Not Found", {}, None)
    with patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com/missing"})
    assert isinstance(result, str)
    assert "404" in result


@pytest.mark.asyncio
async def test_fetch_url_error_returns_string():
    err = urllib.error.URLError("Connection refused")
    with patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool("fetch_url", {"url": "http://unreachable.local"})
    assert isinstance(result, str)
    assert "réseau" in result.lower() or "Connection refused" in result


def test_tool_list_contains_fetch_url():
    names = {t.name for t in _TM.list_tools()}
    assert "fetch_url" in names
    assert "fetch_read" in names
    assert "fetch_list" in names


@pytest.mark.asyncio
async def test_fetch_url_caps_output_and_notes_pagination(monkeypatch):
    monkeypatch.setattr(mcp_web_cache, "READ_CAP", 10)
    body = b"A" * 30
    mock_resp = _make_mock_resp(body, "text/plain")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_url", {"url": "http://example.com/big"})
    assert len(result.resource.text) > 10
    assert result.resource.text.startswith("A" * 10)
    assert "fetch_read" in result.resource.text
    assert "char_start=10" in result.resource.text


@pytest.mark.asyncio
async def test_fetch_read_paginates_cached_content(monkeypatch):
    monkeypatch.setattr(mcp_web_cache, "READ_CAP", 10)
    body = b"0123456789ABCDEFGHIJ"
    mock_resp = _make_mock_resp(body, "text/plain")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/paged"})

    result = await _TM.call_tool(
        "fetch_read", {"url": "http://example.com/paged", "char_start": 10}
    )
    assert result.startswith("ABCDEFGHIJ")
    assert "fetch_read" not in result  # dernière page, pas de notice de suite


@pytest.mark.asyncio
async def test_fetch_read_caps_output_even_with_large_char_end(monkeypatch):
    monkeypatch.setattr(mcp_web_cache, "READ_CAP", 10)
    body = b"0123456789ABCDEFGHIJKLMNOPQRST"  # 30 caractères
    mock_resp = _make_mock_resp(body, "text/plain")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/verybig"})

    result = await _TM.call_tool(
        "fetch_read",
        {"url": "http://example.com/verybig", "char_start": 0, "char_end": 30},
    )
    assert result.startswith("0123456789")
    assert len(result.split("\n\n[")[0]) == 10  # extrait borné au cap, pas aux 30 demandés
    assert "char_start=10" in result


@pytest.mark.asyncio
async def test_fetch_read_unknown_url_returns_clear_message():
    result = await _TM.call_tool("fetch_read", {"url": "http://never-fetched.example.com"})
    assert isinstance(result, str)
    assert "fetch_url" in result


@pytest.mark.asyncio
async def test_fetch_read_rejects_invalid_range():
    body = b"hello"
    mock_resp = _make_mock_resp(body, "text/plain")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/small"})

    result = await _TM.call_tool(
        "fetch_read", {"url": "http://example.com/small", "char_start": -1}
    )
    assert "char_start" in result


_STRUCTURED_HTML = b"""
<html><body>
<h1>Titre principal</h1>
<p>Intro <a href="/a">Lien A</a></p>
<h2>Sous-section</h2>
<a href="https://ext.example.com/b">Lien B</a>
<a href="/empty"></a>
</body></html>
"""


@pytest.mark.asyncio
async def test_fetch_list_extracts_headings_and_links():
    mock_resp = _make_mock_resp(_STRUCTURED_HTML, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/page"})

    result = await _TM.call_tool("fetch_list", {"url": "http://example.com/page"})
    assert "# Titre principal" in result
    assert "## Sous-section" in result
    assert "[Lien A](/a)" in result
    assert "[Lien B](https://ext.example.com/b)" in result
    assert "/empty" not in result  # lien sans texte, ignoré


@pytest.mark.asyncio
async def test_fetch_list_paginates_and_caches_structure(monkeypatch):
    monkeypatch.setattr(mcp_web_cache, "LIST_CAP", 2)
    mock_resp = _make_mock_resp(_STRUCTURED_HTML, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/paged-list"})

    first = await _TM.call_tool("fetch_list", {"url": "http://example.com/paged-list"})
    assert "entry_start=2" in first

    # Le second appel doit relire la structure déjà mise en cache sans reparser
    # le HTML (extract_structure non ré-invoqué) — le nom est importé localement
    # dans mcp_web/__init__.py (from .structure import extract_structure), on
    # patche donc la référence du module package, pas mcp_web.structure.
    import mcp_web

    with patch.object(mcp_web, "extract_structure") as mock_extract:
        second = await _TM.call_tool(
            "fetch_list", {"url": "http://example.com/paged-list", "entry_start": 2}
        )
    mock_extract.assert_not_called()
    assert "Sous-section" in second or "Lien" in second


@pytest.mark.asyncio
async def test_fetch_list_recovers_from_corrupted_structure_cache():
    """WEB7 : un .json corrompu (troncature disque, édition manuelle) doit être
    traité comme un cache absent, pas fuiter json.JSONDecodeError — fetch_list
    ré-extrait la structure depuis le .html déjà en cache."""
    mock_resp = _make_mock_resp(_STRUCTURED_HTML, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/corrupt-structure"})

    mcp_web_cache.structure_path("http://example.com/corrupt-structure").write_text(
        "{not valid json", encoding="utf-8"
    )

    result = await _TM.call_tool("fetch_list", {"url": "http://example.com/corrupt-structure"})
    assert "Titre principal" in result


@pytest.mark.asyncio
async def test_fetch_list_unknown_url_returns_clear_message():
    result = await _TM.call_tool("fetch_list", {"url": "http://never-fetched.example.com"})
    assert isinstance(result, str)
    assert "fetch_url" in result


@pytest.mark.asyncio
async def test_fetch_list_on_non_html_url_returns_distinct_message():
    """W3 : une URL récupérée mais non-HTML doit produire un message distinct
    de "jamais récupérée" — rappeler fetch_url ne changerait rien."""
    mock_resp = _make_mock_resp(b"texte brut", "text/plain")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/plain.txt"})

    result = await _TM.call_tool("fetch_list", {"url": "http://example.com/plain.txt"})
    assert isinstance(result, str)
    assert "n'a pas renvoyé du HTML" in result


@pytest.mark.asyncio
async def test_fetch_list_entry_start_beyond_total_returns_clear_message():
    """WEB5 : entry_start au-delà du total ne doit plus produire la formulation
    bancale "entre 250 et 12 au total"."""
    mock_resp = _make_mock_resp(_STRUCTURED_HTML, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/oob-list"})

    result = await _TM.call_tool(
        "fetch_list", {"url": "http://example.com/oob-list", "entry_start": 250}
    )
    assert "hors bornes" in result
    assert "entre" not in result


@pytest.mark.asyncio
async def test_fetch_list_rejects_invalid_range():
    mock_resp = _make_mock_resp(_STRUCTURED_HTML, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/small-list"})

    result = await _TM.call_tool(
        "fetch_list", {"url": "http://example.com/small-list", "entry_start": -1}
    )
    assert "entry_start" in result


@pytest.mark.asyncio
async def test_fetch_url_rejects_file_scheme():
    """W1 : file:// ne doit jamais être ouvert par l'opener (exfiltration
    locale possible via navigateur, CORS ouvert + pas d'auth)."""
    result = await _TM.call_tool("fetch_url", {"url": "file:///etc/hosts"})
    assert isinstance(result, str)
    assert "schéma" in result.lower() or "autorisé" in result.lower()


@pytest.mark.asyncio
async def test_fetch_url_rejects_ftp_scheme():
    result = await _TM.call_tool("fetch_url", {"url": "ftp://example.com/file"})
    assert isinstance(result, str)
    assert "schéma" in result.lower() or "autorisé" in result.lower()


@pytest.mark.asyncio
async def test_fetch_url_rejects_non_positive_max_bytes():
    result = await _TM.call_tool("fetch_url", {"url": "http://example.com", "max_bytes": 0})
    assert isinstance(result, str)
    assert "max_bytes" in result


@pytest.mark.asyncio
async def test_fetch_read_touches_html_and_structure_mtime(monkeypatch):
    """W6 : un accès via fetch_read doit aussi rafraîchir le mtime de .html et
    .json de la même clé, pas seulement .txt — sinon ces fichiers expirent
    pendant que le texte reste vivant, et fetch_list échoue ensuite à tort."""
    mock_resp = _make_mock_resp(_STRUCTURED_HTML, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/touch-all"})
    await _TM.call_tool("fetch_list", {"url": "http://example.com/touch-all"})

    old_time = time.time() - 1000
    for path in (
        mcp_web_cache.entry_path("http://example.com/touch-all"),
        mcp_web_cache.html_path("http://example.com/touch-all"),
        mcp_web_cache.structure_path("http://example.com/touch-all"),
    ):
        os.utime(path, (old_time, old_time))

    await _TM.call_tool("fetch_read", {"url": "http://example.com/touch-all"})

    for path in (
        mcp_web_cache.html_path("http://example.com/touch-all"),
        mcp_web_cache.structure_path("http://example.com/touch-all"),
    ):
        assert path.stat().st_mtime > old_time


@pytest.mark.asyncio
async def test_fetch_resource_binary_returns_two_blocks():
    body = b"%PDF-1.4 fake pdf bytes"
    mock_resp = _make_mock_resp(body, "application/pdf")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_resource", {"url": "http://example.com/doc.pdf"})
    assert isinstance(result, list)
    assert len(result) == 2
    text_block, resource_block = result
    assert isinstance(text_block, types.TextContent)
    assert "base64" not in text_block.text.lower()
    assert "application/pdf" in text_block.text
    assert str(len(body)) in text_block.text
    assert "http://example.com/doc.pdf" in text_block.text
    assert isinstance(resource_block, types.EmbeddedResource)
    assert isinstance(resource_block.resource, types.BlobResourceContents)
    assert resource_block.resource.mimeType == "application/pdf"
    assert str(resource_block.resource.uri) == "http://example.com/doc.pdf"
    assert base64.b64decode(resource_block.resource.blob) == body


@pytest.mark.asyncio
async def test_fetch_resource_large_json_stays_blob_not_text():
    """AUDIT §3.3 : même du JSON volumineux part en .blob, jamais en .text —
    fetch_resource veut les octets bruts hors contexte, pas une lecture inline."""
    body = ('{"items": [' + ",".join(str(i) for i in range(2000)) + "]}").encode()
    mock_resp = _make_mock_resp(body, "application/json")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_resource", {"url": "http://api.example.com/big.json"})
    assert isinstance(result, list)
    resource_block = result[1]
    assert isinstance(resource_block.resource, types.BlobResourceContents)
    assert not hasattr(resource_block.resource, "text") or resource_block.resource.text is None
    assert base64.b64decode(resource_block.resource.blob) == body


@pytest.mark.asyncio
async def test_fetch_resource_truncates_at_max_bytes():
    body = b"A" * 100
    mock_resp = _make_mock_resp(body, "application/octet-stream")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool(
            "fetch_resource", {"url": "http://example.com/big.bin", "max_bytes": 10}
        )
    text_block, resource_block = result
    decoded = base64.b64decode(resource_block.resource.blob)
    assert len(decoded) == 10
    assert "Tronqué" in text_block.text
    assert "10" in text_block.text


@pytest.mark.asyncio
async def test_fetch_resource_descriptor_is_kv_stable():
    """Checkpoint §7.4 : deux appels identiques doivent produire un descripteur
    byte-identique (pas de timestamp/id/hash)."""
    body = b"stable content"
    mock_resp = _make_mock_resp(body, "text/csv")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result1 = await _TM.call_tool("fetch_resource", {"url": "http://example.com/data.csv"})
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result2 = await _TM.call_tool("fetch_resource", {"url": "http://example.com/data.csv"})
    assert result1[0].text == result2[0].text


@pytest.mark.asyncio
async def test_fetch_resource_rejects_ftp_scheme():
    result = await _TM.call_tool("fetch_resource", {"url": "ftp://example.com/file"})
    assert isinstance(result, str)
    assert "schéma" in result.lower() or "autorisé" in result.lower()


@pytest.mark.asyncio
async def test_fetch_resource_rejects_non_positive_max_bytes():
    result = await _TM.call_tool(
        "fetch_resource", {"url": "http://example.com/x", "max_bytes": 0}
    )
    assert isinstance(result, str)
    assert "max_bytes" in result


@pytest.mark.asyncio
async def test_fetch_resource_url_error_returns_string():
    err = urllib.error.URLError("Connection refused")
    with patch("urllib.request.OpenerDirector.open", side_effect=err):
        result = await _TM.call_tool(
            "fetch_resource", {"url": "http://unreachable.local/file"}
        )
    assert isinstance(result, str)
    assert "réseau" in result.lower() or "Connection refused" in result


def test_resource_max_bytes_env_var_parsed_via_env_int():
    """MIAOU_WEB_RESOURCE_MAX_BYTES respectée : même mécanisme _env_int que
    READ_CAP/LIST_CAP, défaut 5 Mo si absente."""
    assert mcp_web_cache.RESOURCE_MAX_BYTES == mcp_web_cache._env_int(
        "MIAOU_WEB_RESOURCE_MAX_BYTES", 5 * 1024 * 1024
    )


@pytest.mark.asyncio
async def test_fetch_resource_uses_resource_max_bytes_as_default():
    """Sans max_bytes explicite, le défaut résolu doit être web_cache.RESOURCE_MAX_BYTES
    (pas un magic number local) : un corps sous ce seuil ne doit pas être tronqué."""
    body = b"petit corps, largement sous le defaut"
    mock_resp = _make_mock_resp(body, "application/octet-stream")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        result = await _TM.call_tool("fetch_resource", {"url": "http://example.com/small.bin"})
    text_block, resource_block = result
    assert "Tronqué" not in text_block.text
    assert base64.b64decode(resource_block.resource.blob) == body


def test_tool_list_contains_fetch_resource():
    names = {t.name for t in _TM.list_tools()}
    assert "fetch_resource" in names


def test_env_int_invalid_value_raises_named_error(monkeypatch):
    """W8/D2 : une variable d'environnement non numérique doit lever une
    erreur nommant la variable, pas un ValueError anonyme à l'import."""
    monkeypatch.setenv("MIAOU_WEB_READ_CAP", "not-a-number")
    with pytest.raises(ValueError, match="MIAOU_WEB_READ_CAP"):
        mcp_web_cache._env_int("MIAOU_WEB_READ_CAP", 20000)


@pytest.mark.asyncio
async def test_fetch_url_clamps_max_bytes_above_default():
    """WEB1 : max_bytes ne doit pas dépasser _DEFAULT_MAX_BYTES vers le haut —
    sinon un appelant peut demander 10**12 octets et faire lire tout en mémoire."""
    import mcp_web

    body = b"A" * 100
    mock_resp = _make_mock_resp(body, "text/plain")
    captured = {}

    def capturing_open(self, req, *args, **kwargs):
        captured["req"] = req
        return mock_resp

    with patch("mcp_web._fetch_bytes", wraps=mcp_web._fetch_bytes) as spy, \
         patch("urllib.request.OpenerDirector.open", capturing_open):
        await _TM.call_tool(
            "fetch_url", {"url": "http://example.com/huge", "max_bytes": 10**12}
        )
    called_max_bytes = spy.call_args[0][1]
    assert called_max_bytes == mcp_web._DEFAULT_MAX_BYTES


@pytest.mark.asyncio
async def test_fetch_resource_clamps_max_bytes_above_resource_max():
    import mcp_web

    body = b"A" * 100
    mock_resp = _make_mock_resp(body, "application/octet-stream")

    with patch("mcp_web._fetch_bytes", wraps=mcp_web._fetch_bytes) as spy, \
         patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool(
            "fetch_resource", {"url": "http://example.com/huge.bin", "max_bytes": 10**12}
        )
    called_max_bytes = spy.call_args[0][1]
    assert called_max_bytes == mcp_web_cache.RESOURCE_MAX_BYTES


@pytest.mark.asyncio
async def test_fetch_read_out_of_bounds_char_start_returns_notice():
    """WEB2 : char_start >= len(texte) doit renvoyer une notice explicite,
    jamais une chaîne vide nue."""
    body = b"short text"
    mock_resp = _make_mock_resp(body, "text/plain")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/oob"})

    result = await _TM.call_tool(
        "fetch_read", {"url": "http://example.com/oob", "char_start": 1000}
    )
    assert result != ""
    assert "hors bornes" in result.lower()
    assert str(len(body)) in result


@pytest.mark.asyncio
async def test_fetch_list_reflects_new_html_after_refetch():
    """W2 : un re-fetch doit invalider la structure déjà extraite, sinon
    fetch_list resservirait des headings/liens périmés."""
    first_html = b"<html><body><h1>Ancien titre</h1></body></html>"
    mock_resp_1 = _make_mock_resp(first_html, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp_1):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/refetch"})
    await _TM.call_tool("fetch_list", {"url": "http://example.com/refetch"})

    second_html = b"<html><body><h1>Nouveau titre</h1></body></html>"
    mock_resp_2 = _make_mock_resp(second_html, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp_2):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/refetch"})

    result = await _TM.call_tool("fetch_list", {"url": "http://example.com/refetch"})
    assert "Nouveau titre" in result
    assert "Ancien titre" not in result


@pytest.mark.asyncio
async def test_fetch_list_stale_after_html_becomes_text(monkeypatch):
    """WEB3 : une URL d'abord HTML, re-téléchargée en text/*, ne doit plus
    laisser fetch_list servir la structure de l'ancienne page HTML — le .html
    et le .json doivent être purgés par la branche textuelle de fetch_url."""
    html = b"<html><body><h1>Page HTML</h1></body></html>"
    mock_resp_html = _make_mock_resp(html, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp_html):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/switch"})
    await _TM.call_tool("fetch_list", {"url": "http://example.com/switch"})

    text_body = b"maintenant du texte brut"
    mock_resp_text = _make_mock_resp(text_body, "text/plain; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp_text):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/switch"})

    list_result = await _TM.call_tool("fetch_list", {"url": "http://example.com/switch"})
    assert "n'a pas renvoyé du HTML" in list_result

    read_result = await _TM.call_tool("fetch_read", {"url": "http://example.com/switch"})
    assert "maintenant du texte brut" in read_result


@pytest.mark.asyncio
async def test_fetch_read_stale_after_html_becomes_binary():
    """WEB3 : une URL d'abord HTML, re-téléchargée en binaire, ne doit plus
    laisser fetch_read servir l'ancien texte — .txt/.html/.json doivent être
    purgés par la branche binaire de fetch_url."""
    html = b"<html><body><h1>Page HTML</h1></body></html>"
    mock_resp_html = _make_mock_resp(html, "text/html; charset=utf-8")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp_html):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/to-binary"})
    await _TM.call_tool("fetch_list", {"url": "http://example.com/to-binary"})

    binary_body = bytes(range(256))
    mock_resp_bin = _make_mock_resp(binary_body, "image/png")
    with patch("urllib.request.OpenerDirector.open", return_value=mock_resp_bin):
        await _TM.call_tool("fetch_url", {"url": "http://example.com/to-binary"})

    read_result = await _TM.call_tool("fetch_read", {"url": "http://example.com/to-binary"})
    assert isinstance(read_result, str)
    assert "fetch_url" in read_result  # message de cache absent/expiré

    list_result = await _TM.call_tool("fetch_list", {"url": "http://example.com/to-binary"})
    assert "fetch_url" in list_result


# ---------------------------------------------------------------------------
# WEB9 — conversion html2text et écritures cache hors event loop
# (asyncio.to_thread). fetch_url reste fonctionnel de bout en bout, et une
# coroutine concurrente progresse pendant la conversion (CPU-bound sur plusieurs
# Mo de HTML).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_url_renders_html_off_the_event_loop():
    import asyncio

    import mcp_web

    html = b"<html><body><p>Hello world</p></body></html>"
    mock_resp = _make_mock_resp(html, "text/html; charset=utf-8")

    BLOCK = 0.3
    real_render = mcp_web._render_html_blocking

    def _slow_render(*args, **kwargs):
        time.sleep(BLOCK)  # gèlerait la loop si appelée hors to_thread
        return real_render(*args, **kwargs)

    concurrent_ran = asyncio.Event()

    async def _concurrent():
        await asyncio.sleep(BLOCK / 3)
        concurrent_ran.set()

    async def _call():
        with patch.object(mcp_web, "_render_html_blocking", _slow_render):
            with patch("urllib.request.OpenerDirector.open", return_value=mock_resp):
                return await _TM.call_tool("fetch_url", {"url": "http://example.com"})

    call_task = asyncio.create_task(_call())
    await _concurrent()
    assert not call_task.done(), "la conversion trop rapide — le test ne mesure rien"
    assert concurrent_ran.is_set(), "la loop est restée bloquée pendant la conversion html2text"

    result = await call_task
    assert isinstance(result, types.EmbeddedResource)
    assert result.resource.mimeType == "text/plain"
    assert "Hello world" in result.resource.text
