"""Mise à jour du proxy par lui-même (lot AT) : état git du clone, fast-forward.

Fonctions SYNCHRONES et bloquantes (sous-processus git, appels réseau pour
`ls-remote`/`fetch`) : un appelant async passe par `asyncio.to_thread`.

Aucun nom en dur. La branche suivie, son remote et la ref distante se lisent
dans la config de suivi de la branche courante (`for-each-ref
%(upstream:…)`), jamais dans un `origin` ou un `main` supposés : le fork du
travail suit son propre remote. HEAD détaché ou branche sans upstream valent
« non suivi », sans repli sur un remote par défaut.

« En retard » se lit dans l'ascendance (`rev-list --left-right --count`),
jamais dans des dates de commit, qui se mentent. La cible est le sha que le
remote annonce (`ls-remote`), pas la ref de suivi locale, qui ne bouge qu'au
fetch ; et le fetch n'a lieu que si ce sha est inconnu du clone
(`cat-file -e`), donc une sonde sur un clone à jour ne télécharge rien.

L'application est un `merge --ff-only <sha affiché>`, jamais un `pull` (qui
dépend de `pull.rebase`/`pull.ff` et pourrait rebaser ou créer un merge), et
jamais au-delà du sha proposé à l'utilisateur. Elle refuse en amont un arbre
dont un fichier SUIVI est modifié : c'est ce qui rend sûr le retour arrière
d'un checkout interrompu (`reset --hard`), et les deux décisions sont liées —
assouplir l'une exige de revoir l'autre.

Git ne doit jamais attendre quelqu'un : `credential.interactive=never`,
`GCM_INTERACTIVE=never` (sans eux, Git Credential Manager ouvre une FENÊTRE
sous Windows, que `GIT_TERMINAL_PROMPT=0` n'empêche pas), stdin fermé, et un
timeout dur qui tue le GROUPE de processus : `git fetch` en HTTPS lance
`git-remote-https`, et un petit-enfant qui garde le tube ouvert ferait
attendre `communicate()` au-delà du timeout.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

#: Racine attendue du clone : le dossier qui contient le paquet. Un clone
#: englobant (le proxy copié dans un autre dépôt) n'est pas « un clone du
#: proxy » : le mettre à jour avancerait un dépôt qui n'est pas le sien.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

STATE_FILE = "self-update.json"

#: Bornes des appels git. Le réseau (ls-remote, fetch) a la sienne ; le merge,
#: qui écrit l'arbre, une plus large (antivirus sous Windows).
LOCAL_TIMEOUT_S = 15
NETWORK_TIMEOUT_S = 30
MERGE_TIMEOUT_S = 120
#: Après le kill du groupe, ce qu'on attend encore la fermeture des tubes.
_REAP_TIMEOUT_S = 3

SUBJECTS_CAP = 20
DIRTY_CAP = 20

_WINDOWS = sys.platform == "win32"

# États d'une sonde. Une cause brute (stderr de git) accompagne chaque état
# d'échec dans `detail` : un « non » sans cause est indiagnosticable chez un
# collègue.
NOT_A_CLONE = "not_a_clone"
GIT_MISSING = "git_missing"
REPO_REFUSED = "repo_refused"
NOT_TRACKING = "not_tracking"
AUTH_REQUIRED = "auth_required"
FETCH_FAILED = "fetch_failed"
GIT_ERROR = "git_error"
UP_TO_DATE = "up_to_date"
BEHIND = "behind"
AHEAD = "ahead"
DIVERGED = "diverged"
DIRTY = "dirty"

# Genre d'une mise à jour, d'après les chemins qu'elle touche.
KIND_MIAOU = "miaou"
KIND_PROXY = "proxy"
KIND_BOTH = "both"

# Issues d'une application.
APPLIED = "applied"
REFUSED = "refused"
STALE = "stale"
ROLLED_BACK = "rolled_back"
ROLLBACK_FAILED = "rollback_failed"
BLOCKED = "blocked"

_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")

#: Formulations d'un échec d'authentification, en minuscules. Plusieurs
#: formes : git, curl, GCM et les forges ne disent pas la même chose, et
#: `LC_ALL=C` ne fixe que les messages de git lui-même.
_AUTH_MARKERS = (
    "authentication failed",
    "terminal prompts disabled",
    "could not read username",
    "could not read password",
    "user interactivity has been disabled",
    "cannot prompt because",
    "invalid username or password",
    "http basic: access denied",
    "the requested url returned error: 401",
    "the requested url returned error: 403",
    "permission denied (publickey",
)


class GitMissing(Exception):
    """`git` introuvable sur le PATH."""


class GitTimeout(Exception):
    """Un appel git a dépassé sa borne ; son groupe de processus a été tué."""


@dataclass(frozen=True)
class GitRun:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def cause(self) -> str:
        """Ce que git a dit, condensé : stderr, sinon stdout."""
        text = (self.stderr or self.stdout).strip()
        return text or f"git a rendu {self.returncode} sans message"


@dataclass
class UpdateContent:
    """Ce qu'apporte une mise à jour : son genre, et de quoi la décrire."""

    kind: str
    paths: int
    subjects: list[str]
    subjects_total: int


@dataclass
class Status:
    state: str
    detail: str | None = None
    remedy: str | None = None
    branch: str | None = None
    remote: str | None = None
    remote_ref: str | None = None
    head: str | None = None
    target: str | None = None
    ahead: int = 0
    behind: int = 0
    dirty: list[str] = field(default_factory=list)
    update: UpdateContent | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApplyResult:
    outcome: str
    detail: str | None = None
    remedy: str | None = None
    before: str | None = None
    target: str | None = None
    kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Exécution de git
# ---------------------------------------------------------------------------

def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GCM_INTERACTIVE="never",
        # Messages de git en anglais quelle que soit la langue du poste : la
        # reconnaissance de l'échec d'authentification et du dépôt refusé
        # porte sur eux. Les chemins, eux, sortent en `-z`, hors locale.
        LC_ALL="C",
        LANGUAGE="",
    )
    return env


def _spawn_kwargs(windows: bool) -> dict[str, Any]:
    """Git dans son propre groupe de processus, pour le tuer en entier."""
    if windows:
        # Valeur documentée de CREATE_NEW_PROCESS_GROUP, au cas où le module
        # ne l'exposerait pas.
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


def _kill_tree(proc: subprocess.Popen, windows: bool) -> None:
    """Tue git ET ses descendants (`git-remote-https`, fenêtre GCM).

    Repli sur `proc.kill()`, qui ne tue que git, quand l'outil manque :
    `taskkill` absent ou en échec, `os.killpg` absent. Le tube peut alors
    rester tenu par un petit-enfant ; `_communicate` ne l'attend que
    `_REAP_TIMEOUT_S`.
    """
    if windows:
        try:
            done = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=_REAP_TIMEOUT_S,
            )
            if done.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        killpg = getattr(os, "killpg", None)
        sigkill = getattr(signal, "SIGKILL", None)
        if killpg is not None and sigkill is not None:
            try:
                killpg(proc.pid, sigkill)
                return
            except (ProcessLookupError, PermissionError):
                pass
    try:
        proc.kill()
    except OSError:
        pass


def run_git(
    cwd: Path, *args: str, timeout: float = LOCAL_TIMEOUT_S, executable: str = "git"
) -> GitRun:
    """`git -C <cwd> -c credential.interactive=never <args>`, jamais interactif.

    Lève `GitMissing` si git n'est pas sur le PATH, `GitTimeout` au-delà de
    `timeout` (le groupe de processus est alors tué). Ne lève jamais sur un
    code de retour non nul : c'est à l'appelant de lire `GitRun`.
    """
    cmd = [executable, "-C", str(cwd), "-c", "credential.interactive=never", *args]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_env(),
            **_spawn_kwargs(_WINDOWS),
        )
    except FileNotFoundError as e:
        raise GitMissing(str(e)) from e
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc, _WINDOWS)
        try:
            proc.communicate(timeout=_REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            # Un descendant hors du groupe tient encore le tube : on l'y
            # laisse plutôt que d'attendre sans fin.
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass
            # git lui-même est mort : le récolter, sans quoi il reste zombie.
            try:
                proc.wait(timeout=_REAP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                pass
        raise GitTimeout(f"git {args[0] if args else ''} : pas de réponse en {timeout:g} s")
    return GitRun(
        proc.returncode,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def is_auth_failure(stderr: str) -> bool:
    text = stderr.lower()
    return any(marker in text for marker in _AUTH_MARKERS)


def _safe_directory_remedy(stderr: str, root: Path) -> str:
    """La commande que git suggère lui-même, sinon la même composée ici."""
    m = re.search(r"git config --global --add safe\.directory \S.*", stderr)
    if m:
        return m.group(0).strip()
    return f"git config --global --add safe.directory '{root.as_posix()}'"


# ---------------------------------------------------------------------------
# Sonde
# ---------------------------------------------------------------------------

@dataclass
class _Tracking:
    branch: str
    remote: str
    remote_ref: str
    head: str


def _locate(root: Path) -> Status | _Tracking:
    """Le clone, sa branche courante et ce qu'elle suit — tout local."""
    try:
        top = run_git(root, "rev-parse", "--show-toplevel")
    except GitMissing as e:
        return Status(GIT_MISSING, detail=str(e))
    except GitTimeout as e:
        return Status(GIT_ERROR, detail=str(e))
    if not top.ok:
        if "dubious ownership" in top.stderr:
            return Status(
                REPO_REFUSED, detail=top.cause,
                remedy=_safe_directory_remedy(top.stderr, root),
            )
        if "not a git repository" in top.stderr.lower():
            return Status(NOT_A_CLONE, detail=top.cause)
        return Status(GIT_ERROR, detail=top.cause)
    toplevel = Path(top.stdout.strip())
    try:
        same = toplevel.samefile(root)
    except OSError:
        same = False
    if not same:
        return Status(
            NOT_A_CLONE,
            detail=f"le dossier du proxy ({root}) est dans le clone {toplevel}, sans en être la racine",
        )
    try:
        ref = run_git(root, "symbolic-ref", "-q", "HEAD")
        if not ref.ok:
            return Status(NOT_TRACKING, detail="HEAD détaché")
        full = ref.stdout.strip()
        branch = full.removeprefix("refs/heads/")
        up = run_git(
            root, "for-each-ref",
            "--format=%(upstream:remotename)%00%(upstream:remoteref)", full,
        )
        head = run_git(root, "rev-parse", "HEAD")
    except GitTimeout as e:
        return Status(GIT_ERROR, detail=str(e))
    if not up.ok or not head.ok:
        return Status(GIT_ERROR, detail=(up if not up.ok else head).cause, branch=branch)
    remote, _, remote_ref = up.stdout.strip().partition("\0")
    if not remote or not remote_ref:
        return Status(
            NOT_TRACKING, branch=branch, head=head.stdout.strip(),
            detail=f"la branche '{branch}' ne suit aucune branche distante",
        )
    return _Tracking(branch, remote, remote_ref, head.stdout.strip())


def _network_failure(run: GitRun, t: _Tracking, root: Path, what: str) -> Status:
    if is_auth_failure(run.stderr):
        return Status(
            AUTH_REQUIRED, detail=run.cause, branch=t.branch, remote=t.remote,
            remote_ref=t.remote_ref, head=t.head,
            # Lancé à la main, dans un terminal, git ouvre alors la fenêtre
            # d'authentification là où l'utilisateur l'attend.
            remedy=f"git -C '{root.as_posix()}' fetch {t.remote}",
        )
    return Status(
        FETCH_FAILED, detail=f"{what} : {run.cause}", branch=t.branch,
        remote=t.remote, remote_ref=t.remote_ref, head=t.head,
    )


def _remote_sha(root: Path, t: _Tracking) -> str | Status:
    """Le sha que le remote annonce pour la branche suivie, sans rien
    télécharger."""
    try:
        run = run_git(root, "ls-remote", t.remote, t.remote_ref, timeout=NETWORK_TIMEOUT_S)
    except GitTimeout as e:
        return Status(
            FETCH_FAILED, detail=f"ls-remote : {e}", branch=t.branch,
            remote=t.remote, remote_ref=t.remote_ref, head=t.head,
        )
    if not run.ok:
        return _network_failure(run, t, root, "ls-remote")
    for line in run.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == t.remote_ref:
            return sha.strip()
    return Status(
        NOT_TRACKING, branch=t.branch, remote=t.remote, remote_ref=t.remote_ref,
        head=t.head, detail=f"{t.remote_ref} n'existe plus sur '{t.remote}'",
    )


def _has_commit(root: Path, sha: str) -> bool:
    return run_git(root, "cat-file", "-e", f"{sha}^{{commit}}").ok


def _ensure_commit(root: Path, t: _Tracking, sha: str) -> Status | None:
    """Fetch seulement si le clone ne possède pas déjà `sha`."""
    if _has_commit(root, sha):
        return None
    try:
        run = run_git(root, "fetch", t.remote, t.remote_ref, timeout=NETWORK_TIMEOUT_S)
    except GitTimeout as e:
        return Status(
            FETCH_FAILED, detail=f"fetch : {e}", branch=t.branch,
            remote=t.remote, remote_ref=t.remote_ref, head=t.head,
        )
    if not run.ok:
        return _network_failure(run, t, root, "fetch")
    if not _has_commit(root, sha):
        return Status(
            FETCH_FAILED, branch=t.branch, remote=t.remote, remote_ref=t.remote_ref,
            head=t.head, detail=f"{sha} absent après le fetch (branche réécrite entre-temps ?)",
        )
    return None


def _dirty_files(root: Path) -> list[str]:
    run = run_git(root, "status", "--porcelain", "-z", "--untracked-files=no")
    if not run.ok:
        raise RuntimeError(run.cause)
    # Format -z : « XY chemin\0 », et un renommage ajoute « origine\0 ».
    files: list[str] = []
    entries = run.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        files.append(entry[3:])
        if entry[0] in "RC":
            i += 1
    return files


def relative_dist(root: Path, miaou_dist: Path | None) -> str | None:
    """`miaou_dist` relatif à la racine du clone, en `/`, ou None s'il est
    absent ou hors du clone (un `../miaou/dist` que le pull ne touche
    jamais : le genre « MIAOU seul » n'existe pas alors)."""
    if miaou_dist is None:
        return None
    try:
        rel = Path(miaou_dist).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return None
    text = rel.as_posix()
    return None if text in ("", ".") else text


def classify(paths: list[str], dist_rel: str | None) -> str:
    """MIAOU seul, proxy seul, ou les deux, d'après les chemins touchés.

    Une mise à jour sans chemin (commits vides) compte pour le proxy : le
    redémarrage y est inutile mais sans danger, l'inverse ne l'est pas."""
    if dist_rel is None:
        return KIND_PROXY
    fold = str.casefold if _WINDOWS else (lambda s: s)
    prefix = fold(dist_rel) + "/"
    in_dist = [fold(p).startswith(prefix) or fold(p) == fold(dist_rel) for p in paths]
    if paths and all(in_dist):
        return KIND_MIAOU
    if any(in_dist):
        return KIND_BOTH
    return KIND_PROXY


def _changed_paths(root: Path, base: str, target: str, *filters: str) -> list[str]:
    run = run_git(root, "diff", "--name-only", "-z", "--no-renames", *filters, base, target)
    if not run.ok:
        raise RuntimeError(run.cause)
    return [p for p in run.stdout.split("\0") if p]


def describe_update(root: Path, head: str, target: str, dist_rel: str | None) -> UpdateContent:
    paths = _changed_paths(root, head, target)
    log = run_git(root, "log", "--no-merges", "--format=%s", f"{head}..{target}")
    if not log.ok:
        raise RuntimeError(log.cause)
    subjects = [s for s in log.stdout.splitlines() if s.strip()]
    return UpdateContent(
        kind=classify(paths, dist_rel),
        paths=len(paths),
        subjects=subjects[:SUBJECTS_CAP],
        subjects_total=len(subjects),
    )


def probe(root: Path = PROJECT_ROOT, miaou_dist: Path | None = None) -> Status:
    """État du clone vis-à-vis de sa branche distante suivie.

    Réseau : un `ls-remote`, plus un `fetch` seulement si le sha annoncé est
    inconnu du clone. N'écrit rien d'autre que les refs de suivi.
    """
    t = _locate(root)
    if isinstance(t, Status):
        return t
    try:
        sha = _remote_sha(root, t)
        if isinstance(sha, Status):
            return sha
        base = dict(
            branch=t.branch, remote=t.remote, remote_ref=t.remote_ref,
            head=t.head, target=sha,
        )
        dirty = _dirty_files(root)[:DIRTY_CAP]
        if sha == t.head:
            return Status(UP_TO_DATE, dirty=dirty, **base)
        failure = _ensure_commit(root, t, sha)
        if failure is not None:
            return failure
        count = run_git(root, "rev-list", "--left-right", "--count", f"{t.head}...{sha}")
        if not count.ok:
            return Status(GIT_ERROR, detail=count.cause, **base)
        ahead, behind = (int(n) for n in count.stdout.split())
        if ahead and behind:
            return Status(DIVERGED, ahead=ahead, behind=behind, dirty=dirty, **base)
        if ahead:
            return Status(AHEAD, ahead=ahead, dirty=dirty, **base)
        if not behind:
            return Status(UP_TO_DATE, dirty=dirty, **base)
        update = describe_update(root, t.head, sha, relative_dist(root, miaou_dist))
        # Une mise à jour existe, mais l'arbre la bloque : c'est cet état-là
        # que l'utilisateur doit voir AVANT de cliquer. Un arbre sale sans mise
        # à jour reste « à jour », la liste `dirty` le dit sans alarmer.
        state = DIRTY if dirty else BEHIND
        return Status(state, behind=behind, dirty=dirty, update=update, **base)
    except GitTimeout as e:
        return Status(GIT_ERROR, detail=str(e), branch=t.branch, head=t.head)
    except RuntimeError as e:
        return Status(GIT_ERROR, detail=str(e), branch=t.branch, head=t.head)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

def state_path(state_dir: Path) -> Path:
    return Path(state_dir) / STATE_FILE


def _record(state_file: Path | None, **entry: Any) -> None:
    """Note le HEAD d'avant, la cible et l'issue, pour un retour arrière à la
    main si le proxy ne redémarre plus (AT-i). Best-effort : un état non écrit
    ne bloque pas la mise à jour."""
    if state_file is None:
        return
    entry["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _rollback(root: Path, before: str, target: str) -> str | None:
    """Défait un checkout interrompu : arbre suivi remis à `before`, puis
    retrait des seuls fichiers que la cible AJOUTE et que le checkout a déjà
    écrits — ils sont non suivis après le reset, et feraient échouer le
    fast-forward suivant (« untracked working tree files would be
    overwritten »). Jamais de `git clean`, qui emporterait les fichiers non
    suivis de l'utilisateur. Rend None si tout est défait, sinon la cause."""
    reset = run_git(root, "reset", "--hard", "-q", before, timeout=MERGE_TIMEOUT_S)
    if not reset.ok:
        return reset.cause
    added = _changed_paths(root, before, target, "--diff-filter=A")
    if not added:
        return None
    tracked = run_git(root, "ls-files", "-z", "--", *added)
    if not tracked.ok:
        return tracked.cause
    keep = {p for p in tracked.stdout.split("\0") if p}
    for rel in added:
        if rel in keep:
            continue
        path = root / rel
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
        except OSError as e:
            return f"{rel} : {e}"
    return None


def is_valid_target(target: Any) -> bool:
    """Un sha complet (40 ou 64 hexadécimaux) : une entrée venue du réseau ne
    doit jamais devenir une option ni une ref symbolique pour git."""
    return isinstance(target, str) and bool(_SHA.match(target))


def apply_update(
    target: str,
    root: Path = PROJECT_ROOT,
    miaou_dist: Path | None = None,
    state_file: Path | None = None,
    before_merge: Callable[[str], str | None] | None = None,
) -> ApplyResult:
    """Avance la branche courante EXACTEMENT à `target`, en fast-forward.

    Revérifie tout avant d'écrire : arbre propre, branche toujours suivie,
    `target` connu (fetch sinon) et toujours contenu dans la branche distante
    telle que le remote l'annonce MAINTENANT. Un `target` qui n'est plus
    atteignable (branche réécrite, autre clic entre-temps) donne `stale`, sans
    toucher à rien. `before_merge(kind)` est le dernier mot de l'appelant,
    une fois le genre connu et juste avant d'écrire : un motif rendu donne
    `blocked`, sans rien toucher (appels d'outils en vol, qu'un redémarrage
    couperait). Ne quitte jamais le processus : c'est l'appelant qui
    décide du redémarrage, d'après `kind`.
    """
    if not is_valid_target(target):
        return ApplyResult(REFUSED, detail=f"cible invalide : {target!r} (sha complet attendu)")
    t = _locate(root)
    if isinstance(t, Status):
        return ApplyResult(REFUSED, detail=f"{t.state} : {t.detail}", remedy=t.remedy)
    before = t.head
    try:
        dirty = _dirty_files(root)
        if dirty:
            return ApplyResult(
                REFUSED, before=before, target=target,
                detail="fichiers suivis modifiés : " + ", ".join(dirty[:DIRTY_CAP]),
            )
        remote = _remote_sha(root, t)
        if isinstance(remote, Status):
            return ApplyResult(REFUSED, before=before, target=target,
                               detail=f"{remote.state} : {remote.detail}", remedy=remote.remedy)
        for sha in (remote,) if remote == target else (remote, target):
            failure = _ensure_commit(root, t, sha)
            if failure is not None:
                if sha == target and failure.state == FETCH_FAILED:
                    return ApplyResult(STALE, before=before, target=target, detail=failure.detail)
                return ApplyResult(REFUSED, before=before, target=target,
                                   detail=f"{failure.state} : {failure.detail}",
                                   remedy=failure.remedy)
        if target == before:
            return ApplyResult(STALE, before=before, target=target, detail="déjà à jour")
        in_remote = run_git(root, "merge-base", "--is-ancestor", target, remote).ok
        from_head = run_git(root, "merge-base", "--is-ancestor", before, target).ok
        if not (in_remote and from_head):
            return ApplyResult(
                STALE, before=before, target=target,
                detail=(
                    f"{target[:12]} n'est plus atteignable en avance rapide depuis "
                    f"{before[:12]} sur {t.remote}/{t.branch}"
                ),
            )
        kind = describe_update(root, before, target, relative_dist(root, miaou_dist)).kind
    except GitTimeout as e:
        return ApplyResult(REFUSED, before=before, target=target, detail=str(e))
    except RuntimeError as e:
        return ApplyResult(REFUSED, before=before, target=target, detail=str(e))
    if before_merge is not None:
        reason = before_merge(kind)
        if reason is not None:
            return ApplyResult(BLOCKED, before=before, target=target, kind=kind, detail=reason)

    _record(state_file, before=before, target=target, outcome="pending")
    try:
        merge = run_git(root, "merge", "--ff-only", "-q", target, timeout=MERGE_TIMEOUT_S)
        merge_cause = merge.cause if not merge.ok else None
    except GitTimeout as e:
        merge_cause = str(e)
    if merge_cause is None:
        _record(state_file, before=before, target=target, outcome=APPLIED, kind=kind)
        return ApplyResult(APPLIED, before=before, target=target, kind=kind)

    # Échec : refusé avant de toucher l'arbre, ou interrompu en cours de route.
    rollback_cmd = f"git -C '{root.as_posix()}' reset --hard {before}"
    try:
        head = run_git(root, "rev-parse", "HEAD")
        now = head.stdout.strip() if head.ok else None
        touched = now != before or bool(_dirty_files(root))
    except (GitTimeout, RuntimeError):
        touched = True
    if not touched:
        _record(state_file, before=before, target=target, outcome=REFUSED, detail=merge_cause)
        return ApplyResult(REFUSED, before=before, target=target, detail=merge_cause)
    try:
        failed = _rollback(root, before, target)
    except (GitTimeout, RuntimeError) as e:
        failed = str(e)
    if failed is None:
        _record(state_file, before=before, target=target, outcome=ROLLED_BACK, detail=merge_cause)
        return ApplyResult(ROLLED_BACK, before=before, target=target, detail=merge_cause)
    detail = f"{merge_cause} ; retour arrière en échec : {failed}"
    _record(state_file, before=before, target=target, outcome=ROLLBACK_FAILED,
            detail=detail, remedy=rollback_cmd)
    return ApplyResult(ROLLBACK_FAILED, before=before, target=target,
                       detail=detail, remedy=rollback_cmd)
