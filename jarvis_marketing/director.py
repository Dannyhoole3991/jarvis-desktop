"""
The Marketing Director: runs the operating cycle as typed, capped tasks.

    research -> ads (find + reverse-engineer adverts) -> evaluate -> list (verify supplier, Shopify DRAFT, approval
    request) -> go_live (approved listings only) -> brief -> produce -> qa (<= max_revisions rework rounds)
    -> rank (pre-launch assessment) -> design (+ approval requests) -> paid_plan (PAUSED Facebook/Instagram
    test plans; launching is off) -> publish (approved posts only) -> collect -> judge -> learn (+ reports)

Every task: an idempotency key (unique), a lease, a timeout, an attempt cap, a validated input and
output, and an explicit end state (done / failed / timed_out / cancelled / blocked). A cycle has a
hard task cap and wall-time cap, and a stage the current mode doesn't allow is recorded as blocked
rather than skipped silently. The director never changes the mode or permissions; it only reads them.
After a restart, recover() retries or fails interrupted tasks; a post interrupted mid-publish is
never posted again automatically.
"""

import re
import secrets
import threading

from jarvis_marketing import config, controls as C, evidence as E
from jarvis_marketing.adapters import build
from jarvis_marketing.agents import (adintel, assessment, base, channels, creative, evaluation, learning, listing, production, qa,
                                     research, testing)
from jarvis_marketing.store import Store

PUBLISH_IN = {"post_ids?": [int]}
PUBLISH_OUT = {"published": [{"post_id": int, "status": str, "external_ref": str}], "refused": [{"post_id": int, "why": str}]}
DESIGN_IN = {"candidate_ids?": [int]}
DESIGN_OUT = {"experiments": [{"experiment_id": int, "candidate_id": int, "post_ids": [int]}], "notes": [str]}
COLLECT_OUT = {"metrics_stored": int, "orders_added": int, "unavailable": [str]}
JUDGE_OUT = {"judged": [{"experiment_id": int, "verdict": str, "status": str}]}

# stage -> (agent name, permission, function, input schema, output schema)
STAGES = {
    "research": ("research", "research", research.run, research.INPUT, research.OUTPUT),
    "ads": ("ad_research", "research", adintel.run, adintel.INPUT, adintel.OUTPUT),
    "evaluate": ("evaluation", "evaluate", evaluation.run, evaluation.INPUT, evaluation.OUTPUT),
    "list": ("listing", "create_draft", listing.run, listing.INPUT, listing.OUTPUT),
    "go_live": ("listing", "publish_product", listing.go_live, listing.LIVE_IN, listing.LIVE_OUT),
    "brief": ("creative", "brief", creative.run, creative.INPUT, creative.OUTPUT),
    "produce": ("production", "produce", production.run, production.INPUT, production.OUTPUT),
    "qa": ("qa", "qa", qa.run, qa.INPUT, qa.OUTPUT),
    "rank": ("assessment", "qa", assessment.run, assessment.INPUT, assessment.OUTPUT),
    "design": ("testing", "design_experiment", testing.design, DESIGN_IN, DESIGN_OUT),
    "paid_plan": ("testing", "prepare_paid", testing.paid_plan, testing.PAID_IN, testing.PAID_OUT),
    "publish": ("testing", "publish_organic", testing.publish, PUBLISH_IN, PUBLISH_OUT),
    "collect": ("testing", "read_analytics", testing.collect, {}, COLLECT_OUT),
    "judge": ("testing", "evaluate", testing.judge, {}, JUDGE_OUT),
    "learn": ("learning", "learn", learning.run, learning.INPUT, learning.OUTPUT),
    "channels": ("channels", "evaluate", channels.run, channels.INPUT, channels.OUTPUT),
}
CYCLE = ["research", "ads", "evaluate", "list", "go_live", "brief", "produce", "qa", "rank", "design", "paid_plan", "publish",
         "collect", "judge", "channels", "learn"]
NEVER_RETRY = ("publish", "go_live")       # an interrupted publish is checked by a person, never repeated
TERMINAL = ("done", "failed", "timed_out", "cancelled", "blocked")
NO_RETRY = (base.SchemaError, E.FabricatedEvidence, C.PermissionDenied)


class Busy(Exception):
    pass


class Director:
    def __init__(self, business, store=None, adapters=None, world=None, overrides=None):
        self.business = business
        self.store = store or Store(config.db_path(business["profile"]))
        self.bid = self.store.business_id(business)
        self.controls = C.Controls(self.store)
        self.approvals = C.Approvals(self.store)
        self.adapters = adapters or build(business, self.store, self.bid, world=world, overrides=overrides)
        self.cycle_lock = threading.Lock()
        self.publish_lock = threading.Lock()
        self.notify_hook = None          # set by the server: speaks/announces urgent notifications

    # ------------------------------------------------------------ tasks
    def submit(self, stage, inp=None, cycle_id=None, key=None):
        agent, _, _, in_schema, _ = STAGES[stage]
        inp = inp or {}
        base.validate(inp, in_schema, "input")
        key = key or f"adhoc:{stage}:{secrets.token_hex(6)}"
        existing = self.store.one("tasks", idem_key=key)
        if existing:
            return existing["id"]
        lim = self.business["limits"]
        return self.store.insert("tasks", business_id=self.bid, cycle_id=cycle_id, agent=agent, kind=stage, status="queued",
                                 idem_key=key, input_json=inp, attempts=0, max_attempts=lim["task_max_attempts"],
                                 timeout_s=lim["task_timeout_s"], created_at=self.store.clock())

    def _finish(self, task_id, token, status, output=None, error=None):
        ok = self.store.update("tasks", task_id, where={"status": "running", "lease_token": token}, status=status,
                               output_json=output, error=(str(error)[:1000] if error else None), finished_at=self.store.clock(),
                               lease_token=None, lease_until=None)
        if ok:
            self.store.audit(f"director/{self.store.get('tasks', task_id)['agent']}", "task", status, subject=f"task:{task_id}",
                             business_id=self.bid, error=str(error)[:300] if error else None)
        return ok

    def execute(self, task_id):
        """Runs one queued task to an end state (retrying up to its attempt cap). Returns the task row."""
        while True:
            task = self.store.get("tasks", task_id)
            if task["status"] in TERMINAL:
                return task
            if task["status"] != "queued":
                return task
            agent, permission, fn, in_schema, out_schema = STAGES[task["kind"]]
            token, now = secrets.token_hex(8), self.store.clock()
            if not self.store.update("tasks", task_id, where={"status": "queued"}, status="running", lease_token=token,
                                     lease_until=now + task["timeout_s"], attempts=task["attempts"] + 1, started_at=now):
                continue
            stop = self.controls.stopped()
            if stop["on"]:
                self._finish(task_id, token, "blocked", error=f"Emergency stop: {stop.get('reason') or 'on'}")
                continue
            if not self.controls.allowed(self.bid, permission):
                self._finish(task_id, token, "blocked",
                             error=f"'{task['kind']}' isn't allowed in {self.controls.mode(self.bid)} mode (needs {permission}).")
                continue
            ctx = base.Context(self.store, self.business, self.bid, self.adapters, C.AgentControls(self.controls, self.bid),
                               self._gate(f"agent/{agent}"), deadline=now + task["timeout_s"], task_id=task_id)
            box = {}

            def work():
                try:
                    box["out"] = fn(ctx, task["input"] or {})
                except BaseException as error:         # noqa: BLE001 - every failure becomes a task state
                    box["error"] = error
            th = threading.Thread(target=work, daemon=True, name=f"jm-task-{task_id}")
            th.start()
            th.join(task["timeout_s"])
            if th.is_alive():
                ctx.cancel.set()
                self._finish(task_id, token, "timed_out", error=f"no answer within {task['timeout_s']}s; result will be discarded")
                continue
            error = box.get("error")
            if error is None:
                try:
                    base.validate(box["out"], out_schema)
                    out = dict(box["out"], log=ctx.log_lines) if ctx.log_lines else box["out"]
                    self._finish(task_id, token, "done", output=out)
                except base.SchemaError as bad:
                    self._finish(task_id, token, "failed", error=f"output didn't match the schema: {bad}")
                continue
            if isinstance(error, base.TaskCancelled):
                self._finish(task_id, token, "timed_out" if "timed out" in str(error) else "cancelled", error=error)
                continue
            fresh = self.store.get("tasks", task_id)
            if isinstance(error, NO_RETRY) or fresh["attempts"] >= fresh["max_attempts"]:
                self._finish(task_id, token, "failed", error=f"{type(error).__name__}: {error}")
            else:
                self.store.update("tasks", task_id, where={"status": "running", "lease_token": token}, status="queued",
                                  error=f"attempt {fresh['attempts']} failed: {error}"[:1000], lease_token=None, lease_until=None)

    def _gate(self, actor):
        def gate(action, approval_id=None, content=None, amount=0.0):
            self.controls.require(self.bid, action, actor, approval_id=approval_id, content=content, amount=amount)
        return gate

    def run_stage(self, stage, inp=None, cycle_id=None, key=None):
        return self.execute(self.submit(stage, inp, cycle_id, key))

    # ------------------------------------------------------------ the cycle
    def run_cycle(self, stages=None, inputs=None):
        if not self.cycle_lock.acquire(blocking=False):
            raise Busy("A marketing cycle is already running.")
        try:
            return self._cycle(stages or CYCLE, inputs or {})
        finally:
            self.cycle_lock.release()

    def _cycle(self, stages, inputs):
        if self.controls.stopped()["on"]:
            raise C.PermissionDenied("Emergency stop is on.")
        lim = self.business["limits"]
        started = self.store.clock()
        cid = self.store.insert("cycles", business_id=self.bid, status="running", mode=self.controls.mode(self.bid), started_at=started)
        results, count, status = [], 0, "done"

        def run(stage, inp, round_no=0):
            nonlocal count
            count += 1
            self.store.update("cycles", cid, task_count=count)
            task = self.run_stage(stage, inp, cid, key=f"cycle:{cid}:{stage}:{round_no}")
            results.append({"stage": stage, "task_id": task["id"], "status": task["status"], "error": task["error"]})
            return task
        for stage in stages:
            if count >= lim["max_tasks_per_cycle"]:
                status = "capped"
                break
            if self.store.clock() - started > lim["cycle_timeout_s"]:
                status = "timed_out"
                break
            task = run(stage, inputs.get(stage))
            if stage == "qa" and task["status"] == "done":
                rounds = 0
                revise = task["output"]["revise"]
                while revise and rounds < self.business["testing"]["max_revisions"] and count + 2 <= lim["max_tasks_per_cycle"]:
                    rounds += 1
                    prod = run("produce", {"revise": revise, "brief_ids": []}, rounds)
                    if prod["status"] != "done":
                        break
                    new = [c["creative_id"] for c in prod["output"]["creatives"] if c["status"] in ("rendered", "render_failed")]
                    if not new:
                        break
                    again = run("qa", {"creative_ids": new}, rounds)
                    revise = again["output"]["revise"] if again["status"] == "done" else []
            if self.controls.stopped()["on"]:
                status = "stopped"
                break
        if status == "done" and any(r["status"] in ("failed", "timed_out") for r in results):
            status = "partial"
        self.store.update("cycles", cid, status=status, finished_at=self.store.clock(), task_count=count)
        self._announce_cycle(cid, status, results)
        return {"cycle_id": cid, "status": status, "tasks": results}

    def _announce_cycle(self, cid, status, results):
        blocked = [r["stage"] for r in results if r["status"] == "blocked"]
        failed = [r["stage"] for r in results if r["status"] in ("failed", "timed_out")]
        pending = len(self.approvals.pending(self.bid))
        text = f"Marketing cycle {cid} {status}." + (f" Waiting for your approval on {pending} item(s)." if pending else "") \
            + (f" Problems in: {', '.join(failed)}." if failed else "") \
            + (f" Not allowed in {self.controls.mode(self.bid)} mode: {', '.join(blocked)}." if blocked else "")
        last = self.store.find("notifications", business_id=self.bid, order="id DESC", limit=1)
        if not (last and re.sub(r"cycle \d+", "", last[0]["text"]) == re.sub(r"cycle \d+", "", text)):   # no repeats of the same news
            self.store.notify(text, "urgent" if failed else "info", self.bid)
        if self.notify_hook and (pending or failed):
            try:
                self.notify_hook(text)
            except Exception:
                pass

    # ------------------------------------------------------------ approvals -> posts
    def decide_post(self, principal, approval_id, decision, note=""):
        """Danny's answer on a post. Approve -> the post is published right away (in the caller's thread)."""
        a = self.approvals.decide(principal, approval_id, decision, note)
        post = self.store.one("posts", approval_id=approval_id)
        if post and post["status"] == "pending_approval":
            self.store.update("posts", post["id"], where={"status": "pending_approval"},
                              status="approved" if a["status"] == "approved" else "rejected")
        return a, post

    def publish_approved(self, post_ids=None):
        with self.publish_lock:            # one publish at a time (it drives Jarvis's browser)
            return self.run_stage("publish", {"post_ids": post_ids} if post_ids else {})

    def decide(self, principal, approval_id, decision, note=""):
        """Danny's answer on any approval. Returns (approval, kind, subject row)."""
        a = self.store.get("approvals", approval_id)
        if not a or a["business_id"] != self.bid:
            raise ValueError("No such approval.")
        if a["action"] == "publish_organic":
            a, post = self.decide_post(principal, approval_id, decision, note)
            return a, "post", post
        a = self.approvals.decide(principal, approval_id, decision, note)
        if a["action"] == "publish_product":
            row = self.store.one("listings", approval_id=approval_id)
            if row and row["status"] == "awaiting_approval":
                self.store.update("listings", row["id"], where={"status": "awaiting_approval"},
                                  status="approved" if a["status"] == "approved" else "draft_rejected", updated_at=self.store.clock())
            return a, "listing", row
        return a, a["subject_type"], None

    def publish_listing(self, listing_ids=None):
        with self.publish_lock:
            return self.run_stage("go_live", {"listing_ids": listing_ids} if listing_ids else {})

    def launch_paid(self, principal, experiment_id):
        """Danny pressing 'Launch' on a prepared paid test. Paid ads are off in code, so the gate refuses and
        the refusal is audited; nothing is sent to Meta."""
        if not isinstance(principal, C.HumanPrincipal):
            raise C.PermissionDenied("Only Danny can launch a paid test.")
        e = self.store.get("experiments", experiment_id)
        if not e or e["business_id"] != self.bid or e["kind"] != "paid_meta":
            raise ValueError("No such paid test.")
        if e["data"].get("problems"):
            raise C.PermissionDenied("Not ready: " + "; ".join(e["data"]["problems"]))
        self.controls.require(self.bid, "launch_paid", principal, amount=e["data"]["budget"]["money"], campaign_id=e["id"])
        self.adapters.paid.launch(e["data"])          # unreachable while paid ads are off; launch() refuses too

    # ------------------------------------------------------------ recovery
    def recover(self, process_restarted=True):
        """After a restart: interrupted tasks are retried (attempts left) or failed; interrupted posts are flagged."""
        now, notes = self.store.clock(), []
        for t in self.store.find("tasks", business_id=self.bid, status="running"):
            if not process_restarted and (t["lease_until"] or 0) > now:
                continue
            if t["attempts"] < t["max_attempts"] and t["kind"] not in NEVER_RETRY:
                self.store.update("tasks", t["id"], where={"status": "running"}, status="queued", lease_token=None, lease_until=None,
                                  error="interrupted; will retry")
                notes.append(f"task {t['id']} ({t['kind']}) re-queued")
            else:
                self.store.update("tasks", t["id"], where={"status": "running"}, status="failed", lease_token=None, lease_until=None,
                                  error="interrupted by a restart", finished_at=now)
                notes.append(f"task {t['id']} ({t['kind']}) failed: interrupted")
        for p in self.store.find("posts", business_id=self.bid, status="publishing"):
            self.store.update("posts", p["id"], where={"status": "publishing"}, status="check",
                              error="Jarvis stopped while posting. Check TikTok yourself: it will NOT be posted again automatically.")
            self.store.notify(f"Post {p['id']} was interrupted mid-upload. Please check TikTok; I won't repost it.", "urgent", self.bid)
            notes.append(f"post {p['id']} flagged for a manual check")
        for row in self.store.find("listings", business_id=self.bid, status="publishing"):
            self.store.update("listings", row["id"], where={"status": "publishing"}, status="check",
                              error="Jarvis stopped while publishing. Check the product in Shopify: it will NOT be published again automatically.")
            self.store.notify(f"Publishing '{row['data'].get('title')}' was interrupted. Please check it in Shopify; I won't retry.",
                              "urgent", self.bid)
            notes.append(f"listing {row['id']} flagged for a manual check")
        for c in self.store.find("cycles", business_id=self.bid, status="running"):
            self.store.update("cycles", c["id"], status="interrupted", finished_at=now)
        for t in self.store.find("tasks", business_id=self.bid, status="queued"):
            self.execute(t["id"])
        self.approvals.expire_old()
        if notes:
            self.store.audit("director", "recover", "ok", business_id=self.bid, notes=notes)
        return notes


class Scheduler:
    """Runs the cycle when Danny has switched the schedule on, plus read-only upkeep (collect, judge, learn)."""

    def __init__(self, director, check_every=300, upkeep_hours=6):
        self.d, self.check_every, self.upkeep = director, check_every, upkeep_hours * 3600
        self.stopping = threading.Event()
        self.last_upkeep = 0.0

    def due(self):
        sched = self.d.controls.schedule(self.d.bid)
        if not sched["enabled"]:
            return False
        last = self.d.store.sql("SELECT MAX(started_at) AS t FROM cycles WHERE business_id=?", (self.d.bid,))[0]["t"] or 0
        return self.d.store.clock() - last >= sched["every_hours"] * 3600

    def tick(self):
        if self.d.controls.stopped()["on"]:
            return "stopped"
        if self.due():
            try:
                return self.d.run_cycle()["status"]
            except Busy:
                return "busy"
        live = self.d.store.find("posts", business_id=self.d.bid, status=["published", "check"], limit=1)
        if live and self.d.store.clock() - self.last_upkeep >= self.upkeep:
            self.last_upkeep = self.d.store.clock()
            try:
                return self.d.run_cycle(["collect", "judge", "learn"])["status"]
            except Busy:
                return "busy"
        return "idle"

    def start(self):
        def loop():
            while not self.stopping.wait(self.check_every):
                try:
                    self.tick()
                except Exception as error:
                    self.d.store.notify(f"Marketing scheduler error: {str(error)[:200]}", "urgent", self.d.bid)
        threading.Thread(target=loop, daemon=True, name="jarvis-marketing-scheduler").start()
        return self

    def stop(self):
        self.stopping.set()

