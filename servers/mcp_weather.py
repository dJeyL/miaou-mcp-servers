#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.28.1,<2", "uvicorn", "starlette", "truststore"]
# ///
"""
Serveur MCP météo pour MIAOU.

Transport streamable-http (single endpoint POST, réponses en SSE). CORS ouvert
pour permettre au navigateur de l'atteindre directement depuis dist/miaou.html.

Outils exposés :
  - get_weather(city, state?, country?, astronomy?, hourly?, extract?) : météo actuelle +
    prévisions J..J+2 via wttr.in (JSON allégé par défaut, astronomy/hourly réintégrables
    séparément ; ressource hors contexte si extract)

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
import base64
import datetime
import json
import re
import unicodedata
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


def _slug(text: str) -> str:
    """Réduit un libellé de lieu à un identifiant de nom de fichier : ASCII, minuscules,
    tirets. Sans ça, « Saint-Étienne, France » produirait un nom de ressource contenant
    accents, espaces et virgule."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug or "lieu"


def _resource_date(data: dict) -> str:
    """Date yyyymmdd du bulletin : celle du premier jour renvoyé par wttr.in quand elle
    est exploitable, sinon la date locale du serveur."""
    weather = data.get("weather")
    if isinstance(weather, list) and weather and isinstance(weather[0], dict):
        date = weather[0].get("date")
        if isinstance(date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return date.replace("-", "")
    return datetime.date.today().strftime("%Y%m%d")


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
            astronomy: Annotated[
                bool,
                Field(
                    description="Ajoute par jour le bloc astronomy : lever/coucher du soleil et de la lune, phase et illumination lunaires. Coût faible (~500 caractères pour les trois jours)."
                ),
            ] = False,
            hourly: Annotated[
                bool,
                Field(
                    description="Ajoute par jour le bloc hourly : découpage horaire en 8 tranches de 3 h (température, vent, précipitations, ressenti par tranche) au lieu des seules min/max et moyennes journalières. Coût élevé : multiplie la réponse par ~16."
                ),
            ] = False,
            extract: Annotated[
                bool,
                Field(
                    description="Transfère le JSON au client comme ressource binaire nommée weather-<ville>-<yyyymmdd>.json, hors du contexte du modèle : celui-ci ne reçoit alors qu'un descripteur (lieu, date, taille), pas les données."
                ),
            ] = False,
        ) -> str | types.EmbeddedResource | list[types.ContentBlock]:
            """Renvoie la météo d'une ville via wttr.in : conditions actuelles et prévisions
            du jour même plus les deux jours suivants. JSON allégé par défaut : sans les
            blocs astronomy ni hourly, donc sans découpage horaire, seulement les min/max
            et moyennes journalières. Les paramètres astronomy et hourly, indépendants,
            réintègrent chacun le sien. Attention : heures UTC."""
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

            if not isinstance(data, dict):
                return "Réponse invalide de wttr.in (JSON malformé : objet attendu)."

            weather = data.get("weather", [])
            if not isinstance(weather, list):
                return "Réponse invalide de wttr.in (JSON malformé : champ 'weather' inattendu)."
            dropped = [
                key
                for key, keep in (("astronomy", astronomy), ("hourly", hourly))
                if not keep
            ]
            for day in weather:
                if isinstance(day, dict):
                    for key in dropped:
                        day.pop(key, None)

            payload = json.dumps(data, ensure_ascii=False)

            if not extract:
                return types.EmbeddedResource(
                    type="resource",
                    resource=types.TextResourceContents(
                        uri=f"miaou://weather/{location}",  # type: ignore[arg-type]
                        mimeType="application/json",
                        text=payload,
                    ),
                )

            raw_payload = payload.encode("utf-8")
            name = f"weather-{_slug(location)}-{_resource_date(data)}.json"
            kept = [k for k in ("astronomy", "hourly") if k not in dropped]
            contenu = f"avec {' et '.join(kept)}" if kept else "allégée"
            descripteur = (
                f"Météo de {location} transférée au client comme ressource {name} "
                f"({len(raw_payload)} octets, {contenu})."
            )
            return [
                types.TextContent(type="text", text=descripteur),
                types.EmbeddedResource(
                    type="resource",
                    resource=types.BlobResourceContents(
                        uri=f"miaou://weather/{name}",  # type: ignore[arg-type]
                        mimeType="application/json",
                        blob=base64.b64encode(raw_payload).decode(),
                    ),
                ),
            ]

        self.finalize_tools()


server = WeatherServer()
mcp = server.mcp  # exposé pour le proxy in-process

if __name__ == "__main__":
    server.main()
