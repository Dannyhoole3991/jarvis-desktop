"""
Projects: the folders Jarvis can work in, and what's known about each.

Found from three places, nothing new captured:
  1. folders Claude Code has worked in (the `cwd` recorded in ~/.claude/projects transcripts),
  2. git repositories directly on the Desktop,
  3. projects Jarvis created (data/projects.json).
Names arrive by voice, so lookups are forgiving ("the jarvis one", "collective store").
New project names are validated hard: no paths, no "..", letters/numbers/spaces/dashes only.
"""

import difflib
import glob
import json
import os
import re
import subprocess

from . import config

CLAUDE_PROJECTS = os.path.join(os.path.expanduser("~"), ".claude", "projects")
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")
REGISTRY = os.path.join(config.DATA_DIR, "projects.json")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,40}$")
SKIP = re.compile(r"(?i)\\(windows|program files|appdata\\local\\temp)\\|\\worktrees?\\|jarvis_worktrees")


def _git(cwd, *args):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _registered():
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _first_cwd(transcript):
    """The working folder recorded near the top of one Claude Code transcript, or None."""
    try:
        with open(transcript, encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(40), f):
                if '"cwd"' in line:
                    cwd = json.loads(line).get("cwd")
                    if cwd:
                        return os.path.normpath(cwd)
    except Exception:
        pass
    return None


def _claude_code_folders():
    """One folder per Claude Code project directory, read from its most recent transcripts."""
    found = set()
    for d in glob.glob(os.path.join(CLAUDE_PROJECTS, "*")):
        for transcript in sorted(glob.glob(os.path.join(d, "*.jsonl")), key=os.path.getmtime, reverse=True)[:3]:
            cwd = _first_cwd(transcript)
            if cwd:
                found.add(cwd)
                break
    return found


def discover():
    """[{name, path, source}] for every known project folder that still exists, de-duplicated."""
    paths = {}
    for p in _claude_code_folders():
        paths.setdefault(os.path.normcase(p), (p, "claude code"))
    for git_dir in glob.glob(os.path.join(DESKTOP, "*", ".git")):
        p = os.path.dirname(git_dir)
        paths.setdefault(os.path.normcase(p), (p, "desktop"))
    for entry in _registered():
        paths.setdefault(os.path.normcase(entry["path"]), (entry["path"], "created by jarvis"))
    out = []
    for p, source in paths.values():
        if os.path.isdir(p) and not SKIP.search(p + "\\"):
            out.append({"name": os.path.basename(p.rstrip("\\/")) or p, "path": p, "source": source})
    # nested folders of the same project (e.g. Jarvis\store_data\product_pages) collapse into the outermost
    out.sort(key=lambda x: len(x["path"]))
    kept = []
    for proj in out:
        if not any(os.path.normcase(proj["path"]).startswith(os.path.normcase(k["path"]) + os.sep) for k in kept):
            kept.append(proj)
    return sorted(kept, key=lambda x: x["name"].lower())


def find(name):
    """The project Danny means by `name`, or None. Exact name, then contains, then close spelling."""
    if not name:
        return None
    projects = discover()
    key = re.sub(r"(?i)\b(the|project|folder|one|repo)\b", "", name).strip().lower()
    for p in projects:
        if p["name"].lower() == key:
            return p
    contains = [p for p in projects if key and key in p["name"].lower()]
    if len(contains) == 1:
        return contains[0]
    close = difflib.get_close_matches(key, [p["name"].lower() for p in projects], n=1, cutoff=0.6)
    if close:
        return next(p for p in projects if p["name"].lower() == close[0])
    return contains[0] if contains else None


def details(project):
    path = project["path"]
    info = dict(project)
    info["branch"] = _git(path, "rev-parse", "--abbrev-ref", "HEAD") or None
    info["last_commit"] = _git(path, "log", "-1", "--format=%h %s (%cr)") or None
    status = _git(path, "status", "--porcelain")
    info["uncommitted_changes"] = len(status.splitlines()) if status else 0
    readme = next((os.path.join(path, n) for n in ("README.md", "readme.md", "README.txt") if os.path.exists(os.path.join(path, n))), None)
    if readme:
        with open(readme, encoding="utf-8", errors="replace") as f:
            info["readme_start"] = f.read(600)
    info["docs"] = docs(path)
    return info


def docs(path):
    """Specs and plans in a project (docs/specs, docs/plans, docs/superpowers/*)."""
    found = []
    for pattern in ("docs/specs/*.md", "docs/plans/*.md", "docs/superpowers/specs/*.md", "docs/superpowers/plans/*.md", "docs/*PLAN*.md"):
        for f in glob.glob(os.path.join(path, pattern)):
            found.append(os.path.relpath(f, path).replace("\\", "/"))
    return sorted(set(found))


def create(name, parent=None):
    """A new, empty project folder with a git repository. Refuses anything that isn't a plain name."""
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("Project names can only use letters, numbers, spaces, dashes and underscores.")
    parent = parent or DESKTOP
    path = os.path.realpath(os.path.join(parent, name))
    if os.path.dirname(path) != os.path.realpath(parent):
        raise ValueError("That name would put the project somewhere else.")
    if os.path.exists(path):
        raise ValueError(f"Something called {name} already exists on the Desktop.")
    os.makedirs(path)
    _git(path, "init")
    with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as f:
        f.write(f"# {name}\n")
    reg = _registered() + [{"name": name, "path": path}]
    os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
    with open(REGISTRY, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=1)
    return {"name": name, "path": path, "source": "created by jarvis"}


def open_in_editor(project, file=None):
    target = os.path.join(project["path"], file) if file else project["path"]
    subprocess.Popen(["cmd", "/c", "code", target], creationflags=0x08000000)
    return f"Opened {os.path.basename(target)} in VS Code."


def open_in_terminal(project):
    subprocess.Popen(["wt", "-d", project["path"]])
    return f"Opened a terminal in {project['name']}."
