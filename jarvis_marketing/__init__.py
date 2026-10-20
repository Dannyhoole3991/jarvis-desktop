"""
Jarvis's marketing department (plan: docs/MARKETING_DEPT_PLAN.md).

A Marketing Director runs typed, capped tasks for seven agents (research, product evaluation,
creative strategy, video production, creative QA, testing, learning) over one SQLite store per
profile ("mock" or "live"). Every external action goes through controls.py, which is application
code, not a prompt: paid ads, purchases and price changes are hard-coded off in this milestone,
and an organic post needs Danny's approval of that exact post.
"""
