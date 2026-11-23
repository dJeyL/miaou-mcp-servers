"""Garde « MIAOU servi par ce proxy, sur loopback », partagée par les surfaces
qui ne doivent répondre qu'à la page `/app/` : secrets volatils (lot AU) et
relais LLM.

Deux conditions, toutes deux nécessaires :

- `Host` vaut `127.0.0.1:<port>` ou `localhost:<port>`, port d'écoute du
  proxy. C'est la défense contre le DNS rebinding : une page `evil.example`
  rebindée sur 127.0.0.1 est de même origine qu'elle-même, et ne se distingue
  que par son `Host`.
- la requête vient de la même origine (`http://<Host>`). `Origin` en est la
  preuve quand le navigateur l'envoie ; il ne l'envoie pas sur un `GET` de
  même origine, d'où `fetch_metadata` : accepter alors l'absence d'`Origin`
  accompagnée de `Sec-Fetch-Site: same-origin`, en-tête qu'une page ne peut
  pas poser elle-même. Un client hors navigateur (curl) n'envoie ni l'un ni
  l'autre, et il est refusé.
"""

from __future__ import annotations

from typing import Any

ALLOWED_HOSTS = ("127.0.0.1", "localhost")


def loopback_same_origin(headers: Any, port: int | None, *, fetch_metadata: bool = False) -> bool:
    """`headers` : tout objet à `.get(nom)` insensible à la casse (en-têtes
    Starlette). Sans port connu, refus."""
    if headers is None or not port:
        return False
    host = (headers.get("host") or "").strip().lower()
    if host not in {f"{h}:{port}" for h in ALLOWED_HOSTS}:
        return False
    origin = headers.get("origin")
    if origin is not None:
        return origin.strip().lower() == f"http://{host}"
    if not fetch_metadata:
        return False
    return (headers.get("sec-fetch-site") or "").strip().lower() == "same-origin"
