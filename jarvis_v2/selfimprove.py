"""
Jarvis improving his own code, safely.

  1. propose(goal): a separate git worktree (a full copy on its own branch, outside the live folder)
     is made from the committed code, and a Claude Code run works THERE, never in the running Jarvis.
  2. When the run ends, Jarvis does not take its word for anything: he lists what changed, refuses
     any change to a PROTECTED file, and runs the test suite himself in the worktree.
  3. A change that passes becomes "ready for review". ONLY Danny can apply it (the window's Approve
     button calls approve(); no brain tool can). Applying merges the branch into master.
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

from . import config

PROPOSALS_FILE = os.path.join(config.DATA_DIR, "proposals.json")
WORKTREE_ROOT = os.path.join(os.path.dirname(config.REPO_DIR), "Jarvis_worktrees")
BASE_BRANCH = "master"
PROTECTED = ("jarvis_trading/live/", "jarvis_store/shopify.py", "jarvis_store/creds.py",
             "jarvis_v2/selfimprove.py", "jarvis_v2/claude_env.py", "jarvis_v2/runs.py", ".gitignore")
TESTS = ["jarvis_v2.tests.test_v2", "jarvis_v2.tests.test_server", "jarvis_v2.tests.test_phase3", "jarvis_store.test_store"]
TEST_TIMEOUT = 600

BRIEF = """You are improving Jarvis, Danny's assistant, working in a SEPARATE COPY of his code (a git worktree).
The running Jarvis is not affected; Danny reviews your change before it is applied.

Goal: {goal}

Rules:
- Make the smallest change that achieves the goal. Match the surrounding code's style.
- NEVER edit these protected files (a change to any of them is rejected automatically): {protected}.
- Never add live trading, never weaken a safety check, never store passwords or keys in code.
- Add or update tests for what you change. Run: {test_cmd}
  Everything must pass.
- Commit your work on this branch with a clear message. Never push.
- Finish with a short plain-English summary for Danny: what changed, why, and how to try it.
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


def test_command():
    return f"py -3 -m unittest {' '.join(TESTS)}"


def protected_hits(files):
    return [f for f in files if any(f == p or f.startswith(p) for p in PROTECTED)]


def uncommitted_tracked(repo=None):
    """Tracked files with uncommitted changes in the live repo (a worktree is made from COMMITTED code)."""
    code, out = _git(repo or config.REPO_DIR, "status", "--porcelain", "--untracked-files=no")
    return [l[3:] for l in out.splitlines() if l.strip()] if code == 0 else []


def propose(goal, executor, repo=None):
    repo = repo or config.REPO_DIR
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("Say what you'd like changed.")
    dirty = uncommitted_tracked(repo)
    if dirty:
        raise ValueError("Jarvis's current code has unsaved changes in git (" + ", ".join(dirty[:5]) +
                         "). They need saving first, or my copy would be missing them.")
    pid = int(time.time())
    branch = f"jarvis/improve-{pid}"
    path = os.path.join(WORKTREE_ROOT, f"improve-{pid}")
    os.makedirs(WORKTREE_ROOT, exist_ok=True)
    code, out = _git(repo, "worktree", "add", "-b", branch, path, BASE_BRANCH)
    if code != 0:
        raise ValueError(f"Couldn't make a working copy: {out[-300:]}")
    prompt = BRIEF.format(goal=goal, protected=", ".join(PROTECTED), test_cmd=test_command())
    run_id = executor.submit("Jarvis (copy)", path, prompt, kind="self_improve", meta={"proposal_id": pid, "goal": goal})
    with _lock:
        _save(_load() + [{"id": pid, "goal": goal, "branch": branch, "worktree": path, "run_id": run_id,
                          "status": "working", "created_at": time.time()}])
    return get(pid)


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
    return f"Change {p['id']} ('{p['goal'][:80]}'): {labels.get(p['status'], p['status'])}."
