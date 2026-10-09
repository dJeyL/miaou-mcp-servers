"""MIAOU servi par le proxy : clé `miaou_dist` de `config.json`, routes `/app/`.

Le proxy sert le dossier `dist/` d'un clone de MIAOU, et lui seul : `/app/`
rend `miaou.html` (qui ne s'appelle pas `index.html`), le reste du dossier est
servi sous `/app/` (manifeste, icônes, et le service worker de portée `/app/`
que MIAOU y dépose), et `/` redirige vers `/app/`. Clé absente : rien de tout
cela, le proxy se comporte comme avant.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from starlette.datastructures import Headers
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import NotModifiedResponse, StaticFiles

CONFIG_KEY = "miaou_dist"
APP_PREFIX = "/app"
MIAOU_HTML = "miaou.html"
MANIFEST = "manifest.webmanifest"

# Entrées dont la présence dans le dossier désigné trahit un dossier trop large
# (la racine du dépôt MIAOU plutôt que son `dist/`) : servir ce dossier
# exposerait sa config au réseau dès que `host` vaut 0.0.0.0.
_FORBIDDEN_ENTRIES = ("config.json", ".git")

# Types servis, posés explicitement plutôt que demandés à `mimetypes` : sous
# Windows, le module lit le registre, dont les entrées remplacent ses valeurs
# par défaut (`.js` en `text/plain` sur certains postes), et sous Linux il lit
# `/etc/mime.types` s'il existe. Or un navigateur refuse d'enregistrer un service
# worker servi sous un type qui n'est pas JavaScript. Une extension absente de
# la table retombe sur `mimetypes` (FileResponse le consulte quand on ne passe
# pas de type).
_MEDIA_TYPES = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def media_type_for(path: str | os.PathLike[str]) -> str | None:
    return _MEDIA_TYPES.get(Path(path).suffix.lower())


def resolve_miaou_dist(cfg: dict[str, Any], config_path: str | Path) -> Path | None:
    """Chemin absolu du dossier à servir, ou None si la clé est absente.

    Relatif au fichier `config.json`, pas au répertoire courant (qui dépend de
    la façon de lancer le proxy). `null` et `""` valent absence. Lève
    `ValueError` sur une valeur qui n'est pas une chaîne, et sur un dossier qui
    contient l'une des `_FORBIDDEN_ENTRIES` : refus de démarrer, parce que
    c'est une question de sécurité. Un dossier introuvable, lui, n'est pas une
    erreur ici (cf. `miaou_dist_warnings`).
    """
    value = cfg.get(CONFIG_KEY)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"'{CONFIG_KEY}' doit être une chaîne (chemin du dossier dist/ de "
            f"MIAOU), reçu {type(value).__name__}."
        )
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(config_path).resolve().parent / path
    path = path.resolve()
    if path.is_dir():
        found = [name for name in _FORBIDDEN_ENTRIES if (path / name).exists()]
        if found:
            raise ValueError(
                f"'{CONFIG_KEY}' désigne '{path}', qui contient "
                f"{' et '.join(found)} : ce n'est pas le dossier dist/ de MIAOU, "
                f"et le servir exposerait ces fichiers au réseau."
            )
    return path


def _manifest_icon_path(src: str) -> str | None:
    """Chemin relatif au dossier servi d'une icône citée par le manifeste, ou
    None si `src` désigne une autre origine (rien à vérifier ici).

    `src` se résout contre l'URL du manifeste, comme le ferait le navigateur.
    Un chemin qui sort de `/app/` est rendu tel quel : il ne peut pas être
    servi, et l'appelant le signalera comme absent.
    """
    base = f"http://proxy{APP_PREFIX}/{MANIFEST}"
    url = urlsplit(urljoin(base, src))
    if url.netloc != "proxy":
        return None
    prefix = f"{APP_PREFIX}/"
    if not url.path.startswith(prefix):
        return url.path
    return url.path[len(prefix):]


def miaou_dist_warnings(path: Path) -> list[str]:
    """Ce qui manque dans le dossier servi, en phrases prêtes pour le journal.

    Avertissements seulement : le proxy démarre quand même et garde les routes,
    qui relisent le disque à chaque requête — un dossier rempli après coup est
    servi sans redémarrage. `miaou.html` absent : rien n'est servi. Manifeste
    absent, illisible, ou icône citée introuvable : MIAOU est servi mais ne
    pourra pas être installé. Les noms d'icônes sont lus dans le manifeste,
    jamais figés ici.
    """
    if not path.is_dir():
        return [f"dossier introuvable : {path} — /app/ répondra 404."]
    warnings: list[str] = []
    if not (path / MIAOU_HTML).is_file():
        warnings.append(f"{MIAOU_HTML} absent de {path} — /app/ répondra 404.")
    manifest_path = path / MANIFEST
    if not manifest_path.is_file():
        warnings.append(f"{MANIFEST} absent — MIAOU ne pourra pas être installé.")
        return warnings
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        warnings.append(f"{MANIFEST} illisible ({e}) — MIAOU ne pourra pas être installé.")
        return warnings
    icons = manifest.get("icons") if isinstance(manifest, dict) else None
    if not isinstance(icons, list) or not icons:
        warnings.append(f"{MANIFEST} ne cite aucune icône — MIAOU ne pourra pas être installé.")
        return warnings
    for icon in icons:
        src = icon.get("src") if isinstance(icon, dict) else None
        if not isinstance(src, str) or not src:
            warnings.append(f"{MANIFEST} cite une icône sans 'src'.")
            continue
        relative = _manifest_icon_path(src)
        if relative is None:
            continue
        if relative.startswith("/") or not (path / relative).is_file():
            warnings.append(f"icône introuvable : {src} (citée par {MANIFEST}).")
    return warnings


class MiaouStaticFiles(StaticFiles):
    """`StaticFiles` adapté au dossier `dist/` de MIAOU.

    - le chemin vide (`/app/`) rend `miaou.html` ; le mode `html` de Starlette
      ne connaît que `index.html` et reste désactivé (pas de 404.html non plus) ;
    - `Cache-Control: no-cache` sur toute réponse, 304 compris : `StaticFiles`
      pose `ETag`/`Last-Modified` mais aucun `Cache-Control`, et le navigateur
      applique alors une fraîcheur heuristique (~10 % de l'âge du fichier) qui
      peut resservir un vieux HTML sans revalider — un `git pull` de MIAOU ne
      serait pas vu au rechargement suivant ;
    - type de contenu pris dans `_MEDIA_TYPES` ;
    - dossier absent = 404, jamais une erreur : `check_config` est neutralisé
      (il lèverait à la première requête, rendue en 500) ;
    - fichiers cachés jamais servis (`.gitkeep` du dossier de l'image Docker,
      `.DS_Store`).
    """

    def __init__(self, directory: Path) -> None:
        super().__init__(directory=directory, check_dir=False)

    async def check_config(self) -> None:
        return None

    async def get_response(self, path: str, scope: Any) -> Response:
        if path in ("", "."):
            path = MIAOU_HTML
        elif any(part.startswith(".") for part in Path(path).parts):
            return Response("Not Found", status_code=404)
        return await super().get_response(path, scope)

    def file_response(
        self,
        full_path: Any,
        stat_result: os.stat_result,
        scope: Any,
        status_code: int = 200,
    ) -> Response:
        response = FileResponse(
            full_path,
            status_code=status_code,
            stat_result=stat_result,
            media_type=media_type_for(full_path),
            headers={"Cache-Control": "no-cache"},
        )
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response


async def _redirect_to_app(request: Any) -> Response:
    # 302 et non 301 : un 301 est gardé en cache par le navigateur et
    # survivrait au retrait de la clé.
    return RedirectResponse(f"{APP_PREFIX}/", status_code=302)


def build_miaou_routes(directory: Path) -> list[Any]:
    """Routes à poser APRÈS `/mcp` et les routes OAuth, qui gardent la priorité.

    Publiques même quand l'auth entrante est active : `RequireAuthMiddleware`
    n'enveloppe que l'endpoint MCP, et l'appli doit pouvoir charger pour lancer
    son propre parcours. `/app` sans slash est redirigé vers `/app/` par le
    routeur de Starlette.
    """
    return [
        Mount(APP_PREFIX, app=MiaouStaticFiles(directory)),
        Route("/", endpoint=_redirect_to_app, methods=["GET", "HEAD"]),
    ]
