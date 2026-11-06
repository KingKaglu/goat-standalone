# GOAT upgrade plan — Phase 2 (2026-11-06)

Built from `AUDIT.md`. Ordered: security → bugs → voice → router/tokens → cleanup. Each batch is small and independent: **one batch = one commit** (conventional message), then STOP.

## Rules for every batch
- Stage **only the batch's files** (`git add <files>`). Your uncommitted `close_guard.py` / `screen_policy.py` edits stay out of every commit.
- Checks before committing: `self_check.py` preflight + `run_tests.py` (no-cost suites, added in Phase 3) + the batch's own new tests.
- Commits stay **local**. Nothing is pushed to GitHub unless you say so.
- Live app: a change only takes effect after a restart through `restart-goat.ps1` (preflight + auto-rollback). See question 2.
- Rollback for every batch: `git revert <sha>` then restart. Batches with runtime state also list what to restore.

## Phase 3 (before any fix): measure first
- `metrics.py`, enabled by **`GOAT_METRICS=on`** (off = zero cost). One JSON line per event to `python/metrics.jsonl` (gitignored).
- `run_tests.py`: runs the no-cost suites (preflight, engine_router, brain_route, task_router, action_policy, phone_server, voice_latency, two_brains, …) with one summary line.
- New smoke tests: the engine reconnect path (fake client), TTS cancel ordering, phone auth limits.
- Baseline: 15-20 real voice turns from you (a normal 5-minute chat) with `GOAT_METRICS=on`, plus the 13 ledger turns already in the log as a provisional baseline.

### Metrics

| Metric | Definition | Source | Baseline so far |
|---|---|---|---|
| **EOS→first sound** | End of speech (back-dated by the hangover) to the first reply audio sample, filler included | existing ledger + `on_first_audio` | 2.47-2.93 s on Live turns |
| **Time to first word (TTFW)** | End of speech to first *answer* audio (not a filler, not the "Sir," lead) | `on_content_audio` | not logged yet |
| Split: endpoint / ear / voice | Hangover wait; ear finish after end of speech; voice first token to audio | ledger + endpoint state | endpoint ~1.2 s on 14 of 14 turns |
| **Tokens per turn** | Claude job: input, cache_read, cache_create, output (`ResultMessage.usage`). Gemini turn: `usageMetadata` where the API sends it | `_track_usage`, live_talk / talk_brain | not logged |
| **Cache hit rate** | cache_read ÷ (cache_read + cache_create + input), per job and rolling | same | not logged |
| **False barge-ins** | A confirmed interrupt followed by no words of yours within 3 s, or by a clip dropped as echo/murmur/phantom. Also counted: "echo, not you" duck false alarms | `[barge]` paths | 6 confirmed / 1 echo, accuracy unknown |
| Split utterances | Your turn starting ≤ 3 s after your previous one ended, where the first already got a reply (not merged) | `_heard` / `_talk` | unknown |

## Baseline (Phase 3, recorded 2026-11-06 at 248740c + instrumentation)

**Tests:** `run_tests.py` default set: 35/35 suites pass, ~3 min.
- Opt-in groups: `--slow` (test_dsp 18/18) and `--live` (test_screen: 64 pass, 1 already failing on 248740c, see AUDIT B6).

**Voice latency.** Provisional numbers, taken from the 14 ledger turns in `goat-app.log` + `.old` (2026-11-04/05):

| | n | total p50 | total p90 | endpoint p50 | turns with endpoint ≥ 1 s | ear p50 | voice p50 | fillers |
|---|---|---|---|---|---|---|---|---|
| All turns | 14 | 2901 ms | 29048 ms | 1216 ms | 14 / 14 | 645 ms | 1202 ms | 3 |
| Healthy (Live up, < 5 s) | 11 | **2756 ms** | 3569 ms | **1216 ms** | 11 / 11 | 644 ms | 922 ms | 2 |

**Not measurable from the old logs:** TTFW (first answer audio), tokens per job, cache hit rate, false barge-ins, split utterances.
- They're instrumented now: restart GOAT with `GOAT_METRICS=on`, talk about 5 minutes, then run `py -3.13 metrics.py summary`.
- That live session becomes the real baseline that Batch 8 and later are compared against.

## Batches

| # | Name | Fixes | Files | Verification | Rollback / notes |
|---|---|---|---|---|---|
| 1 | `fix(phone): strong token + one rate limiter` | S1, S2, S9, S10 (comment) | `phone_server.py`, `phone_qr.py`, `test_phone_server.py` | A short or custom token is refused and regenerated (~128-bit, phone-typeable alphabet). `?k=` on `/` and `/goat.apk` counts against the same limiter (per IP + global, backoff, pruned). Comment naming the token removed. Tests: limiter, 429, token length. | Revert. **The phone and APK must log in again with the new token** (it's printed in `.phone/url.txt` and the QR). The old token is never restored. |
| 2 | `feat(phone): server-side sessions with expiry` | S3, S10 | `phone_server.py`, `phone_qr.py`, `test_phone_server.py` | The cookie becomes a random session ID (hashed in `.phone/sessions.json`), 30-day sliding expiry, and "log out all" via `phone_server.py logout-all`. `?k=` becomes a one-time 10-minute pairing code. Tests: expiry, revoke, reused code refused. | Revert. Sessions file is gitignored (`.phone/`). One more re-login. |
| 3 | `fix(phone): listen on tailnet + loopback only` | S4 | `phone_server.py`, `setup.ps1` (firewall rule), `test_phone_server.py` | Default bind/allowlist: loopback + 100.64/10 + Tailscale IPv6. `GOAT_PHONE_LAN=on` restores home Wi-Fi. Firewall rule narrowed by `setup.ps1` (needs admin, once). Test: allowed_remote matrix. | Revert or `GOAT_PHONE_LAN=on`. **Depends on question 1.** |
| 4 | `fix(voice-hands): no shell, no secrets, no launchers` | S5, S6, S7 | `voice_hands.py`, new `secret_paths.py`, new `test_voice_hands_safety.py` | `_resume` runs with `shell=False`. `read_file`, `list_folder` and `find_files` refuse secret paths. Opening uses an allowlist of document/media types. Test: injection string stays one argument; secrets refused; `.lnk` refused; normal docs still open/read. | Revert. |
| 5 | `fix(worker): deny secret paths in tool calls` | S8 | `goat_app.py` (hook list), `secret_paths.py`, test | PreToolUse hook on Read/Write/Edit/Glob/Grep/Bash/PowerShell denies the same secret paths. Test with fake hook inputs. | Revert. Only blocks those paths. |
| 6 | `fix(engine): keep the voice alive when Claude can't reconnect` | B1 | `goat_app.py` (run loop), `test_engine_reconnect.py` | `connect()` retried with backoff 2→60 s. If it keeps failing, the worker is "down" (voice says so once) and ears and voice stay up. Test: a fake client that fails N times, then works; audio is never shut down. | Revert. Highest-touch batch: run the full suite + a live restart check. |
| 7 | `fix: stale sentence after barge-in, loop noise, plan-usage log` | B2, B3, B5 | `goat_app.py`, `ui_qt.py` (loop handler) | gen re-checked under the lock before `queue_playback`. One-line log for WinError 10054. Plan-usage errors logged with type. Test: TtsPipeline cancel race with a fake audio. | Revert. |
| 8 | `perf(voice): end the turn at 500 ms when the sentence is done` | V1 | `goat_app.py:_sentence_state`, `test_voice_latency.py` | While the GPU ear job is pending, judge the last finished partial (or wait ≤150 ms). Metrics: endpoint median from ~1.2 s to ≤0.6 s, with split/cut-off count not worse than baseline. | Revert, or `GOAT_ENDPOINT_WAIT=on` (old behaviour behind a flag). **Uses the Phase 3 baseline.** |
| 9 | `fix(voice): merge a second half that lands after a quick reply` | V4 | `goat_app.py:_heard` | **Only if** Phase 3 shows split utterances. Extend the merge to "voice reply started < X s ago": cut it and answer the whole thought. Metric: split count down, no extra false merges. | Revert. Skipped if the data shows no splits. |
| 10 | `fix(router): dismissals stay with the voice; light jobs run light` | R1, R2 | `task_router.py`, `goat_app.py:_route_big`, `test_task_router.py`, `test_engine_router.py` | "forget it / never mind / thank you / I'll do it myself" go to chat. The router returns quick or deep; quick runs at `TASK_EFFORT`. Code and planning stay deep. Tests: the logged misroutes as cases. | Revert. |
| 11 | `chore(deps): declare aiohttp/cryptography, pin edge-tts, tidy imports` | Q3, Q4, Q5, B4 | `requirements.txt`, `phone_server.py` (imports only), `live_talk.py` (repeat-failure note) | Preflight + suites. `pip install -r requirements.txt --dry-run` shows no changes. | Revert. |

Phase 5 (your choice of options) covers **V3 fillers / "Sir," lead, V5 clip splitting, T2 voice prompt size**.
**Not planned without your OK:** Q1/Q2 refactors (e.g. moving `TtsPipeline` out of `goat_app.py`). I'll propose these only if a batch needs them.

## What happened (2026-11-06)
All 11 batches landed (Batch 8 changed approach: the measured cause was the ear's language check, not
the endpoint rule; Batch 9 ran because the night's metrics showed a real split). Commits
`620a0e8` … `8f04336`, Phase 5 option A `8dafdb1`. 40/40 offline suites pass. Nothing pushed.

| Metric | Before | After (measured) |
|---|---|---|
| Laptop ear job, his 4 clips through the real `stt_whisper.Stream` on the live GPU server, p50 | 3233 ms (language check; 2 clips also went to the cloud Georgian check, ~5.2 s) | **502 ms** (sticky English `cc517c5` + plain-json pinned request) |
| Transcript ready after he stops (finished sentence) | ~3.4 s on those clips; live ledger: endpoint 1216 + ear 739 = ~1.95 s | **~0.7 s** (200 ms soft commit + 502 ms job) |
| Endpoint wait (finished sentence) | 1216 ms (56 of 69 "local-pending") | **~700 ms** (ends as soon as the job lands) |
| Filler fires (his 5 recorded turns replayed) | 2 of 5 at 1.5 s | **1 of 5** at 2.5 s (`8dafdb1`) |
| Stale sentence after barge-in (forced race) | 1600 samples played | **0** (`087f590`) |
| Split utterances (his session) | 1 (30 s cap) | held as one turn in the replayed case (`d4c219e`, test) |
| Time to first word p50, live | 3430 ms (5 turns) | to be read from his next session (no live turns since); ear + endpoint save ~1.2 s of it |
| Claude per job / cache hit | 148k in / 89 % | unchanged (no prompt change) |
| False barge-ins | 0 of 1 confirmed; 5 echo ducks | unchanged (no barge change) |

The live voice "after" (TTFW, false barge-ins) needs him talking; everything above it is measured on
his own recordings and logs. Phase 5: he said to stop asking for approval, so option A was picked
as the one that overrides none of his standing orders; B and C remain his call.

Live after-numbers: `py -3.13 python\metrics.py summary` after a normal session (events before
t=1793934582 are the baseline, also copied to `python/metrics-baseline-20261106.jsonl`).

## Decisions I need from you
1. **Phone reach (Batch 3):** does your phone reach GOAT over **Tailscale**, or over home Wi-Fi by IP? (Tailscale-only is safest; LAN stays available as an opt-in.)
2. **Restarts:** may I restart GOAT myself (`restart-goat.ps1`) after each batch to verify it live, or will you restart it?
3. **Push:** keep everything local (default), or push after each batch? Until you push, the public repo still shows the old comment. Batch 1 makes the token it names useless either way.
4. **Baseline session (Phase 3):** I need ~5 minutes of you talking to GOAT with metrics on. When suits you?
