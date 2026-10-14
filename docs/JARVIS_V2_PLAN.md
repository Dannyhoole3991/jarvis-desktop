# Jarvis v2 plan: Claude Code as the brain

**Status:** PLAN ONLY. Nothing is built yet. Waiting for Danny's approval.
**Reference design:** https://github.com/ethanplusai/jarvis (commit 16e37bd, 7 Sep 2026).

## 1. The goal

Jarvis's brain becomes a **Claude Code agent**, running on Danny's Claude subscription. He can then:
- think
- build things
- fix and extend his own code
- run the businesses

The existing systems become **tools he uses**: the store, money check, Fiverr/Redbubble/AdSense checks, the trading lab and browser control. Danny talks to Jarvis instead of coming back to a separate Claude Code session for every job.

**What Danny asked for:**
- **Everything** in ethanplusai's app: tools, dashboard, usage bars and supporting systems.
- **Our original orb is kept.** It replaces his.
- **Our business features are added** on top.
- **Fallback brains:** if Claude's usage runs out, Jarvis switches to the local model, then OpenAI. He **always switches back to Claude** when the usage renews.
- **The phone app is parked**, not deleted, until the desktop UI is right.

## 2. Licence: one thing to settle first

The repo is free to download, but its licence is **not** "do anything you like". It allows **personal, non-commercial use only**. It explicitly bans using it, or anything built from it, "to generate revenue, directly or indirectly" without a paid commercial licence from ethanplus.ai. Jarvis runs Danny's businesses, so that clause applies.

**How we avoid any problem:** we **build every feature ourselves**, using his app as the blueprint. Same features, same layout and behaviour, but our own code. Features and ideas aren't protected; his actual source files are. The result is identical for Danny, and Jarvis stays fully his to use commercially.

Alternative: Danny buys a commercial licence (ethanplus.ai) and we copy code directly. That's quicker in places, but it costs money and still needs a Mac-to-Windows port.

## 3. Architecture

```
 Desktop window (pywebview, Windows)
 ├── OUR orb (kept)                 ├── Dashboard: his 6 tabs + our business tabs
 └── "Needs You" panel (everything only Danny can do)
                │  WebSocket + HTTP (localhost, authenticated)
                ▼
 Jarvis server (new, Python/FastAPI)   ← keeps wake word, voice in/out, HUD links
                │
        BRAIN ROUTER  ── primary:   Claude Code (one long-running `claude -p` session, subscription)
                     ── fallback 1: OpenAI (has credit as of 2026-10-14; keeps PC control + business tools)
                     ── fallback 2: local model (Ollama gpt-oss:20b, free) when OpenAI fails or has no credit
                │  tools (MCP server)
                ▼
 Tools: his 38 equivalents + ours (store, money, business, trading-readonly, browser, later-jobs)
```

### Brain router: switching out and back

- **Detecting the limit:** Claude Code reports a `rate_limit_event`, including the reset time (`resetsAt`), and a turn can end `rate_limited`. His brain already captures this; ours will too.
- **On the limit:**
  - Jarvis says it once ("My Claude allowance is used up until 18:00, sir. I'll use my local brain until then.")
  - The HUD shows which brain is active, with a badge.
  - The router switches to fallback 1. If that's unavailable or fails, it switches to fallback 2.
- **Switching back:** at `resetsAt`, the router checks Claude with a tiny test call. If it works, it switches back and announces it. If there's no reset time, it re-checks every 5 minutes.
- **Keeping the conversation going:** the last few exchanges plus the memory index are passed to whichever brain takes over. The handover note (`write_journal`) is shared, so nothing is lost.
- **What fallback brains can do:** all the normal tools: chat, money check, store status, browser, reminders. They **can't** do coding, builds or self-editing, which need Claude Code. Those requests are queued: "I'll do that when Claude's back."
- **Existing PC control is unaffected:** Steam, apps, games and the other command handlers run without any brain, so they work whichever brain is active.
- **Self-repair:**
  - Only while Claude is the brain.
  - He fixes a worktree copy and runs the tests, then switches over only after Danny approves. The old version is kept for an instant rollback.
- **Crash repair:** a separate watchdog. If Jarvis crashes, it starts a headless Claude Code repair session with the crash log, prepares a fix and asks Danny before restarting.
- **Manual override:** "Jarvis, use your local brain" or "Jarvis, use Claude".
- **Billing safety:** Claude always runs on the subscription. Every `ANTHROPIC_*` variable is stripped from its environment, as in his `claude_env.py`, so it can never silently bill an API key.

## 4. Feature checklist: everything his app has

**Port** = he relies on Mac-only AppleScript or Unix sockets, so we need a Windows equivalent.

### His 38 brain tools

| Group | Tools | Windows | Notes |
|---|---|---|---|
| Watch Claude Code sessions | list_sessions, session_detail, enable_session_inbox | Port | Reads Claude Code's own session files; works on Windows |
| Steer sessions | steer_session, answer_dialog | **Port: hardest part** | He uses Unix sockets and Terminal.app keystrokes. Needs a Windows method; may be limited at first |
| Runs (one-off jobs) | spawn_run, run_status, cancel_run | Same | Recorded in SQLite with full event history |
| Builds | start_build, build_status, review_document, approve_document | Same | Spec → plan → test-driven build, progress read from plan checkboxes |
| Projects | create_project, list_projects, open_in_editor, open_in_terminal, run_command | Port | Windows Terminal / VS Code |
| Reading code | repo_overview, search_repo, read_file | Same | |
| Web | open_in_browser, read_page, look_at_page | Same | Playwright; we already have jarvis_browser |
| Screen | what_is_on_screen, look_at_screen | Port | Windows window list + screenshot |
| GitHub | github_repo | Same | `gh` CLI |
| Usage | usage_status | Same | 5-hour and 7-day bars |
| Connections | connections | Same | |
| Memory | remember, recall, project_note, write_journal | Same | Markdown memory, one fact per file |

### His dashboard (6 tabs) and supporting systems

| Feature | Plan |
|---|---|
| Runs tab (Needs Attention / Active / History, live transcript, cost, retry) | Build |
| Sessions tab (all Claude Code sessions, "waiting on you for 51m", the exact question asked) | Build |
| Memory tab | Build |
| Specs tab (read and approve by section) | Build |
| Projects tab | Build |
| **Usage tab: 5-hour and 7-day subscription bars, per-session token use** | Build. **Also shows which brain is active and fallback status** |
| Spoken interruptions (urgent now, routine batched) | Build. Windows notifications when the window is hidden |
| Speech scheduler (sentence streaming, barge-in, queue) | Build on top of our existing voice |
| Preflight checks (claude installed/logged in, etc.) | Build, for Windows |
| Web security (auth tokens, origin checks, no clickjacking, prompt-injection "taint" rules) | Build. **Important**, because the brain can run commands |
| Settings page | Build |

### Kept from OUR Jarvis

- **Our orb**, upgraded to react to Jarvis's real voice and states, the way his does.
- Wake word, our speech-to-text and text-to-speech. His needs Chrome plus a paid Fish Audio key. Fish can be added later as an option.

### Our business features, added as tools and tabs

- **Money tab:** the `check money` results for Shopify, Fiverr, Redbubble and AdSense.
- **Store tab:** live products, Jarvis drafts waiting for review, and adding a draft (still draft-only).
- **Trading tab:** read-only lab status and forward trials. **No live-trading tools are exposed to the brain.**
- **Business tab:** Fiverr messages with scam flags, Redbubble, blog.
- **NEEDS YOU panel, always visible:** every job only Danny can do, with exact steps. It's fed from LATER_JOBS.md and the business human-action queue.

### Parked

- Phone app / mirror links (port 8765 phone handler): left in place, not shown in the new UI, and revisited after the desktop is right.

## 5. Safety rules (enforced in code and in Claude Code's permission settings)

1. **No real-money trading tools** for the brain. Edits to `jarvis_trading/live/` are denied by permission rules.
2. **Store:** draft-only writes through `jarvis_store` (its guards stay). Edits to `jarvis_store/shopify.py` guards and `creds.py` are denied.
3. **No passwords, captchas or payments.** Those go to the NEEDS YOU panel.
4. **Self-editing:**
   - Jarvis edits his own code in a **separate git worktree**, runs the tests, and **asks Danny before switching over and restarting**.
   - No pushing without approval.
   - The previous version is always kept for an instant rollback.
5. **The 5 forward trials and the trading lab are untouched.**
6. Untrusted web and email text can't trigger actions (his taint system).

## 6. Phases (each ends with a demo and Danny's OK)

| # | Phase | Result |
|---|---|---|
| 0 | **Danny:** install the Claude Code CLI and log in. **Me:** fix the trading-lab leakage-detector bug separately, only if Danny asks | Prerequisites done |
| 1 | Brain + router + fallback + memory + our business tools, in a test copy of Jarvis | Talk to Jarvis; he answers with Claude, switches to local when forced, and switches back |
| 2 | New desktop window: our orb + Usage tab + NEEDS YOU panel + Money/Store/Trading/Business tabs | The new UI you can actually use |
| 3 | Runs, builds, specs, projects, memory tabs + their tools | Jarvis can build things and improve himself, with your approval to restart |
| 4 | Session watching and steering (Windows port), screen tools, notifications | Feature parity with his app |
| 5 | Switch the real Jarvis over (the old engine kept as a backup) + phone app revisited | Done |

**Rough effort:** phases 1–2 are about 1–2 weeks of work. Phases 3–4 are another 2–3 weeks, because his app is large (about 20,000 lines). Each phase is usable on its own.

## 7. Open questions for Danny

1. Licence route: **build our own equivalent** (recommended), or buy his commercial licence?
2. ~~Fallback order~~ **Decided 2026-10-14:** Claude → OpenAI → local, always returning to Claude.
3. Any of his features you **don't** want?
