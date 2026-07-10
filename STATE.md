# GOAT State — handoff brief

Updated: 2026-07-10 evening (token economy v2; skill library wave; typed input + themes; GitHub publish)

## Georgian mode (2026-07-10 evening — Giorgi: "do I have option to choose Georgian in UI?")
Settings drawer got a language row (english / ქართული), persisted as
cfg["lang"], applied at boot (persona LANG_NOTE_KA + Eka voice) and live
(set_language: tts_edge voice swap + one steering turn so the brain
confirms in Georgian).
- Voice OUT: ka-GE-EkaNeural (edge-tts VOICES dict) — verified, speaks
  Georgian beautifully.
- Voice IN: **stays English.** MEASURED tonight on this CPU: whisper base
  multilingual transcribes Georgian into LATIN transliteration; small
  multilingual (482MB, downloaded, kept in stt\) loops/hallucinates at
  13-25s per phrase, greedy or beam. Local Georgian STT = unusable. Typed
  Georgian works (input line is unicode). GOAT_STT_KA=on re-enables the
  experimental path (stt_whisper.set_language restarts server w/ -l ka on
  ggml-small.bin) for a future better model/machine. ggml-base.bin (multi)
  deleted — proven useless.
- WAKE_RE got Georgian garble variants (გოატ|გოუთ|გოთ|ღოატ).
- Boot greeting stays English even in ka mode (code constant) — known nit.

## Continuity + power watcher (2026-07-10 evening — Giorgi: "plan improvements, execute step by step; push latest with new guidelines")
- **On-screen continuity**: engine logs every finished exchange to
  workspace\transcript.jsonl (_log_exchange: 400-line trim at 200KB, never
  breaks a turn; boot-briefings skipped). UI _load_transcript_tail() repaints
  last 6 dimmed at boot; epigraph only when there's no history.
- **Power watcher (first JARVIS watcher)**: power_verdict() pure function
  (AC drop → "check the jack", ≤20% discharging → warn, back-on-AC silent,
  unreadable silent) + _power_watch() task: 45s poll via WMI in a worker
  thread, 5-min alert rate limit, speaks only when idle (status line
  otherwise). GOAT_WATCH=off disables. Targets THIS laptop's real AC-flap
  fault. Tests: 19/19 (5 verdict cases + transcript round-trip); live
  battery read verified.
- README refreshed EN+KA: settings drawer, watchers, continuity, token
  economy, first-class typing, test-suite instructions.

## UI ceremony round + MAJOR layout bug (2026-07-10 evening — Giorgi: "next level, think like a magician designer")
New life, same instrument law (f5deea8, live):
- Boot ignition: string lights left→right over 1.6s (StringLine.ignite(),
  clip-rect + opacity ramp in paintEvent; called after showFullScreen).
- Clock (HH:MM) in the top bar — fullscreen hides the system tray clock.
- Empty-state epigraph "Say the word." — deleted on first exchange.
- TopFade lip: old lines dissolve under the string. TRAP: it must sample the
  backdrop gradient AT ITS OWN Y-POSITION (bg_top over a darker region reads
  as a grey band) — set_frac() mixes bg_top→bg_bot by screen fraction.
- Settings drawer slides in (QPropertyAnimation geometry, 170ms, OutCubic).
- **MAJOR BUG FOUND VIA RENDER REVIEW**: word-wrapped QLabels report ~zero
  minimumSizeHint in QVBoxLayout → once the page outgrew the viewport the
  scroll area COMPRESSED old lines to 0-3px slivers (history looked deleted;
  scrollbar max stayed 0). Present since v5 design in ALL long conversations.
  FIX: PageLabel(QLabel) overrides minimumSizeHint → heightForWidth(width).
  Verified: 12 exchanges → scrollbar max 509, all labels full height.

## UI review round (2026-07-10 evening — Giorgi: "see it yourself, find bugs/improvements")
Rendered the UI offscreen (QT_QPA_PLATFORM=offscreen + QT_QPA_FONTDIR=
C:\Windows\Fonts for real type; fake conversation; all themes ± panel) and
READ the screenshots. Fixed what showed (273b6aa, live):
- Panel covered the title bar → starts at _titlebar_h, controls reachable.
- "⚙" renders as a COLOR emoji in Segoe → replaced with monochrome "≡".
- Paper theme: string_base + faint darkened (were nearly invisible).
- Scroll viewport + host autoFillBackground(False) — banding insurance.
- NEW: window on-top toggle (persisted "ontop", flag re-applied at boot;
  setWindowFlag hides the window — must re-show right after) and
  copy-last-reply action (walks col backwards for last reply label).
LESSON: offscreen render + Read of the PNG = real design review loop; fonts
need QT_QPA_FONTDIR or everything is tofu.

## Settings drawer (2026-07-10 evening — Giorgi: "more UI/UX options to act on things and change them about GOAT or UI")
Gear button (top bar) or Ctrl+, opens a quiet right drawer (SettingsPanel in
ui_qt.py, instrument style: typography, one accent, no chrome):
- theme (4 named options), text size small/normal/large (replyNow px),
  voice on/off (TtsPipeline.enabled — off = zero-length segments, text
  reveals instantly via the UNSPEAKABLE path), voice level quiet/normal/loud
  (TtsPipeline.gain, np.clip), wake word on/off, mic live/muted
  (GoatApp.mic_muted gates _on_utterance AND _on_interrupt; footer shows it),
  new chat (deletes .goat-session-py + gated restart) and restart buttons.
- All prefs persist in ui-config.json (load_ui_config/save_ui_config,
  validated w/ defaults); bind_engine() pushes them into the engine at boot.
- Esc order: panel → input → fullscreen. TESTED offscreen: every setter
  round-trips engine flag + cfg + JSON, reload pushes saved prefs into a
  fresh engine; preflight + 13/13 engine tests still green. Commit 9a348aa.

## Max test sweep (2026-07-10 evening — Giorgi: "test it to the max, fix what comes up")
- **test_engine_router.py** (permanent, free to run: mock client+TTS, no API):
  13/13 — fast/full routing, sticky-full, handoff prefix, /compact success
  AND failure→rotation fallback, stop-order brake, front-desk routing,
  talk-turn steering, STOP/WAKE/WORK regexes. Run it after ANY engine edit.
- Behavioral round 2 (headless SDK session): system-health ✓, stt-teach ✓
  (writes+validates fixes json), screen-look ✓ (described the real screen),
  self-upgrade ✓ (recited procedure, touched nothing), identity ✓ (said
  "Fable 5" on untagged harness message — correct per MODEL TRUTH; harness
  doesn't tag, real app does — artifact, not bug).
- UI offscreen: you→delta→tool→reveal→dim flow, usage footer, limit path ✓.
- windows-control volume keys executed for real (net zero) ✓.
- BUG FOUND+FIXED: system-health called 7.1/7.6GB RAM (93%) "fine" —
  skill now has HARD RULE: ≥85% = warning + name the top hog. Loads next
  session reset.
- Test hygiene: stt-fixes marker removed (json re-validated), memory marker
  removed.

## Headless skill E2E + memory-law fix (2026-07-10 evening — Giorgi: "test it yourself, no voice")
Harness: fresh SDK session w/ GOAT's exact options (persona, workspace,
setting_sources=["project"], NO resume — live app untouched), typed queries.
- PASS file-map: Skill→Read map→exact path+context, no disk hunt.
- PASS briefing: Skill→probes→perfect 3-sentence JARVIS brief w/ ## Open items.
- CAUGHT BUG remember: fact went to the harness's own auto-memory dir
  (~\.claude\projects\...\memory) — the claude_code PRESET's memory directive
  beat the skill; briefings read workspace\memory.md → split-brain memory.
  FIX: PERSONA "MEMORY LAW" (only memory = workspace/memory.md, never the
  harness dir). RETESTED: Skill→Read→Edit into memory.md, correct. Test
  artifacts cleaned both sides. — Giorgi: "give GOAT ability to find file locations without going through all files")
- New skill **file-map** + seeded gazetteer at workspace\file-map.md: curated
  one-line index of the machine (GOAT layout, all his projects w/ real paths
  verified on disk, infra ports, broken-ensurepip gotcha). Rule: read map
  FIRST, search cheap-to-expensive (Glob scoped → Grep filtered → Read
  offset/limit), RECORD hard-won findings back into the map.
- Loads next session reset (app restarted 17:30 — skill written after, so
  one more session reset needed).
Investigated the SDK itself (0.2.114) instead of assuming:
- ClaudeSDKClient.get_context_usage() = exact context breakdown (same as
  /context). MEASURED: autocompact IS enabled in SDK sessions but threshold
  ~934k on a ~1M window — crash protection, useless for cost. Our 60k trim
  stands.
- Rotation upgraded: at ROTATE_CTX the engine now sends "/compact" (muted
  turn: suppressed=True, busy=True, _compacting flag) — model-written
  summary, SAME session, far richer than the 8-exchange handoff. Its
  ResultMessage is verified with get_context_usage(); if context didn't
  shrink, hard-rotate with handoff as before. GOAT_COMPACT=off env kills it.
- Receptionist: effort="low" (SDK effort→CLI --effort; levels
  low/medium/high/xhigh/max) — 1-3 sentence front-desk answers don't need
  deep thinking; cheaper and snappier.
- NOT YET LIVE-TESTED: /compact path needs a real >60k session (and usage
  reset — limit was hit during this work, resets 19:40). Fallback covers
  every failure shape.

## Token economy v2 (2026-07-10 evening — Giorgi: "GOAT on Fable burns way more than Claude Code doing the same work")
Root causes + fixes, all in goat_app.py (constants WORK_RE / STICKY_FULL_CTX=25k / ROTATE_CTX=60k / HANDOFF_KEEP=8):
- Escalation double-pass: obvious work verbs (WORK_RE) now route STRAIGHT to
  the full model — no more fast-model read-everything-say-ESCALATE pass.
  Misses still escalate the old way.
- Per-model prompt-cache thrash: every fast<->full switch re-cached the whole
  history (cache_creation) on the other model. Past 25k context the session
  stays on Fable even for chat (warm cache read ≪ re-cache on Sonnet).
- Unbounded session growth: at 60k context the session rotates proactively at
  turn end; next message carries a [context-handoff] block built from the
  last 8 exchanges kept in Python (zero API cost). Wall-hit ("prompt too
  long") path now ALSO gets the handoff — used to wake with amnesia.
- PERSONA: [context-handoff] rule (absorb silently, never mention).
PREFLIGHT PASS + unit checks (WORK_RE hit/miss lists, handoff builder).

## Skill library wave (2026-07-10 evening — Giorgi: "plan out the skills needed to be better and execute")
Six new skills in workspace\.claude\skills (live from the NEXT session — say
"restart GOAT" to load them):
- **remember** — long-term memory at workspace\memory.md (seeded: Open items,
  Giorgi, Machine sections); save on "remember that", recall on "what did I
  tell you"; morning-briefing reads ## Open.
- **self-upgrade** — the ONLY sanctioned way to edit own code: STATE.md first,
  edit, self_check preflight (never restart on FAIL), confirm with Giorgi
  before restart-goat.ps1, rollback if bad, record lesson in STATE.md.
- **system-health** — battery/AC/disk/RAM/hog probes + this machine's known
  traps (AC flap, 56% battery, QLC C:, 8GB RAM). Probes verified working.
- **windows-control** — exact SendKeys volume/media codes, app open/close,
  clipboard, brightness, lock; confirm-first on kills/lock/power.
- **stt-teach** — stt-fixes.json pair format + mandatory JSON validation
  after write (broken JSON silently kills ALL fixes).
- **morning-briefing** — 3-4 spoken sentences: greeting, ## Open items,
  vitals only-if-alarming; distinct from tool-less [boot-briefing].

## UI: permanent input + themes (2026-07-10 evening — Giorgi: "no option to type, only talking; improve UI, theme setting")
- ROOT CAUSE he couldn't type: the command field was HIDDEN by design
  (voice-first), summoned only by Ctrl+K or "/" — undiscoverable; worse, the
  app-wide "/" QShortcut made typing a literal "/" in the field impossible.
- Fix in ui_qt.py: input field now ALWAYS visible (bare underline at bottom,
  placeholder "speak — or type here, enter to send"); Ctrl+K focuses it;
  Esc clears+unfocuses (then leaves fullscreen); "/" shortcut REMOVED;
  submit/file-note no longer hide the field.
- Theme system: THEMES dict (ember=original amber, paper=light editorial
  vermilion, phosphor=green instrument, graphite=mono white-hot), one accent
  each — design law kept. build_style() generates the stylesheet; Backdrop +
  StringLine got set_theme(). Cycle via top-bar button (shows current name)
  or Ctrl+T; persisted in ui-config.json at root (cosmetic — load/save never
  raise). PREFLIGHT PASS + offscreen Qt smoke test (cycle, persist, submit).

## Published to GitHub (2026-07-10)
- Public repo https://github.com/KingKaglu/goat-standalone (branch master).
- Models/binaries (stt/bin, *.bin, tts/piper, *.onnx) are GITIGNORED — 636MB
  exceeded GitHub's 100MB blob limit; history was rebuilt via orphan branch.
  NEVER commit those paths; README documents where users download them.
- README is bilingual EN+KA: how it works, install, usage, "Can you run it?"
  (others need their own Claude Code login; PERSONA/SEED_VOCAB are personal).

## Front desk / talk-while-working (2026-07-10 midday — Giorgi: "JARVIS talks to Tony while finishing background work; leave Sonnet talking while Fable does the job")
Phase 3.5 receptionist PORTED to Python. PREFLIGHT PASS; NOT yet live-tested
(needs a real session: audio + login) — verify first real work-turn chat.
- Second ClaudeSDKClient `recep` on MODEL_FAST (Sonnet 5), RECEP_PERSONA
  (GOAT front-of-house: 1-3 spoken sentences, no tools, FORWARD keyword for
  heavy asks, identity absolute), max_turns=1, setting_sources=[], fresh each
  run, pre-warmed at boot (background task, optional — failure ≠ deaf).
- Routing in _send_user busy path: (1) STOP_RE + ≤5 words ("stop/cancel/
  abort/hold on/never mind/forget it") → interrupt main turn, speak
  "Stopped."; (2) work turn (_turn_has_tools) → _receptionist_answer(): sends
  "[main-status] working on/current step/elapsed" + his message, speaks reply
  via mark_reply + sentence split; recep usage folded into _track_usage;
  (3) FORWARD/empty/recep-busy/recep-dead → old steering (client.query into
  in-flight turn, last_user_text appended). Talk turns (no tools yet) steer
  directly as before.
- Barge-in split in _on_interrupt: busy + _turn_has_tools → cancel VOICE only,
  work continues ("listening — work continues"); talking turns keep old
  full-interrupt. _turn_has_tools set on first ToolUseBlock, reset each fresh
  turn + escalation; _last_tool/_current_task/_work_started feed the status.
- PERSONA: two-brains section told about the front desk (mid-turn arrivals
  stay priority one).

## Model router v2 (2026-07-10 midday — Giorgi: "use models wisely; haiku is dumb af and kept lying")
PREFLIGHT PASS; applies next launch.
- Talking model Haiku 4.5 → Sonnet 5 (MODEL_FAST="claude-sonnet-5"); Fable 5
  stays the working brain. MODEL_NAMES/footer + ui_qt default label + STT
  vocab updated (Haiku→Sonnet).
- PERSONA "MODEL ROUTING" → "YOUR TWO BRAINS": self-aware routing. GOAT knows
  the [fast-turn] tag = talking brain, untagged = working brain; never
  escalate "to be safe" or to sound smarter; de-escalation automatic (every
  fresh turn resets to talking brain — already true in code, now she knows).
  MODEL TRUTH mapping updated: tagged = sonnet 5, untagged = fable 5.

## JARVIS wave 3 (2026-07-10 midday — Giorgi's order: full laptop access, self-growing skills, full web)
PREFLIGHT PASS; applies next launch.
- setting_sources=[] → ["project"] in goat_app.py: loads ONLY workspace\.claude
  (GOAT's own skill library). Giorgi's global plugins/hooks still excluded —
  the latency win of [] is preserved.
- Skill library live at workspace\.claude\skills\: skill-creator (meta-skill,
  exact SKILL.md format + rules: one job per skill, trigger-first description,
  fix-or-delete lying skills, no secrets) + screen-look (full virtual-screen
  PowerShell capture → inbox\screen.png → Read; fullscreen-game + own-window
  failure modes). New skills go live at next session start.
- PERSONA: FULL ACCESS section (whole machine, not just workspace; web =
  WebSearch/WebFetch freely, check-don't-recall) + SKILLS section (self-grant
  silently when a procedure repeats, Giorgi-grant on "learn this as a skill",
  one-line confirm, activates next session, keep library clean).

## JARVIS wave 2 (2026-07-10 midday — Giorgi's order: "more JARVIS in every possible way, a whole other AI just powered by Claude")
All in goat_app.py, PREFLIGHT PASS verified; needs an app restart to apply.
- PERSONA v2: new CHARACTER section (calm under fire, dry deadpan wit, signature
  acks "On it"/"Consider it done", situational awareness, confidence-with-receipts).
  IDENTITY hardened: GOAT is its own AI; never mention Claude/Anthropic/models
  unprompted; "Claude does my heavy reasoning — but you're talking to GOAT."
  MODEL TRUTH rule unchanged. All prior standing orders preserved verbatim.
- Boot greeting: time-of-day aware (_greeting(), replaces static GREETING).
- Wake word PORTED from Node: idle >120s → voice input must contain a name
  variant (WAKE_RE: goat/goad/ghost/god/coat/go at...); typed input, mid-task
  interjections, and TTS-playing barge-ins never gated; GOAT_WAKE=off disables.
  _last_exchange updated on every send + turn end; boot opens the window.
- Boot briefing PORTED from Node: away ≥6h (old .goat-session-py mtime, read
  before this boot's init overwrites it) → app self-sends "[boot-briefing]" on
  the fast model (no tools, no ESCALATE — persona has the handling rule).
- Phase 4 hands seeded via PERSONA MACHINE CONTROL: act on voice for app/
  volume/media/process/file control; "look at my screen" = self-screenshot to
  inbox/screen.png + Read. Protect-him rule still gates destructive actions.

## Standalone move + launcher fix (2026-07-10)
Home is now C:\Users\user\goat-standalone (this folder). The old C:\Users\user\goat
is frozen — never read/write stt-fixes.json or STATE.md there again (learned STT
fixes were landing in the old dir and never applying; merged back, paths fixed in
PERSONA + goat_doctor). Desktop GOAT.lnk + root GOAT.bat/start-goat.vbs now all
launch python\start-goat-app.vbs (they were stale Node-app launchers — the reason
Giorgi "couldn't run" the app).

## Self-edit safety net (2026-07-10 — Giorgi's order: a self-edit can never brick GOAT)
- python\self_check.py: `preflight` (compile + fresh-subprocess import of the whole
  stack + assets + stt-fixes parse; must print PREFLIGHT PASS before any restart),
  `snapshot`, `rollback`.
- Every successful boot auto-snapshots the running code to
  python\.self-backup\last-good (wired into goat_app.run()).
- restart-goat.ps1: runs preflight FIRST and aborts the restart (app left alive) on
  FAIL; after relaunch, a 45s watchdog auto-restores last-good and relaunches if the
  fresh instance dies at boot. Full protocol is in PERSONA (SELF-EDIT SAFETY).
- PERSONA also gained WORK STANDARD: verify before claiming done, ground truth over
  recall, act-don't-ask on safe steps, token economy, root-cause + record lessons here.

## Usage watch + file attachments (2026-07-09, NOT yet tested by Giorgi)
His asks: (a) "tell me in our application when I'm out of my usage" — he ran
dry mid-session without noticing; (b) "add so I can send you images or files."
- goat_app.py `_track_usage()` (called on every ResultMessage): accumulates
  session in/out tokens (in = input + cache_creation), emits "usage" event
  "in|out"; detects quota exhaustion (is_error + limit/quota keywords in
  msg.result), parses the `|<epoch>` reset stamp if present, then emits
  "limit" + SPEAKS "Giorgi, we're out of Claude usage. It resets around
  HH:MM." once (_limit_warned gates repeats). On limit the turn ends
  cleanly — no escalation retry.
- ui_qt.py: footer now shows "· 12k in / 3k out" (_fmt_tok); "limit" event
  prints the warning as an amber line + stateword "out of usage".
- Attachments: drag-drop anywhere on the window, Ctrl+O file picker, or
  Ctrl+V with an image on the clipboard (saved to C:\Users\user\goat\inbox\
  clip-*.png). ui_qt._send_files → goat.submit_files(paths, note) — note is
  whatever's typed-but-unsent in the input field. Message format:
  "[files from Giorgi]\n<abs paths>", PERSONA has an ATTACHMENTS section
  (open with Read; if no note, describe briefly). File turns SKIP the fast
  router (`_send_user(force_full=True, echo=False)` — Read tool needed, a
  fast turn would just burn an ESCALATE round trip).
- Same session, earlier: PERSONA STATE.md load made lazy (read only when
  needed, not at boot) + router escalation tightened (answer by default,
  escalate only when tools are required). All compile-checked; awaiting his
  restart to test live.

## Desktop app (2026-07-09, Giorgi's explicit order — supersedes browser UI)
His words: "I don't want anything to do with the browser... an application
based on Python... do not touch the old code." Also: Fable 5 as the brain,
but KEEP the model-switching tricks (fast/full router, same as Node app).
Built in C:\Users\user\goat\python\:
- `goat_app.py` — full pipeline: DuplexAudio (WebRTC AEC3, working) → whisper
  STT → ClaudeSDKClient (Fable 5 full / Haiku fast, "[fast-turn]"+ESCALATE
  router ported from server.js incl. delta-hold gate) → sentence-streamed
  TTS. Barge-in calls client.interrupt() + suppresses killed turn's output.
  Cold-start AEC warm-up = spoken boot greeting with warming_up=True.
  Own session file `.goat-session-py` (never fights Node's).
- `tts_edge.py` — Ava voice (en-US-AvaMultilingualNeural, edge-tts pkg,
  +10% rate, mp3→soundfile→16k float32). VERIFIED standalone. Piper (Alan)
  = per-sentence fallback when offline (TtsPipeline.synth).
- `stt_whisper.py` — reuses/spawns whisper-server.exe on 3779, ports
  server.js junk filter + stt-fixes.json. `shutdown()` added.
- `ui_qt.py` — v3 FULL SCI-FI HUD (Giorgi rejected v2 as "literally the
  same, just a little different"; asked for "way more sci-fi", max effort).
  Whole window is a HUD scene: hex-traced grid + corner brackets + frame
  cuts + vignette (pre-rendered pixmap, rebuilt on resize) + slow scanning
  light band. Core: plasma disc w/ conical shimmer + lens-glint cross,
  dashed rotating targeting reticle, 2 counter-rotating arc rings, 96-bar
  audio-history ring orbiting it, 72-tick halo w/ sweeping highlight,
  3 orbiting satellites w/ fading trails, boot draw-in (intro 0→1).
  State-driven (idle/listening/thinking=white-hot fast/speaking=pulses w/
  real speaker envelope — audio_io gained `out_level`). Status text is a
  DecodeLabel (cipher-scramble resolve on change). Monospace readout strip:
  CORE state / 12-seg MicMeter / UPTIME. Bubbles now angular (3px radius,
  accent edge: YOU right cyan-edge, GOAT left teal-edge) + ⟐ tool chips.
  Frameless drag/min/close/size grip; taskbar identity + goat.ico.
  Offscreen render VERIFIED and looks properly cinematic (fonts boxy
  offscreen only — artifact). Qt main + asyncio daemon thread, Signal
  crossing.
  v3.1: launches FULLSCREEN (his ask), ⛶ titlebar toggle + F11 + Esc-to-
  windowed; drag disabled in fullscreen; body text width capped 900px.
  v3.2: he saw v3 live, rejected the bubbles ("don't like this chat style,
  want more advanced") → conversation area is now a MISSION-LOG FEED:
  full-width entries with 2px colored data rail (cyan=YOU, teal=GOAT),
  mono headers "▸ HH:MM:SS · YOU" / "◂ HH:MM:SS · GOAT", body text under,
  tools as dim "⟐ HH:MM:SS EXEC · NAME" lines. _add_entry/_add_exec
  replaced _add_row/_bubble. Offscreen render VERIFIED. Awaiting his look.
- `start-goat-app.vbs` + desktop `GOAT.lnk` REPOINTED to this app
  (pythonw, silent, logs to python\goat-app.log, focuses existing window
  if already running). Old start-goat.vbs (browser flow, has Giorgi's own
  autoplay/profile edits) left intact in goat root.
- Brain link smoke-tested (SDK connect/query/reply OK). NOT yet run
  end-to-end with real mic/speakers — awaiting Giorgi's first launch.
  CAUTION: two GOATs can hear him at once if a Claude Code voice session
  is live while the app runs — one mic, two listeners.
- STANDALONE EXE ATTEMPT — ABANDONED (2026-07-09): Giorgi asked to
  "footprint" the app; PyInstaller onefile built (448MB) but was a swamp:
  onefile+torch unpacks to temp every launch (minutes of dead silence),
  console=False = GUI subsystem = same WinError 50 SDK-subprocess bug as
  pythonw (the planned std-handle shim was never written — got interrupted),
  and silero_vad model data likely not collected. He waited 20 min, nothing.
  Reverted fully: exe, dist/, build/, goat.spec, "GOAT Standalone.lnk" all
  deleted; goat_paths.py (frozen-path helper) kept — harmless, all modules
  now import GOAT_ROOT from it. If retried later: onedir (not onefile),
  console=True or a SetStdHandle shim before SDK connect, --collect-all
  silero_vad/torch/livekit/sounddevice, and test the exe before handing it
  over. The vbs launcher (GOAT.lnk) remains THE way to start the app.
- LAUNCH BUG #1 (his first try, FIXED, refix-verified in launcher-identical
  env): pythonw can't spawn the SDK's CLI subprocess (WinError 50
  DuplicateHandle on std handles) → engine thread died at boot, window
  stayed up looking dead ("Event loop is closed" on every typed input).
  Fix: start-goat-app.vbs now runs python.exe via hidden cmd console
  (window style 0) instead of pythonw; single-instance check matches both
  exe names; ui_qt engine thread now catches crashes and posts
  "engine crashed — check python\goat-app.log" to the status line;
  submit_text guards a dead loop. Verified: SDK spawn passes under
  Start-Process hidden detached cmd + python.exe ("SPAWN OK").

## Hearing upgrade + auto-learning (2026-07-09, his order: "keeps
## automatically learning the way I say things, nothing slips out")
- stt_whisper.py: OWN server now, port 3781 (never reuses Node's 3779),
  base.en + beam search 5 + vocabulary prompt (SEED_VOCAB + every
  stt-fixes.json value folded in at server start) + --carry-initial-prompt.
  MEASURED: 1.2s/phrase, all test phrases word-perfect incl "Fable 5",
  "GOAT", "echo cancellation" (piper-synth → transcribe loop). small.en
  downloaded to stt/ggml-small.en.bin but NOT default: 3.9s/phrase at any
  beam, no accuracy gain over base+vocab. GOAT_STT_MODEL=small to opt in.
- goat_app.py PERSONA: STT SELF-LEARNING section — app brain silently
  merges confident mishearing→meaning pairs into stt-fixes.json
  (read-modify-write, only stable recurring patterns) + keeps the
  giorgi-prompting-patterns memory current. Fixes apply to every future
  transcript immediately (re-read per call) and sharpen raw recognition
  at next server start via the vocab prompt.
- stt-fixes.json seeded with this session's real garbles: faber/femu/
  fibu/fable fine/fable fiverr/freeboo fire→Fable 5, eva mulklingu→Ava
  Multilingual, glowed code→Claude Code, keeot/keeo going→keep going.
- v5 UI ("instrument not spaceship") — he said "I think I like it." KEEP
  THIS DIRECTION: warm near-black, paper type, amber accent, the breathing
  string of light, no boxes/panels/cyan. Don't regress to HUD slop.

## Word-sync text reveal (2026-07-09, his ask: text must appear WITH the
## voice, word for word — not full text first, then narration)
Reply text on screen is no longer driven by the LLM stream (which races
ahead of the voice). Now: audio_io counts `played_samples` (real samples
sent to speaker, in _callback); TtsPipeline registers each spoken chunk's
sample span on that clock (`_register`, `_segments`), `spoken_text()`
returns everything the voice has reached — char-weighted word reveal
inside the currently-playing sentence. UI polls it in the 33ms tick
(`update_spoken`); "delta" events now only create the empty label.
Unspeakable chunks (code/paths, UNSPEAKABLE_RE) and failed synths register
zero-length so text still appears in order. `new_turn()` resets per user
turn; `cancel()` trims the reveal to where the voice stopped and resyncs
the queue clock. VERIFIED by simulation (fake clock → correct word-by-word
output across sentence boundaries).
BUG on his first live run: NO text at all. Cause: ui_qt "turn_done" (and
"tool") handlers nulled _reply_label — but the LLM turn finishes before
the voice even starts for short replies, so update_spoken() had no label
to write into, ever. Fixed: only a new "you" resets _reply_label; one
label per turn (spoken_text is cumulative — second label would duplicate
text after tool calls). Repro'd + verified fixed in offscreen sim
(you→delta→turn_done→reveal). Awaiting his restart test.

## (Older same-day Node-app work below — kept for reference; Giorgi has
## since said he doesn't want to run the browser app at all.)

## Conversation-feel fix (2026-07-09, Giorgi's complaint, NOT yet verified)
"Does not feel like a conversation — you don't stop, I can't interrupt without
yelling." Two-part fix in the Node app:
- server.js PERSONA: TTS section rewritten — default reply is 1-2 short
  sentences, one thought per turn, then yield the floor; long detail only on
  explicit ask, led by a one-line version. (Applies on next session start;
  persistent resumed session may need a reset to pick up the new system prompt.)
- public/index.html barge-in easier at normal volume: BARGE_NEEDED 4→3
  (~0.25s of voice), BLEED_MARGIN 1.6→1.35, and bleedGain learning slowed 4x
  while mic is over the bar (double-talk was inflating the bar mid-interrupt —
  the longer he talked over GOAT, the harder cutting in got). Self-interrupt
  regression is the risk to watch on his next test.

## Model router built (2026-07-09, Giorgi's order, NOT yet verified live)
Casual talk must burn cheap usage; real work gets the full model. server.js:
- MODEL_FAST=claude-haiku-4-5-20251001, MODEL_FULL=claude-fable-5. Main
  session now OPENS on fast (warm-up ping is cheap too).
- runTurn(text, {model}): every fresh turn defaults to fast, tags the
  message "[fast-turn]", switches via q.setModel() (verified in sdk.d.ts
  0.3.203). Mid-turn steering keeps the in-flight model and appends to
  lastUserText so an escalation re-run sees all messages.
- PERSONA has a MODEL ROUTING section: on a [fast-turn] message, pure
  conversation → answer; anything needing tools/work → reply exactly
  "ESCALATE", no tools.
- ESCALATE flow: assistant-text handler catches it (never broadcast/spoken;
  delta gate `holdDeltas` buffers streaming until text can't be "ESCALATE"),
  sets escalatePending; on that turn's result → no "done", status
  "switching to the full model", runTurn(lastUserText, {model: MODEL_FULL}).
  escalatePending is reset on every fresh runTurn (interrupts can orphan it).
- maybeRecycle() handoff brief now forced to MODEL_FULL (it's next session's
  memory). Receptionist unchanged (already Haiku).
- Known accepted degradation: if setModel fails, the message runs untagged
  on whatever model is active (logged [router] setModel failed).
- Python app (goat_app.py) does NOT have the router yet — port it
  (client.set_model) once the Node version proves out.
- UI model badge (Giorgi asked to SEE the live model): header pill
  #modelBadge — dim "HAIKU" on fast, amber "FABLE 5" on full. Server
  broadcasts {type:"model"} via announceModel() on every fresh runTurn and
  sends the current model to each new WS connection.

## Boot greeting + silent-voice fix (2026-07-09, NOT yet verified by Giorgi)
Giorgi's spec: on open he hears ONE warm canned greeting instantly ("Good
evening, Giorgi. How are you?" style) — no click, no time/date, no status
recap, no LLM round trip. Changes:
- server.js: BRIEF_PROMPT/briefPending/BRIEF_FILE deleted; `bootGreeting()`
  (time-of-day-aware canned line, random tail) broadcast on WS connect and
  on session reset, same 8s debounce.
- public/index.html: audio unlock now PROBES autoplay at load (AudioContext
  resume test) — unlocks immediately when the browser allows gesture-free
  sound, shows the tap hint only when refused.
- start-goat.vbs + GOAT.bat: launch Chrome with
  `--autoplay-policy=no-user-gesture-required --app=... --user-data-dir=
  goat\.chrome-goat` (own profile dir forces a new Chrome process — flags
  are ignored if the window joins an existing Chrome). FIRST launch of the
  new profile will ask mic permission once; remembered after.
- Mid-session Giorgi reported "can't even see your text" — page likely lost
  WS connection during the work; he was told to restart GOAT. If text-loss
  recurs after restart, investigate separately (not explained by these edits).
- `node --check server.js` passed. Awaiting his restart + fresh launch via
  start-goat.vbs to confirm: instant spoken greeting, zero clicks.
- Voice status: /tts endpoint verified working this session (POST → 200,
  audio/mpeg, Edge Ava voice) — silence was purely browser-side autoplay lock.

## Who / standing orders
Giorgi (KingKaglu) — talks by voice (garbled whisper STT; decode intent from
context, never correct his wording or spelling). Standing orders:
- No approval needed for obviously-required work — just do it. Large/risky
  architecture forks (rewrites, dropping existing UI, new native deps) still
  get a plan + explicit go-ahead first — but once he's approved the plan,
  don't stop for approval on the routine sub-decisions within it.
- INTERRUPTIONS ARE PRIORITY ONE: answer his new message first, in the very
  next output, before resuming whatever was in progress.
- Don't declare something "fixed" from reasoning alone — I have no ears, no
  browser-automation tool. Every "fixed" claim this session that wasn't
  backed by his actual test run turned out wrong or incomplete. Ship the
  fix, explain the reasoning, then wait for his real test before claiming
  success. He is willing to test repeatedly and paste full console logs —
  use that data, don't re-guess blind.
- Boot briefings and check-ins: ONE short line, no date/time, no recap
  unless asked directly.
- During task work: no step-by-step narration. Work silently, one
  consolidated summary when done. Still answer interruptions immediately.
- "Do not track anything" — don't use TaskCreate/TaskUpdate for this work,
  and don't persist tuning state (bleedGain/noise floor/AEC coefficients)
  across restarts via localStorage/disk — recalibrate/re-warm-up fresh
  every run instead.
- Silent-maintenance handoff prompts (like this one): overwrite this file,
  reply with exactly "saved" — no other text.

## Voice switch (2026-07-09, Giorgi's request)
server.js `/tts` now tries Microsoft "Ava Multilingual" neural voice first
(`en-US-AvaMultilingualNeural` via `msedge-tts` npm pkg, Edge Read Aloud
API, online, mp3, rate 1.1) with resident Piper (Alan) kept as offline
fallback. Edge websocket reused across sentences, rebuilt on any failure.
Verified synthesis works standalone; NOT yet confirmed by Giorgi in the
live app. Python rewrite's `tts_piper.py` still uses Alan — switch it too
once the Node side is confirmed.

## Old app (C:\Users\user\goat) — Node/Express + browser UI — still live, mostly untouched
server.js + public/index.html. Node/Express + WebSocket server driving a
persistent Claude Agent SDK session (@anthropic-ai/claude-agent-sdk),
talked to via a dark JARVIS-style browser UI. Resident Piper neural TTS
(tts/piper), resident whisper.cpp STT (stt/bin), receptionist sidecar,
session persists across server restarts. NOT being touched during the
Python migration — stays live as fallback until the Python replacement
works end-to-end.

## Python rewrite (C:\Users\user\goat\python\) — Giorgi's explicit, detailed spec, approved, in progress
Full spec: shared duplex audio pipeline, real echo cancellation with a
time-aligned reference, Silero VAD replacing RMS thresholds, is_tts_playing
+ asymmetric interrupt timing (300ms while speaking / 100ms while quiet),
volume ducking before a full interrupt, asyncio-structured app eventually
(audio I/O / VAD / STT / Claude streaming / TTS as separate tasks),
sentence-by-sentence TTS streaming, clean cancel-in-flight on interrupt.
Explicit instruction: work incrementally — echo cancellation verified
FIRST, then build the rest on top. Keep whisper.cpp + Piper (confirmed
fine — subprocess-based, no different from Node).

**Two research findings resolved the original architecture pushback:**
1. Python Agent SDK exists with full parity: `pip install claude-agent-sdk`
   → `claude_agent_sdk.ClaudeSDKClient` — streaming input (async generator
   prompt), `await client.interrupt()`, `permission_mode="bypassPermissions"`,
   session `resume=`, full built-in tool access. Confirmed via WebFetch of
   code.claude.com/docs/en/agent-sdk/python. Nothing lost vs the Node SDK.
2. `webrtc-audio-processing` and `speexdsp` are both dead ends on this
   Windows machine (confirmed by actually trying to install them):
   webrtc-audio-processing's setup.py errors immediately; speexdsp needs
   SWIG (not installed) + a native C toolchain we don't have. `sounddevice`
   and `silero-vad` both installed clean as prebuilt wheels. **Decision
   executed**: hand-rolled NLMS adaptive echo canceller in numpy (no native
   lib) + Silero VAD, instead of the named libraries.

## What's built (C:\Users\user\goat\python\)
- `requirements.txt` — sounddevice, numpy, scipy, silero-vad
- `audio_io.py` — `DuplexAudio`: one shared `sd.Stream` (duplex, 16kHz,
  10ms/160-sample blocks) via WASAPI devices selected explicitly
  (`sd.WasapiSettings(auto_convert=True)` — this hardware is 48kHz-native
  and WASAPI shared mode rejects a bare 16kHz request without that flag).
  `NLMSCanceller` (4096 taps, float32, leaky — see bug #2 below, ~2ms/block
  measured, well under the 10ms budget) predicts and subtracts GOAT's own
  echo from the exact samples just sent to the speaker. VAD runs on a
  **separate background thread** via `queue.Queue` (torch inference must
  never risk missing the real-time audio callback's deadline).
  `is_tts_playing` + asymmetric barge-in timing (300ms/100ms). Ducking
  (`DUCK_GAIN=0.5`) on suspected interrupt, full revert if it's echo. 300ms
  preroll ring buffer. Noise-floor-relative RMS gate (`floor * 3.0`) before
  trusting a VAD "speech" verdict — Silero alone judges shape, not
  loudness. `calibrate(seconds)` — real 2s silent measurement at startup,
  not a guessed default. `warming_up` flag — see bug #1 below.
- `tts_piper.py` — reuses `tts/piper/piper.exe` + `en_GB-alan-low.onnx`
  (same voice as the Node app), resamples to 16kHz via scipy.
- `test_aec.py` — standalone verification harness (no STT/Claude/TTS wired
  in yet, deliberately). Flow: calibrate noise floor (2s) → AEC warm-up
  (scripted self-talk, filter adapts, interrupt-detection disabled) → real
  test loop (plays a paragraph on repeat, live meter prints rms/gate/vad_p
  ~3x/sec so tuning is data-driven, not guessed).

## Bugs found and fixed via Giorgi's live testing (in order)
1. **Self-interrupt regression** (same failure mode as the old JS system):
   Silero VAD judges speech by shape, not loudness — quiet residual echo
   surviving imperfect cancellation still scored as "voiced." Fixed:
   noise-floor-relative RMS gate, same technique as the proven browser fix.
2. **Then couldn't hear him at all**: gate started from a hardcoded guess
   (0.003) that didn't match this mic's real ambient level, and had no real
   quiet time to adapt before the first round talked. Fixed:
   `DuplexAudio.calibrate()` — explicit 2s silent measurement, real median
   RMS instead of a decayed guess. **Confirmed correct across many rounds**
   after this — live meter showed clean separation (real voice: rms
   0.02–0.1, vad_p 0.9–1.0; residual echo: rms ~0.003, correctly rejected).
3. **Cold-start gap** (found via a FRESH process run, not a continued
   session): the NLMS filter starts at zero coefficients every process
   start. The first ~300ms of the very first utterance in a session has
   zero cancellation — raw, full-volume, unmistakably-speech-shaped echo,
   easily enough to trip a false "confirmed interrupt" before Giorgi ever
   said anything. Confirmed by log: self-interrupt fired within the first
   ~300-400ms of the very first round of a fresh run, before the first
   throttled meter line even printed. Fixed: `test_aec.py` now plays a
   short scripted "warm-up" phrase right after calibration —
   `audio.warming_up = True` disables duck/interrupt decisions while still
   letting the AEC adapt — then flips back to normal before the real test.
4. **Filter instability**: same fresh-run log showed a `rms=0.6042` reading
   mid-session — far louder than any plausible residual echo, a classic
   runaway-NLMS symptom (coefficients diverging into amplification instead
   of cancellation). Fixed: added leakage (`AEC_LEAK=1e-4`) to the NLMS
   update, standard bounded-coefficient technique. Verified synthetic
   convergence still solid after the change (~21dB suppression, slightly
   less than the pre-leak ~30dB but stable — worthwhile tradeoff).
5. (Unrelated pre-existing breakage, fixed along the way): system-wide
   `torch` had broken DLLs (`shm.dll` load failure) blocking `silero-vad`
   entirely. `pip install --force-reinstall torch torchaudio` (as a
   matched pair — torch alone broke torchaudio's compiled extension)
   fixed it. Also unblocks whatever else on this machine used torch
   (`realtimestt`, `stanza` were listed as other reverse-deps).

## Bug #6: hard-reset voiced-ms counter (found 2026-07-09, same session)
Giorgi ran `test_aec.py` and reported "it did not interrupt at the
beginning." Log showed repeated `possible interrupt -> echo, not you ->
possible interrupt -> ...` flapping before eventually confirming several
seconds late. Root cause in `_on_vad_chunk` (`audio_io.py`): a single
32ms VAD-unvoiced chunk (plosive, sibilant, inter-word gap) hard-reset
`_voiced_ms` to 0, wiping all accumulated progress toward the 300ms
confirm threshold — exact same bug class as the old browser `bargeRun`
hard-reset (see old-app history). Fixed the same way: replaced the
ms-accumulator with a sliding-window majority vote (`_vote_hist`,
`VOTE_WINDOW=10` chunks / `BARGE_NEEDED=7` while playing,
`QUIET_WINDOW=4` / `QUIET_NEEDED=3` while quiet) — tolerates brief dips
instead of resetting to zero. Un-duck ("echo, not you") now only fires
once the *whole* window has gone quiet, not on a single dip.

## Bug #7: shared vote deque leaked across playing/quiet states (found 2026-07-09, my regression from #6)
Giorgi re-ran after #6 and reported every round cut off almost instantly,
right after "This is" (the first two words of the test paragraph) —
no real chance to talk over it. Root cause: `_vote_hist` (the deque from
#6) was a single shared history never reset between states. Voiced blips
during the 3s quiet gap between rounds (ambient noise, room chatter)
stayed in the deque when the next round's playback started, so the vote
was already primed near the 7/10 confirm threshold before real playback
audio even began — one or two genuine chunks tipped it over almost
immediately. Fixed: clear `_vote_hist` whenever `was_playing` flips
(`_vote_hist_state` tracks the last seen value) — each state (playing vs
quiet) now starts its vote fresh instead of inheriting the other's tail.
**NOT yet confirmed live** — needs a fresh `test_aec.py` run.

## Fix #7 confirmed insufficient — self-triggering every round, not a vote-timing artifact
Giorgi re-ran twice after #7. Every single round across both runs (~13+
rounds) still confirmed-interrupt within ~300ms of playback starting,
including presumably-silent "just listen" rounds, and with NO improvement
round-over-round despite the AEC (`self.aec`, persists across the whole
process) replaying and re-learning the *identical* opening phrase every
time. If this were normal NLMS convergence lag, later rounds should get
better — they don't. One line also showed `rms=0.8544` (near full-scale,
not plausible echo) suggesting possible feedback/instability, separate
issue. Conclusion: something structural is preventing the AEC from
cancelling this playback at all (possible causes not yet confirmed:
real acoustic+system round-trip delay exceeding `AEC_TAPS`=4096 (256ms)
window, WASAPI shared-mode resampling group delay smearing the reference,
or output/input clock drift decorrelating ref vs mic over the length of
a 24s utterance).

## Diagnostic instrumentation added (2026-07-09, NOT a fix)
Rather than guess another parameter blind, added real visibility into
whether the AEC is cancelling anything at all — the old meter only ever
showed post-AEC rms, never a baseline to compare against. `audio_io.py`:
`_callback` now also computes pre-AEC mic rms per 10ms block and pushes
it through the queue; `_vad_loop` keeps an EMA (`_raw_rms_ema`) of it.
The periodic meter line now prints `raw=` (pre-AEC level), `erle=`
(dB reduction from raw to cleaned — near 0dB while playing means the AEC
isn't suppressing anything), and `aec_lag=` (ms position of the NLMS
filter's dominant tap, `argmax(|h|)` — if this sits near the edge of the
256ms tap window, the real echo delay likely exceeds what the filter can
model). **Next test run's log is diagnostic data, not a pass/fail** —
read `erle`/`aec_lag` off it before proposing another change.

## NLMS RETIRED → WebRTC AEC3 (2026-07-09, the actual fix)
After the leak/mu/gate fixes, live ERLE still plateaued 2-10dB — not
enough; every round still self-triggered on residual. Giorgi found
LiveKit's docs; their interruption model is cloud-proprietary, but the
lead paid off differently: **`livekit` pip package ships WebRTC AEC3
(the browser-grade canceller) as a prebuilt Windows wheel** —
`livekit.rtc.AudioProcessingModule`. This is the native AEC we couldn't
get before (webrtc-audio-processing/speexdsp wouldn't build).
`audio_io.py`: `NLMSCanceller` deleted, replaced by `WebRtcCanceller`
(echo_cancellation+noise_suppression+high_pass, AGC off so it doesn't
fight the RMS gate; 10ms int16 frames = exactly BLOCK_SAMPLES;
process_reverse_stream(playback) then process_stream(mic), in-place).
`livekit` added to requirements.txt. All NLMS tuning constants removed.

**Measured live (my own quiet-run, 2026-07-09): ERLE 25-58dB (was
2-10dB), echo scores vad_p=0.00, and round 1 played clean through with
zero self-trigger — first time ever.** Later rounds cut on sounds with
erle≈-1dB (uncancellable = real room audio, likely Giorgi/room noise,
not echo) — awaiting his confirmation of what he did/heard.

## AEC CONFIRMED BY GIORGI (2026-07-09): "It did not interrupt itself"
He talked over it and it stopped for him; zero self-triggers. Echo
cancellation is DONE. He then ordered the full pipeline built ("implement
it to our plan and in you").

## FULL PYTHON PIPELINE BUILT (2026-07-09, same session)
- `audio_io.py`: utterance capture added — on speech (quiet) or barge-in
  (playing), collects cleaned audio seeded with preroll; ends after 900ms
  silence; discards <250ms voiced; `on_utterance(float32)` callback.
  While capturing, new interrupt confirms are suppressed (capture owns
  the floor). Constants UTT_*.
- `stt_whisper.py` NEW: client for resident whisper-server :3779 (reuses
  Node's if alive, spawns own if not); ports server.js junk filter +
  stt-fixes.json (re-read per call) + bracket stripping verbatim; httpx.
- `tts_piper.py`: added `PiperResident` (--json-input, temp wav,
  size-stable poll, length_scale 0.85) — ~0.2s/sentence.
- `goat_app.py` NEW — the main app: DuplexAudio + TtsPipeline (worker
  thread, generation counter for race-free barge-in cancel) +
  ClaudeSDKClient (model=claude-fable-5, bypassPermissions, claude_code
  preset + PERSONA copied from server.js, include_partial_messages,
  setting_sources=[], resume from `.goat-session-py` — its OWN session
  file, never the Node app's). Streams text deltas → sentence-split →
  TTS as they arrive; skips unspeakable text (code/paths/URLs);
  tool_use printed; barge-in cancels TTS queue + playback.
  Run: `cd C:\Users\user\goat\python && python goat_app.py`.
- Deps fixed along the way: pydantic-core/httpx/attrs/jsonschema etc.
  were all half-corrupted in site-packages (force-reinstalled); pywin32
  was broken w/ stale pypiwin32 shim (removed, reinstalled).
  requirements.txt now: sounddevice numpy scipy silero-vad livekit
  claude-agent-sdk httpx.
- **Smoke-tested live**: booted, heard Giorgi say "Okay, can you hear
  me?", transcribed correctly, session warmed, brain started reading
  STATE.md. Test window ended before the spoken reply — TTS reply path
  not yet observed end-to-end.
- **WARNING**: don't run goat_app.py and the Node GOAT simultaneously —
  both grab the mic and both will answer. One at a time.

## Next steps
1. Giorgi runs goat_app.py for a real conversation — confirm spoken
   replies + barge-in mid-reply.
2. Model router (Giorgi asked): route hard tasks to fable-5, easy ones
   to cheaper models — `client.set_model()` exists in the Python SDK
   (verified); receptionist-style classifier or heuristic TBD.
3. Then: wake word, boot briefing, receptionist port from Node app.

## Old status (pre-pipeline), for history:
Same run command:
```
cd C:\Users\user\goat\python
python test_aec.py
```
Do NOT declare echo-cancellation "done" again until he confirms this
specific run — bugs #3/#4 were only found by testing a *fresh process*,
so a continued/warm session passing is not sufficient evidence anymore.

## Next steps (in order — incremental, verify before moving on)
1. Get Giorgi's confirmation on the warm-up + leaky-NLMS fix (fresh process
   runs, not just continued sessions — that's exactly what exposed bugs #3/#4).
2. Wire in STT: feed confirmed-interrupt preroll audio (and ongoing mic
   audio generally) to the existing resident whisper.cpp server
   (stt/bin/Release/whisper-server.exe, same as server.js already runs) —
   call it from Python the same way, don't reinvent.
3. Wire in `claude_agent_sdk.ClaudeSDKClient` — streaming input, same
   PERSONA/system prompt as server.js, `permission_mode="bypassPermissions"`,
   `cwd=workspace`. Port session-resume-across-restart / turn-recycling
   logic from server.js if still wanted (ask, don't assume).
4. Sentence-by-sentence TTS streaming into `DuplexAudio.queue_playback()`
   as Claude's response streams in — start talking fast, stop cleanly
   mid-response on interrupt (cancel in-flight Claude stream + TTS
   generation, not just audio playback).
5. Full asyncio restructure once each piece works in isolation (audio I/O
   / VAD / STT / Claude / TTS as separate tasks, per Giorgi's spec) — LAST,
   not first.
6. Decide what happens to the old Node/browser app once Python is
   feature-complete and verified end-to-end — don't touch/retire it before then.
7. Backlog, low priority: 14 claude.ai MCP connectors (unrelated) — last
   check Giorgi hadn't disconnected them.

## Roadmap (JARVIS-UPGRADE-PLAN.md, mostly describes the OLD Node app)
Superseded in large part by the Python rewrite above for anything audio/
interrupt-related. Voice ID, watchers, idle usefulness, Phase 4 OS hands
are still open regardless of which stack they land on.
