"""
Builds: real projects, done properly. Spec -> Danny's approval -> plan -> test-driven build.

  1. start_build writes what was agreed in conversation as a SPEC file inside the project
     (docs/specs/YYYY-MM-DD-<topic>-design.md). Nothing runs yet.
  2. Danny reads it (Specs tab, or "read me section 2") and approves it.
  3. Only then a Claude Code run starts with a brief: write a phased plan with checkboxes
     (docs/plans/...-plan.md), then build it task by task, test first, ticking each box.
  4. "How far has it got" is answered by counting ticked boxes in the plan, not by guessing.

Cloud first (cloud.py, setting JARVIS_CODE_JOBS): when the project is a git repository on GitHub with nothing
unsaved, the approved spec is committed and pushed and a Claude Code cloud session does the build on its own branch.
When that branch arrives it is fetched; if the project is still clean on the same branch it's fast-forwarded to it
(exactly what a build on this PC leaves behind), otherwise it is kept as branch jarvis/build-<id> for Danny.
If the cloud can't take it, the build runs on this PC as before.

Builds of Jarvis himself go through selfimprove.py instead (separate copy, tests, approval).
"""

import datetime
import json
import os
import re
import subprocess
import threading
import time

from . import cloud, config, projects as P

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

CLOUD_BUILD_BRIEF = """You are building a project for Danny in a cloud session on its GitHub repository, working unattended.
Start from the {base} branch as it is on GitHub. The agreed design is in {spec}.

""" + BUILD_BRIEF.split("\n\n", 1)[1].replace("Never push.", "Push only your own branch (below).") + """
Commit your work to a branch named {branch} and push THAT branch only. Never push to {base}, never open or merge a pull request.
Your LAST commit's message is your short summary for Danny and must end with this line on its own: """ + cloud.DONE_MARK + """ {build_id}
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


def approve_document(project_name, doc, executor, approver="Danny", background=True):
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
        _update(build["id"], approved_at=time.time())
        if cloud.mode() == "cloud":
            _update(build["id"], status="building", where="cloud", cloud={"branch": f"claude/jarvis-build-{build['id']}", "starting": True})
            if background:
                threading.Thread(target=_start_cloud, args=(build["id"], executor), daemon=True, name=f"cloud-build-{build['id']}").start()
            else:
                _start_cloud(build["id"], executor, watch=False)
            return (f"Approved, sir. I'm sending the build of '{build['title']}' to a Claude Code cloud session; "
                    "if the cloud can't take it, it'll run on this PC instead.")
        run_id = _start_local(_get(build["id"]), executor)
        return f"Approved, sir. The build of '{build['title']}' has started (run {run_id})."
    return f"Noted your approval of {doc}."


def _get(build_id):
    return next((b for b in _load() if b["id"] == build_id), None)


_lock = threading.Lock()


def _update(build_id, **fields):
    with _lock:
        builds = _load()
        for b in builds:
            if b["id"] == build_id:
                b.update(fields)
        _save(builds)


def _git(cwd, *args):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=120, creationflags=0x08000000 if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as error:
        return -1, str(error)
    return r.returncode, (r.stdout + r.stderr).strip()


def _start_local(build, executor, why=None):
    brief = BUILD_BRIEF.format(spec=build["spec"], plan=build["plan"])
    run_id = executor.submit(build["project"], build["path"], brief, kind="build", meta={"build_id": build["id"], "title": build["title"]})
    fields = dict(status="building", where=cloud.LOCAL, run_ids=build["run_ids"] + [run_id])
    if why:
        fields["cloud_fallback"] = why
    _update(build["id"], **fields)
    return run_id


def _cloud_ready(build):
    """(base branch, None) when the project can go to the cloud, else (None, why not). Commits the approved spec."""
    path = build["path"]
    code, base = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0 or not base or base == "HEAD":
        return None, "the project isn't a git repository on a branch"
    if _git(path, "remote", "get-url", "origin")[0] != 0:
        return None, "the project has no GitHub remote (origin)"
    _, status = _git(path, "status", "--porcelain", "--untracked-files=no")
    spec = build["spec"].replace("\\", "/")
    others = [l[3:] for l in status.splitlines() if l.strip() and l[3:].strip('"') != spec]
    if others:
        return None, "the project has unsaved changes the cloud wouldn't see (" + ", ".join(others[:3]) + ")"
    _git(path, "add", "-f", "--", spec)
    _, staged = _git(path, "diff", "--cached", "--name-only")
    if staged.strip():
        code, out = _git(path, "commit", "-m", f"Spec approved: {build['title'][:60]}", "--", spec)
        if code != 0:
            return None, f"couldn't commit the approved spec ({out[-200:]})"
    return base, None


def _start_cloud(build_id, executor, watch=True):
    build = _get(build_id)
    c = build["cloud"]
    try:
        base, why = _cloud_ready(build)
        info = None
        if base:
            brief = CLOUD_BUILD_BRIEF.format(spec=build["spec"], plan=build["plan"], base=base, branch=c["branch"], build_id=build_id)
            info, why = cloud.start_session(build["path"], brief, push_branch=base)
    except Exception as error:
        info, why = None, f"the cloud couldn't be started ({error})"
    if not info:
        _start_local(build, executor, why)
        return _get(build_id)
    _update(build_id, cloud=dict(c, starting=False, base=base, session_id=info["session_id"], url=info["url"], hint=info["branch"],
                                 started_at=time.time(), deadline=time.time() + config.CLOUD_WAIT_SECONDS))
    if watch:
        watch_cloud(build_id)
    return _get(build_id)


def check_cloud(build_id, now=None):
    """One look at GitHub for a cloud build. When its branch is there and marked done (or the time is up and it is
    there anyway) it's fetched: fast-forwarded into the project if nothing moved, else kept as jarvis/build-<id>."""
    b = _get(build_id)
    if not b or b.get("status") != "building" or b.get("where") != "cloud" or (b.get("cloud") or {}).get("starting"):
        return b
    c, now, path = b["cloud"], now or time.time(), b["path"]
    late = now >= c.get("deadline", 0)
    found = cloud.find_branch(path, c["branch"], key=f"build-{build_id}", hint=c.get("hint"))
    if found:
        ok, message = cloud.fetch(path, found)
        if ok and (cloud.is_done(message) or late):
            local = f"jarvis/build-{build_id}"
            _git(path, "branch", "-f", local, cloud.remote_ref(found))
            _, head = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
            _, dirty = _git(path, "status", "--porcelain", "--untracked-files=no")
            merged = head == c.get("base") and not dirty.strip() and _git(path, "merge", "--ff-only", local)[0] == 0
            note = "" if cloud.is_done(message) else "The cloud session never said it had finished; this is what it had pushed. "
            if not merged:
                note += f"It's on branch {local} in the project (the project had moved on, so I didn't put it in place). "
            _update(build_id, status="finished", finished_at=now, summary=(note + cloud.summary(message))[:3000],
                    cloud=dict(c, branch=found, arrived_at=now, local_branch=local, merged=merged))
            return _get(build_id)
    if late:
        hours = round(config.CLOUD_WAIT_SECONDS / 3600, 1)
        _update(build_id, status="stopped (cloud result never arrived)", finished_at=now,
                detail=f"No branch {c['branch']} on GitHub after {hours:g} hours. You can run it on this PC instead.")
    return _get(build_id)


_watching = set()


def watch_cloud(build_id, sleep=time.sleep):
    if build_id in _watching:
        return
    _watching.add(build_id)
    try:
        while True:
            try:
                b = check_cloud(build_id)
            except Exception as error:
                print("builds: cloud check failed:", error)
                b = _get(build_id)
            if not b or b.get("status") != "building":
                return
            sleep(config.CLOUD_POLL_SECONDS)
    finally:
        _watching.discard(build_id)


def resume_cloud_watches(executor):
    """After a restart: carry on watching cloud builds; one whose start was cut off runs on this PC."""
    for b in _load():
        if b.get("status") != "building" or b.get("where") != "cloud":
            continue
        if (b.get("cloud") or {}).get("starting"):
            target, args = _start_local, (b, executor, "Jarvis was restarted while starting the cloud session")
        else:
            target, args = watch_cloud, (b["id"],)
        threading.Thread(target=target, args=args, daemon=True, name=f"cloud-build-{b['id']}").start()


def run_locally(build_id, executor):
    """Danny's 'Run it on this PC instead' for a cloud build that never arrived."""
    b = _get(int(build_id))
    if not b or b.get("where") != "cloud" or not str(b.get("status", "")).startswith("stopped"):
        raise ValueError("Only a cloud build that didn't arrive can be re-run on this PC.")
    run_id = _start_local(b, executor, "Danny asked for it to run on this PC")
    return f"Building '{b['title']}' on this PC now (run {run_id})."


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
    text = f"'{b['title']}' in {b['project']}: {status.replace('_', ' ')}"
    text += f" ({cloud.where_phrase(b.get('where'))})." if b.get("where") else "."
    if total:
        text += f" {done} of {total} plan tasks done."
    elif b["status"] == "awaiting_approval":
        text += f" The spec ({b['spec']}) is waiting for your approval."
    if run and run.get("error") and run["status"] != "succeeded":
        text += f" Problem: {run['error'][:200]}"
    if b.get("detail"):
        text += f" {b['detail'][:200]}"
    if b.get("where") == "cloud" and b.get("summary"):
        text += f" {b['summary'][:400]}"
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
