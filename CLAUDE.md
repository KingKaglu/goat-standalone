# GOAT — notes for working on this repo

GOAT is Giorgi's personal voice assistant on Windows (PySide6 window, Python 3.13).
`workspace/CLAUDE.md` is GOAT's *own* memory, loaded by GOAT's worker brain; this file is for
working on GOAT's code. Background: `README.md`, `STATE.md` (dev journal), `AUDIT.md` + `PLAN.md`
(2026-11-06 audit and what was done about it).

## How it fits together
- Two brains (default `GOAT_TWO_BRAINS=on`): the **voice** is Gemini Live (`live_talk.py`, REST
  fallback `talk_brain.py`, then Claude covers). The **worker** is Claude via the Agent SDK on his
  Claude Code login (`goat_app.py`, `ClaudeAgentOptions` in `run()`): Fable 5.1 → Opus 5.5, cost =
  his plan usage, not API dollars.
- Turn path: `audio_io` (AEC3, Silero VAD, endpoint) → `_handle_utterance` (ear) → `_heard` (gates,
  merge/hold) → `_talk` → reflex / `task_router` BIG → Claude / `_front_turn` (voice).
- Phone: `phone_server.py` (aiohttp on GOAT's loop, :8797), page `phone.html`, Android bubble app in
  `workspace/goat-bubble` (gitignored; `build.ps1` bakes `.phone/token.txt` into the APK).

## Run and check
- Start: `GOAT.bat`. Restart after a code change: `python\restart-goat.ps1` (preflight, then
  kill + relaunch, watchdog rolls back to `.self-backup/last-good` if the engine never comes up).
  Spawn it detached (`Start-Process powershell ... -File python\restart-goat.ps1`).
- Always `py -3.13` (bare `python` is 3.12 and lacks the deps).
- Tests: `cd python; py -3.13 run_tests.py` — ~40 offline suites, ~2.5 min, no audio/network/API.
  `--qt` window tests, `--slow` test_dsp, `--live` test_screen (moves GOAT's live window).
  Never run `test_voice_e2e`,
  `test_voice_knowledge`, `test_audio_stall`, `test_aec`, `probe_*` casually: real mic/speaker/APIs.
- Metrics: `GOAT_METRICS=on` (set for his user since 2026-11-06) → `python/metrics.jsonl`;
  `py -3.13 metrics.py summary` = time to first word, endpoint/ear/voice split, ear job time,
  Claude tokens + cache hit, false barge-ins, split utterances.

## Rules that bit before
- Commit only the files you changed (`git add <files>`); he may have uncommitted edits of his own.
- Before committing, check the staged diff doesn't contain `.phone/token.txt`'s value. Never commit
  `.goat-secrets.json`, `.phone/`, `voiceid/`, `workspace/`, logs, `metrics*.jsonl`.
- The repo is PUBLIC on GitHub. Don't write tokens, keys, his network names or personal data into
  code, comments, commit messages or docs.
- PowerShell 5.1: commit messages with quotes or `$` → write them to a file without BOM
  (`[IO.File]::WriteAllText(path, msg, [Text.UTF8Encoding]::new($false))`) and `git commit -F`.
- Tests build `GoatApp` / `TtsPipeline` via `__new__`: new attributes need `getattr(self, x, default)`
  on paths the tests touch, or the suites break.
- `test_phone_server.py` runs in a temp `.phone` dir; keep it that way (load_token now *replaces*
  short tokens, a test reading the real one would rotate his login).

## Security model (2026-11-06)
- Phone server: every token check goes through `PhoneServer._check` + `Limiter` (5 wrong / 60 s per
  address, doubling lockout). Browsers get server-side sessions (`.phone/sessions.json`, hashed,
  30 days idle); QR codes/links carry one-time 10-min pairing codes (`phone_qr.py login|apk`);
  the bubble app uses the master token. LAN clients only via a Private or trusted network
  (`LanGate`, `phone_qr.py lan|trust-lan|untrust-lan`, `GOAT_PHONE_LAN=on|off`). Tailscale always.
- `secret_paths.is_secret()` is the one list of private files. The voice's hands refuse them;
  the worker's tools need his one-shot yes (`_secret_guard_hook`, via `close_guard`).
- No `shell=True` with model-written text (Windows `cmd.exe` parses quotes and `&`).

## Switches added in the audit
`GOAT_METRICS`, `GOAT_PHONE_LAN`, `GOAT_STT_EN_STICKY` (skip the slow language check on confident
English turns), `GOAT_BACKCHANNEL_AFTER` (default 2.5 s).
