"""
Cloud first for coding jobs: Jarvis's code-changing work (self-improvements and builds) runs in a
Claude Code CLOUD session when it can, so it doesn't use up the allowance on Danny's PC.

  1. The code the cloud works from must be on GitHub: `git push origin <branch>` first.
  2. start_session(): `claude --cloud "<brief>"` starts the session. Its id/URL is read from what it prints.
     Any error, "no credit", "unavailable" or no session in the output means: do it on this PC instead.
  3. The brief tells the session to push its work to its own branch and to end its LAST commit message
     with the line "Jarvis-Done: <id>". Jarvis polls GitHub (git ls-remote / fetch) until that branch shows
     up and is marked done, or until the time runs out.
  4. What arrives is NOT trusted: the caller puts it through exactly the same checks as a local run
     (protected files, Jarvis running the tests himself, Danny's approval).

Only coding work comes here. Chat, the browser, the shop and jobs stay on this PC.
"""

import os
import re
import subprocess

from . import config

DONE_MARK = "Jarvis-Done:"
SESSION_RE = re.compile(r"\b(session_[A-Za-z0-9]{6,})\b")
URL_RE = re.compile(r"(https://claude\.ai/code/[^\s\"'<>)]+)")
BRANCH_RE = re.compile(r"\b(claude/[A-Za-z0-9._/-]*[A-Za-z0-9])")
# What `claude --cloud` says when it can't take the work (checked in lower case).
UNAVAILABLE = ("credit", "quota", "insufficient", "unavailable", "not available", "not enabled", "not supported",
               "upgrade", "billing", "not logged in", "please log in", "please login", "/login", "unauthorized",
               "unauthorised", "connect github", "no github", "rate limit", "usage limit")


def mode():
    """'cloud' (try the cloud first) or 'local' (always this PC). Setting: JARVIS_CODE_JOBS."""
    return "local" if str(config.CODE_JOBS or "").strip().lower() == "local" else "cloud"


LOCAL = "this PC"            # a job's "where": "cloud" or "this PC"


def where_phrase(where):
    return "in the cloud" if where == "cloud" else "on this PC"


def _git(cwd, *args, timeout=120):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=0x08000000 if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as error:
        return -1, f"git {args[0]} didn't finish: {error}"
    return r.returncode, (r.stdout + r.stderr).strip()


def cloud_cli(brief, cwd, timeout=None):
    """THE one place the cloud CLI is called (adjust the command line here; tests replace this function).
    Returns (exit_code, everything it printed). A timeout returns code None with whatever it printed so far."""
    from .claude_env import child_env
    cmd = [config.find_claude_exe(), "--cloud", brief]
    try:
        r = subprocess.run(cmd, cwd=cwd, env=child_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout or config.CLOUD_START_TIMEOUT,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as error:
        out = error.stdout or b""
        out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
        err = error.stderr or b""
        err = err.decode("utf-8", "replace") if isinstance(err, bytes) else err
        return None, out + err
    except OSError as error:
        return -1, f"Couldn't start Claude Code: {error}"


def parse_session(output):
    """{session_id, url, branch} read from what `claude --cloud` printed (missing ones are None)."""
    text = output or ""
    sid, url, branch = SESSION_RE.search(text), URL_RE.search(text), BRANCH_RE.search(text)
    session_id = sid.group(1) if sid else None
    if not session_id and url:
        tail = SESSION_RE.search(url.group(1))
        session_id = tail.group(1) if tail else None
    return {"session_id": session_id, "url": url.group(1).rstrip(".,") if url else None,
            "branch": branch.group(1) if branch else None}


def start_session(cwd, brief, push_branch="master", cli=None, git=None):
    """Pushes the base branch, then starts the cloud session.
    Returns (info, None) on success, or (None, plain-English reason) when the work should run on this PC."""
    git = git or _git
    code, out = git(cwd, "push", "origin", push_branch)
    if code != 0:
        return None, f"couldn't put {push_branch} on GitHub first ({out[-200:] or 'git push failed'})"
    code, out = (cli or cloud_cli)(brief, cwd)
    info = parse_session(out)
    if (info["session_id"] or info["url"]) and code in (0, None):
        info["output"] = (out or "")[-2000:]
        return info, None
    said_no = next((m for m in UNAVAILABLE if m in (out or "").lower()), None)
    if said_no:
        return None, f"the cloud said it can't take it ({_last_line(out)})"
    if code is None:
        return None, "the cloud didn't start a session in time"
    if code != 0:
        return None, f"the cloud command failed ({_last_line(out) or f'exit code {code}'})"
    return None, "the cloud command didn't say which session it started"


def _last_line(text):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1][:200] if lines else ""


def find_branch(cwd, expected, key=None, hint=None, git=None):
    """The remote branch holding the cloud's work, or None: the one asked for, the one the cloud named,
    else any claude/* branch with this job's key (e.g. "improve-1790000000") in its name."""
    code, out = (git or _git)(cwd, "ls-remote", "--heads", "origin")
    if code != 0:
        return None
    heads = [line.split("refs/heads/", 1)[1].strip() for line in out.splitlines() if "refs/heads/" in line]
    for name in (expected, hint):
        if name and name in heads:
            return name
    if key:
        return next((h for h in heads if h.startswith("claude/") and key in h), None)
    return None


def remote_ref(branch):
    return f"refs/remotes/origin/{branch}"


def fetch(cwd, branch, git=None):
    """Fetches the branch. Returns (ok, its latest commit message)."""
    git = git or _git
    code, out = git(cwd, "fetch", "origin", f"+refs/heads/{branch}:{remote_ref(branch)}")
    if code != 0:
        return False, out
    code, msg = git(cwd, "log", "-1", "--format=%B", remote_ref(branch))
    return code == 0, msg


def is_done(message):
    return DONE_MARK in (message or "")


def summary(message):
    """The cloud's summary for Danny: its last commit message without the done line."""
    return "\n".join(l for l in (message or "").splitlines() if not l.strip().startswith(DONE_MARK)).strip()
