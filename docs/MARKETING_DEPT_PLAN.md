# Jarvis Marketing Department: audit and plan

**Status (20 Oct 2026):**
- All 7 phases are built on branch `claude/sleepy-hypatia-y3xua6` (not merged).
- Mock mode works end to end: 104 marketing tests pass, and no existing Jarvis test newly fails.
- Live adapters are built and tested with stand-in transports. They need Danny's setup (section 11) before real use.
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

## 9. What Danny must do (exact steps in section 11)
- Set the laptop dashboard password: `py -3 -m jarvis_marketing set-password`.
- Choose how the laptop reaches the PC (Tailscale recommended).
- Optionally add the `read_orders` scope to the Shopify app for API order attribution.
- List competitor store URLs in `jarvis_marketing/data/businesses.json`.
- Switch the business profile from `mock` to `live` when ready.

## 10. What was built (files)
| Part | File(s) |
|---|---|
| Records (one SQLite DB per profile, append-only audit, secret redaction) | `jarvis_marketing/store.py` |
| Modes, permissions, hard-off switches, caps, approvals, emergency stop, schedule | `jarvis_marketing/controls.py` |
| Evidence provenance (fact / verified / estimate / hypothesis / prediction), fabrication checks | `jarvis_marketing/evidence.py` |
| Unit economics, break-even CAC/ROAS, scoring and rejections | `jarvis_marketing/scoring.py` |
| Banned/unsupported claims + number checks | `jarvis_marketing/claims.py` |
| Verdict statistics (Wilson intervals, two-proportion test, stop rules) | `jarvis_marketing/stats.py` |
| Agents | `jarvis_marketing/agents/` (`research`, `evaluation`, `creative`, `production`, `qa`, `testing`, `learning`, `base`) |
| Marketing Director + scheduler | `jarvis_marketing/director.py` |
| Adapters (mock, live, video) | `jarvis_marketing/adapters/` |
| Dashboard API (shared) | `jarvis_marketing/api.py` |
| Laptop server (login, CSRF, lockout, TLS) | `jarvis_marketing/remote.py` |
| Dashboard page | `jarvis_marketing/ui/marketing.html` |
| Terminal commands | `jarvis_marketing/__main__.py` |
| Job safety fix | `jarvis_v2/job_browser.py`: jobs' browser can no longer open this PC, home-network or Tailscale addresses, so a job can never press Approve in a dashboard. Marketing button presses must also carry the window's Origin, as CJ Pay's do. |
| Jarvis integration | `jarvis_v2/server.py` (`/marketing` page, `/api/marketing/*`, start/stop), `jarvis_v2/ui/index.html` (Marketing tab), `jarvis_v2/tools.py` (`marketing_status`, `marketing_stop` brain tools; marketing approvals in Needs you), `LATER_JOBS.md` |
| Tests | `jarvis_marketing/tests/` (`test_core`, `test_agents`, `test_dashboard`, `test_live_adapters`) |

### Creative provider comparison (milestone 1)
| Provider | Cost | Quality | Reliability | Product accuracy | Used? |
|---|---|---|---|---|---|
| `local_slideshow` (jarvis_video.py: supplier photos, zooms, captions, edge-tts voice, own music) | free | clean, fast-cut slideshow | high; the voice needs internet | exact: only the supplier's own photos | yes (live) |
| Generative AI video (text/image-to-video services) | paid per clip | can look more "native" | varies | **can alter the product**, which breaks the accuracy rule | no, until a provider can be checked against the real photos |
| `mock` | free | none (manifest only, SIMULATED) | n/a | n/a | tests and the demo |

### Known limits (honest)
- **Organic attribution is weak.** TikTok posts can't hold links and there is one bio link, so variants run one after another. Most organic verdicts will be "inconclusive", and the dashboard says so.
- **Sessions per post** aren't available live: TikTok Studio shows views/likes/comments only. Shopify sessions by UTM would need the analytics API (`read_reports`). Until then, CVR shows "not available" and verdicts stay "insufficient data". This is by design: nothing is invented.
- **No language model is required.** All agents are deterministic and work from evidence. An LLM hook writer was deliberately left out of this milestone: OpenAI credit is gone, and every word must pass the fact checks anyway.
- **Browser-read sources** (Shopify admin orders, TikTok Studio) break when a site changes its layout. They are reported as unavailable, never guessed.
- **Unverified on Danny's PC:** the CJ, Shopify and TikTok live adapters are tested only against stand-in transports here. The first live run should be watched (section 12).

## 11. Exact steps for Danny
1. **Restart Jarvis** (close the window, run `START_JARVIS_V2.bat`). Open the **Marketing** tab. It shows a SIMULATED business (purple banner).
2. **Try it safely in simulation:**
   - Press REVIEW, then *Run a cycle now*.
   - Then press TEST and approve one simulated post in *Approvals*.
   - Nothing real is posted while the profile is mock.
   - To fill it quickly instead, run in PowerShell in the Jarvis folder: `py -3 -m jarvis_marketing demo`.
3. **Laptop access** (optional):
   1. Install Tailscale on this PC and on the laptop (https://tailscale.com/download), and sign in to the same account on both.
   2. On the PC, note its Tailscale address (100.x.y.z, shown in the Tailscale menu).
   3. In PowerShell in the Jarvis folder, run: `py -3 -m jarvis_marketing set-password --bind 100.x.y.z`. Type a password of 12+ characters twice.
   4. Restart Jarvis. If Windows Firewall asks, allow Python on **Private** networks.
   5. On the laptop, open `http://100.x.y.z:8790` and sign in. Tailscale encrypts the connection.
4. **Competitor prices** (optional): tell Claude which competitor shop websites to compare against. Claude will write `jarvis_marketing\data\businesses.json` for you.
5. **Order attribution through the Shopify API** (optional, recommended):
   1. Go to dev.shopify.com, then Apps, then Jarvis, then Configuration. Add the scope `read_orders`.
   2. Release a new version.
   3. In the store admin, approve the updated app.
   4. Tell Claude "orders scope done", and Claude sets `"orders_source": "api"`.
   - Without this, orders are read from the admin page in Jarvis's browser, with no attribution.
6. **Going live:** tell Claude "switch marketing to live". This sets `"profile": "live"` in `businesses.json`, which starts a separate live database in RESEARCH mode.
   - Then step up through REVIEW to TEST yourself, from the dashboard.
   - Every post still needs your approval, and Jarvis's browser must be logged in to TikTok.
7. **Paid ads** stay off in code. Turning them on is a separate, reviewed code change after you decide budgets and limits.

## 12. How to start, stop and verify
- **Start:** the department starts with the Jarvis window.
  - It recovers interrupted work first. A post cut off mid-upload is flagged for you to check and is never reposted.
  - Auto-run is **off** until you switch it on (dashboard button, every 24h).
- **Stop:** any of these works.
  - The red **EMERGENCY STOP** button (window or laptop).
  - Saying "Jarvis, stop marketing" (the `marketing_stop` tool).
  - `py -3 -m jarvis_marketing stop`.
  - Only you can resume (button or `py -3 -m jarvis_marketing resume`).
- **Status:** `py -3 -m jarvis_marketing status`, the Marketing tab, or ask Jarvis ("how's marketing going?").
- **Verify:**
  - Run the tests: `py -3 -m unittest discover -s jarvis_marketing/tests -t .` (about 5 minutes).
  - The *Activity & audit* tab shows every decision and external action, and a reconciliation check that the dashboard numbers match the records.

## 13. Milestone 6: winning-ad research engine (2026-10-20)
Built on top of M1-M5; nothing that worked was rebuilt.

**Danny's decisions:**
- Built locally, not in the cloud.
- Shopify DRAFT products may be created in REVIEW mode and above.
- PUBLISHING a product needs Danny's approval of that exact listing.
- Facebook/Instagram tests may be PREPARED. Launching stays off in code.

### The cycle now
1. `research`
2. `ads`: find adverts and reverse-engineer them.
3. `evaluate`: now includes profit after the planned ad cost.
4. `list`: fresh supplier check, then a Shopify DRAFT, then read it back, then request approval.
5. `go_live`: approved listings only; checks the public page.
6. `brief`: original concepts, ordered by the research.
7. `produce`
8. `qa`
9. `rank`: pre-launch assessment.
10. `design`: organic test.
11. `paid_plan`: a PAUSED Facebook/Instagram plan.
12. `publish`
13. `collect`
14. `judge`
15. `learn`

### Research sources (`jarvis_marketing/sources.py`)
Every source has its own adapter, access requirement, rate limit and written limits. Every call is logged in `source_runs`, and a failing source never stops the others. The Discovery tab shows coverage and failures.

| Source | Status on Danny's PC (20 Oct) | What it can't show |
|---|---|---|
| Meta Ad Library API | Built and tested with a stand-in; **needs Danny's Meta token** | Non-political ads only if they reached the EU (searched via IE); no spend, clicks or sales |
| TikTok Creative Center Top Ads / Top Products | **No official API.** Danny adds ads he sees (dashboard form: link, hook, text, optional .mp4) | TikTok's CTR/like labels are its own signals |
| TikTok Commercial Content API | Unavailable: needs TikTok's approval | EU ads only, no performance |
| CJ supplier (search, details, UK shipping, **stock**) | Live, verified on this PC | "Listed by N stores" is popularity, not sales |
| Competitor stores (public products.json) | Live once URLs are listed | Prices only |
| Our store pages and orders | Live | Attribution only via UTM |
| Google Trends / TikTok Research API / reviews | Unavailable (invite-only / academic-only / none) | |

### Advert reverse-engineering (`agents/adintel.py`)
Each advert is broken into the same structure:
- hook and first seconds
- problem or desire
- first shot and product reveal
- scenes and pacing
- demonstration
- benefits and objections answered
- emotional triggers
- text overlays, captions and audio
- editing style
- offer, price presentation and call to action
- customer and angle
- performance evidence and its limits
- what's worth testing

Each element is marked OBSERVED (from the ad's text or dates, or measured from a video file with ffmpeg scene detection) or NOT OBSERVABLE. "Why it might work" is always a hypothesis.

`principles()` summarises a keyword's adverts. Ads that kept running 30+ days count double, but run length is never treated as profit. Social-proof hooks are never turned into our concepts, because we have no reviews to cite.

`too_close()` drops any line of ours that copies an advert. Music from other adverts is never used.

### Listings (`agents/listing.py`)
- Each product is re-read at CJ: identity, variants, UK shipping and stock.
- The listing is built by jarvis_store's own rules (`pipeline.evaluate`).
- Every number in the copy must appear in the supplier's data.
- Profit is worked out after a planned ad cost per order (`economics.planned_cpa`, an ESTIMATE, default £3). Below `min_profit_after_ads` (default £1), the product is rejected with the reason.
- The DRAFT is created, read back from Shopify and compared field by field.
- Approval is bound to that exact content: title, prices, SKUs, photos and description hash.
- If the draft changes after approval, publishing is refused.
- Publishing uses `Shopify.publish_jarvis_draft()`. It only works on Jarvis's own DRAFTs and also tries to add the Online Store channel.
- A product only counts as live when its public page loads, is in stock and shows the planned price. No advert may point anywhere else.

### Creatives and pre-launch ranking
- **New concepts:** curiosity open, benefit-led, and objection/comparison (answered with a real material fact).
- **Concept order:** our own verified lessons first, then the research principles.
- **Brief records:** each brief records which principles and example ads informed it.
- **Ranking (`agents/assessment.py`):** scores eight criteria (hook clarity, visual clarity, demonstration, pacing compared with measured adverts, offer, CTA, audience suitability, landing-page consistency). Every score is labelled PREDICTION.
- **No prediction service:** every ranking states that no platform gives a pre-launch prediction through our APIs. No CTR/CVR/ROAS forecast is ever invented.

### Facebook/Instagram tests (`testing.paid_plan`, `adapters/commerce.py`)
Each prepared plan contains:
- the objective: sales, created PAUSED;
- one ad set per creative, all on equal budgets (proposed: £5/day for 4 days);
- the audience: GB, ages 18-65, broad;
- Reels/Feed/Stories placements;
- a link to the verified product page, with a UTM per creative;
- the pixel;
- the success criteria: cost per purchase under the break-even CPA, and profit after spend;
- the stopping rules and attribution.

`check_plan()` lists what's missing.

Launching is refused in three places:
- `PAID_ADS_ENABLED = False`;
- spend caps can't exceed £0;
- `launch()` always refuses.

Every launch attempt is audited.

### End-to-end acceptance (20 Oct 2026, one real product)
| # | Step | Result |
|---|---|---|
| 1 | Find the product through research | **Real.** CJ, keywords "cable organizer" and "sink caddy". 8 products were researched, 3 shortlisted, and the rest rejected with reasons. |
| 2 | Gather demand evidence and inspect adverts | **Real, partly by hand.** The Meta API needs Danny's token, and TikTok Creative Center shows only 3 unrelated ads when signed out. Two real "cable organiser" adverts were copied from Meta's public Ad Library page through the import path (Orbitkey, running since 14 Aug; Sundaiz, since 9 Oct). |
| 3 | Explain the creative principles | **Real.** Both were analysed as benefit-led hooks, with their benefits and relief triggers extracted. Principle: "benefit openings" (2 ads, 1 running 30+ days). Visual elements were marked not observable: no video file. |
| 4 | Verify supplier and calculate profit | **Real.** The CJ product exists, stock is 10k-35k per variant, and delivery is YunExpress 4-7 days. The weakest variant (£13.49) makes about £4.83 per order, or about £1.83 after the planned £3 ad cost (both estimates). |
| 5 | Prepare and validate a Shopify draft | **Real.** DRAFT "Silicone USB Cable Winder Desktop Organizer Management": 10 variants, £10.99-£15.49, 8 photos. Read back from Shopify: every field matches. |
| 6-7 | Generate and rank original adverts | Simulated end to end in tests. For the real product they run on the next cycle **after Danny publishes it** (creatives may only point at a live page). |
| 8 | Publish after approval | **Waiting for Danny** (Marketing, then Approvals). |
| 9 | Prepare a Facebook/Instagram test | Simulated in tests. For the real product it happens after step 7. It also needs the Meta ad account, page and pixel ids. |
| 10 | Launch only after authorisation | **Disabled in code.** It refuses and is audited (tested). |
| 11 | Real performance and profit | Not yet. This needs live posts or ads. The collection code is tested with stand-ins. |
| 12 | Feed results back | Simulated in tests (lessons, priorities). |

### What Danny must do for the live parts
1. **Meta Ad Library (ad research):**
   1. Go to developers.facebook.com. Create an app (type "Business").
   2. Confirm your identity at facebook.com/ID.
   3. In Graph API Explorer, make a user token and exchange it for a long-lived one.
   4. Run `py -3 -m jarvis_store.setup_credentials` and paste it at "Meta Ad Library access token". Press Enter to skip the other fields.
2. **TikTok Creative Center:** when you see a good ad there, open Marketing → Ad research → *Add an advert you've seen*. Paste its link and opening line. A saved .mp4 lets Jarvis measure its pacing.
3. **Publishing a product:** Marketing → Approvals. Check the photos, prices, profit and checks, then press *Approve: publish to the store*.
   - If the public page check fails, open the product in Shopify and tick the Online Store sales channel.
4. **Before any paid test (later, your decision):**
   - Put your Meta ad account id, Facebook page id and pixel id in `businesses.json` (tell Claude).
   - Install the Facebook & Instagram app on Shopify for the pixel.
   - Approve the audience.
   - Then decide the budget and ask for paid ads to be switched on. That is a reviewed code change.

## 14. Milestone 7: Google and multi-platform listings (2026-10-20)
Built on M1-M6 locally. Nothing is registered, submitted or published on any new platform. Readiness and permission to publish stay separate.

### What was built
| Part | File |
|---|---|
| Platform knowledge base: dropshipping rules, fees, integrations, sync and delivery expectations, each fact with its source and status (verified / unconfirmed / unknown) | `jarvis_marketing/platforms.py` |
| Central product record: one verified record per product. It is built from live Shopify products (read-only) and Jarvis's drafts, and linked to CJ by the product code in each SKU. Supplier, stock and shipping are re-read when over 3 days old, about 10 products per run because CJ allows 1 request a second. | `jarvis_marketing/catalog.py`, table `catalog` |
| Readiness per product × platform: platform payload, safe auto-fixes (each recorded), mandatory checks, READY / NEEDS_FIXES / REJECTED, profit after that platform's fees, recommendation with reasons | `jarvis_marketing/readiness.py`, table `channel_listings` |
| Google: Merchant Center feed rows built to Google's product data spec and validated. The prepared feed for READY products is written to `data/<profile>/google_feed_prepared.tsv` for review, never submitted. Merchant Center, Search Console and Shopping results show as unavailable until connected. | `readiness.py`, `adapters/__init__.py`, `sources.py` |
| Safeguards: oversell risk, price below cost, duplicate products/SKUs, unmapped variants, stale supplier data, platforms without sync. `route_order()` holds any order that can't be fulfilled safely; Jarvis never places supplier orders. | `jarvis_marketing/sync.py` |
| The `channels` stage in the cycle, plus a "Channels" dashboard tab (matrix, details, fixes, safeguards, Google statuses, platform research) | `agents/channels.py`, `api.py`, `ui/marketing.html` |
| `CHANNEL_PUBLISH_ENABLED = False`: publishing to any new platform is off in code even with an approval | `controls.py` |

### Readiness rules
- **Mandatory checks:**
  - eligibility and dropshipping policy;
  - product policy (own-store words, plus stricter marketplace words);
  - electrical safety documents (marketplaces);
  - supplier verified within 3 days;
  - every variant mapped to a CJ variant;
  - supplier stock;
  - profit after the platform's fees at least the business minimum (£4);
  - listing data (needs a live page link);
  - barcodes (Amazon, OnBuy);
  - brand (Google);
  - images;
  - stock/price sync;
  - orders and tracking;
  - delivery promise against platform expectations;
  - evidence of demand.
- **REJECTED:** the platform forbids the model (Etsy) or the price makes a loss.
- **NEEDS_FIXES:** any other mandatory check fails, whatever the score.
- **Safe fixes (always recorded):**
  - remove promotional words from titles;
  - shorten over-long titles at a word boundary;
  - convert HTML to plain text;
  - drop non-https or excess images;
  - set `identifier_exists = no` when there's no barcode;
  - derive availability from supplier stock.

### Platforms (researched 2026-10-20)
| Platform | Verdict now | Why |
|---|---|---|
| Shopify (own store) | Live | Already connected |
| Google free listings | Strong fit; **needs your brand decision and the Google & YouTube channel** | No fee; syncs through Shopify; field limits verified on Google's spec page |
| Facebook & Instagram Shops | Strong fit; needs the Shopify channel | UK uses website checkout (unconfirmed), so no selling fee |
| Pinterest | Good fit; needs the Shopify app | Product Pins link to our site (unconfirmed for the UK) |
| eBay UK | Possible later | Dropshipping from a *wholesale supplier* allowed, from another retailer forbidden (**verified**). About 1.9-2.4 lower profit per order from fees. Needs eBay account + integration. |
| Amazon UK | Not now | Seller-of-record and packaging rules (unconfirmed). Needs GTINs or an exemption, £25/month, fast delivery. Delivery metrics are risky with CJ. |
| TikTok Shop UK | Not now | UK dropshipping rules unclear; delivery deadlines likely too short for CJ (unconfirmed) |
| OnBuy | Unknown | No dropshipping policy found |
| Etsy | **Rejected** | Only own-made/designed, vintage or craft supplies (secondary sources; Etsy's pages refused automated reading) |

### Live result (20 Oct 2026, 43 store products)
- **All 43 products are linked to their CJ product, with every variant mapped.** This took 6 passes at about 10 products a pass.
- **READY for Shopify, Meta and Pinterest:** 25 products.
- **Google:** Danny decided the brand is **Collective** (`"brand": "Collective"` in businesses.json). After that, 25 products are READY for Google and the prepared feed has 67 variant rows with no validation problems. The other 18 are held by profit, demand evidence, policy wording or stock.
- **Below the £4 minimum on our own store:** 7 products. These are estimates at live prices after CJ's cheapest UK shipping. Price changes are Danny's decision.
  - Flameless LED Taper Candles: £0.80/order
  - Pinecone Wreath: £1.35
  - Acacia Chopping Board: £3.24
  - Motion Sensor Light: £3.47
  - Oil Sprayer: £3.76
  - Bauble Set: £3.97
  - LED Message Board: £3.99
- **Marketplaces:** none recommended yet. eBay/Amazon fees, unconfirmed dropshipping conditions, no integration and (for Amazon/OnBuy) no barcodes. Some products are REJECTED there because the fees make them loss-making.
- **Safeguards:** no oversell, duplicate or SKU-mapping problems found.
### What Danny must do (in order of value)
1. **Brand for Google:** decide whether these products are sold as your own brand ("Collective"). If yes, tell Claude and it sets `"brand": "Collective"` in `businesses.json`. If not, Google listings stay NEEDS_FIXES.
2. **Install the Google & YouTube app in Shopify** and link or create a Merchant Center account (UK, GBP). This is the free-listings route.
3. **Install the Facebook & Instagram and Pinterest apps in Shopify** if you want those catalogues.
4. **Optional:**
   - Add the `read_publications` scope to the Jarvis Shopify app so Jarvis can see which channels are installed.
   - For Jarvis to read Google approval and indexing statuses, a Google Cloud OAuth client (Merchant API + Search Console API). Claude can walk you through it.
5. **Marketplaces (eBay first, if wanted):** open a business seller account yourself and keep CJ's supplier agreement on file. A connected integration and a reviewed code change are needed before Jarvis lists anything there.
