"""Config en couches : chaîne de fichiers, fusion, variables d'environnement,
allègement d'une config à l'ancienne.

La config effective est la fusion d'une chaîne de fichiers, du plus général au
plus particulier :

- `config.defaults.json` — versionné dans le dépôt public, la base ;
- `config.site.json` — ABSENT du dépôt public, posé par un fork (valeurs
  communes d'une organisation). Comme il n'existe pas en amont, un merge
  amont → fork n'y crée jamais de conflit : c'est sa raison d'être ;
- `config.json` — gitignoré, le spécifique d'une installation (clefs,
  overrides).

Chacun est facultatif, mais il en faut au moins un. `--config` (répétable)
remplace la chaîne entière : `--config autre.json` seul lit ce seul fichier,
comme avant les couches.

La fusion est JSON Merge Patch (RFC 7386), sans variante maison : les objets
fusionnent récursivement, `null` supprime la clé, tout le reste REMPLACE —
tableaux compris (`search.order`, `args` ne se concatènent jamais). La limite de
la RFC, ne pas pouvoir poser une valeur `null`, est sans effet ici : aucune clé
de la config ne distingue `null` de l'absence.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULTS_FILE = "config.defaults.json"
SITE_FILE = "config.site.json"
LOCAL_FILE = "config.json"
DEFAULT_CHAIN = (DEFAULTS_FILE, SITE_FILE, LOCAL_FILE)

# Clé racine dont la valeur est un chemin relatif AU FICHIER qui la déclare :
# la fusion doit retenir lequel, sinon un `--config ailleurs/x.json` résoudrait
# le chemin d'une couche contre le dossier d'une autre.
_MIAOU_DIST_KEY = "miaou_dist"


@dataclass
class LoadedConfig:
    cfg: dict[str, Any]
    # Fichiers effectivement lus, dans l'ordre de fusion.
    layers: list[Path]
    # Dernier maillon de la chaîne, lu ou non : cible de --migrate-config, et
    # emplacement de `state/`. Pour la chaîne par défaut, `config.json` même
    # absent.
    local: Path
    # Fichier contre lequel résoudre un `miaou_dist` relatif : la dernière
    # couche qui le déclare.
    miaou_dist_base: Path
    # Variables `${VAR}` sans valeur ni défaut, avec le chemin de la clé ;
    # remplacées par "" (comme docker compose), à signaler au démarrage.
    unset_vars: list[tuple[str, str]] = field(default_factory=list)


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386, section 2. Ne modifie ni `target` ni `patch`."""
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def read_layer(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"La config '{path}' n'est pas un JSON valide : {e}") from e
    if not isinstance(data, dict):
        raise ValueError(
            f"La config '{path}' doit être un objet JSON, reçu {type(data).__name__}."
        )
    return data


def resolve_chain(explicit: list[str] | None, base_dir: Path | None = None) -> tuple[list[Path], Path]:
    """(fichiers à lire, maillon local). Explicite : tous doivent exister.
    Par défaut : ceux de `DEFAULT_CHAIN` qui existent, au moins un."""
    if explicit:
        paths = [Path(p) for p in explicit]
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            raise ValueError(f"config introuvable : {', '.join(missing)}.")
        return paths, paths[-1]
    base = base_dir or Path(".")
    chain = [base / name for name in DEFAULT_CHAIN]
    present = [p for p in chain if p.exists()]
    if not present:
        raise ValueError(
            f"aucune config trouvée (ni {', '.join(DEFAULT_CHAIN)}) dans "
            f"'{base.resolve()}'."
        )
    return present, chain[-1]


def load_layers(paths: list[Path], local: Path | None = None) -> LoadedConfig:
    merged: dict[str, Any] = {}
    miaou_dist_base = paths[-1]
    for path in paths:
        layer = read_layer(path)
        if _MIAOU_DIST_KEY in layer:
            miaou_dist_base = path
        merged = merge_patch(merged, layer)
    if "port" not in merged:
        names = ", ".join(f"'{p}'" for p in paths)
        raise ValueError(f"La config ({names}) doit contenir la clé 'port'.")
    unset: list[tuple[str, str]] = []
    cfg = interpolate_env(merged, os.environ, unset)
    return LoadedConfig(
        cfg=cfg,
        layers=list(paths),
        local=local or paths[-1],
        miaou_dist_base=miaou_dist_base,
        unset_vars=unset,
    )


# --- Variables d'environnement ---------------------------------------------

# `$$` → `$` littéral ; `${VAR}`, `${VAR:-défaut}` (défaut si absente OU vide),
# `${VAR-défaut}` (défaut si absente seulement) — la syntaxe de docker compose,
# pour qu'un même `.env` serve au compose et à la config. Un `$` seul, ou suivi
# d'autre chose que `{`, reste tel quel.
_VAR_RE = re.compile(
    r"\$\$|\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:?-)(?P<default>[^}]*))?\}"
)


def _block_disabled(block: dict[str, Any]) -> bool:
    # Même règle que `contract.is_disabled`, sans son avertissement de
    # migration : il sortira de toute façon quand le bloc sera lu.
    if "disabled" in block:
        return bool(block["disabled"])
    return bool(block.get("_disabled"))


def interpolate_env(
    value: Any,
    env: Any,
    unset: list[tuple[str, str]],
    path: str = "",
    quiet: bool = False,
) -> Any:
    """Substitue `${VAR}` dans les CHAÎNES de la config fusionnée.

    Après la fusion et non par couche : une valeur littérale de `config.json`
    qui remplace un `${ORG_API_SECRET}` de `config.site.json` ne doit rien exiger
    de l'environnement. Les clés à souligné en tête (`_comment`, `_example_*`)
    sont laissées intactes : ignorées par le proxy, elles citent la syntaxe sans
    l'employer. Un bloc `disabled` est interpolé (`--auth` réveille un bloc
    `auth` neutralisé) mais ses variables manquantes ne sont pas signalées.
    """
    if isinstance(value, dict):
        quiet = quiet or _block_disabled(value)
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k.startswith("_"):
                out[k] = v
            else:
                sub = f"{path}.{k}" if path else k
                out[k] = interpolate_env(v, env, unset, sub, quiet)
        return out
    if isinstance(value, list):
        return [
            interpolate_env(v, env, unset, f"{path}[{i}]", quiet)
            for i, v in enumerate(value)
        ]
    if not isinstance(value, str) or "$" not in value:
        return value

    def substitute(m: re.Match[str]) -> str:
        if m.group(0) == "$$":
            return "$"
        name, op = m.group("name"), m.group("op")
        current = env.get(name)
        if op == ":-" and not current:
            return m.group("default")
        if op == "-" and current is None:
            return m.group("default")
        if current is None:
            if not quiet:
                unset.append((name, path))
            return ""
        return current

    return _VAR_RE.sub(substitute, value)


# --- Affichage de la config effective --------------------------------------

# Liste noire de NOMS de clés : un nom imprévu passe en clair, d'où les
# familles larges (`pass` seul attrape `LOGS_PASS`, `auth` un en-tête
# `X-Auth`). `pass` n'est pas suivi d'une lettre : `passthrough` n'est pas un
# secret.
_SECRET_KEY_RE = re.compile(
    r"secret|pass(?![a-z])|password|passwd|pwd|api[-_]?key|token|auth",
    re.IGNORECASE,
)
# Ces suffixes désignent une URL ou un choix de méthode, publics : sans eux,
# `token_endpoint` ou `token_endpoint_auth_method` sortiraient masqués — et ce
# sont justement eux qu'on vient lire dans un diagnostic OAuth.
_PUBLIC_KEY_RE = re.compile(r"(_endpoint|_url|_uri|_method|_scopes?)$", re.IGNORECASE)
# Identifiants embarqués dans une URL (`https://user:mdp@hôte`) : seul le mot
# de passe est masqué, l'utilisateur aide au diagnostic.
_URL_PASSWORD_RE = re.compile(r"^([a-z][a-z0-9+.-]*://[^/@:]*):[^/@]*@", re.IGNORECASE)


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key)) and not _PUBLIC_KEY_RE.search(key)


def mask_secrets(value: Any, mask_all: bool = False) -> Any:
    """Copie à afficher : les valeurs non vides des clés à l'air de secret
    deviennent `***`, comme TOUTES celles d'un `env` (variables d'un upstream
    stdio, aux noms imprévisibles), et le mot de passe d'une URL aussi. Pour
    `--print-config`, qu'on colle dans un ticket. Liste noire : elle réduit
    l'exposition, elle ne la garantit pas."""
    if isinstance(value, dict):
        return {
            k: ("***" if isinstance(v, str) and v and (mask_all or _is_secret_key(k))
                else mask_secrets(v, mask_all or k == "env"))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets(v, mask_all) for v in value]
    if isinstance(value, str):
        return _URL_PASSWORD_RE.sub(r"\1:***@", value)
    return value


# --- Allègement d'une config à l'ancienne ----------------------------------


def _is_comment_key(key: str) -> bool:
    return key.startswith("_comment")


def strip_comments(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_comments(v) for k, v in value.items() if not _is_comment_key(k)}
    if isinstance(value, list):
        return [strip_comments(v) for v in value]
    return value


@dataclass
class Slimmed:
    content: dict[str, Any]
    # Chemins des valeurs retirées parce qu'identiques à la base.
    removed: list[str] = field(default_factory=list)
    # Clés de la base absentes de la config locale, donc héritées. L'allègement
    # n'y est pour rien — la fusion les ajoutait déjà —, mais une config à
    # l'ancienne a pu les retirer de sa copie à dessein : un serveur ôté y
    # revient actif. Listées pour être neutralisées à la main
    # (`"disabled": true`, ou `null`).
    inherited: list[str] = field(default_factory=list)


def slim(base: dict[str, Any], local: dict[str, Any]) -> Slimmed:
    """Le patch minimal qui, posé sur `base`, rend la même config effective
    que `local` : `merge_patch(base, slim(...).content)` égale
    `merge_patch(base, local)` aux `_comment*` près. Ceux-là sont retirés
    partout, changés ou non : ils documentent la base, une copie locale ne
    fait que vieillir."""
    out = Slimmed(content={})
    out.content = _slim(base, local, "", out)
    return out


def _slim(base: dict[str, Any], local: dict[str, Any], path: str, out: Slimmed) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in local.items():
        if _is_comment_key(key):
            continue
        sub = f"{path}.{key}" if path else key
        if key not in base:
            result[key] = strip_comments(value)
            continue
        ref = base[key]
        if isinstance(value, dict) and isinstance(ref, dict):
            before = len(out.removed)
            nested = _slim(ref, value, sub, out)
            if nested:
                result[key] = nested
            elif len(out.removed) == before:
                out.removed.append(sub)
        elif strip_comments(value) == strip_comments(ref):
            out.removed.append(sub)
        else:
            result[key] = strip_comments(value)
    for key in base:
        if key in local or key.startswith("_"):
            continue
        out.inherited.append(f"{path}.{key}" if path else key)
    return result


def _base_of(paths: list[Path], local: Path) -> dict[str, Any] | None:
    """Fusion des couches qui PRÉCÈDENT `local` dans la chaîne, ou None s'il
    n'y en a aucune."""
    target = local.resolve()
    position = next(
        (i for i, p in enumerate(paths) if p.resolve() == target), len(paths)
    )
    if position == 0:
        return None
    base: dict[str, Any] = {}
    for p in paths[:position]:
        base = merge_patch(base, read_layer(p))
    return base


def migrate_local(paths: list[Path], local: Path) -> Slimmed:
    """Réécrit `local` allégé de ce que les couches précédentes portent déjà,
    après l'avoir sauvegardé en `<local>.bak`. Refuse si la sauvegarde existe
    déjà (une seconde migration écraserait l'original) ou si `local` est la
    seule couche (rien à retrancher)."""
    if not local.exists():
        raise ValueError(f"'{local}' n'existe pas : rien à alléger.")
    base = _base_of(paths, local)
    if base is None:
        raise ValueError(
            f"'{local}' est la seule couche de la chaîne : rien à retrancher."
        )
    backup = local.with_name(local.name + ".bak")
    if backup.exists():
        raise ValueError(
            f"'{backup}' existe déjà : la retirer d'abord (elle garde peut-être "
            f"l'original d'une migration précédente)."
        )
    result = slim(base, read_layer(local))

    # copy2 garde le mode : un config.json en 0600 (il porte des secrets) ne
    # doit pas devenir lisible de tous par sa sauvegarde ni par sa réécriture.
    shutil.copy2(local, backup)
    mode = local.stat().st_mode & 0o777
    tmp = local.with_name(local.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(result.content, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, local)
    return result


def needs_slimming(paths: list[Path], local: Path) -> bool:
    """La couche locale répète-t-elle la base ? Signal de démarrage pour
    proposer --migrate-config, sans jamais réécrire d'office."""
    if not local.exists():
        return False
    base = _base_of(paths, local)
    if base is None:
        return False
    content = read_layer(local)
    return slim(base, content).content != content
