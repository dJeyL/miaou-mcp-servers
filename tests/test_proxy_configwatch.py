"""Surveillance de la config (lot AV-1a) : empreinte, chemins changés,
validation partagée avec `main()`, `bind` d'essai."""
import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_SERVERS = _ROOT / "servers"
for p in (_ROOT, _SERVERS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from mcp_proxy import configwatch as cw
from mcp_proxy import entry
from mcp_proxy.layers import load_layers, resolve_chain


def _write(path: Path, data) -> Path:
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


BASE = {"port": 8765, "mcpServers": {"bench": {"type": "inprocess", "module": "mcp_bench"}}}


def _watch(options: cw.LaunchOptions | None = None, env: dict | None = None) -> cw.ConfigWatch:
    """Ce que fait `main()` : chaîne, couches, `prepare_config`, puis la
    surveillance prend ce dict-là pour référence."""
    options = options or cw.LaunchOptions()
    env = {} if env is None else env
    paths, local = resolve_chain(list(options.config) if options.config else None)
    prepared = cw.prepare_config(load_layers(paths, local, env=env), options)
    return cw.ConfigWatch(options, env, prepared)


def _check(watch: cw.ConfigWatch, **kw) -> cw.ConfigCheck:
    return asyncio.run(watch.check(**kw))


@pytest.fixture
def chain(tmp_path, monkeypatch):
    """Chaîne par défaut dans un dossier jetable : defaults + config.json."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "config.defaults.json", BASE)
    _write(tmp_path / "config.json", {"mcpServers": {}})
    return tmp_path


# ---------------------------------------------------------------------------
# Empreinte et chemins
# ---------------------------------------------------------------------------

def test_fingerprint_ignores_comments_and_key_order():
    a = {"port": 1, "x": {"b": 2, "a": 1}}
    b = {"_comment": "note", "x": {"a": 1, "b": 2, "_comment_b": "n"}, "port": 1}
    assert cw.fingerprint(a) == cw.fingerprint(b)
    assert cw.fingerprint(a) != cw.fingerprint({**a, "port": 2})


def test_fingerprint_is_sha256_of_canonical_json():
    import hashlib
    assert cw.fingerprint({"é": 1, "a": [1]}) == hashlib.sha256(
        '{"a":[1],"é":1}'.encode("utf-8")
    ).hexdigest()


def test_changed_paths():
    before = {
        "port": 1, "host": "a", "_comment": "x",
        "mcpServers": {"bench": {"module": "b"}, "web": {"url": "u"}, "gone": {}},
    }
    after = {
        "port": 2, "_comment": "y", "auth": {"issuer_url": "i"},
        "mcpServers": {"bench": {"module": "b", "_comment": "z"}, "web": {"url": "v"}, "new": {}},
    }
    assert cw.changed_paths(before, after) == [
        "auth", "host", "mcpServers.gone", "mcpServers.new", "mcpServers.web", "port",
    ]


def test_changed_paths_when_servers_is_not_an_object():
    assert cw.changed_paths({"mcpServers": {}}, {"mcpServers": []}) == ["mcpServers"]


# ---------------------------------------------------------------------------
# Relecture de la chaîne
# ---------------------------------------------------------------------------

def test_unchanged_at_start(chain):
    result = _check(_watch())
    assert result.state == cw.UNCHANGED
    assert result.changed == [] and result.detail is None
    assert result.layers == ["config.defaults.json", "config.json"]


def test_resaved_identically_or_reformatted_is_unchanged(chain):
    watch = _watch()
    (chain / "config.json").write_text('{\n    "mcpServers": {}\n}\n')
    assert _check(watch).state == cw.UNCHANGED


def test_comment_only_is_unchanged(chain):
    watch = _watch()
    _write(chain / "config.json", {"_comment": "note", "mcpServers": {"_comment_x": "y"}})
    assert _check(watch).state == cw.UNCHANGED


def test_value_change_is_changed_with_paths(chain):
    watch = _watch()
    _write(chain / "config.json", {"mcpServers": {"bench": {"type": "inprocess", "module": "autre"}}})
    result = _check(watch)
    assert result.state == cw.CHANGED
    assert result.changed == ["mcpServers.bench"]
    assert result.detail is None


def test_a_layer_appearing_counts(chain):
    (chain / "config.json").unlink()
    watch = _watch()
    assert _check(watch).layers == ["config.defaults.json"]
    _write(chain / "config.site.json", {"host": "127.0.0.1"})
    result = _check(watch)
    assert result.state == cw.CHANGED
    assert result.changed == ["host"]
    assert result.layers == ["config.defaults.json", "config.site.json"]


def test_a_layer_disappearing_counts(chain):
    _write(chain / "config.json", {"host": "127.0.0.1"})
    watch = _watch()
    (chain / "config.json").unlink()
    result = _check(watch)
    assert result.state == cw.CHANGED
    assert result.changed == ["host"]


def test_reference_is_what_main_loaded_not_a_reread(chain):
    """Une édition faite entre le chargement et la construction de la
    surveillance ne devient pas l'état initial."""
    paths, local = resolve_chain(None)
    prepared = cw.prepare_config(load_layers(paths, local, env={}), cw.LaunchOptions())
    _write(chain / "config.json", {"host": "127.0.0.1"})
    watch = cw.ConfigWatch(cw.LaunchOptions(), {}, prepared)
    assert _check(watch).state == cw.CHANGED


def test_env_change_after_load_is_not_a_config_change(chain, monkeypatch):
    """L'empreinte précède la substitution : `--proxy` ou l'`env` d'un
    upstream inprocess modifient `os.environ` après le chargement."""
    _write(chain / "config.json", {"mcpServers": {"bench": {"env": {"http_proxy": "${http_proxy}"}}}})
    monkeypatch.setenv("http_proxy", "http://a:1")
    watch = _watch(env=dict(__import__("os").environ))
    monkeypatch.setenv("http_proxy", "http://b:2")
    assert _check(watch).state == cw.UNCHANGED


def test_validation_substitutes_against_the_launch_env(chain, monkeypatch):
    """Le processus relancé retrouvera l'environnement de son superviseur, pas
    un `os.environ` modifié depuis : c'est contre lui que la validation
    substitue."""
    monkeypatch.delenv("AV_TEST_PORT", raising=False)
    watch = _watch(env={"AV_TEST_PORT": "8800"})
    monkeypatch.setenv("AV_TEST_PORT", "pas-un-port")
    _write(chain / "config.json", {"port": "${AV_TEST_PORT}"})
    monkeypatch.setattr(cw, "probe_bind", _no_bind)
    assert _check(watch).state == cw.CHANGED


def test_truncated_json_is_invalid_then_recovers(chain):
    watch = _watch()
    (chain / "config.json").write_text('{"mcpServers": {"bench": ')
    result = _check(watch)
    assert result.state == cw.INVALID
    assert "n'est pas un JSON valide" in result.detail
    assert result.layers == ["config.defaults.json", "config.json"]
    _write(chain / "config.json", {"host": "127.0.0.1"})
    assert _check(watch).state == cw.CHANGED


def test_explicit_config_gone_is_invalid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _write(tmp_path / "autre.json", BASE)
    watch = _watch(cw.LaunchOptions(config=(str(cfg),)))
    cfg.unlink()
    result = _check(watch)
    assert result.state == cw.INVALID
    assert "config introuvable" in result.detail and "autre.json" in result.detail
    assert result.layers == []


def test_explicit_chain_is_kept_as_given(tmp_path, monkeypatch):
    """`--config` remplace la chaîne : un config.json apparu à côté ne compte pas."""
    monkeypatch.chdir(tmp_path)
    cfg = _write(tmp_path / "autre.json", BASE)
    watch = _watch(cw.LaunchOptions(config=(str(cfg),)))
    _write(tmp_path / "config.json", {"port": 1})
    assert _check(watch).state == cw.UNCHANGED


def test_unreadable_layer_is_invalid_not_an_exception(chain, monkeypatch):
    watch = _watch()

    def refuse(self, *a, **kw):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "read_text", refuse)
    result = _check(watch)
    assert result.state == cw.INVALID
    assert "Permission denied" in result.detail


def test_validation_is_cached_per_fingerprint(chain, monkeypatch):
    calls = []
    real = cw.prepare_config
    monkeypatch.setattr(cw, "prepare_config", lambda *a: (calls.append(1), real(*a))[1])
    watch = _watch()
    calls.clear()
    assert _check(watch).state == cw.UNCHANGED
    assert calls == []  # inchangée : rien à valider
    _write(chain / "config.json", {"host": "127.0.0.1"})
    _check(watch)
    _check(watch)
    assert len(calls) == 1
    _write(chain / "config.json", {"host": "localhost"})
    monkeypatch.setattr(cw, "probe_bind", _no_bind)
    _check(watch)
    assert len(calls) == 2


def test_validation_logs_nothing_and_writes_nothing(chain, capsys):
    """Les fonctions du démarrage journalisent (ancien `_disabled`, endpoints
    OAuth déclarés) ; rejouées pour valider, elles se taisent. Et aucune
    n'écrit le fichier de jetons."""
    watch = _watch()
    capsys.readouterr()
    _write(chain / "config.json", {"mcpServers": {
        "old": {"type": "inprocess", "module": "x", "_disabled": True},
        "tiers": {"type": "http", "url": "http://127.0.0.1:1/mcp", "auth": {
            "redirect_uri": "http://127.0.0.1:8765/callback",
            "token_endpoint": "http://127.0.0.1:1/token",
            "authorization_endpoint": "http://127.0.0.1:1/authorize",
        }},
    }})
    result = _check(watch)
    assert result.state == cw.CHANGED, result.detail
    assert capsys.readouterr().err == ""
    assert not (chain / "state").exists()


# ---------------------------------------------------------------------------
# Validation partagée : chaque sortie en 1 de config de main() est un `invalid`
# ---------------------------------------------------------------------------

#: Inventaire des `except` de config de `main()` : lecture des couches
#: (`read_layer`, `load_layers`), hôte et port, `miaou_dist`, `self_update`,
#: `lifecycle`, auth entrante, `build_upstreams`, `build_upstream_authorizers`.
INVALID_CONFIGS = {
    "json": '{"port": ',
    "not-an-object": "[1]",
    "no-port": {"port": None},
    "port-not-a-number": {"port": "abc"},
    "port-not-a-scalar": {"port": [1]},
    "miaou_dist-type": {"miaou_dist": 3},
    "miaou_dist-git": {"miaou_dist": "."},
    "self_update-type": {"self_update": "yes"},
    "lifecycle-type": {"lifecycle": "false"},
    "auth-not-object": {"auth": "x"},
    "auth-no-issuer": {"auth": {"resource_url": "http://x/mcp"}},
    "inprocess-no-module": {"mcpServers": {"x": {"type": "inprocess"}}},
    "stdio-no-command": {"mcpServers": {"x": {"type": "stdio"}}},
    "http-no-url": {"mcpServers": {"x": {"type": "http"}}},
    "unknown-type": {"mcpServers": {"x": {"type": "ftp"}}},
    "protocol_era": {"mcpServers": {"x": {"type": "stdio", "command": "c", "protocol_era": "x"}}},
    "volatile-misplaced": {"mcpServers": {"x": {"type": "stdio", "command": "c", "env": {"P": {"volatile": "k"}}}}},
    "auth-on-stdio": {"mcpServers": {"x": {"type": "stdio", "command": "c", "auth": {}}}},
}


def _no_bind(host, port):
    async def ok():
        return None
    return ok()


@pytest.mark.parametrize("name", sorted(INVALID_CONFIGS))
def test_every_config_exit_of_main_is_invalid_with_the_same_cause(name, chain, monkeypatch, capsys):
    if name == "miaou_dist-git":
        (chain / ".git").mkdir()
    watch = _watch()
    _write(chain / "config.json", INVALID_CONFIGS[name])

    result = _check(watch)
    assert result.state == cw.INVALID, result
    assert result.detail

    monkeypatch.setattr(entry, "enable_system_trust_store", lambda: None)
    monkeypatch.setattr(entry, "serve_http", lambda *a, **kw: pytest.fail("main() a démarré"))
    monkeypatch.setattr(sys, "argv", ["mcp_proxy"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 1
    assert f"Erreur : {result.detail}" in capsys.readouterr().err


def test_command_line_overrides_are_replayed(chain, monkeypatch):
    """Une surcharge qui rend la clef fautive sans effet au démarrage la rend
    sans effet à la validation : `--no-auth` ignore le bloc `auth`, `--port`
    la clef `port`."""
    monkeypatch.setattr(cw, "probe_bind", _no_bind)
    watch = _watch(cw.LaunchOptions(cli_auth=False, port=9999))
    _write(chain / "config.json", {"auth": "x", "port": "abc"})
    result = _check(watch)
    assert result.state == cw.CHANGED, result.detail
    assert result.changed == ["auth", "port"]


def test_auth_override_without_auth_block_is_invalid(chain):
    """`--auth` exige un bloc `auth` : le retirer de la config, c'est un
    proxy qui ne redémarrerait pas."""
    _write(chain / "config.json", {"auth": {"issuer_url": "http://127.0.0.1:1"}})
    watch = _watch(cw.LaunchOptions(cli_auth=True))
    _write(chain / "config.json", {})
    result = _check(watch)
    assert result.state == cw.INVALID
    assert "--auth" in result.detail


# ---------------------------------------------------------------------------
# bind d'essai
# ---------------------------------------------------------------------------

def test_taken_port_is_invalid(chain):
    watch = _watch()
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        _write(chain / "config.json", {"port": port})
        result = _check(watch)
    assert result.state == cw.INVALID
    assert f"127.0.0.1:{port}" in result.detail


def test_free_port_is_changed(chain):
    watch = _watch()
    _write(chain / "config.json", {"port": _free_port()})
    assert _check(watch).state == cw.CHANGED


def test_no_bind_when_the_endpoint_does_not_change(chain, monkeypatch):
    """Le proxy écoute déjà sur ce port : un essai y échouerait toujours."""
    watch = _watch()
    monkeypatch.setattr(cw, "probe_bind", lambda *a: pytest.fail("bind inutile"))
    _write(chain / "config.json", {"mcpServers": {}, "self_update": False})
    assert _check(watch).state == cw.CHANGED


def test_no_bind_when_port_is_overridden(chain, monkeypatch):
    """`--port` rend un changement de `port` sans effet : pas d'essai, mais
    le chemin reste listé (c'est un fait)."""
    watch = _watch(cw.LaunchOptions(port=8765))
    monkeypatch.setattr(cw, "probe_bind", lambda *a: pytest.fail("bind inutile"))
    _write(chain / "config.json", {"port": 1})
    result = _check(watch)
    assert result.state == cw.CHANGED
    assert result.changed == ["port"]


def test_bind_is_cached_for_reads_and_redone_on_demand(chain, monkeypatch):
    calls = []

    async def probe(host, port):
        calls.append((host, port))
        return None

    monkeypatch.setattr(cw, "probe_bind", probe)
    watch = _watch()
    _write(chain / "config.json", {"port": 9100})
    _check(watch)
    _check(watch)
    assert calls == [("127.0.0.1", 9100)]
    _check(watch, fresh_bind=True)
    assert len(calls) == 2


def test_probe_bind_reports_an_impossible_port():
    assert "ne peut pas être ouvert" in asyncio.run(cw.probe_bind("127.0.0.1", 70000))
