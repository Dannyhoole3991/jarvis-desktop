"""
Telling Danny when something needs him, without nagging.

  URGENT  (a session needs him, a run failed, a change is ready to review): spoken at once.
  ROUTINE (a session or run finished): saved up and spoken as ONE sentence at the next quiet
          moment (Jarvis not talking or thinking, and nothing announced for a little while).
  If the Jarvis window is minimised/hidden, a Windows notification is shown as well.

Speech goes through the voice engine's /speak endpoint. Notification text comes from other
programs (session names, run results), so it is passed to PowerShell through environment
variables and XML-escaped there -- never pasted into a command line.

The Watcher polls every few seconds and turns STATE CHANGES into announcements, once each:
sessions (needs_you / finished), and Jarvis's own proposals (ready for review / failed checks).
Runs announce themselves through the executor's listener.
"""

import json
import os
import subprocess
import threading
import time
import urllib.request

from . import config, sessions as S

QUIET_SECONDS = 20          # routine news waits until nothing has been said for this long
TOAST_PS = r"""
$t = $env:JV_TITLE; $b = $env:JV_BODY
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml("<toast><visual><binding template='ToastGeneric'><text>" + [Security.SecurityElement]::Escape($t) + "</text><text>" + [Security.SecurityElement]::Escape($b) + "</text></binding></visual></toast>")
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def windows_notification(title, body):
    env = dict(os.environ, JV_TITLE=str(title)[:120], JV_BODY=str(body)[:400])
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", TOAST_PS], env=env,
                         creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _engine_post(path, payload, timeout=5):
    secret = os.environ.get("JARVIS_PHONE_SECRET", "")
    req = urllib.request.Request(config.ENGINE_URL + path, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    if secret:
        req.add_header("Authorization", "Bearer " + secret)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _engine_busy():
    try:
        with urllib.request.urlopen(config.ENGINE_URL + "/status", timeout=1.5) as r:
            s = json.loads(r.read().decode("utf-8"))
        return bool(s.get("speaking") or s.get("processing"))
    except Exception:
        return False


class Announcer:
    def __init__(self, speak=None, notify=windows_notification, busy=_engine_busy, clock=time.time):
        self.speak = speak or (lambda text: _engine_post("/speak", {"text": text}))
        self.notify, self.busy, self.clock = notify, busy, clock
        self.pending = []                     # routine sentences waiting for a quiet moment
        self.last_spoken = 0.0
        self.window_visible = True
        self.thinking = lambda: False         # set by the server: is the brain mid-answer?
        self.history = []                     # [{"ts", "text", "urgent"}] for the window
        self.lock = threading.Lock()

    def _say(self, text, urgent):
        spoken = False
        try:
            self.speak(text)
            spoken = True
        except Exception:
            pass
        if urgent and (not self.window_visible or not spoken):
            self.notify("Jarvis", text)
        self.last_spoken = self.clock()
        self.history = (self.history + [{"ts": self.clock(), "text": text, "urgent": urgent}])[-50:]

    def urgent(self, text):
        with self.lock:
            self._say(text, urgent=True)

    def routine(self, text):
        with self.lock:
            if text not in self.pending:
                self.pending.append(text)

    def flush(self, force=False):
        """Speaks the saved-up routine news as one sentence, if it's a quiet moment."""
        with self.lock:
            if not self.pending:
                return None
            quiet = self.clock() - self.last_spoken >= QUIET_SECONDS and not self.busy() and not self.thinking()
            if not (quiet or force):
                return None
            items, self.pending = self.pending, []
            text = items[0] if len(items) == 1 else "While you were busy: " + "; ".join(i.rstrip(".") for i in items) + "."
            self._say(text, urgent=False)
            if not self.window_visible:
                self.notify("Jarvis", text)
            return text


class Watcher:
    """Turns changes in sessions and proposals into announcements, each change once."""

    def __init__(self, announcer, snapshot=S.snapshot, proposals=None, interval=5):
        self.announcer, self.snapshot, self.interval = announcer, snapshot, interval
        self.proposals = proposals
        self.states = None                    # session_id -> state (None until the first look)
        self.proposal_states = None
        self._stop = False
        self.last = []

    def tick(self):
        try:
            current = self.snapshot()
        except Exception:
            current = []
        self.last = current
        now = {s["session_id"]: s for s in current}
        if self.states is not None:           # the first look only learns; it never announces old news
            for sid, s in now.items():
                before = self.states.get(sid)
                if s["state"] == S.NEEDS_YOU and before != S.NEEDS_YOU:
                    hand = " Only you can answer that one." if s["human_hand"] else ""
                    self.announcer.urgent(f"Your session '{s['name']}' in {s['project']} needs you: {s['reason']}.{hand}")
                elif s["state"] == S.IDLE and before == S.WORKING:
                    self.announcer.routine(f"'{s['name']}' in {s['project']} has finished")
        self.states = {sid: s["state"] for sid, s in now.items()}

        if self.proposals is not None:
            try:
                props = {p["id"]: p for p in self.proposals()}
            except Exception:
                props = {}
            if self.proposal_states is not None:
                for pid, p in props.items():
                    before = self.proposal_states.get(pid)
                    if before == p["status"]:
                        continue
                    if p["status"] == "ready_for_review":
                        self.announcer.urgent(f"My change '{p['goal'][:60]}' is ready for you to review in the Changes tab, sir.")
                    elif p["status"] in ("tests_failed", "blocked", "run_failed"):
                        self.announcer.urgent(f"My change '{p['goal'][:60]}' didn't pass my checks, so I haven't offered it. Details are in the Changes tab.")
            self.proposal_states = {pid: p["status"] for pid, p in props.items()}
        self.announcer.flush()

    def start(self):
        def loop():
            while not self._stop:
                self.tick()
                time.sleep(self.interval)
        threading.Thread(target=loop, daemon=True, name="jarvis-watcher").start()
        return self

    def stop(self):
        self._stop = True


def on_run_change(announcer):
    """Executor listener: runs announce their own ending."""
    def listener(run_id, run):
        if not run or run["status"] not in ("succeeded", "failed", "timed_out"):
            return
        if run.get("kind") == "self_improve":
            return                            # the proposal check announces the outcome instead
        what = (run.get("meta") or {}).get("title") or run["prompt"][:60].replace("\n", " ")
        if run["status"] == "succeeded":
            announcer.routine(f"the job in {run['project']} ('{what}') is done")
        else:
            announcer.urgent(f"The job in {run['project']} ('{what}') stopped with a problem. It's in the Runs tab, sir.")
    return listener
