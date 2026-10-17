"""Démarrage des upstreams par le lifespan : distants d'abord et en parallèle.

Upstreams factices (sous-classes de `StdioUpstream`/`HttpUpstream` dont `start()`
est remplacé) pour la durée et l'ordre du journal ; vrais subprocess stdio pour
le modèle de tâche (aucun « cancel scope » au journal, rien d'orphelin).
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest

for p in (Path(__file__).parent.parent, Path(__file__).parent.parent / "servers"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import mcp_proxy  # noqa: E402,F401
from mcp_proxy import build_app, build_proxy_server  # noqa: E402
from mcp_proxy.upstream import HttpUpstream, InProcessUpstream, StdioUpstream  # noqa: E402
from tests.test_proxy import _run_lifespan  # noqa: E402

TESTS = Path(__file__).parent
DELAY = 1.0


class _SlowStdio(StdioUpstream):
    def __init__(self, delay: float = DELAY, fail: bool = False) -> None:
        super().__init__(command="x", args=[])
        self.delay, self.fail = delay, fail
        self.stopped = False
        self.begun = None

    async def start(self) -> None:
        self.begun = time.monotonic()
        await anyio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("refus factice")

    async def stop(self) -> None:
        self.stopped = True

    async def list_tools(self):
        return []


class _SlowHttp(HttpUpstream):
    def __init__(self, delay: float = DELAY) -> None:
        super().__init__("http://x.test/mcp")
        self.delay = delay
        self.stopped = False
        self.begun = None

    async def start(self) -> None:
        self.begun = time.monotonic()
        await anyio.sleep(self.delay)

    async def stop(self) -> None:
        self.stopped = True

    async def list_tools(self):
        return []


class _Local(InProcessUpstream):
    def __init__(self) -> None:
        super().__init__("mcp_bench")
        self.begun = None

    async def start(self) -> None:
        self.begun = time.monotonic()
        await super().start()


async def _boot(upstreams: dict) -> float:
    app = build_app(build_proxy_server(upstreams, {}), upstreams)
    t0 = time.monotonic()
    async with _run_lifespan(app):
        return time.monotonic() - t0


async def test_remote_upstreams_start_in_parallel():
    ups = {"a": _SlowStdio(), "b": _SlowStdio(), "c": _SlowHttp()}
    elapsed = await _boot(ups)
    # Série : >= 3 s. Parallèle : ~1 s. Marge large dans les deux sens.
    assert elapsed < 2.2, elapsed


async def test_remote_upstreams_start_before_the_inprocess_ones():
    local = _Local()
    remote = _SlowStdio(delay=0.3)
    # L'inprocess est PREMIER dans la config : il ne passe pas devant pour autant.
    await _boot({"local": local, "remote": remote})
    assert remote.begun <= local.begun


async def test_a_failing_remote_does_not_cancel_the_others(capsys):
    ups = {"ok": _SlowStdio(), "bad": _SlowStdio(delay=0.2, fail=True), "web": _SlowHttp()}
    ok, web = ups["ok"], ups["web"]
    app = build_app(build_proxy_server(ups, {}), ups)
    async with _run_lifespan(app):
        assert set(ups) == {"ok", "web"}
    assert ok.stopped and web.stopped
    assert "bad          unavailable — refus factice" in capsys.readouterr().err


async def test_startup_log_follows_the_config_order(capsys):
    # Le dernier de la config finit le premier : l'affichage reste dans l'ordre.
    ups = {
        "slow": _SlowStdio(delay=0.6),
        "mid": _SlowHttp(delay=0.3),
        "local": _Local(),
        "fast": _SlowStdio(delay=0.0, fail=True),
    }
    await _boot(ups)
    err = capsys.readouterr().err
    order = [err.index(f"  {n:<12} ") for n in ("slow", "mid", "local", "fast")]
    assert order == sorted(order), err


async def test_cancelled_startup_stops_every_remote_started_or_pending():
    quick, hanging = _SlowStdio(delay=0.0), _SlowStdio(delay=60)
    ups = {"quick": quick, "hang": hanging}
    app = build_app(build_proxy_server(ups, {}), ups)

    sent: list = []

    async def receive():
        return {"type": "lifespan.startup"} if not sent else await anyio.sleep_forever()

    async def send(message):
        sent.append(message)

    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: app({"type": "lifespan"}, receive, send))
        with anyio.fail_after(10):
            while hanging.begun is None or quick.begun is None:
                await anyio.sleep(0.01)
        await anyio.sleep(0.2)
        tg.cancel_scope.cancel()
    assert quick.stopped is True


# --- Vrais subprocess : le modèle de tâche -----------------------------------------------


def _alive(marker: str) -> list[str]:
    out = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout
    return [pid for pid in out.split() if pid]


def _real(log: Path) -> StdioUpstream:
    # Le chemin du journal, unique par test, sert de marqueur au pgrep.
    return StdioUpstream(command=sys.executable, args=[str(TESTS / "legacy_stdio_fixture.py"), str(log)])


@pytest.mark.skipif(sys.platform == "win32", reason="pgrep")
async def test_real_stdio_upstreams_start_together_and_stop_cleanly(tmp_path, capsys):
    a, b = _real(tmp_path / "a.jsonl"), _real(tmp_path / "b.jsonl")
    ups = {"a": a, "b": b}
    app = build_app(build_proxy_server(ups, {}), ups)
    async with _run_lifespan(app):
        assert a._session is not None and b._session is not None
        assert len(_alive(str(tmp_path))) >= 2
    await anyio.sleep(0.3)
    err = capsys.readouterr().err
    assert "cancel scope" not in err.lower()
    assert "Arrêt de" not in err
    assert _alive(str(tmp_path)) == []


@pytest.mark.skipif(sys.platform == "win32", reason="pgrep")
async def test_real_stdio_started_from_a_child_task_stops_from_another(tmp_path):
    up = _real(tmp_path / "child.jsonl")
    async with anyio.create_task_group() as tg:
        tg.start_soon(up.start)
        with anyio.fail_after(15):
            while up._session is None:
                await anyio.sleep(0.05)
    # start() a été appelé par une tâche qui a fini ; stop() l'est ici.
    await up.stop()
    await anyio.sleep(0.3)
    assert _alive(str(tmp_path)) == []
