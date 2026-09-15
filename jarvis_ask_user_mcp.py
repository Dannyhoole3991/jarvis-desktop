"""
Standalone MCP server exposing exactly one tool, ask_user, that lets a
Claude-powered self-repair / execution agent ask Danny a real question
through Jarvis's own voice/text interface and get his actual answer
back -- for a genuine decision the agent can't reasonably make on its
own (a real choice between options, or explicit go-ahead on something
significant), not for routine steps along the way.

Deliberately NOT importing Jarvis_FINAL_WORKING.py directly -- that file
has no __main__ guard, so importing it boots the entire live assistant
(microphone, text-to-speech, network server) a second time. Instead
this talks to the ALREADY-RUNNING Jarvis process purely through two
small JSON files on disk, which the main process watches for (see
_agent_question_watcher_loop in Jarvis_FINAL_WORKING.py).
"""
import json
import os
import time
import uuid

from mcp.server.mcpserver import MCPServer

JARVIS_DIR = os.path.dirname(os.path.abspath(__file__))
QUESTION_FILE = os.path.join(JARVIS_DIR, "_agent_question.json")


def _answer_file_path(question_id):
    return os.path.join(JARVIS_DIR, f"_agent_answer_{question_id}.json")


server = MCPServer("jarvis")


@server.tool()
def ask_user(question: str, options: list[str] | None = None) -> str:
    """
    Ask Danny a real question out loud through Jarvis and wait for his
    actual spoken or typed answer. Use this ONLY for a genuine decision
    you can't reasonably make yourself -- a real choice between
    options, or explicit go-ahead on something significant -- not for
    routine steps you can just decide and continue with. Returns his
    answer as plain text, or a note that he didn't respond in time (in
    which case, use your own best judgement and continue).
    """
    question_id = uuid.uuid4().hex
    answer_file = _answer_file_path(question_id)
    payload = {"id": question_id, "question": question, "options": options or []}

    with open(QUESTION_FILE, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)

    deadline = time.time() + 180
    while time.time() < deadline:
        if os.path.exists(answer_file):
            try:
                with open(answer_file, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                os.remove(answer_file)
                return str(data.get("answer", "")).strip() or "(empty response)"
            except Exception:
                pass
        time.sleep(0.5)

    return "(Danny didn't respond in time -- proceed with your own best judgement.)"


if __name__ == "__main__":
    server.run()
