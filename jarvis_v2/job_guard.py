"""
The safety guard every Jarvis job runs under: a Claude Code PreToolUse hook.

Claude Code runs this before EVERY tool call a job makes and passes the call as JSON on stdin.
Exit 0 lets it through; exit 2 blocks it and the reason (stderr) goes back to the job. Every call,
allowed or not, is appended to the job's audit log (data/job_config/<id>/audit.jsonl).

It enforces, whatever the model decides:
  - writing, editing and deleting only inside the job's own folder,
  - no reading of secrets (browser profile, tokens, store credentials, .env, ssh keys),
  - no credentials or card numbers in any tool input,
  - shell commands: no trading, no git changes, no network tools, no driving any browser except
    through the toolkit, no installs or system changes, no killing programs, no scripts outside the
    job folder, and a job script's own text is checked by the same rules before it runs.
The browser toolkit (job_browser.py) adds the page-level rules: payments, passwords, captchas,
sign-ups, and Danny's approval for anything public or permanent.

Standalone on purpose (standard library only): it must work however Claude Code starts it.
Fails closed: if anything goes wrong while checking, the call is blocked.
"""

import json
import os
import re
import sys
import time

SECRET_PATH_RE = re.compile(r"(?i)(jarvis_browser_profile|server_token|credentials\.bin|[\\/]\.jarvis_store\b|[\\/]\.ssh\b|"
                            r"(^|[\\/])\.env(\.|$|[\\/])|login data|[\\/]cookies\b|\.pem$|\.key$|id_rsa|jarvis_trading[\\/]live)")
CREDENTIAL_VALUE_RE = re.compile(r"(shpat_|shpss_|shpca_|sk-[A-Za-z0-9_-]{20,}|sk-ant-|ghp_[A-Za-z0-9]{20,}|xox[bap]-|AKIA[0-9A-Z]{16}|"
                                 r"-----BEGIN [A-Z ]*PRIVATE KEY|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.)")
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob"}
SHELL_TOOLS = {"Bash", "PowerShell"}

SHELL_RULES = [
    (r"jarvis_trading|trading_lab|freqtrade|live[ _-]?trad|arm_live|\barm\b.*\blive\b|--live\b",
     "Trading is off limits to jobs."),
    (r"\bgit\b(?!\s+(status|log|diff|show|ls-files|rev-parse|blame)\b)", "Jobs may only look at git (status, log, diff, show), never change it."),
    (r"\b(curl|wget|invoke-webrequest|invoke-restmethod|iwr|irm|httpx|aiohttp|http\.client|urllib|requests\.|socket\.|ftp)\b",
     "Network calls from the shell aren't allowed: use the browser toolkit or WebFetch."),
    (r"9333|playwright|connect_over_cdp|jarvis_browser|msedge|chrome(\.exe)?\b|firefox|start-process|\bstart\s+\S*https?:|"
     r"explorer(\.exe)?\s+\S*https?:|webbrowser|selenium|pyautogui|pywinauto|sendkeys",
     "Browsers and the screen are only driven through the job's browser toolkit."),
    (r"server_token|credentials\.bin|\.jarvis_store|jarvis_browser_profile|login data|\bcookies\b|\.ssh\b|\.env\b|x-jarvis",
     "Jobs can't read Jarvis's or Danny's secrets."),
    (r"pip3?\s+install|\bwinget\b|\bchoco\b|npm\s+(i|install)\s+-g|\bsetx\b|\breg(\.exe)?\s|\bschtasks\b|set-executionpolicy|"
     r"\bshutdown\b|stop-computer|restart-computer|\bbcdedit\b|\bnetsh\b|new-service|\bsc(\.exe)?\s+(create|config|delete)|"
     r"\btakeown\b|\bicacls\b|new-scheduledtask|register-scheduledtask|\bformat\s+[a-z]:",
     "Installing software or changing system settings isn't allowed in a job."),
    (r"\btaskkill\b|stop-process|\bkill\s+-?\d|\bpkill\b|os\.kill|terminate\(", "Jobs can't stop other programs."),
    (r"\bsubprocess\b|os\.system|os\.popen|os\.startfile|ctypes|win32api|win32com|\bexec\(|\beval\(|__import__",
     "Job scripts can't start other programs or run hidden code."),
]
WRITE_VERB_RE = re.compile(r"(?i)(\b(rm|del|erase|rmdir|rd|remove-item|move-item|mv|move|copy-item|cp|copy|xcopy|robocopy|rename-item|"
                           r"ren|set-content|add-content|out-file|new-item|mkdir|md|tee|clear-content|truncate|sed\s+-i)\b|>|"
                           r"shutil\.|os\.(remove|unlink|rmdir|rename|replace|makedirs|mkdir)|\.unlink\(|rmtree|\.write_(text|bytes)\(|"
                           r"open\([^)]*['\"](w|a|x|r\+)[b+]?['\"])")
ABS_PATH_RE = re.compile(r"(?i)([a-z]:[\\/][^\s'\"|;&<>(),]*|(?<![\w.])/[a-z]/[^\s'\"|;&<>(),]*|\\\\[^\s'\"|;&<>(),]+)")
ESCAPE_RE = re.compile(r"(\.\.[\\/]|(^|[\s'\"=(])~[\\/]?|\$env:|%\w+%|\$home\b|\$\{?(userprofile|home|appdata|localappdata|temp|tmp)\b|"
                       r"expanduser|getenv|environ|pathlib\.Path\.home)", re.I)
HARMLESS_TARGETS = {"/dev/null", "nul", "$null"}
RUN_SCRIPT_RE = re.compile(r"(?i)\b(py(thon)?3?(\.exe)?|node)\b((?:\s+-[-\w]+(?:\s+\d+)?)*)\s+(-m\s+\S+|-c\b|['\"]?[^\s'\"]+\.(py|js|mjs)['\"]?)")


def _inside(path, folder):
    if not folder:
        return False
    p = os.path.normcase(os.path.realpath(path))
    f = os.path.normcase(os.path.realpath(folder))
    return p == f or p.startswith(f + os.sep)


def _posix_to_win(path):
    m = re.match(r"^/([a-z])/(.*)$", path, re.I)
    return f"{m.group(1)}:/{m.group(2)}" if m else path


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _looks_like_card(text):
    for m in re.finditer(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)", text):
        digits = [int(c) for c in re.sub(r"\D", "", m.group(0))]
        total = 0
        for i, d in enumerate(reversed(digits)):
            if i % 2:
                d *= 2
                d = d - 9 if d > 9 else d
            total += d
        if total % 10 == 0 and len(set(digits)) > 1:
            return True
    return False


def check_text(text, job_dir, script=False):
    """Rules for a shell command or a job script's text. Returns a block reason or None."""
    for pattern, reason in SHELL_RULES:
        if re.search(pattern, text, re.I):
            if not script and pattern.startswith(r"\bsubprocess"):
                continue                  # the shell itself starts programs; this rule is for scripts' text
            return reason
    if WRITE_VERB_RE.search(text):
        if ESCAPE_RE.search(text):
            return "Writing, moving or deleting is only allowed inside the job's own folder (no '..', '~' or variables in those paths)."
        for raw in ABS_PATH_RE.findall(text):
            target = _posix_to_win(raw.rstrip(".,:"))
            if target.lower() in HARMLESS_TARGETS:
                continue
            if not _inside(target, job_dir):
                return f"Writing, moving or deleting is only allowed inside the job's own folder (not {raw[:80]})."
    return None


def check_shell(command, job_dir):
    reason = check_text(command, job_dir)
    if reason:
        return reason
    for m in RUN_SCRIPT_RE.finditer(command):
        target = m.group(5).strip("'\"")
        if target.startswith("-m"):
            return "Jobs can't run Python modules; put any helper script in the job's folder."
        if target == "-c":                # inline code: held to the same rules as a script's text
            reason = check_text(command, job_dir, script=True)
            if reason:
                return reason
            continue
        full = target if os.path.isabs(_posix_to_win(target)) else os.path.join(job_dir or "", target)
        full = _posix_to_win(full)
        if not _inside(full, job_dir):
            return "Jobs can only run scripts they wrote in their own folder."
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                body = f.read(400_000)
        except OSError:
            return None                   # it doesn't exist yet: the run will simply fail
        reason = check_text(body, job_dir, script=True)
        if reason:
            return "That script breaks a job rule: " + reason
    return None


def check(tool, tool_input, job_dir):
    """Returns None to allow the call, or the reason it's blocked."""
    tool_input = tool_input or {}
    for s in _strings(tool_input):
        if CREDENTIAL_VALUE_RE.search(s):
            return "That contains what looks like a password, key or token. Jobs never handle credentials."
        if tool.startswith("mcp__jobkit__browser") and _looks_like_card(s):
            return "That looks like a card number. Jobs never enter payment details."
    if tool in WRITE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if not path or not _inside(path, job_dir):
            return "Jobs can only create or change files inside their own folder."
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(job_dir)).replace("\\", "/").lower()
        if rel.startswith(".claude") or os.path.basename(rel) in ("claude.md", ".mcp.json", "claude.local.md"):
            return "Jobs can't change Claude Code's own settings."
        return None
    if tool in READ_TOOLS:
        for key in ("file_path", "path", "pattern", "glob"):
            if SECRET_PATH_RE.search(str(tool_input.get(key) or "")):
                return "Jobs can't read Jarvis's or Danny's secrets (logins, tokens, keys)."
        return None
    if tool in SHELL_TOOLS:
        return check_shell(str(tool_input.get("command") or ""), job_dir)
    if tool == "mcp__jobkit__browser_open":
        if not re.match(r"(?i)^https?://", str(tool_input.get("url") or "")):
            return "The browser only opens http(s) web addresses."
    return None


def audit(entry, folder=None):
    folder = folder or os.environ.get("JARVIS_JOB_CONFIG")
    if not folder:
        return
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "audit.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _clip(value, limit=1500):
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= limit else text[:limit] + "…"


def main(stdin=None, stderr=None):
    stdin, stderr = stdin or sys.stdin, stderr or sys.stderr
    job_dir = os.environ.get("JARVIS_JOB_DIR", "")
    tool, tool_input = "?", {}
    try:
        event = json.loads(stdin.read() or "{}")
        tool, tool_input = str(event.get("tool_name") or "?"), event.get("tool_input") or {}
        reason = check(tool, tool_input, job_dir) if job_dir else "The job's folder is unknown, so nothing is allowed."
    except Exception as error:                   # fail closed
        reason = f"The safety check couldn't read this call ({error}), so it was blocked."
    try:
        audit({"ts": time.time(), "tool": tool, "input": _clip(tool_input), "allowed": reason is None, "reason": reason})
    except Exception:
        pass
    if reason:
        stderr.write("BLOCKED by Jarvis's safety rules: " + reason)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
