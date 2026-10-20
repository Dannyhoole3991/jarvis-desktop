# Jarvis Marketing Department: audit and plan

**Status:** milestone 1 is being built on branch `claude/sleepy-hypatia-y3xua6`. Mock mode works end to end. Live adapters exist but need Danny's configuration (section 9).
**First business:** Collective Finds (Shopify `mwrfta-mh.myshopify.com`, public site `collectivefinds.co.uk`).
**Milestone 1 decision (Danny):**
- Research and review run freely.
- **Organic** posting is allowed, but only after Danny approves **each individual post** in the dashboard.
- **Paid** campaigns, ad spend, purchases and price changes are **disabled in code** until Danny sets limits.

---

## 1. Audit: what exists today (read on 2026-10-20, master `ce08287`)

### Architecture
| Part | Where | State |
|---|---|---|
| Voice engine (wake word, TTS, PC control) | `Jarvis_FINAL_WORKING.py`, port 8765 | Works. Not touched. |
| v2 server + window (brain router, tabs, Needs-you panel) | `jarvis_v2/server.py` (stdlib `http.server`, port 8780, 127.0.0.1 only, per-launch token + Host check) and `jarvis_v2/ui/index.html` (one file, 14 tabs) | Works. **Reuse:** the Marketing tab and API are mounted here. |
| Brains | `jarvis_v2/brains/` (Claude Code via `claude -p` on the subscription; OpenAI; Ollama local) | Works. OpenAI credit ran out on 19 Oct 2026, so it is **not** assumed. |
| Runs / jobs | `jarvis_v2/runs.py` (SQLite RunStore + Executor), `jobs.py` (ApprovalStore, job guard, audit) | Works. These run Claude Code agents with free-form goals. The marketing department does **not** reuse them for its loop: it needs deterministic, typed tasks with hard caps (section 4). It reuses the *pattern* (SQLite, approval store, audit). |
| Alerts | `jarvis_v2/alerts.py` Announcer (spoken + Windows toast) | Works. **Reuse:** marketing notifications go through it when the window runs. |
| Phone sync | `jarvis_v2/phone_sync.py` pushes a read-only snapshot to the phone backend | Works. A marketing summary can be added later. Not needed for the laptop. |
| Business tab | `jarvis_business.py` (SQLite `jarvis_business.db`: opportunities, experiments, budget, lessons) + `server.business_view` | Works for the older "business brain" (blog/Redbubble/etc). **Kept as is.** The marketing department keeps its own DB because it needs evidence provenance, approvals and per-record labels that this schema doesn't have. |
| Store | `jarvis_store/` Shopify client **guarded to draft-only writes**, CJ read-only client (1 req/s), pricing, honest copywriter with `check_claims`, DPAPI-encrypted creds outside the repo | Works. **Reuse:** Shopify reads, CJ search/details/UK shipping, `check_claims`, pricing constants. The guards are not touched. |
| Shopify orders | `jarvis_money.shopify_orders()` reads the admin orders page in Jarvis's browser (no API scope needed) | Works (browser read). **Reuse** as the "browser" order source. An API order source is added too, but it needs the `read_orders` scope (section 9). |
| TikTok posting | `jarvis_v2/posting.py`: queue + browser upload, caption verified word for word, AI label, only when Danny presses Post | Works. **Reuse:** the live organic publisher is a thin adapter over it. |
| TikTok analytics | `jarvis_v2/advertising.py` reads TikTok Studio pages (views, likes, comments, followers per video) | Works (browser read). **Reuse** as the live analytics adapter. It has **no clicks, sessions or retention**, so those stay "not available", never guessed. |
| Video maker | `jarvis_video.py` (Pillow + edge-tts + ffmpeg, 1080x1920) | Works on Danny's PC. **Reuse** as the "local slideshow" video provider. Its deps (edge_tts, imageio_ffmpeg) aren't in the cloud test box, so tests use the mock provider. |
| Trading | `jarvis_trading/`, `trading_lab/` | Paper-only. **Not touched.** |

### Storage
- SQLite everywhere (runs/jobs DB, business DB, trading registry), and JSON files for queues and caches.
- `.gitignore` is an allowlist. New source must be un-ignored, and data folders re-ignored.

### Credentials (checked by name only, values never read)
| Key | Where | Present in repo? |
|---|---|---|
| Shopify client id/secret or `shpat_` token, CJ API key | DPAPI file `~/.jarvis_store/credentials.bin` (Danny's PC) | No (correct) |
| `JARVIS_PHONE_SECRET`, `JARVIS_PHONE_BACKEND_URL` | env on Danny's PC | No |
| `JARVIS_OPENAI_API_KEY`, `JARVIS_ANTHROPIC_API_KEY`, `JARVIS_ELEVENLABS_*`, `JARVIS_CF_*` | env on Danny's PC | No |
| TikTok | Jarvis's own logged-in browser profile (no API keys) | No |

Nothing secret is in git. The marketing department adds one new secret, **the laptop dashboard password**. Only a salted PBKDF2 hash of it is stored, in the data folder (git-ignored). It is set by Danny in his own terminal.

### Tests at the start (baseline)
- `jarvis_v2/tests`: 253 tests run. 2 failures and 1 error are Windows-only (named pipes, window listing, read-only job script), and they fail the same way on master in this Linux box.
- `jarvis_trading/tests`: 378 tests run, 3 failures (read-only bundle / timestamp checks under root on Linux).
- `jarvis_store/test_store.py`: 11 OK.

These are pre-existing and unrelated. The rule is that the marketing work must not add any new failures to these suites.

### What's missing for a real marketing operation
- No evidence store with provenance (source URL, date, confidence, fact vs estimate).
- No transparent product-economics scoring (break-even CAC/ROAS).
- No creative briefs with a single test variable, and no independent QA.
- No experiment design, stopping rules or statistical evaluation, and no attribution windows.
- No mode/permission state machine, spend caps or emergency stop for marketing.
- No duplicate-post protection across restarts tied to content hashes.
- No remote (laptop) access: the v2 server deliberately refuses anything but 127.0.0.1.

## 2. Real platform capabilities (checked 2026-10-20; never assume a feature exists)
| Need | Reality | Consequence |
|---|---|---|
| TikTok publishing via API | Content Posting API: **unaudited apps can only post privately (SELF_ONLY)**. Public posting needs a TikTok app audit (weeks). | Live organic posting reuses the **existing browser poster** (`posting.py`), always behind Danny's per-post approval. An API publisher stub documents the audit requirement. |
| TikTok analytics via API | Display API `video.list` returns the user's own videos with view/like/comment/share counts (OAuth, needs app approval). No clicks/sessions. | Live analytics = existing TikTok Studio browser read. Clicks, sessions and purchases come from **Shopify**, not TikTok. |
| TikTok Research API (trends, public videos) | **Non-profit academic researchers only.** Not usable commercially. | No public-video trend adapter. Research uses supplier data, competitor stores and our own results. The trend adapter is MOCK only, labelled. |
| Google Trends | Official API is an **invite-only alpha**. No self-serve access. | Search-demand adapter is MOCK only until access is granted. Unofficial scrapers are not used. |
| Shopify orders | Admin API needs `read_orders` (last 60 days; older needs `read_all_orders`). `customerJourneySummary` gives the first-visit source/UTM. | API order source added. It needs the scope on Danny's app. The browser order read keeps working meanwhile. |
| Competitor prices | Many Shopify stores expose public `/products.json`. | A real competitor adapter reads it, for URLs Danny lists in config only. |
| CJ supplier | Official API, 1 req/s, product details + UK freight quotes. | Reused through `jarvis_store.cj`. |
| Paid ads (TikTok/Meta) | Not built. | Adapter interface exists and **always refuses** in this milestone. |

Sources: [TikTok Direct Post reference](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post), [TikTok Display API](https://developers.tiktok.com/doc/display-api-overview/), [TikTok Research API eligibility](https://developers.tiktok.com/products/research-api), [Shopify Order object](https://shopify.dev/docs/api/admin-graphql/2026-07/objects/Order), [Google Trends API alpha](https://ppc.land/google-opens-alpha-testing-for-new-trends-api-targeting-developers-and-journalists/).

## 3. Architecture (new package `jarvis_marketing/`)
```
 Jarvis window (Marketing tab, iframe /marketing)      Laptop browser (password login)
            │ token headers (127.0.0.1)                          │ session cookie + CSRF
            ▼                                                    ▼
 jarvis_v2/server.py  ── /api/marketing/* ──►  jarvis_marketing.api  ◄── jarvis_marketing.remote (own port)
                                                     │
                     controls.py (modes, permissions, caps, e-stop: CODE, not prompts)
                                                     │
 director.py: typed tasks, leases, timeouts, attempt caps, idempotency keys, recovery
   ├─ agents/research.py      evidence with provenance → ranked opportunities
   ├─ agents/evaluation.py    scoring.py unit economics → shortlist / rejections
   ├─ agents/creative.py      distinct concepts, one test variable each
   ├─ agents/production.py    provider registry → renders + technical checks
   ├─ agents/qa.py            independent QA (scores labelled "prediction")
   ├─ agents/testing.py       experiments, approval-gated organic posts, stats verdicts
   └─ agents/learning.py      lessons with evidence → research priorities, reports
                                                     │
 adapters/: shopify (live/public/mock), supplier CJ (live/mock), competitor (live/mock),
            trends (mock only), publisher (TikTok browser/mock, paid: always refused),
            analytics (TikTok Studio/mock), orders (Shopify API/browser/mock), video (local/mock), notify
                                                     │
 store.py: one SQLite DB per profile (data/mock/… and data/live/…), so simulated data never mixes with live data.
```
- **Agents produce data, not actions.** Every external action goes through `controls.require()` and an adapter. No agent gets a handle that can change modes, caps or permissions.
- **Multi-business:** every record carries `business_id`. Each business has its own config (store, supplier, competitors, thresholds) in `businesses.json`.
- **LLM use is optional.** All agents work deterministically from evidence. An optional brain (Claude Code CLI on the subscription) may suggest hooks. Its text passes the same claims check and product-fact check, and is labelled with the model name. OpenAI is never required.

## 4. No endless loops
- Every task has: `max_attempts` (default 2), `timeout_s` and an idempotency key (unique).
- A task's status is one of: `queued → running → done | failed | timed_out | cancelled | blocked`.
- A cycle has a hard cap on tasks (default 60) and wall time.
- A running task holds a lease. On restart, expired leases are retried (if attempts remain) or failed. They are never silently lost.
- The QA ↔ production rework loop is capped (default 2 revisions per creative). After that, it's rejected with reasons.

## 5. Modes and controls (enforced in `controls.py`)
| Mode | Allowed |
|---|---|
| RESEARCH | research, evidence, scoring, reports |
| REVIEW | + briefs, production, QA, requesting approvals |
| TEST | + organic posts that have **an approval for that exact content** (content hash); reading analytics/orders |
| SCALE | **refused in milestone 1** (`MAX_MODE = TEST`) |

**Hard-coded off:**
- `PAID_ADS_ENABLED`, `PURCHASES_ENABLED` and `PRICE_CHANGES_ENABLED` are module constants set to `False`.
- Spend caps (total and per campaign) default to 0. Every spend check fails at 0.

**Who can change what:**
- Mode changes, cap changes, emergency stop/resume and approvals need a `HumanPrincipal`. Only the authenticated server layer mints one (window UI token or laptop login).
- Agents have no code path to them.
- Caps can't be raised above the hard ceiling while paid is disabled.

**Other protections:**
- **Emergency stop:** blocks every task and external action at once, and is persistent across restarts.
- **Approvals:** single-use, bound to the content hash, expire after 24h, and can't be reused for different content.
- **Duplicate prevention:** unique (platform, content hash) for posts, unique experiment keys, unique task idempotency keys.
- **Stale data:** analytics/orders readings older than their TTL are marked stale and are never used for verdicts.
- **API failures:** recorded as task errors. There are no fake fallbacks.
- **Audit log:** append-only table of every decision, approval and external action, with secrets redacted.

## 6. Milestones
1. **M1:** plan + store + controls + scoring + evidence rules (unit tested).
2. **M2:** agents + director + mock adapters (full mock cycle end to end).
3. **M3:** live adapters (Shopify/CJ/competitor/TikTok browser publisher/analytics/orders), mounted in the v2 server.
4. **M4:** Marketing dashboard (window tab + laptop server with login).
5. **M5:** experiments/verdict statistics, reconciliation, reports, lessons, final hardening + report.

## 7. Acceptance tests (all in `jarvis_marketing/tests`, mock mode)
- Structured agent exchange: tasks validate input/output schemas, and bad output fails the task.
- Profit maths matches hand calculations. Ranking is deterministic and explained.
- A "fact" without a matching fetched source is rejected or downgraded. Unsupported claims are blocked.
- Product identity (supplier id + product id + image set) is identical across every variant of a brief.
- Missing analytics stay `None`/"not available" and never become 0 or an estimate.
- Spend/paid/purchase/price actions are refused in every mode. Agents can't change mode or caps. Caps can't be raised.
- Duplicate posts/experiments/tasks are refused, including after a restart.
- Restart recovery: interrupted tasks are retried or failed. A post interrupted mid-publish is never auto-reposted.
- Approval + emergency stop gates are covered, including a stale or mismatched approval.
- Metrics reconcile with the stored orders/metrics.
- An inconclusive result is never shown as a winner. Views/likes never pick a winner.
- Lessons keep their evidence links.
- The dashboard API matches the DB records. The laptop server needs login and rejects missing CSRF.
- All existing Jarvis suites show no new failures.

## 8. Risks
- **Thin real data:** without paid traffic, organic tests produce few sessions. Most early verdicts will honestly be "inconclusive".
- **Browser-based TikTok/Shopify reads break when layouts change.** They are reported as "couldn't read", never guessed.
- **Supplier data can change** (price/stock). Readings carry a timestamp and are re-checked before any post is approved.
- **Laptop access exposes a login page.** Bind it to a private network (Tailscale recommended) or enable TLS. It is off by default.

## 9. What Danny must do (exact steps are in the final report)
- Set the laptop dashboard password: `py -3 -m jarvis_marketing set-password`.
- Choose how the laptop reaches the PC (Tailscale recommended).
- Optionally add the `read_orders` scope to the Shopify app for API order attribution.
- List competitor store URLs in `jarvis_marketing/data/businesses.json`.
- Switch the business profile from `mock` to `live` when ready.
