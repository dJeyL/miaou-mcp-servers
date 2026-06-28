#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2", "uvicorn", "starlette"]
# ///
"""
Serveur MCP météo pour MIAOU.

Transport streamable-http (single endpoint POST, réponses en SSE). CORS ouvert
pour permettre au navigateur de l'atteindre directement depuis dist/miaou.html.

Outils exposés :
  - get_weather(city, state?, country?) : météo actuelle via wttr.in (JSON allégé)

Lancement :
    uv run servers/mcp_weather.py                          # HTTP sur 127.0.0.1:8766
    uv run servers/mcp_weather.py --transport stdio        # stdin/stdout
    uv run servers/mcp_weather.py --host 0.0.0.0           # HTTP sur toutes interfaces
    uv run servers/mcp_weather.py --host 0.0.0.0 --port 8766
    uv run servers/mcp_weather.py 0.0.0.0 8766             # syntaxe positionnelle (compat)

Dans MIAOU → Paramètres → Serveurs MCP → Ajouter :
    Nom       : weather
    URL       : http://127.0.0.1:8766/mcp
    Transport : streamable-http   (deviné depuis /mcp)
    Activé    : oui
"""

import json
import os
import urllib.parse
import urllib.request
from typing import Optional

from mcp import types

from mcp_base import MiaouMCPBase


class WeatherServer(MiaouMCPBase):
    def __init__(self) -> None:
        super().__init__("miaou-weather", default_port=8766)

        @self.mcp.tool()
        async def get_weather(
            city: str,
            state: Optional[str] = None,
            country: Optional[str] = None,
        ) -> types.EmbeddedResource:
            """Renvoie la météo actuelle pour une ville via wttr.in (JSON allégé sans astronomy ni hourly). Attention : heures UTC."""
            parts = [city]
            if state:
                parts.append(state)
            if country:
                parts.append(country)
            location = ",".join(parts)

            url = f"http://wttr.in/{urllib.parse.quote(location)}?format=j1"

            proxy_url = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
            if proxy_url:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                )
            else:
                opener = urllib.request.build_opener()

            with opener.open(url, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            for day in data.get("weather", []):
                day.pop("astronomy", None)
                day.pop("hourly", None)

            return types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri=f"miaou://weather/{location}",  # type: ignore[arg-type]
                    mimeType="application/json",
                    text=json.dumps(data, ensure_ascii=False),
                ),
            )


server = WeatherServer()
mcp = server.mcp  # exposé pour le proxy in-process

if __name__ == "__main__":
    server.main()
