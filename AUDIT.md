# GOAT audit — Phase 1 (2026-11-06, read-only)

Scope: `python/` at HEAD `248740c`, plus the running config (`ui-config.json`: `lang=en`, `work_model=opus 5.5`, `effort=high`) and the two live logs (`goat-app.log`, `goat-app.log.old`, about 290 lines in total, because the log rotates on every launch).
Audited as GOAT actually runs: **two brains on** (Gemini Live voice, then REST Gemini, then Claude as cover; Claude Agent SDK as the worker on the plan login). Uncommitted edits to `close_guard.py` and `screen_policy.py` were not reviewed.

**Basis:** FACT = verified in code, log or runtime this session. ASSUMPTION = plausible, not proven. OPINION = judgement.
**Severity:** Critical (exploitable now / data loss) > High > Medium > Low > Info.

## Security

| ID | Severity | Category | File:line | Problem | Fix | Risk | Effort | Basis |
|---|---|---|---|---|---|---|---|---|
| S1 | **Critical** | Token | `phone_server.py:131`, `:126-139`, public `origin/master` | The live phone login token is a 5-letter lowercase word. The code comment at `:131` names it, and that comment is pushed to the **public** GitHub `origin` (checked: the API says `private=false`, and the comment is in `origin/master:python/phone_server.py`). `load_token` accepts any custom token of 4+ characters. | Rotate now to `secrets.token_urlsafe(24)` or longer. Refuse tokens under ~20 random characters. Delete the comment. History rewrite isn't needed once the token is rotated. | Phone and APK must log in again. | S | FACT |
| S2 | **Critical** | Brute force | `phone_server.py:655-657`, `:786-788` (limiter only at `:663-680`) | `GET /?k=` and `GET /goat.apk?k=` check the token with **no rate limit**. Only `POST /login` counts failures. Unlimited guesses, and the response says which guess worked (cookie or file versus login page). | Send every token comparison through one limiter (per IP plus global, with exponential backoff). Make `?k=` a one-time, short-lived pairing code instead of the master token. | QR/APK flow (`phone_qr.py:8`) must use the pairing code. | S | FACT |
| S3 | High | Session / expiry | `phone_server.py:632-640`, `phone_qr.py:8` | The cookie **is** the master token, valid 400 days. There's no server-side session, no expiry, and no way to revoke one device. The token also travels in URLs (`?k=`), so it lands in browser history and QR images. | Random session IDs stored server-side (`.phone/sessions.json`, hashed) with a sliding expiry (e.g. 30 days) and a "log out all devices" action. Keep the token for pairing only. | Re-login once. | M | FACT |
| S4 | High | Bind / exposure | `phone_server.py:570`, `:86-98`, `:205-207` | The server binds `0.0.0.0`. `allowed_remote` admits **any** RFC1918 address, and the firewall rule is `-Profile Any` for 192.168/16, 10/8 and 172.16/12. On shared Wi-Fi (hotel, café, shared flat) every device on that LAN reaches the login page. Together with S1/S2 that means full laptop control (`/act` click/type, `/screen`, `/upload`, `/phone/do`, text and voice orders to a `bypassPermissions` agent). | Default to loopback plus the Tailscale address (100.64/10, fd7a:115c:a1e0::/48). Make LAN access opt-in (`GOAT_PHONE_LAN=on`). Firewall: `-Profile Private`, RemoteAddress 100.64.0.0/10. | Phone on home Wi-Fi without Tailscale stops working unless LAN is opted in. | S | FACT (exposure) / ASSUMPTION (that you use untrusted networks) |
| S5 | High | Shell injection | `voice_hands.py:644-647` | `subprocess.Popen(["claude", …, msg], shell=True)`. `msg` is written by the model (Gemini), from your speech **or** from content it read (browser history, files, web search). With `shell=True` on Windows the list goes through `cmd.exe`, so a `"` or `&` in `msg` can run another command. `claude` resolves to `C:\Users\giorg\.local\bin\claude.exe`, so no shell is needed. | `shell=False` with `shutil.which("claude")`. Same at `:541-543` (no external input there; Low). | None expected. | S | FACT (mechanism) / ASSUMPTION (practical injection path) |
| S6 | High | Data exposure | `voice_hands.py:267-293`, `:69-71` | `read_file` and `list_folder` accept **any** path. `.json` and `.txt` are readable, so the voice can read `.goat-secrets.json`, `~/.claude/.credentials.json` and `.phone/token.txt`, and the contents go to Google (Gemini). A prompt injection could ask for them. | Denylist secret paths and patterns in `read_file`, `list_folder` and `find_files`: GOAT_ROOT `.goat-secrets.json`, `.phone/`, `voiceid/`, `~/.claude/`, `~/.ssh/`, `*.pem`, `*.key`, `.env`. | None for normal use. | S | FACT |
| S7 | Medium | Exec via open | `voice_hands.py:77-78`, `:296-306` | `NEVER_OPEN` misses launchable types: `.lnk .url .pif .cpl .msc .vbe .jse .wsh .scf .appref-ms .settingcontent-ms .application .library-ms .msp`. `os.startfile` on these can start programs. | Allowlist document and media extensions instead of a denylist, or add the missing ones. | "open the X shortcut" may need Claude. | S | FACT |
| S8 | Medium | Agent permissions | `goat_app.py:5591`, `:1036-1038` | The worker runs `bypassPermissions` with Bash, PowerShell, Write and Edit. The only guards are the close-guard and action-policy hooks. Any job text (phone, voice or injected content) runs with your full user rights. By design, but there's no guard for secret files. | Add a PreToolUse hook that denies reads and writes of the S6 secret paths. Keep everything else as is. | Low. | M | FACT / OPINION |
| S9 | Low | Brute force | `phone_server.py:502`, `:663-680` | `_fails` is per IP and never pruned. The limit is 5 per minute per IP, so 7,200 guesses a day per IP. | Covered by S2 (backoff plus pruning). | — | S | FACT |
| S10 | Low | Secret at rest | `phone_server.py:582-585` | `.phone/url.txt` repeats the token in plain text. It's gitignored with the same ACL, so this is minor. | Drop the token line after S3. | — | S | FACT |
| S11 | Info | Verified OK | various | Checked and found fine: git history has no API keys or private keys (regex scan of all commits). Secrets, `.phone/` and `voiceid/` are gitignored. The token never appears in logs. `hmac.compare_digest` is used. The cookie is `Secure`, `HttpOnly` and `SameSite=Strict`, which covers CSRF. The WebSocket checks Origin (`:686-688`). No CORS headers. `phone.html` uses `innerHTML` only for static templates and puts data in through `textContent` (no XSS found). Upload filenames are sanitised (`:981-984`), so no path traversal. | — | — | — | FACT |

## Bugs

| ID | Severity | Category | File:line | Problem | Fix | Risk | Effort | Basis |
|---|---|---|---|---|---|---|---|---|
| B1 | High | Reconnect | `goat_app.py:5756-5763` (also `:5786-5788`, `:5796-5799`, `:5748-5752`) | The engine self-heal calls `client.connect()` **without a guard** inside the `except`. If the network or CLI is still down, that raise leaves the loop and the `finally:` runs `shutdown_audio()`: the window stays up but GOAT is deaf and mute. Also, 3 crashes in 300 s re-raises and kills the voice too, though Gemini doesn't need Claude. | Retry `connect()` with capped exponential backoff (e.g. 2→60 s). If it keeps failing, mark the worker down (like `claude_out`) and keep the voice and ears running. | Touches the main loop, so test with a fake client. | M | FACT |
| B2 | Medium | Race | `goat_app.py:1552-1587` | `_player` checks `gen` at `:1552`, then runs callbacks and `_tap` (the phone send), then `queue_playback` at `:1587`. A `cancel()` in that gap has already cleared playback, so one stale sentence plays **after** a barge-in. | Re-check `gen == self.gen` under `self._lock` right before `queue_playback`. | Very low. | S | FACT (window) / ASSUMPTION (frequency) |
| B3 | Low | Unhandled noise | log; no `set_exception_handler` in `goat_app.py` / `ui_qt.py` | The Windows Proactor `ConnectionResetError [WinError 10054]` traceback appears in the log. Harmless, but it buries real errors. | Loop exception handler that logs this one case as a single line. | None. | S | FACT |
| B4 | Low | Reconnect | `live_talk.py:451` + log 2026-11-04/05 | When Live failed with `1007 thinkingConfig`, it retried the identical setup every 120 s all day (REST fallback turns took 3.6-12 s to first word). This specific error is fixed by `c0562a0` (`live_talk.py:69`, `:198`). There's still no general distinction between a permanent error (same config fails the same way) and a transient one. | Count identical failures and surface them once ("Live keeps refusing: …"). | — | S | FACT / OPINION |
| B6 | Low | Test / UI | `test_screen.py:134-136`, `screen_hands.stand_aside` | `stand_aside drops always-on-top` fails on clean `248740c` (found in Phase 3, reproduced in a clean worktree). The test also moves GOAT's own live window, so it's now opt-in (`run_tests.py --live`). | Investigate when touching screen_hands. | — | S | FACT |
| B5 | Low | Logging | log `[plan-usage] TimeoutError:` (`goat_app.py:5159-5213`) | The exception is logged with an empty message, so the cause is unknown. | Log `type(e).__name__` plus `repr(e)`. | — | S | FACT |

## Voice

| ID | Severity | Category | File:line | Problem | Fix | Risk | Effort | Basis |
|---|---|---|---|---|---|---|---|---|
| V1 | **High** | Latency (endpoint) | `goat_app.py:2175-2182`, `audio_io.py:62`, `:68`, `:783-788`, `stt_whisper.py:531-534` | In English mode the ear is the local GPU stream. While its Whisper job is **pending**, `_sentence_state` returns False, which forces the **1200 ms** wait instead of 500 ms. The job (large-v3-turbo) starts at 200 ms of silence. **All 14 logged turns** ended at 1152-1216 ms (corrected in Phase 3; Phase 1 said 12 of 13): about 700 ms of dead air per turn. | At the 500 ms mark, judge the last *finished* partial (or wait for the job only up to ~150 ms), then decide. Measure first (Phase 3). | More cut-offs if judged wrong. The 1.5 s merge window is the safety net. | S-M | FACT (code + log) / ASSUMPTION (pending is the cause in every turn; job time isn't logged yet) |
| V2 | Medium | Latency (ear) | log `[latency]` lines | Healthy Live turns: **2.47-2.93 s** from end of speech to first word = endpoint ~1.2 s + ear 0.12-0.88 s + voice 0.59-1.25 s. Where the ear's time goes (queue wait vs inference) isn't measured. | Phase 3: log the ear split. Then consider a small model (base.en q5) for partials only. | — | M | FACT (totals) / ASSUMPTION (split) |
| V3 | Medium | Filler | `goat_app.py:703-708`, `:795`, `:3883-3897`, `:3511-3520`, `:810` | Fillers by design: "Sir, one moment." / "Sir, checking." after 1.5 s or on any hand call, plus a cached "Sir," before every reply. These were your orders of 2026-10-18, but they conflict with "no filler". In the log, fillers fired at 2.3-3.4 s on 4 turns. | Phase 5 decision (options there). | Silence on slow turns. | S | FACT / OPINION |
| V4 | Medium | Utterance split | `goat_app.py:2959-2984`, `:241`, `:246` | Split protection = 1.2 s endpoint + 1.6 s hold on a half-finished line + 1.5 s merge window. The merge only applies **while the voice is still answering** (`answering`, `:2966`). Live answers in ~0.6 s, so a second half that arrives after a short first answer becomes its own turn, after a reply was already spoken. No `[merge]` or split events appear in the current logs, so how often this happens is unknown. | Phase 3: log split candidates (two of your turns less than 3 s apart). Decide after measuring. | — | S | FACT (logic) / ASSUMPTION (frequency) |
| V5 | Low | TTS chunking | `goat_app.py:750-765`, `:1351` | `split_long` cuts sentences over 16 words at commas into separate edge-tts clips. The seams can sound like pauses. Edge/Piper paths only; Live audio is untouched. | Raise to ~24 words for non-first sentences. | Slower synth per clip. | S | FACT / OPINION |
| V6 | Info | Barge-in | log | 6 confirmed interrupts and 1 "echo, not you" in the logs. No evidence either way on **false** barge-ins. | Phase 3 counter: barge followed by no words or by the echo-drop paths. | — | S | FACT |

## Router

| ID | Severity | Category | File:line | Problem | Fix | Risk | Effort | Basis |
|---|---|---|---|---|---|---|---|---|
| R1 | Medium | Misroute | `task_router.py:174`, `:186-206` + log | "No, forget it. I'm going to make it by terminal clock. Thank you" went **BIG**, becoming a deep **Opus 5.5 job at high effort** (log: `routed deep`). Dismissals are only recognised while a job is running (`:174`). | Dismissal pattern (`forget it`, `never mind`, `no thanks`, `thank you`, `I'll do it myself`), so these go to chat. Add a test case. | Few. | S | FACT |
| R2 | Medium | Wasted effort | `goat_app.py:3950`, `:4204-4206`, `:4517-4519` | Every router BIG is sent as `"deep"`, and deep means `hard=True`, which means your full dial (high now; default max). This skips the tiering. "Schedule it for every day" ran at high; so did the voice's `"Giorgi said no, stop…"` hand-off. | Let `task_router` return quick or deep. Quick uses `TASK_EFFORT` (low). | Under-thinking a real job. Keep code and planning on deep. | S | FACT |
| R3 | Info | Mental model | `goat_app.py:5586`, `episodes.py:35` | In two-brains mode the worker is always Fable 5.1, falling back to Opus 5.5. The Sonnet tiers in `brain_route` apply only in one-brain mode. Haiku is used only for the daily episode and reflection calls. | — | — | — | FACT |

## Tokens

| ID | Severity | Category | File:line | Problem | Fix | Risk | Effort | Basis |
|---|---|---|---|---|---|---|---|---|
| T1 | Medium | Observability | `goat_app.py:4218-4239`, `:4774-4780`, `:233` | Each job sends the desktop note (~385 chars), the last 8 voice exchanges (up to ~2.4k chars) and labels into a resumed session that rotates at 44k. Cache read and creation are read but **no hit rate or tokens per job is logged**, so cost can't be judged. | Phase 3: one line per job with input, cache read, cache create and output, plus the running hit rate. | — | S | FACT |
| T2 | Low | Prompt size | `talk_brain.TALK_SYSTEM` (14,244 chars, ~3.5k tokens) | Sent once per Live session (free), but on **every** request of the REST fallback. Per-turn notes are small (clock 37 chars, desktop ~385). | Trim together with Phase 5 (voice style). | Behaviour drift. | S | FACT / OPINION |
| T3 | Info | Already lean | `goat_app.py:5605-5636` | Custom 10.8k-char worker prompt (no claude_code preset), slim tool list, `strict_mcp_config`, 12 own skills (~2.6k chars of descriptions). | — | — | — | FACT |

## Code quality

| ID | Severity | Category | File:line | Problem | Fix | Risk | Effort | Basis |
|---|---|---|---|---|---|---|---|---|
| Q1 | Medium | God files | `goat_app.py` (5.6k lines; `GoatApp` ≈ `:1661-5847`), `ui_qt.py` (6.6k) | Hard to change safely. | No rewrite. Extract only what a batch touches (e.g. `TtsPipeline` into `tts_pipeline.py`). | Import cycles. | L | OPINION |
| Q2 | Low | Hidden state | `goat_app.py` (87× `getattr(self, "_…", default)`) | Attributes are created lazily, which hides state and typos. | Move defaults into `__init__` when a batch touches them. | — | M | FACT |
| Q3 | Low | Duplication | `phone_server.py` (24 local `from aiohttp import web`) | Noise. | One module-level import. | — | S | FACT |
| Q4 | Low | Deps | `python/requirements.txt` | `aiohttp` and `cryptography` (imported at `phone_server.py:144`, `:534`) are missing and only arrive transitively. `comtypes` is listed twice (`:12`, `:23`). No pins except PySide6 and the SDK floor. | Add the two, drop the duplicate, pin `edge-tts==7.3.1` (see Q5). | — | S | FACT |
| Q5 | Low | Fragile dep | `tts_edge.py:267-301` | Imports edge-tts private internals (`drm`, `constants`, `communicate`). An upgrade can break the live socket; it falls back, but more slowly. | Pin the version; test before bumping. | — | S | FACT |
| Q6 | Low | Tests | `python/test_*.py` (~60 scripts) | No single runner. Preflight compiles only `CORE`. Baseline today: preflight PASS; engine_router 136/0; brain_route 44/0; voice_latency 56/0; phone_server ALL PASS; task_router and action_policy OK. | Phase 3: `run_tests.py` for the no-cost suites. | — | S | FACT |
| Q7 | Low | Observability | `python/start-goat-app.vbs` (log rotation) | The log rotates on every launch and keeps one `.old`, so about 290 lines of history: too little to measure anything. | Phase 3: metrics in their own `metrics.jsonl` (gitignored). | — | S | FACT |

## Outcome (Phase 4-6, 2026-11-06)

| ID | Status | Commit / note |
|---|---|---|
| S1, S2, S9 | Fixed | `620a0e8`: 26-char token (old one retired by hash), one per-address limiter on every token check |
| S3 | Fixed (browsers) | `248f3a5`: hashed server-side sessions, 30 days idle, `logout-all`; one-time pairing codes in QR/links. The bubble app still uses the master token (its config secret). |
| S4 | Fixed (app layer) | `306073e`: LAN only via Private or trusted networks. Firewall NOT changed (needs admin): see S12. |
| S5, S6, S7 | Fixed | `fdae7ee`: no shell for model text, `secret_paths`, launchable types refused |
| S8 | Fixed | `b6f7309`: worker tool hook, his one-shot yes for private files |
| S10 | Open, by choice | `.phone/url.txt` keeps the token line (convenience for the app's settings) |
| S12 (new) | **Open, needs admin** | Windows has an *allow all inbound* rule for `python.exe` on **Public** networks (from Python's first run), and the GOAT rule is profile Any. Fix: mark home Wi-Fi Private, GOAT rule → Private, disable the two python.exe Public rules. |
| B1 | Fixed | `2b89fa3`: `_connect_engine` retries forever, boot without engine, no crash-loop raise |
| B2, B3, B5 | Fixed | `087f590` (B2 reproduced on the old code: 1600 stale samples queued after cancel) |
| B4 | Open (low) | Live repeat-failure note not added |
| B6 (new) | Open | `test_screen` "stand_aside drops always-on-top" fails on 248740c |
| V1 | Fixed, **root cause corrected** | The endpoint wait wasn't the cost by itself; the ear's job was. Every laptop job asked Whisper to detect the language first: 1310 ms vs 495 ms pinned, on his clips. That kept the job pending past 500 ms → the 1.2 s wait (56/69 endpoints "local-pending"). `cc517c5`: sticky English. |
| V2 | Measured | Ear job split now logged (`ear` event) |
| V3 | Option A done | `8dafdb1`: filler after 2.5 s (was 1.5). B/C not applied (they override his standing orders). |
| V4 | Fixed | `d4c219e`: a sentence cut by the 30 s cap is held; holds wait while he's still talking |
| V5 | Open (low) | clip splitting at 16 words unchanged |
| R1, R2 | Fixed | `fd277c2` |
| T1 | Measured | Baseline: one job, 148k input tokens over its steps, 89 % cache hit |
| T2 | Open | TALK_SYSTEM 14k chars, untouched (Phase 5 kept the prompt) |
| Q4, Q5, Q6, Q7 | Fixed | `8f04336`, `28f5369` |
| Q1, Q2, Q3 | Open, by choice | No refactors without his OK |
| Q8 (new) | Open (low) | `_pull_plan_usage` reads the meter through a private SDK call (`client._query._send_control_request`); an SDK update can break the meter (it fails quietly) |

## Most urgent
1. **S1 + S2 + S4 together:** the phone token is public, has no rate limit on `?k=`, and the server is reachable from any LAN you're on. Rotating the token (S1) closes the immediate hole by itself and takes minutes.
2. **B1:** a network blip during reconnect leaves GOAT deaf and mute.
3. **V1:** about 700 ms of avoidable wait on almost every English turn.
