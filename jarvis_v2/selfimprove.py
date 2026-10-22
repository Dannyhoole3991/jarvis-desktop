"""
Jarvis improving his own code, safely.

  1. propose(goal): a separate git worktree (a full copy on its own branch, outside the live folder)
     is made from the committed code, and a Claude Code run works THERE, never in the running Jarvis.
  2. When the run ends, Jarvis does not take its word for anything: he lists what changed, refuses
     any change to a PROTECTED file, and runs the test suite himself in the worktree.
  3. A change that passes becomes "ready for review". ONLY Danny can apply it (the window's Approve
     button calls approve(); no brain tool can). Applying merges the branch into master.
  Cloud first (cloud.py, setting JARVIS_CODE_JOBS): the work itself is done by a Claude Code cloud session when
     it can be (master is pushed to GitHub, the session pushes its own branch). Jarvis fetches that branch into the
     same kind of worktree and puts it through exactly the same checks. If the cloud can't take it, it runs here.
  4. Every applied change can be undone (git revert of that merge). Nothing is ever force-pushed,
     reset or deleted from history.

Protected files can't be changed this way at all: the live-trading code, the store's safety guards,
credentials, and this approval machinery itself.
"""

import json
import os
import subprocess
import sys
import threading
import time

from . import cloud, config

PROPOSALS_FILE = os.path.join(config.DATA_DIR, "proposals.json")
WORKTREE_ROOT = os.path.join(os.path.dirname(config.REPO_DIR), "Jarvis_worktrees")
BASE_BRANCH = "master"
PROTECTED = ("jarvis_trading/live/", "jarvis_store/shopify.py", "jarvis_store/creds.py",
             "jarvis_v2/selfimprove.py", "jarvis_v2/claude_env.py", "jarvis_v2/runs.py", ".gitignore")
TESTS = ["jarvis_v2.tests.test_v2", "jarvis_v2.tests.test_server", "jarvis_v2.tests.test_phase3", "jarvis_v2.tests.test_cloud_jobs",
         "jarvis_store.test_store"]
TEST_TIMEOUT = 600

RULES = """- Make the smallest change that achieves the goal. Match the surrounding code's style.
- NEVER edit these protected files (a change to any of them is rejected automatically): {protected}.
- Never add live trading, never weaken a safety check, never store passwords or keys in code.
- Add or update tests for what you change. Run: {test_cmd}
  Everything must pass."""

BRIEF = """You are improving Jarvis, Danny's assistant, working in a SEPARATE COPY of his code (a git worktree).
The running Jarvis is not affected; Danny reviews your change before it is applied.

Goal: {goal}

Rules:
""" + RULES + """
- Commit your work on this branch with a clear message. Never push.
- Finish with a short plain-English summary for Danny: what changed, why, and how to try it.
"""

CLOUD_BRIEF = """You are improving Jarvis, Danny's assistant, in a cloud session on his GitHub repository.
The running Jarvis on his PC is not affected: Jarvis checks your work himself and Danny reviews it before it is applied.

Goal: {goal}

Rules:
- Start from the master branch as it is on GitHub.
""" + RULES + """
  (Tests that only work on Windows may fail here: say which, and don't change them to make them pass.)
- Commit your work to a branch named {branch} and push THAT branch only. Never push to master, never open or merge
  a pull request.
- Your LAST commit's message is your short plain-English summary for Danny (what changed, why, and how to try it)
  and must end with this line on its own: """ + cloud.DONE_MARK + """ {pid}
- Finish with the same summary.
"""


def _git(cwd, *args, timeout=60):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=0x08000000 if os.name == "nt" else 0)
    return r.returncode, (r.stdout + r.stderr).strip()


def _load():
    try:
        with open(PROPOSALS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(items):
    os.makedirs(os.path.dirname(PROPOSALS_FILE), exist_ok=True)
    with open(PROPOSALS_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=1)


_lock = threading.Lock()


def _update(pid, **fields):
    with _lock:
        items = _load()
        for p in items:
            if p["id"] == pid:
                p.update(fields)
        _save(items)


def get(pid):
    return next((p for p in _load() if p["id"] == int(pid)), None)


def listing():
    return list(reversed(_load()))


def test_command(cloud_session=False):
    return f"{'python' if cloud_session else 'py -3'} -m unittest {' '.join(TESTS)}"


def protected_hits(files):
    return [f for f in files if any(f == p or f.startswith(p) for p in PROTECTED)]


def uncommitted_tracked(repo=None):
    """Tracked files with uncommitted changes in the live repo (a worktree is made from COMMITTED code)."""
    code, out = _git(repo or config.REPO_DIR, "status", "--porcelain", "--untracked-files=no")
    return [l[3:] for l in out.splitlines() if l.strip()] if code == 0 else []


def propose(goal, executor, repo=None, where=None, background=True):
    """Starts a change. Cloud first (unless the setting says local): the cloud session is started in the
    background (pushing master and starting it can take a while); if it can't be, the work runs on this PC."""
    repo = repo or config.REPO_DIR
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("Say what you'd like changed.")
    dirty = uncommitted_tracked(repo)
    if dirty:
        raise ValueError("Jarvis's current code has unsaved changes in git (" + ", ".join(dirty[:5]) +
                         "). They need saving first, or my copy would be missing them.")
    pid = int(time.time())
    record = {"id": pid, "goal": goal, "branch": f"jarvis/improve-{pid}", "worktree": os.path.join(WORKTREE_ROOT, f"improve-{pid}"),
              "run_id": None, "status": "working", "created_at": time.time()}
    if (where or cloud.mode()) != "cloud":
        _start_local(record, executor, repo)
        with _lock:
            _save(_load() + [dict(record, where=cloud.LOCAL)])
        return get(pid)
    with _lock:
        _save(_load() + [dict(record, where="cloud", cloud={"branch": f"claude/jarvis-improve-{pid}", "starting": True})])
    if background:
        threading.Thread(target=_start_cloud, args=(pid, executor, repo), daemon=True, name=f"cloud-proposal-{pid}").start()
    else:
        _start_cloud(pid, executor, repo, watch=False)
    return get(pid)


def _start_local(p, executor, repo):
    """The run on this PC: a worktree from master and a local `claude -p` in it. Sets p["run_id"]."""
    os.makedirs(WORKTREE_ROOT, exist_ok=True)
    code, out = _git(repo, "worktree", "add", "-b", p["branch"], p["worktree"], BASE_BRANCH)
    if code != 0:
        raise ValueError(f"Couldn't make a working copy: {out[-300:]}")
    prompt = BRIEF.format(goal=p["goal"], protected=", ".join(PROTECTED), test_cmd=test_command())
    p["run_id"] = executor.submit("Jarvis (copy)", p["worktree"], prompt, kind="self_improve",
                                  meta={"proposal_id": p["id"], "goal": p["goal"]})
    return p["run_id"]


def _start_cloud(pid, executor, repo, watch=True):
    p = get(pid)
    c = p["cloud"]
    brief = CLOUD_BRIEF.format(goal=p["goal"], protected=", ".join(PROTECTED), test_cmd=test_command(cloud_session=True),
                               branch=c["branch"], pid=pid)
    try:
        info, why = cloud.start_session(repo, brief, push_branch=BASE_BRANCH)
    except Exception as error:
        info, why = None, f"the cloud couldn't be started ({error})"
    if info:
        _update(pid, cloud=dict(c, starting=False, session_id=info["session_id"], url=info["url"], hint=info["branch"],
                                started_at=time.time(), deadline=time.time() + config.CLOUD_WAIT_SECONDS))
        if watch:
            watch_cloud(pid, repo)
        return get(pid)
    return _fall_back_local(pid, executor, repo, why)


def _fall_back_local(pid, executor, repo, why):
    p = get(pid)
    if not p or p["status"] != "working":         # rejected while the cloud was being tried
        return p
    try:
        _start_local(p, executor, repo)
    except Exception as error:
        _update(pid, status="run_failed", where=cloud.LOCAL, cloud_fallback=why, detail=f"Couldn't run it on this PC either: {error}")
        return get(pid)
    _update(pid, where=cloud.LOCAL, run_id=p["run_id"], cloud_fallback=why)
    return get(pid)


def check_cloud(pid, repo=None, now=None, run_tests=None):
    """One look at GitHub for a cloud change. When its branch is there and marked done (or the time is up and it
    is there anyway), it's fetched into a local worktree and finalise() checks it like any other change."""
    repo = repo or config.REPO_DIR
    p = get(pid)
    if not p or p["status"] != "working" or p.get("where") != "cloud" or (p.get("cloud") or {}).get("starting"):
        return p
    c, now = p["cloud"], now or time.time()
    late = now >= c.get("deadline", 0)
    found = cloud.find_branch(repo, c["branch"], key=f"improve-{pid}", hint=c.get("hint"))
    if found:
        ok, message = cloud.fetch(repo, found)
        if ok and (cloud.is_done(message) or late):
            os.makedirs(WORKTREE_ROOT, exist_ok=True)
            code, out = _git(repo, "worktree", "add", "-b", p["branch"], p["worktree"], cloud.remote_ref(found))
            if code != 0:
                _update(pid, status="run_failed", detail=f"The cloud's work arrived but I couldn't make a working copy of it: {out[-300:]}")
                return get(pid)
            _update(pid, cloud=dict(c, branch=found, arrived_at=now))
            note = "" if cloud.is_done(message) else "(The cloud session never said it had finished; this is what it had pushed.)\n\n"
            return finalise(pid, {"status": "succeeded", "result": note + cloud.summary(message)}, repo=repo, run_tests=run_tests)
    if late:
        hours = round(config.CLOUD_WAIT_SECONDS / 3600, 1)
        _update(pid, status="run_failed", detail=f"The cloud session's work never arrived on GitHub (no branch {c['branch']} after "
                                                 f"{hours:g} hours). You can run it on this PC instead.")
    return get(pid)


_watching = set()


def watch_cloud(pid, repo=None, sleep=time.sleep):
    """Polls GitHub every couple of minutes until the change is checked or given up on."""
    if pid in _watching:
        return
    _watching.add(pid)
    try:
        while True:
            try:
                p = check_cloud(pid, repo)
            except Exception as error:
                print("selfimprove: cloud check failed:", error)
                p = get(pid)
            if not p or p["status"] != "working":
                return
            sleep(config.CLOUD_POLL_SECONDS)
    finally:
        _watching.discard(pid)


def resume_cloud_watches(executor, repo=None):
    """After a restart: carry on watching cloud changes; one whose start was cut off runs on this PC."""
    for p in _load():
        if p["status"] != "working" or p.get("where") != "cloud":
            continue
        if (p.get("cloud") or {}).get("starting"):
            target, args = _fall_back_local, (p["id"], executor, repo or config.REPO_DIR, "Jarvis was restarted while starting the cloud session")
        else:
            target, args = watch_cloud, (p["id"], repo)
        threading.Thread(target=target, args=args, daemon=True, name=f"cloud-proposal-{p['id']}").start()


def run_locally(pid, executor, repo=None):
    """Danny's 'Run it on this PC instead' for a cloud change that never arrived."""
    repo = repo or config.REPO_DIR
    p = get(pid)
    if not p or p.get("where") != "cloud" or p["status"] != "run_failed":
        raise ValueError("Only a cloud change that didn't arrive can be re-run on this PC.")
    dirty = uncommitted_tracked(repo)
    if dirty:
        raise ValueError("Jarvis's current code has unsaved changes in git (" + ", ".join(dirty[:5]) + "). They need saving first.")
    _cleanup(p, repo, delete_branch="force")
    _start_local(p, executor, repo)
    _update(pid, status="working", where=cloud.LOCAL, run_id=p["run_id"], detail="", cloud_fallback="Danny asked for it to run on this PC")
    return f"Running change {pid} on this PC now."


def on_run_change(run_id, run):
    """Executor listener: when a self-improvement run ends, check its work (in the background)."""
    pid = ((run or {}).get("meta") or {}).get("proposal_id")
    if not pid or run["status"] not in ("succeeded", "failed", "cancelled", "timed_out"):
        return
    threading.Thread(target=finalise, args=(pid, run), daemon=True, name=f"check-proposal-{pid}").start()


def finalise(pid, run, repo=None, run_tests=None):
    """Lists the changes, enforces protected files, runs the tests itself. Sets the proposal's verdict."""
    repo = repo or config.REPO_DIR
    p = get(pid)
    if not p or p["status"] != "working":
        return p
    if run["status"] != "succeeded":
        _update(pid, status="run_failed", detail=(run.get("error") or run["status"])[:1000], summary=run.get("result", "")[:2000])
        return get(pid)
    wt = p["worktree"]
    # anything the run left uncommitted is committed so the review sees exactly what will be merged
    code, out = _git(wt, "status", "--porcelain")
    if out.strip():
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", f"Jarvis improvement: {p['goal'][:60]} (uncommitted work from the run)")
    _, names = _git(wt, "diff", "--name-only", f"{BASE_BRANCH}...HEAD")
    files = [f for f in names.splitlines() if f.strip()]
    _, stat = _git(wt, "diff", "--stat", f"{BASE_BRANCH}...HEAD")
    base = dict(files=files, diffstat=stat[-3000:], summary=(run.get("result") or "")[:3000], checked_at=time.time())
    if not files:
        _update(pid, status="no_changes", **base)
        return get(pid)
    hits = protected_hits(files)
    if hits:
        _update(pid, status="blocked", detail="It changed protected files: " + ", ".join(hits), **base)
        return get(pid)
    ok, output = (run_tests or _run_tests)(wt, files)
    _update(pid, status="ready_for_review" if ok else "tests_failed", tests_passed=ok, test_output=output[-4000:], **base)
    return get(pid)


def _run_tests(worktree, files):
    modules = list(TESTS)
    cmd = [sys.executable, "-m", "unittest", *modules]
    try:
        r = subprocess.run(cmd, cwd=worktree, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=TEST_TIMEOUT, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return r.returncode == 0, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        return False, "The tests took too long and were stopped."


def diff(pid, repo=None, max_chars=60000):
    p = get(pid)
    if not p:
        return ""
    where = p["worktree"] if os.path.isdir(p["worktree"]) else (repo or config.REPO_DIR)
    _, out = _git(where, "diff", f"{BASE_BRANCH}...{p['branch']}" if where != p["worktree"] else f"{BASE_BRANCH}...HEAD")
    return out[:max_chars]


def _cleanup(p, repo, delete_branch):
    if os.path.isdir(p["worktree"]):
        _git(repo, "worktree", "remove", "--force", p["worktree"])
    _git(repo, "worktree", "prune")
    if delete_branch:
        _git(repo, "branch", "-D" if delete_branch == "force" else "-d", p["branch"])


def approve(pid, repo=None):
    """DANNY ONLY (window button). Merges a reviewed, tested change into the live code."""
    repo = repo or config.REPO_DIR
    p = get(pid)
    if not p:
        raise ValueError("No such change.")
    if p["status"] != "ready_for_review":
        raise ValueError(f"That change can't be applied: it is {p['status'].replace('_', ' ')}.")
    clash = set(uncommitted_tracked(repo)) & set(p.get("files") or [])
    if clash:
        raise ValueError("These files have unsaved edits in the live copy, so merging could lose work: " + ", ".join(sorted(clash)))
    code, out = _git(repo, "merge", "--no-ff", "--no-edit", "-m", f"Apply Jarvis improvement {pid}: {p['goal'][:60]}", p["branch"])
    if code != 0:
        _git(repo, "merge", "--abort")
        raise ValueError(f"The change didn't merge cleanly, so nothing was applied: {out[-400:]}")
    _, sha = _git(repo, "rev-parse", "HEAD")
    _cleanup(p, repo, delete_branch="soft")
    _update(pid, status="applied", merge_commit=sha.strip(), applied_at=time.time())
    return "Applied. Restart Jarvis to load the change. You can undo it from the Changes tab."


def reject(pid, repo=None):
    repo = repo or config.REPO_DIR
    p = get(pid)
    if not p:
        raise ValueError("No such change.")
    if p["status"] in ("applied", "undone"):
        raise ValueError("That change was already applied; use Undo instead.")
    _cleanup(p, repo, delete_branch="force")
    _update(pid, status="rejected", rejected_at=time.time())
    return "Rejected and thrown away. Nothing in Jarvis changed."


def undo(pid, repo=None):
    """Reverts an applied change with a new commit (history is never rewritten)."""
    repo = repo or config.REPO_DIR
    p = get(pid)
    if not p or p["status"] != "applied":
        raise ValueError("Only an applied change can be undone.")
    code, out = _git(repo, "revert", "-m", "1", "--no-edit", p["merge_commit"])
    if code != 0:
        _git(repo, "revert", "--abort")
        raise ValueError(f"Couldn't undo it automatically: {out[-400:]}")
    _update(pid, status="undone", undone_at=time.time())
    return "Undone. Restart Jarvis to load the previous version."


def describe(p):
    if not p:
        return "No such change."
    labels = {"working": "being worked on", "ready_for_review": "ready for you to review in the Changes tab",
              "tests_failed": "finished but its tests failed, so it can't be applied", "blocked": "blocked (it touched a protected file)",
              "run_failed": "stopped before finishing", "no_changes": "finished without changing anything",
              "applied": "applied", "rejected": "rejected", "undone": "undone"}
    where = cloud.where_phrase(p.get("where"))
    if p["status"] == "working" and p.get("where") == "cloud" and (p.get("cloud") or {}).get("starting"):
        where = "starting in the cloud"
    return f"Change {p['id']} ('{p['goal'][:80]}'): {labels.get(p['status'], p['status'])} ({where})."
