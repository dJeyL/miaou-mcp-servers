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
    pour HTML, texte brut pour text/*, binaire base64 pour tout le reste.

Lancement :
    uv run servers/mcp_web.py                          # HTTP sur 127.0.0.1:8768
    uv run servers/mcp_web.py --transport stdio        # stdin/stdout
    uv run servers/mcp_web.py --host 0.0.0.0           # HTTP sur toutes interfaces

Dans MIAOU → Paramètres → Serveurs MCP → Ajouter :
    Nom       : fetch
    URL       : http://127.0.0.1:8768/mcp
    Transport : streamable-http   (deviné depuis /mcp)
    Activé    : oui
"""

import base64
import urllib.error
import urllib.request

import html2text
from mcp import types

from mcp_base import MiaouMCPBase, make_opener

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 Mo
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


class WebServer(MiaouMCPBase):
    def __init__(self) -> None:
        super().__init__("miaou-web", default_port=8768)

        @self.mcp.tool()
        async def fetch_url(
            url: str,
            max_bytes: int = _DEFAULT_MAX_BYTES,
        ) -> str | types.EmbeddedResource:
            """Télécharge une URL et renvoie son contenu : HTML converti en texte (html2text), text/* renvoyé tel quel, binaire (image, etc.) encodé en base64. Taille limitée à max_bytes (défaut 5 Mo)."""
            opener = make_opener()
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            try:
                with opener.open(req, timeout=10) as resp:
                    content_type = resp.headers.get("Content-Type", "application/octet-stream")
                    raw = resp.read(max_bytes + 1)
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
                h = html2text.HTML2Text()
                h.ignore_images = True
                h.body_width = 0
                cleaned = h.handle(html_text).strip() + truncation_note
                return types.EmbeddedResource(
                    type="resource",
                    resource=types.TextResourceContents(
                        uri=url,  # type: ignore[arg-type]
                        mimeType="text/plain",
                        text=cleaned,
                    ),
                )
            elif mime.startswith("text/"):
                try:
                    text = raw.decode(charset, errors="replace")
                except LookupError:
                    text = raw.decode("utf-8", errors="replace")
                return types.EmbeddedResource(
                    type="resource",
                    resource=types.TextResourceContents(
                        uri=url,  # type: ignore[arg-type]
                        mimeType=mime,
                        text=text + truncation_note,
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


server = WebServer()
mcp = server.mcp  # exposé pour le proxy in-process

if __name__ == "__main__":
    server.main()
