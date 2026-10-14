"""
Talk to Jarvis v2 in a terminal (Phase 1 test bench; the voice/HUD come in Phase 2).

    py -3 -m jarvis_v2.chat                 automatic: Claude -> OpenAI -> local
    py -3 -m jarvis_v2.chat --use local     start fixed to one brain

While chatting:
    /brain              which brain is active, Claude's usage windows
    /use claude|openai|local|auto
    /quit
"""

import sys

from . import router as R


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    router = R.default_router()
    if "--use" in argv:
        choice = argv[argv.index("--use") + 1]
        print(router.force(None if choice == "auto" else choice))
    print("Jarvis v2 test console. /brain, /use claude|openai|local|auto, /quit\n")
    try:
        while True:
            try:
                text = input("You: ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text in ("/quit", "/exit"):
                break
            if text == "/brain":
                print("  " + R.status_text() + "\n")
                continue
            if text.startswith("/use "):
                choice = text.split(None, 1)[1].strip()
                try:
                    print("  " + router.force(None if choice == "auto" else choice) + "\n")
                except ValueError as error:
                    print(f"  {error}\n")
                continue
            reply = router.ask(text)
            tools = f"  [tools: {', '.join(reply.tools)}]" if reply.tools else ""
            print(f"Jarvis ({R.LABELS.get(reply.brain, reply.brain)}): {reply.spoken()}{tools}\n")
    finally:
        claude = router.brains.get("claude")
        if claude is not None:
            claude.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
