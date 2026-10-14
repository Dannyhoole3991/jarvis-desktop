"""
Jarvis v2: Claude Code as the brain, with automatic fallback brains.

    brain order:  Claude Code (subscription)  ->  OpenAI  ->  local model (Ollama)

Claude is always preferred. When its usage window is exhausted, Jarvis moves
down the list and comes back to Claude as soon as the window renews. Plan:
docs/JARVIS_V2_PLAN.md. This package runs alongside the existing engine
(Jarvis_FINAL_WORKING.py) and does not modify it.
"""
