# GOAT State — handoff brief

Current design: ONE Claude brain that speaks and sees (see first section). Older history, including the removed talk lane / front desk / Gemini, lives in STATE-archive.md — grep it, never load it whole. Keep this file under ~450 lines: when it grows, move the oldest sections to the archive.


## Fast answers + close guard (2026-10-01 evening, his order: "make GOAT
## answer fast … route simple turns low and hard ones high" and "make it
## confirm before closing any window that has a running session")
- Measured (bench in scratch, GOAT's real persona/tools/note): fresh session
  low/medium/high all 10/10 correct; on a FORK of his live session (42k ctx)
  conversational replies hit first text ~2.8s low / ~3.8s medium / ~4.7s
  high; short factual + trick questions ~1-2s at every level.
- `/effort X` as a query switches the live session in 0.1s, no reconnect,
  no API cost (CLI 2.1.281) — but it RE-CACHES the message history (cache
  read drops to the system+tools prefix). So _route_effort sends it only
  when the turn kind changes. Talk/questions -> QUICK_EFFORT (low); WORK_RE,
  DEEP_RE, >40 words, or hard -> his dial. _consume swallows the command's
  echo (init + AssistantMessage + ResultMessage; no stream events).
  GOAT_EFFORT_ROUTE=off / GOAT_QUICK_EFFORT=medium to change.
- Persona told "how are you" = run goat_doctor; it cost 6-30s per greeting.
  Now small talk is small talk (his 09-27 rule in memory.md).
- The close: brain ran (Get-Process -Id 17688).CloseMainWindow() on his
  Windows Terminal. close_guard.py: terminals/Claude always protected,
  programs inside his terminals, editors on unsaved title or force-kill;
  GOAT's own descendants exempt (except editors). Doors: PreToolUse hook on
  Bash|PowerShell (SDK honours deny under bypassPermissions — verified),
  reflex close asks + holds the hwnd, screen_hands alt/ctrl+f4. YES_RE runs
  the held close or grants 90s; NO_RE says "leaving it open".
- [engine] log: thinking_tokens added to ENGINE_ROUTINE (flooded the log).

## Full audit pass (2026-10-01 night, his order: "find issues in the GOAT
## app and fix all — performance, visual, technical")
- New chat no longer restarts the app: GoatApp.new_chat() rotates in place
  (cut turn, drop session, reopen) with a 4-exchange reference recap; UI
  clears the page; {"new_chat": true} marker in transcript.jsonl so the boot
  page starts after it. Cause of his "the memory is cleared up": old lines
  painted on screen beside a brain that had none of them.
- Memory page: Qt Markdown ate "<key>"/"<name>" as HTML -> "<" escaped.
  Chat lines are Qt.PlainText (a reply explaining HTML used to vanish).
- FlowLayout: heightForWidth at the real width, even rows for grids, hidden
  items take no gap, wrap test fixed (rect.right() is width-1). Tools and
  Skills use CardGrid(min_w=300/280). Composer chips go compact (icons,
  "high", slim padding, File hidden) when one row won't fit — at 150% on a
  1536px screen they wrapped to three rows. TRAP: never name a dynamic
  property "icon" on a QPushButton — it IS the icon property.
- Boot: ui_qt no longer imports tts_edge at the top (scipy.signal +
  edge_tts, ~2s in front of the first paint): import ui_qt 2.55s -> 0.47s.
  qt.qpa.fonts warnings (8514oem/Fixedsys) silenced via QT_LOGGING_RULES.
- A transcript ending mid-word/trailing off ("This is-") waits 1.6s for
  the rest and joins it (DANGLING_RE, HOLD_DANGLING_S).
- [turn] line has "wait" (query -> first model step); non-routine CLI
  system messages log as [engine] <subtype>. A live turn had 22.5s of
  unexplained time — the next one will say where.
- Stale tests fixed: test_voice_latency used "Mm-hm." (HUM_RE strips it),
  test_audio_resilience lacked the loopback tap. test_aec is a MANUAL
  harness that plays speech out loud — not part of the suite.
- Checked, left alone: whisper -bs 1 only ~10% faster than -bs 5; private
  bytes 1.09GB after 3.4h was FLAT at idle (a one-off peak, not a leak).

## Overall performance pass (2026-10-01, his question: "is there anything we
## can do to improve GOAT's overall performance?" then "fix these step by step")
- Reflex gaps from his live log (each cost 3-4s in the brain): "could you
  scroll this"/"keep scrolling" = scroll down; "close it now" (the "now" was
  read as a window name); a click name never spans a sentence break ("click
  stop. First image you see" searched 491ms, then deferred). reflex 130/130.
- Idle CPU: Chat page held 36% of a core idle (real window, 30Hz). VoiceOrb
  math moved to numpy, dots batched by bucket (drawPoints, round pen),
  halo+core cached per energy step; idle orb and rail wave repaint at 10fps;
  hud_tick skips animation while minimized. Now 10% / home 3.1% / min 0.
  TRAP for benches: a window parked at (-4000,0) is never painted — measure
  on-screen at opacity 0 instead.
- [turn] log line per brain turn: total = prep + model (steps, first token
  each) + tools (named, per gap between steps) + other, with effort and ctx.
- Effort measured (throwaway session, GOAT's real setup, 7 questions x
  low/medium/high): first word is decided by whether adaptive thinking
  kicks in (~0.8s without, 1.8-3.9s with), NOT by the dial — tool-free
  averages low 1.9s, medium 2.1s, high 1.65s. Kept "high". Don't lower the
  effort for speed without new evidence.

## Fast hands (2026-10-01, his order: "goat takes about 10 seconds to do the
## simple tasks, close the tab, click this, click that — optimize this")
- Cause, from his live test: "close the two tabs" was not a reflex, so the
  brain thought, pressed ctrl+w (42ms), screenshotted the tab strip, pressed
  again, screenshotted again, show_self — five model round trips.
- fast_hands.py (new): target_window() = topmost real window that is NOT
  GOAT (z-order), browser-only for tab verbs; keys() focuses it then presses;
  click_named() finds the control by accessible name via UI Automation
  (comtypes, ~30-90ms), else OCR of the window (Windows.Media.Ocr via winrt,
  ~200ms), else returns "DEFER:" and goat_app._reflex hands the turn to the
  brain (ack already spoken).
- reflex.py rules (EN+KA): close N/this/all tabs, new/reopen/next/previous
  tab, back/forward, refresh, zoom, scroll, press <key> [N times],
  copy/paste/undo/redo/select all/save, click/tap/press <name>. "click this",
  "close the tabs" (no count) stay with the brain. test_reflex 124/124.
- FIXED a latent trap: _window() with no name (close/minimize this) used
  GetForegroundWindow() — GOAT itself when he had just spoken to it; WM_CLOSE
  would have quit GOAT. Now it targets fast_hands.target_window().
- Measured on a throwaway tk window through the real paths: press enter
  177ms, click "Zebra Launch" 529ms (OCR), ctrl+w 167ms.
- TRAP: his Brave does not expose web page content to UIA (renderer
  accessibility off) — only browser chrome. Page text goes through OCR, so
  the target is focused and given 120ms to paint before the grab.
- Persona SPEED rule: one press with times=N, verify once, small regions.
- Known, NOT from this change: test_voice_latency "TTS worker tells a filler
  from the answer" fails on the prior commit too.

## New face v7 (2026-09-30, his concept image: "the new design of the GOAT app")
- ui_qt.py rebuilt as three columns on a painted midnight room: left rail
  (goat mark, pages Home/Chat/Skills/Files/Memory/Tools/Settings, presence
  card = compact StringLine + state word + footer meter), centre (greeting
  over a painted mountain range, 5 quick actions, Recent from
  transcript.jsonl, the composer + chips), right rail (search, Active brain,
  Tools <-> Activity, System switches, Today card).
- Built from the concept, but only real features: no invented models/tools.
  Activity card = the old WorkPanel (steps, thinking, context meter). The
  settings drawer is now SettingsPage (self.panel) with the same set_*_opt
  handlers. Voice-synced reveal, bubble/pop, Snap hit-test, geometry,
  global zoom, themes and every shortcut carry over; Ctrl+F = search.
- Theme "midnight" is the new default; DESIGN_VERSION=7 re-seats his saved
  theme once (config key "design"), then his pick sticks.
- Scenery (paint_scene) and the goat mark (paint_goat_mark) are QPainter
  code, cached; goat.ico is rendered from the same mark (make_icon.py).
- TRAPS: (1) a right-aligned wrapped QLabel in a QVBoxLayout gets its height
  at the FULL column width, then is squeezed -> clipped 2nd line: ChatBubble
  opts out of height-for-width and sizes itself. (2) A wrapped QLabel's
  sizeHint is Qt's guess at another width; the scroll host summed those and
  left a gap under the last line: PageLabel.sizeHint reports the real hfw.
  (3) Qt drops QSS border-radius larger than half the height -> chips went
  square at 150%: keep radii under half height at every zoom.
  (4) offscreen QPA renders no fonts; judge renders with QT_QPA_PLATFORM=windows.
- Voice orb (same night, his order: "a sphere in the middle that moves like
  in the movies when AI talks"): VoiceOrb on the Chat page — 760-point
  Fibonacci globe + orbit rings + halo, surface waves driven by the REAL
  level (speaker envelope speaking, mic listening). Ticked from hud_tick only
  while Chat is on screen; wave projections precomputed and colours
  bucketed so a frame is ~4ms. Halo must fade to 0 inside the widget or its
  edge shows as a box.
- Bubble = the same orb (his ask: "same sphere as bubble"): Bubble owns a
  hidden VoiceOrb(n=220) and calls paint_orb() into its disc; hud_tick feeds
  set_level(). Still animates only on the busy/unread beat — idle is a still
  frame (test_bubble pins that battery rule).
- Message beside the bubble is text only (his ask): no card. MessagePop's
  QLabel only measures/wraps (ink transparent); paintEvent draws the words
  with a two-ring dark halo, then light ink — a QGraphicsDropShadowEffect
  alone was unreadable on a white wallpaper. An alpha-1 wash keeps the box
  clickable (fully transparent layered-window pixels pass clicks through).
- Tests: test_scroll was already stale (win._follow) -> fixed to _pin.follow
  and shows the chat page; test_scroll/test_statusword now write a temp
  config, never his real ui-config.json.


## Music cancellation via loopback (2026-09-28, "you've been hearing the music I'm playing")
- Cause: AEC3 reference was only GOAT's own playback block, so Brave/YouTube
  audio hit the mic uncancelled; lyrics became "his" messages and cut him off.
- Fix (audio_io.py): `LoopbackTap` reads WASAPI loopback of the default
  speaker via the `soundcard` package (py -3.13; sounddevice 0.5.5 has no
  loopback). Reference = own block while TTS plays, tap otherwise.
  `music_on` (tap level > MUSIC_LEVEL) makes the quiet-mode speech vote use
  the stricter barge vote (7/10).
- TRAP: the tap buffer MUST stay shallow (MAX_MS=40, drop oldest). With a 1s
  ring the reference lagged the echo by ~100ms and AEC gained 0.5dB; capped,
  it leads by ~29ms and gained 15dB, VAD false hits 95 -> 7 of 156.
- TRAP: pycparser 2.22 in py3.13 was missing c_parser.py (soundcard's cffi
  cdef needs it) — force-reinstalled. If the tap logs "[loopback]
  unavailable", check that first. Tap failure degrades to old behaviour.
- TRAP: test_aec.py / test_audio_resilience.py play speech out loud; the live
  GOAT transcribes it as a flood of fake messages. Run them only when he's away.
- Verified live 2026-09-28 00:34: he spoke over the playlist, music ignored; one possible lyric leak ("You cannot use that"), unconfirmed.

## Autostart asleep (2026-09-27, his order: "on startup, not awake, just on the spot")
- Startup-folder GOAT.lnk -> wscript start-goat-app.vbs /startup -> ui_qt.py --startup.
- --startup: window collapses to the dot before first paint (no ignite),
  goat.quiet_boot=True -> no scripted greeting (AEC learns on his first
  reply via _warm_on_first_reply, barge-in off for it), no boot briefing,
  no _warm_brain (zero usage), _asleep=True: name gate applies even though
  his ui-config has wake=false; cleared on first addressed transcript.
- Manual launches / restart-goat.ps1 unchanged (no flag).
- Startup folder only fires on a real boot/login — NOT on wake from sleep.
  2026-09-27 23:52 "startup doesn't work": PC resumed from sleep with the
  old GOAT still up, and its WASAPI stream had died silently on resume
  (no callbacks, no error) -> deaf. Fix: DuplexAudio._stream_watchdog
  (audio_io.py) reopens the stream after 3s with no callback, re-initing
  PortAudio so devices re-scan. Live test: test_audio_stall.py.
- Lesson: bare `python` is 3.12 (no scipy) — run self_check with py -3.13.


## Torch removed from the ears (2026-09-27, his ask: lighter, never slower)
- GOAT's python was ~610MB with RAM at 87-89%; torch_cpu.dll (~300MB) came
  only from silero-vad. audio_io.SileroVAD now runs silero_vad.onnx on
  onnxruntime directly (the silero_vad package imports torch even in ONNX
  mode — never import it). Bench on 4 recorded clips: probabilities identical
  (maxdiff 0.000), 0.13ms/chunk vs 0.27-0.6ms, load 0.17s vs 1.56s.
- goat_app import: no torch, no silero_vad in sys.modules; RSS 177MB.
- Trap: .self-backup/last-good still has the torch version; that's fine for rollback.


## Self-knowledge kept current (2026-09-26, his order: automatic, lean on usage)
- STATE.md split: history before 2026-09-14 midday -> STATE-archive.md (1780 -> 424 lines). Keep under ~450.
- goat_doctor check 7 'self-knowledge' scans memory.md + skills for STALE_TERMS of removed features.
- self-upgrade skill step 7: any change to how GOAT works updates memory.md ## Self + skills in the same turn.
- Trap: shell `python` is 3.12 (no scipy); the app runs Python313. Preflight must use C:/Users/user/AppData/Local/Programs/Python/Python313/python.exe.


## ONE BRAIN WITH SIGHT (2026-09-23 night — his order: "remove the talking
## model and Gemini; it must see and perceive everything on my laptop")

Trigger: asked about his screen, the talking brain answered "I can't see the
screen directly — that's the working side's job, but I can check which
terminal processes are running." Two minds, one of them blind.
- **Talking lane deleted.** `_talk_gemini`, `_talk_claude`,
  `_ensure_talk_client`, `_escalate_from_talk`, `_offline_cover`,
  TALK_BRAINS and `local_llm.py` (Gemini) are gone; the drawer's "talking
  brain" row and TALK_OPTS are gone from ui_qt. `set_talk_brain` is a no-op
  stub so an old config can't crash. Routing is now: stop brake -> reflex ->
  the one Claude brain (`_work`), for everything.
- **It speaks.** `_consume` sends text deltas to `_speak_delta` (middle
  label + TTS) instead of the silent left panel; the tail is flushed at
  turn end. Thinking still goes to the left panel, never the voice. A
  backchannel covers a slow start instead of the old "On it" ack.
- **It sees.** `live_view.note()` (~20ms: EnumWindows + psutil) prefixes
  every turn: front window, open windows with their apps, busiest processes
  since he last spoke, RAM, battery, time. Screenshots stay available via
  the `computer` tool for pixels. PERSONA "ONE BRAIN, WITH EYES" replaces the
  old YOUR BRAINS section.
- **Speed:** default effort max -> high (ui_qt DEFAULT_CFG and his
  ui-config.json). Live probe (`probe_sight.py`, real Opus 5):
  "რა მაქვს ახლა ეკრანზე?" -> described Brave/Katy Perry, the terminals,
  minimised Notepad, RAM — first word 3.0s cold; "what app is using the most
  CPU" -> "MsMpEng at 14 percent", first word 0.8s.
- Claude out of usage: honest spoken line with the reset time; reflexes
  (open/close/volume/media/time) keep working with no model.
- Rollback safety: `.self-backup/last-good/local_llm.py` restored from git
  HEAD, because the last-good goat_app still imports it.
Tests: test_engine_router rewritten for one brain (83/83), reflex 89,
latency 39, bubble 40, statusword/audio/reveal pass, PREFLIGHT PASS.

## Act, verify, then report (2026-09-23 — his goal: "work like Claude Code
## in the terminal: do what I say, voice, Georgian + English, fast, exact")

Read from the 2026-09-18 transcript. Four failure shapes, each fixed:
1. **False "done".** "Turn off Ubisoft, Roblox and Steam" -> "closed all
   three"; Ubisoft (process `upc`) was still up. New reflex `quit`
   (`reflex.QUIT_APPS`, EN + KA stems): terminate, force stragglers, then
   re-list processes; the result names what is STILL running, so the
   REFLEX_FAIL line is spoken instead of an ack-and-lie.
2. **Closing the wrong window.** `_window(close, "სტიმი")` found no such
   title and closed the FOREGROUND window, then reported Steam closed. A
   named target that doesn't resolve is now `ERROR`, and a close with an
   unknown name is not a reflex at all (a brain looks for it).
3. **Georgian blind spot in the lie gate** (`local_llm.ACTION_HINT_RE` /
   `CLAIM_RE` were English-only). Georgian orders and claims added.
4. **Run-on sentences.** "open the PFP picture? For me. That I have on my
   desktop." fell through. `_clean_target` keeps the first sentence and drops
   "that I have…"; picture/photo/video (+ KA) are file nouns.
Also: both talk personas carry a VERIFY-BEFORE-REPORT rule, the Sonnet talk
client's `max_turns` 4 -> 8 (room to check), "გამორთე ხმა" = mute.
Tests: test_reflex 86/86 (GG tests now bring their own folder — his real GG
left the desktop), test_engine_router 106/106, test_voice_latency 39/39.
**Voice end-to-end, `test_voice_e2e.py` — 13/13.** edge-tts speaks each
phrase (Georgian neural voice for KA), it streams into the real
`stt_realtime.Pair`, the transcript goes through `reflex.match`, and the
result is checked against Windows (mute state via pycaw, the test's own
window process exiting, the steam process list). Ear commit 220-300ms.
The real ear found four bugs no unit test had: it punctuates ("გამორთე,
ხმა."), writes case endings on Latin names ("Google-ი"), moves ს across the
word gap ("დახურე სტიმი" -> "დახურეს ტიმი"), and "close the Zebra window"
looked for "the Zebra". All fixed, and pinned in test_reflex (89/89).
Trap: never test window-close on Notepad — Win11 Notepad opens files as tabs
in HIS window. The e2e test uses its own tkinter window titled "zebra".
Not yet tried by him live.

Updated: 2026-09-15 night (sub-500ms voice shipped; then the deafness:
his mic endpoint was at 66% and GOAT was inventing English over his Georgian)

## Reflex lane — instant device commands (2026-09-15, his complaint: "it
## takes long to do my commands, for example to open a file on my PC or open
## Google — I want it to feel instant. Also it crashed a few minutes ago")

He was right and the numbers were brutal. Measured that morning on his key:

    gemini-3.8-flash  "say ok"          19.0s      <- the talking brain default
    gemini-3.5-flash  "say ok"           1.3s
    gemini-3.5-flash-lite "say ok"       0.7s

Root cause: **Gemini 3 models always think.** Google's docs say reasoning
cannot be disabled on them, so the `reasoning_effort: "none"` that
`_post_stream` has always sent is accepted and silently ignored. Every "open
Google" paid 8-19s of hidden thinking, then a SECOND round trip to narrate the
tool result, then TTS.

"Open the file GG on my desktop" was worse, and had its own separate bug: his
Desktop is **OneDrive-redirected** (`C:\Users\user\OneDrive\Desktop`), but
`local_llm.chat` asserted `{home}\Desktop` as machine fact. That path also
exists and is nearly empty, so the model looked in a real folder, found
nothing, and ESCALATEd — and the work lane went hunting the icon with
screenshots. Twenty seconds for a double-click.

### What was built
- **`reflex.py`** — a deterministic intent router in front of both brains: the
  two-tier pattern every shipped voice assistant uses, where rigid device
  commands are matched on-device and the LLM only ever sees the weird phrasing.
  Covers open site/app/file/folder, volume, media, brightness, lock, window
  state (by title too), screenshot, time, date, battery — EN + KA, stem-matched
  because cloud STT garbles his casual speech.
- **`reflex_index.py`** — name-to-path gazetteer of Desktop / Downloads /
  Documents / Pictures / Videos / Music / Start Menu. 1144 entries, built in
  ~0.1s on a daemon thread at boot, cached to `workspace/file-index.json` so
  the first command after a restart is instant too. Folders come from
  `SHGetKnownFolderPath`, so redirection can never lie to us again.
  A miss falls through to `lookup_live`, which re-scans Desktop +
  Downloads at depth 1 (~4ms) and then kicks a full rebuild behind it
  — otherwise a file saved since boot was invisible, and "I just
  downloaded it, open it" is one of the most natural things he says.
- **`GoatApp._reflex`** — speaks the ack BEFORE running the action, because the
  ack is the part he perceives as speed. The ack lines live in the TTS prewarm
  cache (`ACK_REFLEX`), so the sound starts immediately instead of after a
  ~0.85s edge-tts call; the precise name goes to the screen, which is free. A
  failed action speaks `REFLEX_FAIL` — a reflex never says "done" about
  something that did not happen.
- **Latency guard** in `local_llm`: `_note_ttft` demotes the primary talking
  model to the fallback for 15 min after two turns over `SLOW_TTFT_S` (6s), and
  the first-byte socket deadline dropped 30s to `TIMEOUT_TTFT` (9s), with
  `_relax()` lifting it the moment anything streams so a long reply is never
  cut mid-sentence. Runtime fallback only — his configured pick is never
  rewritten, and the demotion lapses on its own.

### Measured after
    open google                                   match 0.31ms + act 102ms
    "გახსენი ... ფაილი, სახელად GG, დესკტოპზე არის"  match 0.40ms + act  62ms
    volume up                                     match 0.01ms + act   5ms

Router average 213-247us per input across 200 iterations. 70 reflex tests +
6 new routing tests in `test_engine_router.py` (106 there now, all green).

### Traps hit
- `run` and `load` had to come OUT of the opener verbs, and `_WORKISH_RE` was
  added: "run the tests" would otherwise double-click a folder named `tests`.
- The test suite feeds REAL phrases ("open chrome", "turn the volume up")
  through `_talk` to prove they stay off the work lane — with the reflex lane
  live, that actually launched Chrome and moved his volume. `GOAT_REFLEX=off`
  is now set at the top of `test_engine_router.py`, and the routing case stubs
  the matcher instead. `GOAT_REFLEX=off` doubles as the production kill switch.
- Georgian word order puts the verb last as often as first, and he corrects
  himself mid-sentence ("აპლიკაცია, უფრო სწორად ფაილი, სახელად GG"), so
  `_clean_target` peels tail then place then lead then noun in a loop rather
  than in one anchored match.

## Message card beside the bubble (2026-09-25, his order: Messenger-style pop-up)
Collapsed, the dot only said "a reply arrived". Now `MessagePop` in `ui_qt.py`
shows the reply itself in a rounded card beside the dot, like a chat head's
message. Fed from `update_spoken()` so it follows the VOICE word for word, not
the model stream. Opens toward screen centre, follows the dot on drag
(`Bubble.relocated`), fades 9s after the last word, left-click opens GOAT,
right-click dismisses, a new "you" turn clears it, expand() hides it.
WA_ShowWithoutActivating: it must never steal his caret.
TRAPS: QLabel.heightForWidth() before polish returns a far-too-tall guess —
height is measured with fontMetrics().boundingRect instead. Run tests and
preflight with `py -3.13` (plain `python` lacks numpy → false PREFLIGHT FAIL).
Not covered by `stand_aside` (neither is the bubble) — it sits in the corner
and fades on its own.
VERIFIED: test_message_pop.py 19/19, test_bubble.py 40/40, preflight PASS.

## 2026-09-27 — daily self-update (his order: autonomous, tell me only when updated)
- python/self_update.py: 24h check of claude-agent-sdk (engine, py3.13), global Claude Code CLI (`claude update`), and new Opus/Sonnet/Fable ids on docs.claude.com (probed via bundled CLI; announced, NOT auto-switched).
- goat_app._update_watch: every 30 min; pending engine update → restarts via restart-goat.ps1 after 20 min idle. restart-goat installs between kill and relaunch; watchdog reverts engine on boot crash.
- Results queued in python/update-notice.txt → prepended to his next turn once.
- Lesson: Python urllib rejected docs.claude.com cert (expired chain) — use curl.exe there.

- 2026-09-27 00:45 LESSON: never send alt+f4 via computer tool to close an app - it closed GOAT itself. Close apps by process/taskkill. Also: start-goat-app.vbs relaunch silently failed twice (restart-goat logged BOOT CRASH + rollback, no python spawned); direct 'py -3.13 -u ui_qt.py' worked. Launcher needs a look.
- 2026-09-27 00:56 FIXED (launcher + alt+f4): root cause of the failed relaunch = orphaned engine claude.exe held goat-app.log (inherited >> handle) -> launcher MoveFile 'Permission denied' -> vbs died before launch -> watchdog rolled back innocent code. Now: child_guard.py puts every engine in a kill-on-close Job Object (engine dies with GOAT, apps it opened survive); launcher kills orphan engines + logs to goat-app.<stamp>.log if the log is still held; restart-goat.ps1 kills orphans, treats 'never started' as a launch failure (retry, no rollback), verifies the rollback relaunch; screen_hands refuses alt+f4/ctrl+f4 while GOAT's own window has focus, even with confirm=true. Tests: test_child_guard.py.
