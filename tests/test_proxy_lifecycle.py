"""Extension `miaou/lifecycle` (lot AV-1b) : état de la config, redémarrage à
la demande par la sortie en 43, et son bout-en-bout sous `scripts/supervise.sh`."""
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent
_SERVERS = _ROOT / "servers"
for p in (_ROOT, _SERVERS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import mcp.types as types
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from pydantic import TypeAdapter

from mcp_proxy import configwatch as cw
from mcp_proxy import entry
from mcp_proxy import lifecycle as lc
from mcp_proxy import updater as up
from mcp_proxy.app import build_app
from mcp_proxy.layers import load_layers, resolve_chain
from mcp_proxy.server import InFlight, build_proxy_server
from tests.proxy_client import _BaseExceptionGroup, _single_cause

BASH = shutil.which("bash")
SUPERVISE = _ROOT / "scripts" / "supervise.sh"


def _write(path: Path, data) -> Path:
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _rpc(server, method: str, params: dict | None = None, mode: str = "2026-07-28") -> dict:
    try:
        async with Client(server, mode=mode) as client:
            request = types.Request[Any, str](method=method, params=params)
            return await client.session.send_request(request, TypeAdapter(dict[str, Any]))
    except _BaseExceptionGroup as group:
        cause = _single_cause(group)
        if cause is None:
            raise
        raise cause from None


def _contract(result: dict) -> dict:
    """Les champs du contrat, sans ce qu'ajoute le SDK (`_meta`, `resultType`)."""
    return {k: result[k] for k in ("restarting", "instance")}


async def _refusal(server, params: dict | None = None) -> MCPError:
    with pytest.raises(MCPError) as info:
        await _rpc(server, lc.RESTART_METHOD, params)
    return info.value


@pytest.fixture
def supervised(monkeypatch):
    monkeypatch.setenv(up.SUPERVISED_ENV, "1")
    monkeypatch.setenv(up.RESTART_BY_EXIT_ENV, "1")


@pytest.fixture(autouse=True)
def unsupervised_by_default(monkeypatch):
    """La suite peut tourner elle-même sous supervise.sh."""
    monkeypatch.delenv(up.SUPERVISED_ENV, raising=False)
    monkeypatch.delenv(up.RESTART_BY_EXIT_ENV, raising=False)


class Harness:
    """Une `Lifecycle` sur une chaîne jetable, la sortie demandée notée au
    lieu d'arrêter quoi que ce soit."""

    def __init__(self, tmp_path: Path, applying=None, options: cw.LaunchOptions | None = None):
        self.dir = tmp_path
        self.options = options or cw.LaunchOptions()
        paths, local = resolve_chain(None)
        prepared = cw.prepare_config(load_layers(paths, local, env={}), self.options)
        self.in_flight = InFlight()
        self.exits: list[int] = []
        self.identity = lc.ProcessIdentity()
        self.lifecycle = lc.Lifecycle(
            self.identity,
            cw.ConfigWatch(self.options, {}, prepared),
            self.in_flight,
            request_exit=self.exits.append,
            exit_pending=lambda: bool(self.exits),
            restartable=up.restartable,
            exit_code=entry.RESTART_EXIT_CODE,
            applying=applying,
        )
        self.server = build_proxy_server({}, {}, in_flight=self.in_flight)
        self.lifecycle.install(self.server)

    def edit(self, data) -> None:
        _write(self.dir / "config.json", data)

    async def status(self) -> dict:
        return (await _rpc(self.server, lc.STATUS_METHOD, {}))["status"]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "config.json", {"port": 8765, "mcpServers": {}})
    return Harness(tmp_path)


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------

async def test_extension_is_declared_in_the_modern_era(harness):
    async with Client(harness.server, mode="auto") as client:
        assert lc.EXTENSION_ID in (client.server_capabilities.extensions or {})


async def test_status_at_start(harness):
    status = await harness.status()
    assert status["instance"] == harness.identity.instance
    assert datetime.fromisoformat(status["started_at"]).utcoffset().total_seconds() == 0
    assert status["restartable"] is False
    assert status["restarting"] is False
    config = status["config"]
    assert config["state"] == cw.UNCHANGED
    assert config["changed"] == [] and config["changed_total"] == 0
    assert config["detail"] is None
    assert config["layers"] == ["config.json"]
    datetime.fromisoformat(config["checked_at"])


@pytest.mark.parametrize("env, expected", [
    ({}, False),
    ({up.SUPERVISED_ENV: "1"}, False),
    ({up.RESTART_BY_EXIT_ENV: "1"}, False),
    ({up.SUPERVISED_ENV: "1", up.RESTART_BY_EXIT_ENV: "1"}, True),
])
async def test_restartable_needs_both_variables(harness, monkeypatch, env, expected):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert (await harness.status())["restartable"] is expected


async def test_status_rereads_the_config(harness):
    harness.edit({"port": 8765, "mcpServers": {"bench": {"type": "inprocess", "module": "mcp_bench"}}})
    config = (await harness.status())["config"]
    assert config["state"] == cw.CHANGED
    assert config["changed"] == ["mcpServers.bench"]
    harness.edit('{"port": ')
    config = (await harness.status())["config"]
    assert config["state"] == cw.INVALID
    assert "n'est pas un JSON valide" in config["detail"]


async def test_changed_paths_are_capped_with_the_total(harness):
    servers = {f"s{i:02}": {"type": "inprocess", "module": "m"} for i in range(25)}
    harness.edit({"port": 8765, "mcpServers": servers})
    config = (await harness.status())["config"]
    assert len(config["changed"]) == lc.MAX_CHANGED_PATHS
    assert config["changed_total"] == 25


async def test_methods_are_not_tools(harness):
    tools = await _rpc(harness.server, "tools/list", {})
    assert all("lifecycle" not in t["name"] for t in tools["tools"])


# ---------------------------------------------------------------------------
# restart : refus dans l'ordre, forçage, acceptation
# ---------------------------------------------------------------------------

def _everything_wrong(harness: Harness) -> None:
    harness.edit('{"port": ')
    harness.in_flight.count = 2


async def test_refused_without_supervisor_first(harness):
    applying = True
    harness = Harness(harness.dir, applying=lambda: applying)
    _everything_wrong(harness)
    error = await _refusal(harness.server, {"force": True})
    assert error.error.data["code"] == lc.NOT_SUPERVISED
    assert error.error.code == types.INVALID_REQUEST
    assert harness.exits == []


async def test_refused_during_an_update_second(harness, supervised):
    harness = Harness(harness.dir, applying=lambda: True)
    _everything_wrong(harness)
    error = await _refusal(harness.server, {"force": True})
    assert error.error.data["code"] == lc.UPDATE_IN_PROGRESS
    assert harness.exits == []


async def test_refused_on_invalid_config_third_with_its_cause(harness, supervised):
    _everything_wrong(harness)
    error = await _refusal(harness.server, {"force": True})
    assert error.error.data["code"] == lc.CONFIG_INVALID
    assert "n'est pas un JSON valide" in error.error.data["detail"]
    assert error.error.data["detail"] in error.error.message
    assert harness.exits == []


async def test_refused_with_tool_calls_in_flight_last(harness, supervised):
    harness.in_flight.count = 2
    error = await _refusal(harness.server)
    assert error.error.data == {"code": lc.TOOLS_IN_FLIGHT, "count": 2}
    assert harness.exits == []


async def test_force_lifts_only_the_calls_in_flight(harness, supervised, capsys):
    harness.in_flight.count = 2
    result = _contract(await _rpc(harness.server, lc.RESTART_METHOD, {"force": True}))
    assert result == {"restarting": True, "instance": harness.identity.instance}
    assert harness.exits == [43]
    assert "2 appel(s) d'outil en cours coupé(s)" in capsys.readouterr().err


async def test_accepted_without_any_change(harness, supervised):
    """Point 4 de l'intention : redémarrer d'un geste volontaire, config
    inchangée."""
    result = _contract(await _rpc(harness.server, lc.RESTART_METHOD, {}))
    assert result == {"restarting": True, "instance": harness.identity.instance}
    assert harness.exits == [entry.RESTART_EXIT_CODE]
    assert (await harness.status())["restarting"] is True


async def test_accepted_with_a_valid_change(harness, supervised):
    harness.edit({"port": 8765, "mcpServers": {}, "self_update": False})
    await _rpc(harness.server, lc.RESTART_METHOD, {})
    assert harness.exits == [43]


async def test_restart_is_idempotent(harness, supervised):
    first = await _rpc(harness.server, lc.RESTART_METHOD, {})
    harness.in_flight.count = 3
    harness.edit('{"port": ')
    second = await _rpc(harness.server, lc.RESTART_METHOD, {})
    assert first == second
    assert harness.exits == [43]


async def test_concurrent_restarts_ask_once(harness, supervised):
    results = await asyncio.gather(
        harness.lifecycle.handle_restart(None, None),
        harness.lifecycle.handle_restart(None, None),
    )
    assert results[0] == results[1]
    assert harness.exits == [43]


async def test_an_exit_already_requested_by_an_update_counts_as_restarting(harness, supervised):
    harness.exits.append(entry.UPDATE_EXIT_CODE)
    result = await _rpc(harness.server, lc.RESTART_METHOD, {})
    assert result["restarting"] is True
    assert harness.exits == [42]


async def test_restart_redoes_the_bind(harness, supervised):
    """Le port libre à la lecture de l'état peut être pris depuis."""
    port = _free_port()
    harness.edit({"port": port, "mcpServers": {}})
    assert (await harness.status())["config"]["state"] == cw.CHANGED
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", port))
        busy.listen()
        error = await _refusal(harness.server)
    assert error.error.data["code"] == lc.CONFIG_INVALID
    assert f"127.0.0.1:{port}" in error.error.data["detail"]
    assert harness.exits == []


# ---------------------------------------------------------------------------
# Mise à jour (AT) pendant un redémarrage
# ---------------------------------------------------------------------------

async def test_apply_is_refused_once_an_exit_is_pending(tmp_path):
    """Sans quoi git tournerait pendant l'arrêt du processus. Refusé avant
    tout appel à git : `root` ne pointe sur aucun clone."""
    updater = up.SelfUpdater(
        up.UpdateConfig(root=tmp_path, miaou_dist=None, supervised=True, state_file=None),
        InFlight(),
        exit_pending=lambda: True,
    )
    server = build_proxy_server({}, {})
    updater.install(server)
    with pytest.raises(MCPError) as info:
        await _rpc(server, up.APPLY_METHOD, {"target": "a" * 40})
    assert info.value.error.data["code"] == up.RESTARTING


# ---------------------------------------------------------------------------
# main() : extension publiée par défaut, une seule identité de processus
# ---------------------------------------------------------------------------

def _main_build_app_kwargs(tmp_path, monkeypatch, extra: dict | None = None) -> dict:
    conf = _write(tmp_path / "config.json", {"port": 8765, "mcpServers": {
        "secret": {"type": "inprocess", "module": "volatile_fixture_server",
                   "config": {"password": {"volatile": "k"}}},
    }, **(extra or {})})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mcp_proxy", "--config", str(conf)])
    monkeypatch.setattr(entry, "enable_system_trust_store", lambda: None)
    seen: dict = {"conf": conf}
    real_build_app = entry.build_app

    def spy(*a, **kw):
        seen.update(kw)
        seen["server"] = a[0]
        return real_build_app(*a, **kw)

    monkeypatch.setattr(entry, "build_app", spy)
    monkeypatch.setattr(entry, "serve_http", lambda *a, **kw: None)
    entry.main()
    return seen


def test_main_builds_the_extension_by_default_and_shares_the_instance(tmp_path, monkeypatch):
    seen = _main_build_app_kwargs(tmp_path, monkeypatch)
    conf = seen["conf"]
    lifecycle, secrets = seen["lifecycle"], seen["secrets"]
    assert lc.EXTENSION_ID in seen["server"].extensions
    assert isinstance(lifecycle, lc.Lifecycle)
    assert secrets.instance == lifecycle.identity.instance
    assert lifecycle.exit_code == entry.RESTART_EXIT_CODE
    assert lifecycle.watch.options.config == (str(conf),)


def test_main_without_the_lifecycle_key_publishes_nothing(tmp_path, monkeypatch):
    """`lifecycle: false` : ni `Lifecycle`, ni extension déclarée, ni méthode ;
    l'identité reste celle des secrets."""
    seen = _main_build_app_kwargs(tmp_path, monkeypatch, {"lifecycle": False})
    assert seen["lifecycle"] is None
    server = seen["server"]
    assert lc.EXTENSION_ID not in server.extensions
    for method in (lc.STATUS_METHOD, lc.RESTART_METHOD):
        with pytest.raises(MCPError) as info:
            asyncio.run(_rpc(server, method, {}))
        assert info.value.error.code == types.METHOD_NOT_FOUND
    assert seen["secrets"].instance


@pytest.mark.parametrize("value, expected", [(None, True), (True, True), (False, False)])
def test_resolve_lifecycle(value, expected):
    cfg = {} if value is None else {"lifecycle": value}
    assert cw.resolve_lifecycle(cfg) is expected
    assert cw.resolve_lifecycle({"lifecycle": None}) is True


# ---------------------------------------------------------------------------
# De bout en bout : vrai uvicorn, vrai transport, sortie en 43
# ---------------------------------------------------------------------------

def _wait_for_port(port: int, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"port {port} jamais ouvert")


async def _wire_rpc(port: int, method: str, params: dict) -> dict:
    transport = streamable_http_client(f"http://127.0.0.1:{port}/mcp")
    async with Client(transport, mode="2026-07-28") as client:
        request = types.Request[Any, str](method=method, params=params)
        return await client.session.send_request(request, TypeAdapter(dict[str, Any]))


def test_restart_over_the_wire_exits_with_43(tmp_path, monkeypatch, supervised):
    """`serve_http` dans le thread principal (gestionnaires de signaux), le
    client dans un autre : la réponse part, puis le processus sort en 43."""
    port = _free_port()
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "config.json", {"port": port, "mcpServers": {}})
    options = cw.LaunchOptions()
    prepared = cw.prepare_config(load_layers(*resolve_chain(None), env={}), options)
    exit_request = entry.ExitRequest()
    in_flight = InFlight()
    identity = lc.ProcessIdentity()
    lifecycle = lc.Lifecycle(
        identity, cw.ConfigWatch(options, {}, prepared), in_flight,
        request_exit=exit_request, exit_pending=lambda: exit_request.pending,
        restartable=up.restartable, exit_code=entry.RESTART_EXIT_CODE,
    )
    app = build_app(build_proxy_server({}, {}, in_flight=in_flight), {}, lifecycle=lifecycle)
    outcome: dict = {}

    def client():
        try:
            _wait_for_port(port, time.monotonic() + 15)
            outcome["result"] = asyncio.run(_wire_rpc(port, lc.RESTART_METHOD, {}))
        except BaseException as e:  # noqa: BLE001 - rapporté au test
            outcome["error"] = e
            os.kill(os.getpid(), signal.SIGINT)

    watchdog = threading.Timer(30, lambda: os.kill(os.getpid(), signal.SIGINT))
    previous = entry.RESTART_SIGNAL and signal.getsignal(entry.RESTART_SIGNAL)
    thread = threading.Thread(target=client)
    watchdog.start()
    thread.start()
    code = None
    try:
        entry.serve_http(app, "127.0.0.1", port, exit_request=exit_request)
    except SystemExit as e:
        code = e.code
    finally:
        watchdog.cancel()
        thread.join(10)
        if entry.RESTART_SIGNAL is not None:
            signal.signal(entry.RESTART_SIGNAL, previous)
    if "error" in outcome:
        raise outcome["error"]
    assert _contract(outcome["result"]) == {"restarting": True, "instance": identity.instance}
    assert code == 43


def _status_over_the_wire(port: int, deadline: float) -> dict:
    """L'état, dès que le proxy répond : pendant une relance, le port se
    ferme puis se rouvre."""
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return asyncio.run(_wire_rpc(port, lc.STATUS_METHOD, {}))["status"]
        except BaseException as e:  # noqa: BLE001 - relance en cours
            last = e
            time.sleep(0.2)
    raise TimeoutError(f"proxy muet sur {port} ({last!r})")


@pytest.mark.skipif(BASH is None or not hasattr(os, "killpg"), reason="bash et groupes POSIX requis")
def test_supervised_proxy_comes_back_as_another_instance(tmp_path):
    """Le vrai `main()` sous `scripts/supervise.sh` : `restart` → sortie en
    43 → relance par le superviseur → un `instance` différent. Le groupe de
    processus est tué à la fin, lui seul (jamais un motif)."""
    port = _free_port()
    conf = _write(tmp_path / "config.json", {"port": port, "mcpServers": {}})
    log = open(tmp_path / "proxy.log", "w")
    proc = subprocess.Popen(
        [BASH, str(SUPERVISE), sys.executable, "-m", "mcp_proxy", "--config", str(conf)],
        cwd=_ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        before = _status_over_the_wire(port, time.monotonic() + 30)
        assert before["restartable"] is True
        assert before["config"]["state"] == cw.UNCHANGED
        _write(conf, {"port": port, "mcpServers": {}, "self_update": False})
        assert _status_over_the_wire(port, time.monotonic() + 5)["config"]["changed"] == ["self_update"]
        result = _contract(asyncio.run(_wire_rpc(port, lc.RESTART_METHOD, {})))
        assert result == {"restarting": True, "instance": before["instance"]}
        deadline = time.monotonic() + 30
        while True:
            after = _status_over_the_wire(port, deadline)
            if after["instance"] != before["instance"]:
                break
            assert time.monotonic() < deadline, "jamais relancé"
            time.sleep(0.2)
        # La config relue au démarrage devient la nouvelle référence.
        assert after["config"]["state"] == cw.UNCHANGED
        assert after["restarting"] is False
        assert proc.poll() is None
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(15)
        log.close()
    output = (tmp_path / "proxy.log").read_text()
    assert "sortie en 43 : redémarrage demandé par un client" in output
    assert "supervise: redémarrage demandé" in output
