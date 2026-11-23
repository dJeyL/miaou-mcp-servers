"""Relais vers des API de LLM OpenAI-compatibles, sous `/llm/<slug>/…`.

MIAOU appelle son LLM depuis le navigateur : seule une API qui accepte toute
origine (CORS) et tout `User-Agent` lui est accessible. Le relais lève cette
contrainte. `/llm/<slug>/<reste>` part vers `<base_url>/<reste>`, méthode,
query et corps intacts. Les en-têtes sont ceux que la config déclare, et
aucun autre. La réponse revient morceau par morceau, flux SSE compris.

**Config** (clef racine `llm`) :

    "llm": {
      "request_headers":  { "<En-Tête>": true | false | "valeur", … },
      "response_headers": { … },
      "backends": {
        "<slug>": {
          "base_url": "https://api.example.org/v1",
          "disabled": false,
          "request_headers": { … }, "response_headers": { … },
          "timeout": 600
        }
      }
    }

`true` recopie la valeur reçue (du client pour la requête, de l'amont pour la
réponse), une chaîne pose une valeur fixe, `false` retire l'en-tête. Un
en-tête que la config ne cite pas TOMBE : c'est tout l'objet du relais, que
`Origin`, `Referer`, `User-Agent` ou `Sec-Fetch-*` du navigateur ne partent
pas. Les dictionnaires globaux (listes blanches de `config.defaults.json`)
s'appliquent sous ceux du backend, qui l'emportent clef par clef. `false`
existe pour cela : `null` n'agit que d'une couche de config à l'autre, sur le
même chemin, et ne retire donc pas une clef globale au niveau d'un backend.
Les noms sont insensibles à la casse ; deux casses d'un même nom dans un
dictionnaire (deux couches qui l'écrivent différemment) font refuser le
démarrage, sinon l'une gagnerait en silence.

Hors de portée de la config, et refusés s'ils y figurent : ce que httpx2
calcule (`Host`, `Content-Length`, `Accept-Encoding`, hop-by-hop) et, en
réponse, ce qu'il décode (`Content-Encoding`, `Content-Length`) ou ce qui
appartient au proxy (`Access-Control-*`). httpx2 n'ajoute que
`Accept-Encoding` (ce qu'il sait décompresser) et `Accept: */*` si la config
n'en pose pas : son `User-Agent` est retiré, aucun n'est envoyé sans
déclaration.

**Garde** : `origin_guard.loopback_same_origin`, `fetch_metadata` compris. Le
relais ne sert que MIAOU servi par ce proxy sous `/app/`, sur loopback : avec
une clef d'API fixe dans la config, toute autre page ouverte dans le
navigateur pourrait sinon la dépenser. Refus en 403.

**Hors de l'app Starlette** : le wrapper ASGI de `build_app` l'aiguille avant
elle. Ni l'auth entrante (dont le `BearerAuthBackend` prendrait la clef de
l'API pour un jeton à vérifier), ni le CORS du proxy ne s'y appliquent.

**Erreurs** : amont injoignable en 502, délai dépassé avant la réponse en 504,
au format d'erreur OpenAI (`{"error": {"message", "type", "code"}}`). Une
panne APRÈS le début de la réponse ne peut plus changer le statut : la
connexion est coupée (exception relevée), pour qu'une réponse tronquée ne
passe pas pour complète.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from .contract import is_disabled
from .logging import _log
from .origin_guard import loopback_same_origin

LLM_KEY = "llm"
PREFIX = "/llm"
REQUEST_HEADERS = "request_headers"
RESPONSE_HEADERS = "response_headers"
BACKENDS = "backends"

#: Délai de LECTURE par défaut, entre deux morceaux de la réponse : un modèle
#: de raisonnement peut se taire plusieurs minutes avant son premier token.
DEFAULT_TIMEOUT_S = 600.0
CONNECT_TIMEOUT_S = 30.0

ORIGIN_REFUSED = "LLM_ORIGIN_REFUSED"
UNKNOWN_BACKEND = "LLM_UNKNOWN_BACKEND"
INVALID_PATH = "LLM_INVALID_PATH"
UPSTREAM_UNREACHABLE = "LLM_UPSTREAM_UNREACHABLE"
UPSTREAM_TIMEOUT = "LLM_UPSTREAM_TIMEOUT"

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-connection", "te", "trailer",
    "transfer-encoding", "upgrade",
})
_FORBIDDEN_REQUEST = _HOP_BY_HOP | {
    "host", "content-length", "accept-encoding", "proxy-authorization",
}
_FORBIDDEN_RESPONSE = _HOP_BY_HOP | {
    "content-length", "content-encoding", "proxy-authenticate",
}
_FORBIDDEN_RESPONSE_PREFIX = "access-control-"

_BACKEND_KEYS = frozenset({
    "base_url", "disabled", "_disabled", REQUEST_HEADERS, RESPONSE_HEADERS, "timeout",
})
_LLM_KEYS = frozenset({REQUEST_HEADERS, RESPONSE_HEADERS, BACKENDS})

# RFC 9110 §5.6.2 : un nom d'en-tête est un `token`.
_TOKEN_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# Le slug est un segment de chemin : mêmes caractères qu'un nom d'upstream.
_SLUG_RE = re.compile(r"[A-Za-z0-9_-]+")

HeaderRule = bool | str


def _is_comment(key: str) -> bool:
    return key.startswith("_comment")


def _header_map(where: str, value: Any, *, response: bool) -> dict[str, HeaderRule]:
    """Dictionnaire d'en-têtes validé, aux noms en minuscules."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{where} : objet attendu, reçu {type(value).__name__}.")
    out: dict[str, HeaderRule] = {}
    spelled: dict[str, str] = {}
    for name, rule in value.items():
        if _is_comment(name) or rule is None:
            continue
        if not _TOKEN_RE.fullmatch(name):
            raise ValueError(f"{where} : {name!r} n'est pas un nom d'en-tête valide.")
        low = name.lower()
        if low in spelled:
            raise ValueError(
                f"{where} : {spelled[low]!r} et {name!r} désignent le même en-tête "
                f"(casse indifférente) ; n'en garder qu'une écriture dans toutes les "
                f"couches de config."
            )
        forbidden = (
            low in _FORBIDDEN_RESPONSE or low.startswith(_FORBIDDEN_RESPONSE_PREFIX)
            if response else low in _FORBIDDEN_REQUEST
        )
        if forbidden:
            raise ValueError(
                f"{where}.{name} : en-tête géré par le relais lui-même, il ne se "
                f"configure pas ; retirer cette clef."
            )
        if isinstance(rule, str):
            if "\r" in rule or "\n" in rule:
                raise ValueError(f"{where}.{name} : retour à la ligne interdit dans une valeur.")
        elif not isinstance(rule, bool):
            raise ValueError(
                f"{where}.{name} : true, false ou une chaîne attendu, reçu "
                f"{type(rule).__name__}."
            )
        spelled[low] = name
        out[low] = rule
    return out


@dataclass(frozen=True)
class Backend:
    slug: str
    base_url: str
    request_headers: dict[str, HeaderRule] = field(default_factory=dict)
    response_headers: dict[str, HeaderRule] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class LlmConfig:
    backends: dict[str, Backend] = field(default_factory=dict)
    disabled: tuple[str, ...] = ()


def _base_url(where: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}.base_url : URL http(s) attendue.")
    url = value.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{where}.base_url : URL http(s) attendue, reçu {url!r}.")
    if parts.username is not None or parts.password is not None:
        raise ValueError(
            f"{where}.base_url : identifiants dans l'URL refusés ; les passer en "
            f"en-tête (request_headers)."
        )
    if parts.query or parts.fragment:
        raise ValueError(f"{where}.base_url : ni query ni fragment.")
    return url.rstrip("/")


def _timeout(where: str, value: Any) -> float:
    if value is None:
        return DEFAULT_TIMEOUT_S
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(
            f"{where}.timeout : nombre de secondes fini et supérieur à 0 attendu, "
            f"reçu {value!r}."
        )
    return float(value)


def resolve_llm_config(cfg: dict[str, Any]) -> LlmConfig:
    """La clef `llm`, validée. Absente ou null : aucun relais. Chaque erreur
    est une ValueError, sortie en 1 de `main()` et `invalid` de configwatch."""
    block = cfg.get(LLM_KEY)
    if block is None:
        return LlmConfig()
    if not isinstance(block, dict):
        raise ValueError(f"'{LLM_KEY}' : objet attendu, reçu {type(block).__name__}.")
    unknown = sorted(k for k in block if k not in _LLM_KEYS and not _is_comment(k))
    if unknown:
        raise ValueError(
            f"'{LLM_KEY}' : clef(s) inconnue(s) {', '.join(unknown)} ; attendu "
            f"{', '.join(sorted(_LLM_KEYS))}."
        )
    global_req = _header_map(f"{LLM_KEY}.{REQUEST_HEADERS}", block.get(REQUEST_HEADERS), response=False)
    global_resp = _header_map(f"{LLM_KEY}.{RESPONSE_HEADERS}", block.get(RESPONSE_HEADERS), response=True)
    entries = block.get(BACKENDS)
    if entries is None:
        entries = {}
    if not isinstance(entries, dict):
        raise ValueError(f"'{LLM_KEY}.{BACKENDS}' : objet attendu, reçu {type(entries).__name__}.")

    backends: dict[str, Backend] = {}
    disabled: list[str] = []
    for slug, entry in entries.items():
        where = f"{LLM_KEY}.{BACKENDS}.{slug}"
        if not _SLUG_RE.fullmatch(slug):
            raise ValueError(
                f"{where} : slug invalide (lettres, chiffres, `-` et `_` seulement : "
                f"il forme le chemin /llm/<slug>/)."
            )
        if not isinstance(entry, dict):
            raise ValueError(f"{where} : objet attendu, reçu {type(entry).__name__}.")
        unknown = sorted(k for k in entry if k not in _BACKEND_KEYS and not _is_comment(k))
        if unknown:
            raise ValueError(
                f"{where} : clef(s) inconnue(s) {', '.join(unknown)} ; attendu "
                f"{', '.join(sorted(_BACKEND_KEYS - {'_disabled'}))}."
            )
        if is_disabled(entry, where):
            disabled.append(slug)
            continue
        backends[slug] = Backend(
            slug=slug,
            base_url=_base_url(where, entry.get("base_url")),
            request_headers={
                **global_req,
                **_header_map(f"{where}.{REQUEST_HEADERS}", entry.get(REQUEST_HEADERS), response=False),
            },
            response_headers={
                **global_resp,
                **_header_map(f"{where}.{RESPONSE_HEADERS}", entry.get(RESPONSE_HEADERS), response=True),
            },
            timeout=_timeout(where, entry.get("timeout")),
        )
    return LlmConfig(backends=backends, disabled=tuple(disabled))


# --- Purs : en-têtes et chemin ----------------------------------------------


def apply_rules(rules: dict[str, HeaderRule], received: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Les en-têtes à émettre : ceux que `rules` cite, et eux seuls. `received`
    est une liste de paires (un nom peut s'y répéter, `Set-Cookie`) ; un
    `true` les recopie toutes, dans l'ordre reçu."""
    by_name: dict[str, list[str]] = {}
    for name, value in received:
        by_name.setdefault(name.lower(), []).append(value)
    out: list[tuple[str, str]] = []
    for name, rule in rules.items():
        if rule is True:
            out.extend((name, v) for v in by_name.get(name, ()))
        elif isinstance(rule, str):
            out.append((name, rule))
    return out


def split_path(raw_path: bytes) -> tuple[str, str] | None:
    """(`slug`, suite encodée telle que reçue, `/` initial compris) d'un
    chemin `/llm/<slug>/<suite>`, ou None. La suite garde son encodage
    d'origine : la décoder puis la réencoder changerait ce qui part."""
    path = raw_path.decode("latin-1")
    head = PREFIX + "/"
    if not path.startswith(head):
        return None
    slug, sep, rest = path[len(head):].partition("/")
    if not slug or not sep:
        return None
    return slug, "/" + rest


def safe_suffix(suffix: str) -> bool:
    """Ni segment `.`/`..` (encodé compris), ni suite vide : le relais ne sort
    pas du chemin de `base_url` sur l'hôte amont."""
    if suffix in ("", "/"):
        return False
    for segment in suffix.split("/"):
        if unquote(segment) in (".", ".."):
            return False
    return True


# --- App ASGI ---------------------------------------------------------------


def _error_body(message: str, code: str, kind: str) -> bytes:
    return json.dumps(
        {"error": {"message": message, "type": kind, "code": code}}, ensure_ascii=False
    ).encode("utf-8")


async def _send_error(send: Any, status: int, message: str, code: str, kind: str = "invalid_request_error") -> None:
    body = _error_body(message, code, kind)
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class LlmRelay:
    """L'app ASGI du relais. `port` : port d'écoute, pour la garde `Host`.
    `transport` : transport httpx2 injectable (tests)."""

    def __init__(self, backends: dict[str, Backend], port: int | None, transport: Any = None) -> None:
        self.backends = backends
        self.port = port
        self._transport = transport
        self._client: Any = None

    def _http(self) -> Any:
        """Client partagé, créé au premier appel (dans la boucle du serveur).
        `trust_env` au défaut : --proxy/--noproxy, posés dans os.environ par
        main(), s'appliquent au relais comme aux upstreams http."""
        if self._client is None:
            import httpx2

            client = httpx2.AsyncClient(transport=self._transport, follow_redirects=False)
            # Aucun User-Agent sans déclaration : ce qui part est ce que la
            # config dit, pas l'identité du client HTTP.
            client.headers.pop("user-agent", None)
            self._client = client
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        from starlette.datastructures import Headers

        headers = Headers(scope=scope)
        if not loopback_same_origin(headers, self.port, fetch_metadata=True):
            where = f"http://127.0.0.1:{self.port}/app/" if self.port else "/app/ de ce proxy"
            await _send_error(
                send, 403,
                f"Relais LLM : seul MIAOU servi par ce proxy peut l'employer. Ouvrir "
                f"MIAOU à {where} (ou par localhost).",
                ORIGIN_REFUSED, "permission_error",
            )
            return
        split = split_path(scope.get("raw_path") or scope["path"].encode("utf-8"))
        backend = self.backends.get(split[0]) if split else None
        if split is None or backend is None:
            known = ", ".join(sorted(self.backends)) or "aucun"
            await _send_error(
                send, 404,
                f"Relais LLM : backend inconnu ou désactivé. Chemin attendu "
                f"{PREFIX}/<slug>/…, backends servis : {known}.",
                UNKNOWN_BACKEND, "not_found_error",
            )
            return
        _, suffix = split
        if not safe_suffix(suffix):
            await _send_error(
                send, 400,
                f"Relais LLM : chemin refusé après {PREFIX}/{backend.slug} (vide, ou "
                f"segment . ou ..).",
                INVALID_PATH,
            )
            return
        await self._relay(backend, suffix, scope, headers, receive, send)

    async def _relay(self, backend: Backend, suffix: str, scope: Any, headers: Any, receive: Any, send: Any) -> None:
        import anyio
        import httpx2

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break

        query = scope.get("query_string") or b""
        url = backend.base_url + suffix + (("?" + query.decode("latin-1")) if query else "")
        client = self._http()
        request = client.build_request(
            scope["method"],
            url,
            content=bytes(body) if body else None,
            headers=apply_rules(backend.request_headers, headers.items()),
            timeout=httpx2.Timeout(CONNECT_TIMEOUT_S, read=backend.timeout),
        )
        try:
            response = await client.send(request, stream=True)
        except httpx2.TimeoutException as e:
            _log(f"Relais LLM '{backend.slug}' : délai dépassé ({type(e).__name__}).")
            await _send_error(
                send, 504,
                f"Relais LLM : le backend '{backend.slug}' n'a pas répondu à temps "
                f"({type(e).__name__}).",
                UPSTREAM_TIMEOUT, "api_error",
            )
            return
        except httpx2.HTTPError as e:
            _log(f"Relais LLM '{backend.slug}' : injoignable ({type(e).__name__}: {e}).")
            await _send_error(
                send, 502,
                f"Relais LLM : backend '{backend.slug}' injoignable "
                f"({type(e).__name__}: {e}).",
                UPSTREAM_UNREACHABLE, "api_error",
            )
            return

        out = apply_rules(backend.response_headers, list(response.headers.multi_items()))
        try:
            await send({
                "type": "http.response.start",
                "status": response.status_code,
                "headers": [(n.encode("latin-1"), v.encode("latin-1")) for n, v in out],
            })

            async def stream() -> None:
                try:
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            await send({"type": "http.response.body", "body": chunk, "more_body": True})
                except httpx2.HTTPError as e:
                    _log(
                        f"Relais LLM '{backend.slug}' : réponse interrompue "
                        f"({type(e).__name__}: {e}) ; connexion coupée."
                    )
                    raise
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                tg.cancel_scope.cancel()

            async def watch_disconnect() -> None:
                # Le client parti, la génération amont s'arrête aussi : sinon
                # elle continue (et se facture) jusqu'à son terme.
                while (await receive())["type"] != "http.disconnect":
                    pass
                tg.cancel_scope.cancel()

            async with anyio.create_task_group() as tg:
                tg.start_soon(watch_disconnect)
                tg.start_soon(stream)
        finally:
            await response.aclose()
