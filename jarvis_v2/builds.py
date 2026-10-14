"""
Builds: real projects, done properly. Spec -> Danny's approval -> plan -> test-driven build.

  1. start_build writes what was agreed in conversation as a SPEC file inside the project
     (docs/specs/YYYY-MM-DD-<topic>-design.md). Nothing runs yet.
  2. Danny reads it (Specs tab, or "read me section 2") and approves it.
  3. Only then a Claude Code run starts with a brief: write a phased plan with checkboxes
     (docs/plans/...-plan.md), then build it task by task, test first, ticking each box.
  4. "How far has it got" is answered by counting ticked boxes in the plan, not by guessing.

Builds of Jarvis himself go through selfimprove.py instead (separate copy, tests, approval).
"""

import datetime
import json
import os
import re
import time

from . import config, projects as P

BUILDS_FILE = os.path.join(config.DATA_DIR, "builds.json")
APPROVALS_FILE = os.path.join(config.DATA_DIR, "approvals.jsonl")
CHECK_RE = re.compile(r"^\s*[-*] \[( |x|X)\]", re.M)
SECTION_RE = re.compile(r"^(#{1,3})\s+(.*)$", re.M)

BUILD_BRIEF = """You are building a project for Danny, working unattended. The agreed design is in {spec}.

1. Read {spec} carefully.
2. Write a phased implementation plan to {plan}. Every task is a Markdown checkbox line "- [ ] ...".
   Small tasks; each phase ends with something that runs.
3. Then carry out the plan task by task using test-driven development: write the test, see it fail,
   make it pass. Tick each box ("- [x]") in {plan} as soon as that task is done and its tests pass,
   so progress can be read from the plan at any time.
4. Commit with git at the end of each phase (clear messages). Never push. Never delete files you didn't create.
5. If something in the spec is unclear, choose the simplest safe option and note the decision in the plan.
6. Finish with a short summary: what was built, how to run it, and anything Danny needs to do himself.
"""


def _load():
    try:
        with open(BUILDS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(builds):
    os.makedirs(os.path.dirname(BUILDS_FILE), exist_ok=True)
    with open(BUILDS_FILE, "w", encoding="utf-8") as f:
        json.dump(builds, f, indent=1)


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "build"


def is_jarvis_repo(path):
    return os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(config.REPO_DIR))


def start_build(project_name, title, goal, constraints="", out_of_scope="", now=None):
    """Writes the spec. Returns the build record. Nothing is run until approve_document()."""
    project = P.find(project_name)
    if not project:
        raise ValueError(f"I don't know a project called {project_name}.")
    if is_jarvis_repo(project["path"]):
        raise ValueError("Changes to Jarvis himself go through improve_jarvis (a separate copy, tests, and your approval).")
    day = datetime.date.fromtimestamp(now or time.time()).isoformat()
    slug = _slug(title)
    spec_rel = f"docs/specs/{day}-{slug}-design.md"
    plan_rel = f"docs/plans/{day}-{slug}-plan.md"
    spec_abs = os.path.join(project["path"], spec_rel)
    os.makedirs(os.path.dirname(spec_abs), exist_ok=True)
    with open(spec_abs, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n_Design agreed with Danny on {day}. Status: **awaiting Danny's approval**._\n\n"
                f"## 1. Goal\n\n{goal.strip()}\n\n"
                f"## 2. Constraints\n\n{constraints.strip() or 'None stated.'}\n\n"
                f"## 3. Out of scope\n\n{out_of_scope.strip() or 'Nothing stated.'}\n")
    build = {"id": int(time.time() * 1000), "project": project["name"], "path": project["path"], "title": title,
             "spec": spec_rel, "plan": plan_rel, "status": "awaiting_approval", "run_ids": [], "created_at": time.time()}
    builds = _load() + [build]
    _save(builds)
    return build


def _find_build(project_path, doc):
    doc = doc.replace("\\", "/")
    for b in reversed(_load()):
        if os.path.normcase(b["path"]) == os.path.normcase(project_path) and doc in (b["spec"], b["plan"]):
            return b
    return None


def approve_document(project_name, doc, executor, approver="Danny"):
    """Records Danny's approval. Approving a build's spec is what starts the build."""
    project = P.find(project_name)
    if not project:
        raise ValueError(f"I don't know a project called {project_name}.")
    path = os.path.join(project["path"], doc)
    if not os.path.isfile(path):
        raise ValueError(f"There's no {doc} in {project['name']}.")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n\n---\n_Approved by {approver} on {stamp}._\n")
    with open(APPROVALS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "project": project["name"], "doc": doc, "approver": approver}) + "\n")
    build = _find_build(project["path"], doc)
    if build and build["status"] == "awaiting_approval" and doc == build["spec"]:
        brief = BUILD_BRIEF.format(spec=build["spec"], plan=build["plan"])
        run_id = executor.submit(project["name"], project["path"], brief, kind="build", meta={"build_id": build["id"], "title": build["title"]})
        builds = _load()
        for b in builds:
            if b["id"] == build["id"]:
                b.update(status="building", run_ids=b["run_ids"] + [run_id], approved_at=time.time())
        _save(builds)
        return f"Approved, sir. The build of '{build['title']}' has started (run {run_id})."
    return f"Noted your approval of {doc}."


def progress(path):
    """(done, total) checkboxes in a plan file."""
    try:
        with open(path, encoding="utf-8") as f:
            marks = CHECK_RE.findall(f.read())
    except OSError:
        return 0, 0
    return sum(1 for m in marks if m.lower() == "x"), len(marks)


def build_status(project_name=None, store=None):
    builds = _load()
    if project_name:
        project = P.find(project_name)
        if not project:
            return f"I don't know a project called {project_name}."
        builds = [b for b in builds if os.path.normcase(b["path"]) == os.path.normcase(project["path"])]
    if not builds:
        return "No builds yet."
    b = builds[-1]
    done, total = progress(os.path.join(b["path"], b["plan"]))
    run = store.get(b["run_ids"][-1]) if (store and b["run_ids"]) else None
    status = b["status"]
    if run and run["status"] in ("succeeded", "failed", "cancelled", "timed_out"):
        status = {"succeeded": "finished", "failed": "stopped with a problem", "cancelled": "cancelled", "timed_out": "ran out of time"}[run["status"]]
    text = f"'{b['title']}' in {b['project']}: {status.replace('_', ' ')}."
    if total:
        text += f" {done} of {total} plan tasks done."
    elif b["status"] == "awaiting_approval":
        text += f" The spec ({b['spec']}) is waiting for your approval."
    if run and run.get("error") and run["status"] != "succeeded":
        text += f" Problem: {run['error'][:200]}"
    return text


def sections(text):
    """[(number, heading, body)] for the document's headings."""
    marks = list(SECTION_RE.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((i + 1, m.group(2).strip(), text[m.end():end].strip()))
    return out


def review_document(project_name, doc=None, section=None):
    """With no section: the document's outline. With a section number: that section's text."""
    project = P.find(project_name)
    if not project:
        return f"I don't know a project called {project_name}."
    if not doc:
        docs = P.docs(project["path"])
        return ("Documents in " + project["name"] + ":\n" + "\n".join(docs)) if docs else f"{project['name']} has no specs or plans yet."
    path = os.path.join(project["path"], doc)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return f"There's no {doc} in {project['name']}."
    secs = sections(text)
    if section is None:
        return "Sections:\n" + "\n".join(f"{n}. {h}" for n, h, _ in secs)
    for n, h, body in secs:
        if n == int(section):
            return f"{n}. {h}\n\n{body[:3000]}"
    return f"There's no section {section}; it has {len(secs)}."


def all_documents():
    """Every spec/plan across known projects, for the Specs tab."""
    out = []
    for project in P.discover():
        for doc in P.docs(project["path"]):
            full = os.path.join(project["path"], doc)
            done, total = progress(full) if "/plans/" in doc or "plan" in doc.lower() else (0, 0)
            try:
                with open(full, encoding="utf-8") as f:
                    approved = "_Approved by" in f.read()
            except OSError:
                approved = False
            out.append({"project": project["name"], "doc": doc, "approved": approved, "done": done, "total": total,
                        "modified": os.path.getmtime(full)})
    return sorted(out, key=lambda d: -d["modified"])


def on_run_change(run_id, run):
    """Executor listener: when a build's run ends, record how the build ended."""
    build_id = ((run or {}).get("meta") or {}).get("build_id")
    if not build_id or run["status"] not in ("succeeded", "failed", "cancelled", "timed_out"):
        return
    builds = _load()
    for b in builds:
        if b["id"] == build_id:
            b["status"] = "finished" if run["status"] == "succeeded" else f"stopped ({run['status'].replace('_', ' ')})"
            b["finished_at"] = time.time()
    _save(builds)


def builds_list():
    out = []
    for b in reversed(_load()):
        done, total = progress(os.path.join(b["path"], b["plan"]))
        out.append(dict(b, done=done, total=total))
    return out
