"""Tests unitaires pour servers/mcp_base.py (T1 — zéro test jusqu'ici)."""
import sys
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "servers"))

from mcp_base import MiaouMCPBase, make_opener


# ---------------------------------------------------------------------------
# Patch global ArgModelBase extra="forbid" — rejet des arguments non déclarés
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_tool_argument_is_rejected():
    """Un argument non déclaré dans la signature de l'outil doit produire une
    erreur de validation explicite, pas être avalé silencieusement (le patch
    ArgModelBase.model_config["extra"] = "forbid" dans mcp_base.py)."""
    from mcp_bench import server as bench_server

    tm = bench_server.mcp._tool_manager
    with patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(Exception):
            await tm.call_tool("echo", {"text": "bonjour", "page": 2})


@pytest.mark.asyncio
async def test_known_tool_argument_still_works():
    """Non-régression : un appel avec seulement des arguments déclarés
    continue de fonctionner normalement sous le patch extra=forbid."""
    from mcp_bench import server as bench_server

    tm = bench_server.mcp._tool_manager
    with patch("asyncio.sleep", new=AsyncMock()):
        result = await tm.call_tool("echo", {"text": "bonjour"})
    assert result == "bonjour"


# ---------------------------------------------------------------------------
# finalize_tools : cleandoc des descriptions + strip des "title" Pydantic
# ---------------------------------------------------------------------------

class _DummyServer(MiaouMCPBase):
    def __init__(self) -> None:
        super().__init__("dummy-test-server", default_port=9999)

        async def indented_tool(char_start: int = 0, title: str = "") -> str:
            return "ok"

        indented_tool.__doc__ = """Ligne 1.
        Ligne 2 avec indentation source.
        Ligne 3."""
        self.mcp.tool(name="indented_tool")(indented_tool)

        self.finalize_tools()


def test_finalize_tools_cleandocs_manually_assigned_docstring():
    """Une docstring assignée à la main (func.__doc__ = f\"\"\"...\"\"\", pattern
    des caps interpolés) part sur le wire avec l'indentation source de chaque
    ligne de continuation si finalize_tools ne l'a pas cleandoc-ée."""
    server = _DummyServer()
    tool = server.mcp._tool_manager._tools["indented_tool"]
    assert "        " not in tool.description
    assert tool.description == "Ligne 1.\nLigne 2 avec indentation source.\nLigne 3."


def test_finalize_tools_strips_pydantic_title_but_keeps_param_named_title():
    """Les clés "title" auto-générées par Pydantic dans le schéma disparaissent,
    mais un paramètre d'outil qui s'appelle lui-même "title" (une clé de
    properties, pas une clé de schéma) doit survivre."""
    server = _DummyServer()
    tool = server.mcp._tool_manager._tools["indented_tool"]
    schema = tool.parameters
    assert "title" not in schema
    assert "title" in schema["properties"]


def test_finalize_tools_is_idempotent():
    server = _DummyServer()
    tool = server.mcp._tool_manager._tools["indented_tool"]
    before = tool.description
    server.finalize_tools()
    assert tool.description == before


# ---------------------------------------------------------------------------
# make_opener — proxy-aware via ProxyHandler()/getproxies() (B2)
# ---------------------------------------------------------------------------

def test_make_opener_returns_opener_director():
    opener = make_opener()
    assert isinstance(opener, urllib.request.OpenerDirector)


def test_make_opener_picks_up_http_proxy_env(monkeypatch):
    """make_opener() délègue à ProxyHandler() sans arguments (B2) : le mapping
    scheme -> proxy passe par urllib.request.getproxies(), lu à la construction
    du handler à partir des variables d'environnement standard."""
    monkeypatch.setenv("http_proxy", "http://proxy.example.com:8080")
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)

    opener = make_opener()
    proxy_handler = next(
        h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)
    )
    assert proxy_handler.proxies.get("http") == "http://proxy.example.com:8080"


def test_make_opener_without_proxy_env_has_no_proxy_mapping(monkeypatch):
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(var, raising=False)

    opener = make_opener()
    proxy_handlers = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    # ProxyHandler() sans proxies configurés n'ajoute aucun handler par scheme
    # (build_opener n'installe le handler que s'il a au moins un scheme géré) ;
    # dans les deux cas, aucune requête http/https ne doit être proxifiée.
    assert not any(h.proxies.get("http") or h.proxies.get("https") for h in proxy_handlers)


# ---------------------------------------------------------------------------
# Magasin de confiance système (truststore) — AC d'entreprise interne
# ---------------------------------------------------------------------------

def test_system_trust_store_patches_ssl_context_class():
    """L'injection doit remplacer ssl.SSLContext lui-même.

    C'est la propriété qui rend un seul appel suffisant pour tout le process :
    urllib comme httpx construisent leur contexte via ssl.create_default_context(),
    donc via la classe patchée. Un test qui se contenterait de vérifier « ça ne
    lève pas » ne dirait rien de cette portée.

    L'injection est globale et irréversible dans un process, donc exécutée dans
    un sous-process : la poser dans celui de pytest contaminerait les autres
    tests (et le mock d'OpenerDirector.open de test_weather/test_ddg)."""
    import subprocess

    code = (
        "import ssl, sys; sys.path.insert(0, 'servers');"
        "from mcp_base import enable_system_trust_store;"
        "before = ssl.SSLContext;"
        "ok = enable_system_trust_store();"
        "print(ok, before is not ssl.SSLContext,"
        " isinstance(ssl.create_default_context(), ssl.SSLContext))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True,
        cwd=str(Path(__file__).parent.parent),
    )
    assert out.returncode == 0, out.stderr
    injected, class_replaced, default_ctx_ok = out.stdout.split()
    assert injected == "True", out.stderr
    assert class_replaced == "True"
    # Le contexte par défaut (celui qu'utilisent urllib et httpx) est bien une
    # instance de la classe injectée, pas de l'implémentation stdlib d'origine.
    assert default_ctx_ok == "True"


def test_system_trust_store_absent_does_not_raise(monkeypatch, capsys):
    """truststore absent (installation pip minimale) doit dégrader, pas planter.

    Sur un poste sans AC interne la vérification par bundle CA fonctionne déjà :
    faire échouer le démarrage y serait une régression pure."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "truststore":
            raise ImportError("No module named 'truststore'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    from mcp_base import enable_system_trust_store

    assert enable_system_trust_store() is False
    assert "truststore" in capsys.readouterr().err


def test_server_main_enables_system_trust_store(monkeypatch):
    """Le lancement standalone d'un serveur doit activer le magasin système.

    Point d'entrée unique des six serveurs : s'il cessait d'appeler le helper,
    chacun repartirait silencieusement sur le bundle CA."""
    import mcp_base

    calls = []
    monkeypatch.setattr(mcp_base, "enable_system_trust_store", lambda: calls.append(True))
    monkeypatch.setattr(sys, "argv", ["srv", "--transport", "stdio"])

    srv = MiaouMCPBase("t", default_port=9999)
    monkeypatch.setattr(srv, "run_stdio", lambda: None)
    srv.main()

    assert calls == [True]
