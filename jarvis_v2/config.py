"""Paths and settings for Jarvis v2. Override any setting with the matching environment variable."""

import os

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(PACKAGE_DIR)

# The folder Claude Code runs in: holds CLAUDE.md (Jarvis's personality and rules) and the
# project settings (permissions). Kept apart from the repo root so the brain loads only these.
HOME_DIR = os.path.join(PACKAGE_DIR, "home")
DATA_DIR = os.environ.get("JARVIS_V2_DATA", os.path.join(PACKAGE_DIR, "data"))
MEMORY_DIR = os.path.join(DATA_DIR, "memory")
BRAIN_STATE_FILE = os.path.join(DATA_DIR, "brain_state.json")
USAGE_FILE = os.path.join(DATA_DIR, "usage.json")
LOG_FILE = os.path.join(DATA_DIR, "brain.log")
MCP_CONFIG_FILE = os.path.join(DATA_DIR, "mcp.json")
LATER_JOBS_FILE = os.path.join(REPO_DIR, "LATER_JOBS.md")

def find_claude_exe():
    """The native claude.exe the npm package installs (started directly, avoiding cmd.exe quoting)."""
    override = os.environ.get("JARVIS_CLAUDE_EXE")
    if override:
        return override
    import shutil
    for root in filter(None, [os.path.dirname(shutil.which("claude.cmd") or ""),
                              os.path.join(os.environ.get("APPDATA", ""), "npm")]):
        exe = os.path.join(root, "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
        if os.path.exists(exe):
            return exe
    return shutil.which("claude") or shutil.which("claude.cmd") or "claude"


CLAUDE_CONTEXT_FILE = os.path.join(DATA_DIR, "claude_context.md")
CLAUDE_MODEL = os.environ.get("JARVIS_CLAUDE_MODEL", "sonnet")
QUICK_MODEL = os.environ.get("JARVIS_QUICK_MODEL", "haiku")      # the quick voice for small talk
CLAUDE_TURN_TIMEOUT = float(os.environ.get("JARVIS_CLAUDE_TURN_TIMEOUT", "180"))
# A rate-limit block without a usable reset time is retried after this long (never "forever").
CLAUDE_DEFAULT_BLOCK_SECONDS = 300
# After an auth/startup failure (not a usage limit), how long before Claude is tried again.
CLAUDE_FAILURE_RETRY_SECONDS = 300

OPENAI_MODEL = os.environ.get("JARVIS_OPENAI_MODEL", "gpt-5-mini")
OPENAI_RETRY_SECONDS = 1800          # after "no credit", try OpenAI again this much later

OLLAMA_HOST = os.environ.get("JARVIS_OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("JARVIS_OLLAMA_MODEL", "gpt-oss:20b")

# The existing Jarvis engine, used for PC control (open Steam, apps, games, volume ...).
# 127.0.0.1, not "localhost": on Windows "localhost" tries IPv6 first and the engine only
# listens on IPv4, which cost ~1.5 s on EVERY call (measured).
ENGINE_URL = os.environ.get("JARVIS_ENGINE_URL", "http://127.0.0.1:8765")

MAX_TOOL_ROUNDS = 8                   # fallback brains: tool calls per turn before giving up
HANDOVER_TURNS = 10                   # recent exchanges passed to whichever brain takes over


def ensure_dirs():
    for d in (DATA_DIR, MEMORY_DIR, HOME_DIR):
        os.makedirs(d, exist_ok=True)
