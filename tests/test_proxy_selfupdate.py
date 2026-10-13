"""Mise à jour du proxy par lui-même : module git (`mcp_proxy/selfupdate.py`,
lot AT-1a), extension `miaou/self-update` (`mcp_proxy/updater.py`, AT-1b),
sortie en 42 et `scripts/supervise.sh` (AT-1c).

Sur de VRAIS dépôts jetables sous `tmp_path` : un dépôt nu sert de remote, un
clone de travail y pousse, et le « clone du proxy » est un second clone dont
le remote ne s'appelle pas `origin` et la branche pas `main` — rien ne doit
dépendre de ces noms. Aucun réseau. La config git de l'utilisateur est mise
hors jeu (`GIT_CONFIG_GLOBAL`, `GIT_CONFIG_NOSYSTEM`) : une signature de
commits ou un hook global ne doivent ni casser ni colorer ces tests.

Les états que git ne sait pas produire sur un dépôt local (refus
d'authentification, dépôt « dubious ownership », git qui ne répond plus)
passent par un faux `git` placé en tête du PATH, qui délègue au vrai pour
tout le reste.
"""
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
from datetime import timedelta
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

from mcp_proxy import entry
from mcp_proxy import selfupdate as su
from mcp_proxy import updater as up
from mcp_proxy.app import build_app
from mcp_proxy.server import InFlight, build_proxy_server
from mcp_proxy.upstream import InProcessUpstream
from tests.proxy_client import _BaseExceptionGroup, _single_cause

REAL_GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(REAL_GIT is None, reason="git absent")

BRANCH = "trunk"
REMOTE = "fork"


@pytest.fixture(autouse=True)
def isolated_git(tmp_path, monkeypatch):
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.invalid")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [REAL_GIT, "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class Repos:
    """Remote nu, clone de publication (`seed`), clone du proxy (`clone`)."""

    def __init__(self, base: Path) -> None:
        self.bare = base / "upstream.git"
        self.seed = base / "seed"
        self.clone = base / "proxy"
        git(base, "init", "-q", "--bare", "-b", BRANCH, str(self.bare))
        git(base, "clone", "-q", str(self.bare), str(self.seed))
        git(self.seed, "checkout", "-q", "-b", BRANCH)
        commit(self.seed, {
            "mcp_proxy/app.py": "v1\n",
            "miaou_dist/version.json": '{"build": "1"}\n',
            "README.md": "readme\n",
        }, "initial")
        git(self.seed, "push", "-q", "origin", BRANCH)
        git(base, "clone", "-q", "-o", REMOTE, "-b", BRANCH, str(self.bare), str(self.clone))

    def publish(self, files: dict[str, str], message: str) -> str:
        sha = commit(self.seed, files, message)
        git(self.seed, "push", "-q", "origin", BRANCH)
        return sha

    def head(self) -> str:
        return git(self.clone, "rev-parse", "HEAD")

    @property
    def dist(self) -> Path:
        return self.clone / "miaou_dist"


@pytest.fixture
def repos(tmp_path) -> Repos:
    return Repos(tmp_path)


def fetch_head(r: Repos) -> Path:
    return r.clone / ".git" / "FETCH_HEAD"


# ---------------------------------------------------------------------------
# Faux git
# ---------------------------------------------------------------------------

def fake_git(tmp_path: Path, monkeypatch, body: str) -> None:
    """Un `git` en tête du PATH : `body` (bash) voit "$@", puis le vrai git
    prend la main s'il n'a pas quitté."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "git"
    script.write_text(f'#!/bin/bash\n{body}\nexec "{REAL_GIT}" "$@"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


# ---------------------------------------------------------------------------
# États
# ---------------------------------------------------------------------------

def test_not_a_clone(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    status = su.probe(plain)
    assert status.state == su.NOT_A_CLONE
    assert "not a git repository" in status.detail.lower()


def test_folder_inside_another_clone_is_not_a_clone(repos):
    status = su.probe(repos.clone / "mcp_proxy")
    assert status.state == su.NOT_A_CLONE
    assert "sans en être la racine" in status.detail


def test_git_missing_is_a_state(repos, monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert su.probe(repos.clone).state == su.GIT_MISSING


def test_dubious_ownership_gives_the_fix(repos, tmp_path, monkeypatch):
    fake_git(tmp_path, monkeypatch, r"""
if [[ " $* " == *" rev-parse --show-toplevel "* ]]; then
  echo "fatal: detected dubious ownership in repository at '/srv/proxy'" >&2
  printf "To add an exception for this directory, call:\n\n\tgit config --global --add safe.directory /srv/proxy\n" >&2
  exit 128
fi""")
    status = su.probe(repos.clone)
    assert status.state == su.REPO_REFUSED
    assert status.remedy == "git config --global --add safe.directory /srv/proxy"
    assert "dubious ownership" in status.detail


def test_detached_head_is_not_tracking(repos):
    git(repos.clone, "checkout", "-q", "--detach")
    status = su.probe(repos.clone)
    assert status.state == su.NOT_TRACKING
    assert "détaché" in status.detail


def test_branch_without_upstream_is_not_tracking(repos):
    git(repos.clone, "checkout", "-q", "-b", "local-only")
    status = su.probe(repos.clone)
    assert status.state == su.NOT_TRACKING
    assert status.branch == "local-only"
    assert status.remote is None


def test_up_to_date_without_fetch(repos):
    assert not fetch_head(repos).exists()
    status = su.probe(repos.clone)
    assert status.state == su.UP_TO_DATE
    assert (status.remote, status.remote_ref) == (REMOTE, f"refs/heads/{BRANCH}")
    assert status.target == status.head == repos.head()
    assert not fetch_head(repos).exists()


def test_behind_fetches_and_describes(repos):
    sha = repos.publish({"mcp_proxy/app.py": "v2\n"}, "feat: v2")
    status = su.probe(repos.clone)
    assert status.state == su.BEHIND
    assert status.behind == 1 and status.ahead == 0
    assert status.target == sha
    assert git(repos.clone, "rev-parse", f"{REMOTE}/{BRANCH}") == sha
    assert status.update.subjects == ["feat: v2"]
    assert status.update.kind == su.KIND_PROXY
    # La sonde n'a pas bougé HEAD.
    assert repos.head() != sha


def test_known_sha_is_not_fetched_again(repos):
    repos.publish({"README.md": "v2\n"}, "docs: v2")
    git(repos.clone, "fetch", "-q", REMOTE)
    fetch_head(repos).unlink()
    status = su.probe(repos.clone)
    assert status.state == su.BEHIND
    assert not fetch_head(repos).exists()


def test_ahead(repos):
    commit(repos.clone, {"local.txt": "x\n"}, "local")
    status = su.probe(repos.clone)
    assert status.state == su.AHEAD
    assert status.ahead == 1 and status.update is None


def test_diverged(repos):
    repos.publish({"README.md": "remote\n"}, "remote")
    commit(repos.clone, {"local.txt": "x\n"}, "local")
    status = su.probe(repos.clone)
    assert status.state == su.DIVERGED
    assert (status.ahead, status.behind) == (1, 1)


def test_dirty_blocks_an_available_update(repos):
    repos.publish({"README.md": "v2\n"}, "docs")
    (repos.clone / "mcp_proxy" / "app.py").write_text("edited\n")
    (repos.clone / "notes.txt").write_text("untracked, ignored\n")
    status = su.probe(repos.clone)
    assert status.state == su.DIRTY
    assert status.dirty == ["mcp_proxy/app.py"]
    assert status.update is not None


def test_dirty_without_update_stays_up_to_date(repos):
    (repos.clone / "README.md").write_text("edited\n")
    status = su.probe(repos.clone)
    assert status.state == su.UP_TO_DATE
    assert status.dirty == ["README.md"]


def test_untracked_files_are_not_dirt(repos):
    repos.publish({"README.md": "v2\n"}, "docs")
    (repos.clone / "notes.txt").write_text("x\n")
    assert su.probe(repos.clone).state == su.BEHIND


def test_remote_branch_gone_is_not_tracking(repos):
    git(repos.bare, "update-ref", "-d", f"refs/heads/{BRANCH}")
    status = su.probe(repos.clone)
    assert status.state == su.NOT_TRACKING
    assert "n'existe plus" in status.detail


def test_unreachable_remote_is_fetch_failed_with_cause(repos):
    git(repos.clone, "remote", "set-url", REMOTE, str(repos.clone.parent / "nowhere.git"))
    status = su.probe(repos.clone)
    assert status.state == su.FETCH_FAILED
    assert status.detail.startswith("ls-remote : ")
    assert "nowhere.git" in status.detail


@pytest.mark.parametrize("message", [
    "fatal: Authentication failed for 'https://forge.example/fork.git/'",
    "fatal: could not read Username for 'https://forge.example': terminal prompts disabled",
    "fatal: Cannot prompt because user interactivity has been disabled.",
    "remote: HTTP Basic: Access denied",
])
def test_auth_failure_is_recognised_in_several_shapes(repos, tmp_path, monkeypatch, message):
    fake_git(tmp_path, monkeypatch, f"""
if [[ " $* " == *" ls-remote "* ]]; then echo "{message}" >&2; exit 128; fi""")
    status = su.probe(repos.clone)
    assert status.state == su.AUTH_REQUIRED
    assert status.detail == message
    assert status.remedy.endswith(f"fetch {REMOTE}")


def test_git_never_prompts(repos, tmp_path, monkeypatch):
    log = tmp_path / "env.log"
    fake_git(tmp_path, monkeypatch, f"""
echo "$GIT_TERMINAL_PROMPT $GCM_INTERACTIVE $* " >> "{log}" """)
    su.probe(repos.clone)
    lines = log.read_text().splitlines()
    assert lines
    for line in lines:
        assert line.startswith("0 never ")
        assert " credential.interactive=never " in line


def test_ls_remote_timeout_is_fetch_failed(repos, tmp_path, monkeypatch):
    fake_git(tmp_path, monkeypatch, """
if [[ " $* " == *" ls-remote "* ]]; then sleep 30; fi""")
    monkeypatch.setattr(su, "NETWORK_TIMEOUT_S", 1)
    started = time.monotonic()
    status = su.probe(repos.clone)
    assert time.monotonic() - started < 10
    assert status.state == su.FETCH_FAILED
    assert "pas de réponse" in status.detail


# ---------------------------------------------------------------------------
# Timeout : tout le groupe de processus
# ---------------------------------------------------------------------------

def _grandchild_git(tmp_path: Path) -> tuple[Path, Path]:
    """Un faux git qui lance un petit-enfant gardant stdout, puis dort."""
    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "slowgit"
    script.write_text(f'#!/bin/bash\nsleep 60 &\necho $! > "{pidfile}"\nsleep 60\n')
    script.chmod(0o755)
    return script, pidfile


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_kills_the_whole_group(tmp_path):
    script, pidfile = _grandchild_git(tmp_path)
    started = time.monotonic()
    with pytest.raises(su.GitTimeout):
        su.run_git(tmp_path, "fetch", timeout=1, executable=str(script))
    assert time.monotonic() - started < su._REAP_TIMEOUT_S + 2
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)


def test_timeout_without_killpg_still_returns(tmp_path, monkeypatch):
    """Repli quand l'API manque : seul git est tué, le petit-enfant tient le
    tube, et l'appel rend la main après `_REAP_TIMEOUT_S` au lieu d'attendre."""
    script, pidfile = _grandchild_git(tmp_path)
    monkeypatch.delattr(os, "killpg")
    monkeypatch.setattr(su, "_REAP_TIMEOUT_S", 1)
    started = time.monotonic()
    try:
        with pytest.raises(su.GitTimeout):
            su.run_git(tmp_path, "fetch", timeout=1, executable=str(script))
        assert time.monotonic() - started < 6
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 9)
            except ProcessLookupError:
                pass


def test_windows_kill_falls_back_when_taskkill_is_missing(monkeypatch):
    monkeypatch.setenv("PATH", "")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        su._kill_tree(proc, windows=True)
        assert proc.wait(timeout=5) != 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_spawn_kwargs_per_platform():
    assert su._spawn_kwargs(False) == {"start_new_session": True}
    flags = su._spawn_kwargs(True)["creationflags"]
    assert flags == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)


# ---------------------------------------------------------------------------
# Contenu d'une mise à jour
# ---------------------------------------------------------------------------

def test_relative_dist(tmp_path):
    root = tmp_path / "proxy"
    assert su.relative_dist(root, None) is None
    assert su.relative_dist(root, root / "miaou_dist") == "miaou_dist"
    assert su.relative_dist(root, tmp_path / "miaou" / "dist") is None


@pytest.mark.parametrize("paths, expected", [
    (["miaou_dist/miaou.html", "miaou_dist/version.json"], su.KIND_MIAOU),
    (["mcp_proxy/app.py"], su.KIND_PROXY),
    (["miaou_dist/miaou.html", "uv.lock"], su.KIND_BOTH),
    (["miaou_dist_old/x"], su.KIND_PROXY),
    ([], su.KIND_PROXY),
])
def test_classify(paths, expected):
    assert su.classify(paths, "miaou_dist") == expected


def test_classify_without_dist_in_clone_is_never_miaou_only():
    assert su.classify(["miaou_dist/miaou.html"], None) == su.KIND_PROXY


@pytest.mark.parametrize("files, expected", [
    ({"miaou_dist/version.json": '{"build": "2"}\n'}, su.KIND_MIAOU),
    ({"mcp_proxy/app.py": "v2\n"}, su.KIND_PROXY),
    ({"miaou_dist/version.json": '{"build": "2"}\n', "mcp_proxy/app.py": "v2\n"}, su.KIND_BOTH),
])
def test_kind_on_a_real_update(repos, files, expected):
    repos.publish(files, "update")
    assert su.probe(repos.clone, miaou_dist=repos.dist).update.kind == expected


def test_subjects_skip_merges_but_keep_merged_commits(repos):
    git(repos.seed, "checkout", "-q", "-b", "feature")
    commit(repos.seed, {"a.txt": "a\n"}, "feat: from upstream")
    git(repos.seed, "checkout", "-q", BRANCH)
    commit(repos.seed, {"b.txt": "b\n"}, "chore: on the fork")
    git(repos.seed, "merge", "-q", "--no-ff", "-m", "Merge branch 'feature'", "feature")
    git(repos.seed, "push", "-q", "origin", BRANCH)
    update = su.probe(repos.clone).update
    assert sorted(update.subjects) == ["chore: on the fork", "feat: from upstream"]
    assert update.subjects_total == 2


def test_subjects_are_capped(repos, monkeypatch):
    monkeypatch.setattr(su, "SUBJECTS_CAP", 2)
    for i in range(3):
        commit(repos.seed, {"README.md": f"{i}\n"}, f"c{i}")
    git(repos.seed, "push", "-q", "origin", BRANCH)
    update = su.probe(repos.clone).update
    assert update.subjects == ["c2", "c1"]
    assert update.subjects_total == 3


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

def test_apply_advances_exactly_to_the_shown_sha(repos, tmp_path):
    shown = repos.publish({"mcp_proxy/app.py": "v2\n"}, "v2")
    newer = repos.publish({"mcp_proxy/app.py": "v3\n"}, "v3")
    before = repos.head()
    state_file = su.state_path(tmp_path / "state")
    result = su.apply_update(shown, repos.clone, miaou_dist=repos.dist, state_file=state_file)
    assert result.outcome == su.APPLIED
    assert (result.before, result.target, result.kind) == (before, shown, su.KIND_PROXY)
    assert repos.head() == shown != newer
    recorded = json.loads(state_file.read_text())
    assert recorded["before"] == before and recorded["target"] == shown
    assert recorded["outcome"] == su.APPLIED
    assert recorded["at"].endswith("+00:00")


def test_apply_fetches_an_unknown_target(repos):
    shown = repos.publish({"README.md": "v2\n"}, "v2")
    assert su.apply_update(shown, repos.clone).outcome == su.APPLIED
    assert repos.head() == shown


def test_apply_refuses_a_malformed_target(repos):
    before = repos.head()
    for target in ("HEAD", "--upload-pack=x", before[:12], None):
        result = su.apply_update(target, repos.clone)
        assert result.outcome == su.REFUSED
    assert repos.head() == before


def test_apply_refuses_a_dirty_tree(repos):
    shown = repos.publish({"README.md": "v2\n"}, "v2")
    (repos.clone / "mcp_proxy" / "app.py").write_text("edited\n")
    result = su.apply_update(shown, repos.clone)
    assert result.outcome == su.REFUSED
    assert "mcp_proxy/app.py" in result.detail
    assert (repos.clone / "mcp_proxy" / "app.py").read_text() == "edited\n"


def test_apply_target_rewritten_away_is_stale(repos):
    shown = repos.publish({"README.md": "v2\n"}, "v2")
    su.probe(repos.clone)  # le clone connaît `shown`
    git(repos.seed, "reset", "-q", "--hard", "HEAD~1")
    commit(repos.seed, {"README.md": "other\n"}, "rewritten")
    git(repos.seed, "push", "-q", "-f", "origin", BRANCH)
    before = repos.head()
    result = su.apply_update(shown, repos.clone)
    assert result.outcome == su.STALE
    assert repos.head() == before


def test_apply_unknown_target_absent_from_remote_is_stale(repos):
    repos.publish({"README.md": "v2\n"}, "v2")
    result = su.apply_update("ab" * 20, repos.clone)
    assert result.outcome == su.STALE


def test_apply_on_head_is_stale(repos):
    result = su.apply_update(repos.head(), repos.clone)
    assert result.outcome == su.STALE
    assert result.detail == "déjà à jour"


def test_apply_diverged_is_stale(repos):
    shown = repos.publish({"README.md": "v2\n"}, "v2")
    commit(repos.clone, {"local.txt": "x\n"}, "local")
    before = repos.head()
    result = su.apply_update(shown, repos.clone)
    assert result.outcome == su.STALE
    assert repos.head() == before


def test_apply_refused_by_git_leaves_everything(repos, tmp_path):
    """Un fichier non suivi sur le chemin d'un fichier ajouté : git refuse
    AVANT de toucher l'arbre. Rien à défaire, et surtout pas ce fichier."""
    shown = repos.publish({"new.txt": "from upstream\n"}, "add new.txt")
    (repos.clone / "new.txt").write_text("mine\n")
    state_file = su.state_path(tmp_path / "state")
    result = su.apply_update(shown, repos.clone, state_file=state_file)
    assert result.outcome == su.REFUSED
    assert "untracked" in result.detail
    assert (repos.clone / "new.txt").read_text() == "mine\n"
    assert json.loads(state_file.read_text())["outcome"] == su.REFUSED


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="droits POSIX, inopérants pour root")
def test_interrupted_checkout_is_rolled_back(repos, tmp_path):
    """Checkout interrompu en cours de route (dossier non inscriptible) :
    l'arbre suivi revient à l'état d'avant, les fichiers que la cible ajoute
    et que git a déjà écrits sont retirés, ceux de l'utilisateur restent. Le
    fast-forward suivant vers le même sha passe alors."""
    commit(repos.seed, {"z/keep.txt": "k\n"}, "add z/")
    git(repos.seed, "push", "-q", "origin", BRANCH)
    git(repos.clone, "pull", "-q", "--ff-only")
    before = repos.head()
    shown = repos.publish(
        {"README.md": "v2\n", "a_new.txt": "added\n", "z/new.txt": "blocked\n"},
        "touch three paths",
    )
    (repos.clone / "notes.txt").write_text("user file\n")
    locked = repos.clone / "z"
    locked.chmod(0o555)
    try:
        result = su.apply_update(shown, repos.clone)
    finally:
        locked.chmod(0o755)
    assert result.outcome == su.ROLLED_BACK, result
    assert repos.head() == before
    assert git(repos.clone, "status", "--porcelain") == "?? notes.txt"
    assert not (repos.clone / "a_new.txt").exists()
    assert (repos.clone / "README.md").read_text() == "readme\n"
    assert su.apply_update(shown, repos.clone).outcome == su.APPLIED
    assert repos.head() == shown


def test_rollback_failure_gives_the_command(repos, tmp_path, monkeypatch):
    shown = repos.publish({"README.md": "v2\n"}, "v2")
    before = repos.head()
    # Merge qui laisse l'arbre sale sans avancer HEAD, puis reset qui échoue.
    fake_git(tmp_path, monkeypatch, f"""
if [[ " $* " == *" merge --ff-only "* ]]; then
  echo half > "{repos.clone}/README.md"; echo "error: unable to write" >&2; exit 1
fi
if [[ " $* " == *" reset --hard "* ]]; then echo "fatal: index.lock exists" >&2; exit 128; fi""")
    state_file = su.state_path(tmp_path / "state")
    result = su.apply_update(shown, repos.clone, state_file=state_file)
    assert result.outcome == su.ROLLBACK_FAILED
    assert "index.lock" in result.detail
    assert result.remedy.endswith(f"reset --hard {before}")
    recorded = json.loads(state_file.read_text())
    assert recorded["outcome"] == su.ROLLBACK_FAILED
    assert recorded["remedy"] == result.remedy


def test_to_dict_is_json_ready(repos):
    repos.publish({"README.md": "v2\n"}, "v2")
    data = su.probe(repos.clone).to_dict()
    assert json.loads(json.dumps(data)) == data
    assert set(data["update"]) == {"kind", "paths", "subjects", "subjects_total"}


# ---------------------------------------------------------------------------
# Extension miaou/self-update (lot AT-1b) : méthodes, refus, surface
# ---------------------------------------------------------------------------

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


async def _capabilities(server):
    try:
        async with Client(server, mode="auto") as client:
            return client.server_capabilities
    except _BaseExceptionGroup as group:
        raise _single_cause(group) from None


def _updater(r: Repos, tmp_path: Path, *, supervised=True, in_flight=None, restarts=None):
    in_flight = in_flight or InFlight()
    config = up.UpdateConfig(
        root=r.clone, miaou_dist=r.dist, supervised=supervised,
        state_file=su.state_path(tmp_path / "state"),
    )
    callback = (lambda: restarts.append(True)) if restarts is not None else None
    updater = up.SelfUpdater(config, in_flight, request_restart=callback)
    server = build_proxy_server({}, {}, in_flight=in_flight)
    updater.install(server)
    return updater, server


@pytest.mark.parametrize("value, expected", [(None, False), (False, False), (True, True)])
def test_resolve_self_update(value, expected):
    cfg = {} if value is None else {"self_update": value}
    assert up.resolve_self_update(cfg) is expected


@pytest.mark.parametrize("value", ["true", "${X:-false}", 1])
def test_resolve_self_update_refuses_non_booleans(value):
    with pytest.raises(ValueError, match="self_update"):
        up.resolve_self_update({"self_update": value})


def test_supervised_only_on_exactly_one():
    assert up.is_supervised({"MIAOU_PROXY_SUPERVISED": "1"})
    for value in ({}, {"MIAOU_PROXY_SUPERVISED": "0"}, {"MIAOU_PROXY_SUPERVISED": "yes"}):
        assert not up.is_supervised(value)


def test_served_build(tmp_path):
    assert up.served_build(None) is None
    assert up.served_build(tmp_path) is None
    (tmp_path / "version.json").write_text("not json")
    assert up.served_build(tmp_path) is None
    (tmp_path / "version.json").write_text('{"build": "abc"}')
    assert up.served_build(tmp_path) == "abc"


async def test_option_off_publishes_nothing():
    server = build_proxy_server({}, {})
    caps = await _capabilities(server)
    assert up.EXTENSION_ID not in (caps.extensions or {})
    for method in (up.STATUS_METHOD, up.APPLY_METHOD):
        with pytest.raises(MCPError) as info:
            await _rpc(server, method, {})
        assert info.value.error.code == types.METHOD_NOT_FOUND


async def test_extension_is_declared_in_the_modern_era(repos, tmp_path):
    _, server = _updater(repos, tmp_path)
    caps = await _capabilities(server)
    assert caps.extensions == {up.EXTENSION_ID: {}}


async def test_status_before_first_probe_is_checking(repos, tmp_path):
    _, server = _updater(repos, tmp_path)
    status = (await _rpc(server, up.STATUS_METHOD))["status"]
    assert status["state"] == up.CHECKING
    assert status["checked_at"] is None
    assert status["supervised"] is True and status["applying"] is False
    assert status["served_build"] == "1"


async def test_status_reads_the_cache_and_refresh_probes(repos, tmp_path):
    updater, server = _updater(repos, tmp_path)
    await updater.refresh()
    sha = repos.publish({"mcp_proxy/app.py": "v2\n"}, "feat: v2")
    cached = (await _rpc(server, up.STATUS_METHOD, {}))["status"]
    assert cached["state"] == su.UP_TO_DATE
    # Une sonde vient d'avoir lieu : le refresh est borné, le cache reste.
    bounded = (await _rpc(server, up.STATUS_METHOD, {"refresh": True}))["status"]
    assert bounded["state"] == su.UP_TO_DATE
    updater._checked_at -= timedelta(seconds=up.REFRESH_MIN_INTERVAL_S)
    fresh = (await _rpc(server, up.STATUS_METHOD, {"refresh": True}))["status"]
    assert fresh["state"] == su.BEHIND and fresh["target"] == sha
    assert fresh["update"]["kind"] == su.KIND_PROXY
    assert fresh["update"]["subjects"] == ["feat: v2"]


async def test_concurrent_refreshes_share_one_probe(repos, tmp_path, monkeypatch):
    calls = []
    real = su.probe

    def counting(*args):
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(su, "probe", counting)
    updater, _ = _updater(repos, tmp_path)
    await asyncio.gather(updater.refresh(), updater.refresh(), updater.refresh())
    assert len(calls) == 1


async def _apply_error(server, target: str) -> MCPError:
    with pytest.raises(MCPError) as info:
        await _rpc(server, up.APPLY_METHOD, {"target": target})
    return info.value


async def test_apply_refused_without_supervisor(repos, tmp_path):
    shown = repos.publish({"README.md": "v2\n"}, "v2")
    before = repos.head()
    _, server = _updater(repos, tmp_path, supervised=False)
    error = await _apply_error(server, shown)
    assert error.error.data["code"] == up.NOT_SUPERVISED
    assert error.error.code == types.INVALID_REQUEST
    assert repos.head() == before


async def test_apply_refuses_a_malformed_target(repos, tmp_path):
    _, server = _updater(repos, tmp_path)
    error = await _apply_error(server, "HEAD")
    assert error.error.data["code"] == up.INVALID_TARGET
    assert error.error.code == types.INVALID_PARAMS


async def test_apply_refused_while_another_runs(repos, tmp_path):
    shown = repos.publish({"README.md": "v2\n"}, "v2")
    updater, server = _updater(repos, tmp_path)
    async with updater._apply_lock:
        status = (await _rpc(server, up.STATUS_METHOD))["status"]
        assert status["applying"] is True
        error = await _apply_error(server, shown)
    assert error.error.data["code"] == up.IN_PROGRESS


async def test_apply_refused_with_tool_calls_in_flight(repos, tmp_path):
    shown = repos.publish({"mcp_proxy/app.py": "v2\n"}, "v2")
    before = repos.head()
    in_flight = InFlight()
    _, server = _updater(repos, tmp_path, in_flight=in_flight)
    with in_flight:
        error = await _apply_error(server, shown)
    assert error.error.data["code"] == up.TOOLS_IN_FLIGHT
    assert repos.head() == before


async def test_miaou_only_update_ignores_calls_in_flight_and_does_not_restart(repos, tmp_path):
    shown = repos.publish({"miaou_dist/version.json": '{"build": "2"}\n'}, "build MIAOU")
    in_flight, restarts = InFlight(), []
    updater, server = _updater(repos, tmp_path, in_flight=in_flight, restarts=restarts)
    with in_flight:
        result = await _rpc(server, up.APPLY_METHOD, {"target": shown})
    assert result["applied"] is True and result["restart"] is False
    assert result["kind"] == su.KIND_MIAOU and result["target"] == shown
    assert restarts == []
    status = (await _rpc(server, up.STATUS_METHOD))["status"]
    assert status["state"] == su.UP_TO_DATE
    assert status["served_build"] == "2"


async def test_proxy_update_asks_for_a_restart(repos, tmp_path):
    shown = repos.publish({"mcp_proxy/app.py": "v2\n"}, "v2")
    restarts = []
    _, server = _updater(repos, tmp_path, restarts=restarts)
    result = await _rpc(server, up.APPLY_METHOD, {"target": shown})
    assert (result["restart"], result["kind"]) == (True, su.KIND_PROXY)
    assert restarts == [True]
    assert repos.head() == shown


async def test_stale_target_is_a_structured_refusal(repos, tmp_path):
    _, server = _updater(repos, tmp_path)
    error = await _apply_error(server, repos.head())
    assert error.error.data["code"] == up.STALE
    assert error.error.data["before"] == repos.head()


async def test_dirty_tree_refusal_keeps_the_cause(repos, tmp_path):
    shown = repos.publish({"README.md": "v2\n"}, "v2")
    (repos.clone / "README.md").write_text("edited\n")
    _, server = _updater(repos, tmp_path)
    error = await _apply_error(server, shown)
    assert error.error.data["code"] == up.REFUSED
    assert "README.md" in error.error.message


async def test_in_flight_counts_every_tool_call():
    """Le compteur couvre l'appel routé vers l'upstream, et retombe à zéro,
    erreur comprise."""
    in_flight = InFlight()
    bench = InProcessUpstream("mcp_bench")
    await bench.start()
    seen = []
    real = bench.call_tool

    async def spy(name, arguments):
        seen.append(in_flight.count)
        if name == "add":
            raise RuntimeError("boom")
        return await real(name, arguments)

    bench.call_tool = spy
    server = build_proxy_server({"bench": bench}, {}, in_flight=in_flight)
    async with Client(server, mode="legacy") as client:
        await client.call_tool("bench__echo", {"text": "hi"})
        result = await client.call_tool("bench__add", {"a": 1, "b": 2})
    assert result.is_error
    assert seen == [1, 1]
    assert in_flight.count == 0


# ---------------------------------------------------------------------------
# scripts/supervise.sh (lot AT-1c)
# ---------------------------------------------------------------------------

SUPERVISE = _ROOT / "scripts" / "supervise.sh"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash absent")


def _countdown(tmp_path: Path, codes: list[int]) -> Path:
    """Une commande qui rend `codes` dans l'ordre, un par lancement, et note
    MIAOU_PROXY_SUPERVISED à chaque fois."""
    (tmp_path / "codes").write_text("\n".join(map(str, codes)) + "\n")
    script = tmp_path / "fake_proxy.sh"
    script.write_text(
        '#!/bin/bash\n'
        f'echo "run ${{MIAOU_PROXY_SUPERVISED:-unset}} $*" >> "{tmp_path}/runs"\n'
        f'code=$(head -n 1 "{tmp_path}/codes")\n'
        f'tail -n +2 "{tmp_path}/codes" > "{tmp_path}/codes.next"\n'
        f'mv "{tmp_path}/codes.next" "{tmp_path}/codes"\n'
        'exit "$code"\n'
    )
    script.chmod(0o755)
    return script


@needs_bash
@pytest.mark.parametrize("codes, final", [([42, 42, 0], 0), ([42, 7], 7), ([3], 3), ([0], 0)])
def test_supervise_relaunches_on_42_only(tmp_path, codes, final):
    fake = _countdown(tmp_path, codes)
    done = subprocess.run([BASH, str(SUPERVISE), str(fake), "--port", "1"],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == final
    runs = (tmp_path / "runs").read_text().splitlines()
    assert runs == ["run 1 --port 1"] * len(codes)


@needs_bash
def test_supervise_points_to_the_rollback_after_a_failed_restart(tmp_path):
    fake = _countdown(tmp_path, [42, 1])
    done = subprocess.run([BASH, str(SUPERVISE), str(fake)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 1
    assert "s'est arrêté en erreur (code 1)" in done.stderr
    assert "reset --hard" in done.stderr or "self-update.json" in done.stderr


@needs_bash
@pytest.mark.parametrize("codes", [[1], [42, 143], [42, 130]])
def test_supervise_says_nothing_after_a_plain_failure_or_a_signal(tmp_path, codes):
    """Ni après un échec sans mise à jour, ni après un arrêt par signal
    (SIGTERM rend 143 : mesuré, uvicorn relève le signal après l'arrêt)."""
    fake = _countdown(tmp_path, codes)
    done = subprocess.run([BASH, str(SUPERVISE), str(fake)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == codes[-1]
    assert "s'est arrêté en erreur" not in done.stderr


@needs_bash
def test_supervise_sourced_reevaluates_the_launch_function(tmp_path):
    """Sourcé : la fonction de lancement est rappelée à chaque tour, donc ce
    qu'elle calcule (ici un compteur, chez un fork le proxy réseau) l'est
    aussi."""
    fake = _countdown(tmp_path, [42, 42, 0])
    caller = tmp_path / "start.sh"
    caller.write_text(
        '#!/bin/bash\n'
        'n=0\n'
        f'launch() {{ n=$((n + 1)); "{fake}" "tour-$n" "$@"; }}\n'
        f'. "{SUPERVISE}"\n'
        'miaou_supervise launch --x\n'
        'echo "after $?"\n'
    )
    done = subprocess.run([BASH, str(caller)], capture_output=True, text=True, timeout=30)
    assert done.stdout.strip() == "after 0"
    assert (tmp_path / "runs").read_text().splitlines() == [
        "run 1 tour-1 --x", "run 1 tour-2 --x", "run 1 tour-3 --x",
    ]


def test_supervise_is_lf_only():
    """Git Bash échoue sur un CRLF : `.gitattributes` force LF, et le
    fichier versionné n'en porte pas."""
    assert b"\r" not in SUPERVISE.read_bytes()
    assert "*.sh text eol=lf" in (_ROOT / ".gitattributes").read_text()


# ---------------------------------------------------------------------------
# De bout en bout : vrai uvicorn, vrai transport (lot AT-1c)
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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


def _serve_and_drive(app, port: int, exit_request, drive) -> tuple[int | None, dict]:
    """`serve_http` dans le thread principal (il pose des gestionnaires de
    signaux), `drive(port)` dans un autre. Un garde-fou envoie SIGINT au
    bout de 30 s : un test cassé échoue au lieu de pendre."""
    outcome: dict = {}

    def client():
        try:
            _wait_for_port(port, time.monotonic() + 15)
            outcome["result"] = drive(port)
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
    return code, outcome.get("result")


def _app(repos: Repos, tmp_path: Path, exit_request) -> Any:
    config = up.UpdateConfig(
        root=repos.clone, miaou_dist=repos.dist, supervised=True,
        state_file=su.state_path(tmp_path / "state"),
    )
    updater = up.SelfUpdater(config, InFlight(), request_restart=exit_request)
    server = build_proxy_server({}, {}, in_flight=updater.in_flight)
    return build_app(server, {}, miaou_dist=repos.dist, updater=updater)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT du garde-fou")
def test_apply_over_http_answers_then_exits_42(repos, tmp_path):
    shown = repos.publish({"mcp_proxy/app.py": "v2\n"}, "v2")
    exit_request = entry.ExitRequest()
    app = _app(repos, tmp_path, exit_request)

    def drive(port):
        return asyncio.run(_wire_rpc(port, up.APPLY_METHOD, {"target": shown}))

    code, result = _serve_and_drive(app, _free_port(), exit_request, drive)
    assert result["applied"] is True and result["restart"] is True
    assert code == entry.UPDATE_EXIT_CODE
    assert repos.head() == shown


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT du garde-fou")
def test_miaou_only_over_http_serves_the_new_build_without_exiting(repos, tmp_path):
    import http.client

    shown = repos.publish({"miaou_dist/version.json": '{"build": "2"}\n'}, "build MIAOU")
    exit_request = entry.ExitRequest()
    app = _app(repos, tmp_path, exit_request)

    def version(port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", "/app/version.json")
            return json.loads(conn.getresponse().read())["build"]
        finally:
            conn.close()

    def drive(port):
        before = version(port)
        result = asyncio.run(_wire_rpc(port, up.APPLY_METHOD, {"target": shown}))
        after = version(port)
        status = asyncio.run(_wire_rpc(port, up.STATUS_METHOD, {}))["status"]
        os.kill(os.getpid(), signal.SIGINT)
        return before, result, after, status

    code, (before, result, after, status) = _serve_and_drive(
        app, _free_port(), exit_request, drive
    )
    assert code is None
    assert exit_request.code is None
    assert (before, after) == ("1", "2")
    assert result["restart"] is False
    assert status["served_build"] == "2" and status["state"] == su.UP_TO_DATE
