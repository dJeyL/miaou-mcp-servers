#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.28.1", "uvicorn", "starlette"]
# ///
"""
Serveur MCP météo pour MIAOU.

Transport streamable-http (single endpoint POST, réponses en SSE). CORS ouvert
pour permettre au navigateur de l'atteindre directement depuis dist/miaou.html.

Outils exposés :
  - get_weather(city, state?, country?) : météo actuelle via wttr.in (JSON allégé)

Lancement :
    uv run servers/mcp_weather.py                          # HTTP sur 127.0.0.1:8767
    uv run servers/mcp_weather.py --transport stdio        # stdin/stdout
    uv run servers/mcp_weather.py --host 0.0.0.0           # HTTP sur toutes interfaces
    uv run servers/mcp_weather.py --host 0.0.0.0 --port 8767
    uv run servers/mcp_weather.py 0.0.0.0 8767             # syntaxe positionnelle (compat)

Dans MIAOU → Paramètres → Serveurs MCP → Ajouter :
    Nom       : weather
    URL       : http://127.0.0.1:8767/mcp
    Transport : streamable-http   (deviné depuis /mcp)
    Activé    : oui
"""

import asyncio
import json
import urllib.error
import urllib.parse
from typing import Annotated, Optional

from mcp import types
from pydantic import Field

from mcp_base import MiaouMCPBase, make_opener


def _fetch_weather_bytes(url: str) -> bytes:
    """I/O bloquante isolée pour être déportée via asyncio.to_thread (T2) :
    sans ça, chaque appel gèle l'event loop pendant tout le round-trip réseau."""
    with make_opener().open(url, timeout=10) as resp:
        return resp.read(2 * 1024 * 1024)


class WeatherServer(MiaouMCPBase):
    def __init__(self) -> None:
        super().__init__("miaou-weather", default_port=8767)

        @self.mcp.tool()
        async def get_weather(
            city: str,
            state: Annotated[
                Optional[str],
                Field(
                    description="Région/État pour désambiguïser la ville en cas d'homonymie (optionnel, ex. \"Texas\")."
                ),
            ] = None,
            country: Annotated[
                Optional[str],
                Field(
                    description="Pays pour désambiguïser la ville en cas d'homonymie (optionnel, nom en anglais recommandé)."
                ),
            ] = None,
        ) -> str | types.EmbeddedResource:
            """Renvoie la météo actuelle pour une ville via wttr.in (JSON allégé sans astronomy ni hourly). Attention : heures UTC."""
            parts = [city]
            if state:
                parts.append(state)
            if country:
                parts.append(country)
            location = ",".join(parts)

            url = f"http://wttr.in/{urllib.parse.quote(location)}?format=j1"

            try:
                raw = await asyncio.to_thread(_fetch_weather_bytes, url)
            except urllib.error.HTTPError as e:
                return f"Erreur HTTP {e.code} ({e.reason}) — wttr.in"
            except urllib.error.URLError as e:
                return f"Erreur réseau ({e.reason}) — wttr.in"
            except TimeoutError:
                return "Timeout (10 s) — wttr.in"
            except Exception as e:
                return f"Erreur inattendue ({type(e).__name__}: {e}) — wttr.in"

            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as e:
                return f"Réponse invalide de wttr.in (JSON malformé : {e})."

            weather = data.get("weather", [])
            if not isinstance(weather, list):
                return "Réponse invalide de wttr.in (JSON malformé : champ 'weather' inattendu)."
            for day in weather:
                if isinstance(day, dict):
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

        self.finalize_tools()


server = WeatherServer()
mcp = server.mcp  # exposé pour le proxy in-process

if __name__ == "__main__":
    server.main()
