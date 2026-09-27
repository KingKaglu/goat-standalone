# GOAT State — handoff brief

Current design: ONE Claude brain that speaks and sees (see first section). Older history, including the removed talk lane / front desk / Gemini, lives in STATE-archive.md — grep it, never load it whole. Keep this file under ~450 lines: when it grows, move the oldest sections to the archive.


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

## "GOAT cannot hear me in Georgian" (2026-09-15 night) — it was the MIC LEVEL

Not an STT bug, and not the language pinning. Numbers first, from his own
captures in `python/stt-debug/`:

    02:2x (working)   peak 0.49-0.86   -6 to -1 dBFS   Georgian transcribed well
    21:03 (deaf)      peak 0.05-0.10  -26 to -20 dBFS   nothing, or English

The Windows capture endpoint "Microphone (C-Media(R) Audio)" was sitting at
**66%**; his speech was landing ~20dB below where it had been the night
before. At that level the two ears fail in the two worst possible ways at
once: the ka-pinned ear commits an EMPTY transcript, and the en-pinned ear
invents fluent English over his Georgian — "The term will be $10 a month" for
a Georgian sentence, "Rach Debar." for "რა ხდება", "Hello. Aba, aba, aba." for
"აბა აბა აბა". `Pair.result` then does what it was written to do: ka is empty,
en is Latin, so it hands back the English. GOAT answered words he never said.

### Proof it was level, not language
- `stt_realtime` ka-pinned, raw 21:03 capture -> committed `''`.
- Same capture x5 -> `'ფეის და გირტყმენ'` (garbled, but Georgian).
- Endpoint raised to 100%: the live floor went from **-35.7 dBFS to -11.5**.
- The ka ear on a GOOD (02:2x) capture, paced in real time, commits in
  ~200-310ms and reads correctly — so the wave's code was never the problem.

### What changed in the code
1. **Makeup gain for the ears only** (`audio_io._learn_ear_gain` /
   `_ear_level`). Each finished utterance sets the gain that would put its
   peak at `EAR_TARGET_PEAK` (0.5), capped at x12, never below x1, smoothed
   0.5/0.5 so one shouted word cannot slam the next turn — and the remembered
   gain is applied to the LIVE blocks going to the streaming ear, not just the
   batch copy. The VAD and the noise-floor gate still see raw audio: this is
   deliberately not WebRTC AGC, which stays off because it fights that gate.
   Only real speech teaches the level; a discarded blip does not.
2. **The quiet-mic guard** (`GoatApp._mishearing`). Under
   `EAR_QUIET_PEAK` (0.12 / -18 dBFS), in ka or auto mode, a transcript with
   no Georgian letters is dropped instead of answered, and GOAT says once —
   in the turn's language — that the mic is too quiet to make out. A healthy
   turn clears the gate so the next outage speaks again. Answering a
   hallucination is worse than admitting deafness: it puts words in his mouth
   and then replies to them.
3. The status line now names the lift: `mic is very quiet (-26 dBFS) —
   lifting it x10.2 for the ear`.

Honest limit: gain cannot rescue audio that is already ruined. Replaying the
21:03 captures through the shipped path lifts them and warns correctly, but
they still do not transcribe — at -26 dBFS with noise suppression on top,
there is not enough of his voice left. The hardware level is the fix; the code
is the net that makes the failure visible instead of fluent.

### If it happens again
    py -3.13 -c "import wave,numpy as np;w=wave.open('stt-debug/<file>.wav','rb');a=np.frombuffer(w.readframes(w.getnframes()),dtype=np.int16)/32768.0;print(abs(a).max())"
Peak under ~0.15 means the mic, not the model. Check the endpoint level
(pycaw: `IAudioEndpointVolume.GetMasterVolumeLevelScalar`), and remember this
machine also exposes an ASUS "AI Noise-cancelling Input" virtual mic and
several Bluetooth headset mics — GOAT follows the WASAPI default input, so a
device switch moves the level too.

## Sub-500ms voice — the streaming ear (2026-09-15 night, his goal: "use
## streaming architectures ... target sub-500ms latency")

Written 02:27-02:44, left uncommitted and undocumented when the session ended.
Verified and shipped the evening of 2026-09-15; this section is the record.

The budget he actually feels is *last sound he makes -> first sound GOAT
makes*. Before this wave the ear owned most of it, and the ear was a batch
upload: wait for the whole utterance, upload the whole WAV, wait for the whole
answer. Measured on his own captured audio:

    batch ElevenLabs scribe    1089-2594 ms   after he stopped speaking
    local whisper server       1745-3122 ms   (and cannot do Georgian)
    realtime, language pinned   134- 554 ms   same audio, same key

All of that batch time is pure overlap waste — the first five seconds of a
six-second sentence could have been transcribed while he was still saying the
sixth.

### What was built
- **`stt_realtime.py`** — Scribe v2 Realtime over a WebSocket, fed every
  captured block as it arrives (preroll first, so his first word is in).
  `Pair` runs one pinned ear per language for bilingual "auto" and lets the
  ALPHABET say which one answered — the same rule the rest of the app already
  uses for turn language. Any failure returns None and the caller falls back to
  the batch ear with the audio it already holds: worst case is today's
  latency, never a lost sentence. `GOAT_STT_REALTIME=off` is the kill switch.
- **Adaptive endpointing** (`audio_io`) — the 700ms hangover exists to protect
  a THOUGHT (he pauses mid-sentence working out what he wants, and cutting him
  off there is worse than any latency). A short utterance is not a thought, it
  is a command, so under `UTT_SHORT_VOICED_MS` (2000ms voiced) the hangover is
  `UTT_SILENCE_STOP_SHORT_MS` = 480ms. ~220ms off every command.
- **Soft commit** (`UTT_SOFT_COMMIT_MS` = 200ms) — 200ms into the silence the
  ear is told "he may be finished, start wrapping up", so finalising overlaps
  the hangover instead of starting after it. Re-armed if he resumes speaking,
  so a breath does not truncate the sentence.
- **Latency ledger** (`GoatApp._lat_*`) — every voice turn prints
  `endpoint + ear + think/voice = total`, back-dated by the hangover that had
  already elapsed (counting from the callback would flatter every number by
  exactly the wait he sat through), marked / . / ! at 500ms / 1s. The total
  also goes to the UI. A target nobody measures is a wish.
- **First breath on a clause** — `FIRST_CLAUSE_RE` + `GOAT_FIRST_CLAUSE_MIN`
  (28 chars): the FIRST fragment of a reply goes to TTS at a comma, not a full
  stop; later sentences are never split that way. A fragment under the minimum
  waits, because "Yes," alone sounds like a glitch.
- **Backchannel** — one short listening noise after `GOAT_BACKCHANNEL_AFTER`
  (0.9s) when the answer itself is slow. Once per turn, never over a reply that
  already started, dropped if the turn was cancelled, pre-synthesised in the
  prewarm list so it costs no network. `GOAT_BACKCHANNEL=off`.

### Measured live (his own voice, session 2026-09-15 20:52)
    [stt-rt] committed in 194ms / 198ms / 199ms / 240ms
    endpoint 480ms + ear(streaming) 198ms + think/voice 2318ms = 2997ms  (first turn, cold)
    endpoint 480ms + ear(streaming) 199ms + think/voice  905ms = 1584ms  (warm)

So the ear is done: it is no longer the expensive stage. **What is left is the
talking brain's first token plus the first TTS clip** — ~900ms warm, ~2.3s on
the first turn of a session. Reflex commands, which skip both, are inside the
budget already. Next candidates, in order of expected win, none of them done:
1. Connection reuse in `local_llm` — it opens a fresh TLS connection per turn
   through urllib; a kept-alive connection is worth roughly a handshake.
2. Warm the talking brain and the TTS socket at boot so turn one is not the
   slow one.
3. Speculative dispatch: send the soft-committed partial to the talking brain
   during the hangover and discard it if he resumes. Costs tokens on a
   false ending, so measure the win before trusting it.

### Traps hit
- **Realtime must be language-PINNED.** On auto-detect it heard his Georgian as
  Russian and returned Cyrillic transliteration ("Ааа, мотхидэн..."). Pinned to
  `kat` the same audio came back in proper Mkhedruli. This is the OPPOSITE of
  the batch endpoint, where auto-detect is the accurate mode (see stt_gladia) —
  the two ears are configured differently on purpose, do not "unify" them.
- **GOAT's VAD owns the turn boundary**, not the server's: with
  `commit_strategy=vad` the server split one sentence into several committed
  fragments on its own schedule. `manual` puts the boundary where the rest of
  the app already believes the utterance ended.
- Piper's API is `synth_to_16k`, not `synth` — the first benchmark harness
  called the wrong name and measured nothing.
- Wrapping stdout in a UTF-8 writer inside a bench script closed the underlying
  file and every later print raised.

### Tests (all green, 2026-09-15 evening, `py -3.13`)
    test_voice_latency.py     25 passed   endpointing, soft commit, ledger, backchannel, barge-in
    test_reflex.py            71 passed   incl. match cost 380us/input
    test_engine_router.py    106 passed
    test_audio_resilience.py / test_statusword.py  all passed

### Crash watch
`goat-crash.log` holds the 02:20:59 access violation (Qt event loop, main
thread, `<no Python frame>` in the faulting thread) that the faulthandler pass
was added to catch. The two sessions run this evening (20:50:31, 20:52:46)
recorded no fault — the page cap and the stale-wrapper nulling stand as the
best available lead, unconfirmed until it either recurs or does not.

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

## The 01:56:55 crash (same session)
Windows logged it, GOAT did not: `python.exe` faulting in
`shiboken6.abi3.dll`, exception `0xc0000005` — an access violation inside
PySide6's binding layer. `goat-app.log` was empty and no Python traceback
existed anywhere, because a native crash never raises. No dump survived (WER
kept only `Report.wer`) and no debugger is installed, so that stack is gone for
good. Two changes so the next one is not:
- **`faulthandler`** is enabled at the top of `main()` against
  `python/goat-crash.log`, all threads. The next fault writes the Python stack
  of every thread at the moment it happens.
- **The page is now capped** (`GoatWindow.PAGE_MAX = 240`, `_trim_page`). It
  grew without bound before, and `_dim_previous()` re-polished the WHOLE column
  on every new line — hundreds of style recalcs per turn in a long session. The
  trim also nulls `_you_label` / `_reply_label` / `epigraph` when they point at
  a widget it just deleted: a Python name aimed at a destroyed C++ object is
  exactly the class of access violation that was logged, and it is the one lead
  the evidence supports.

No Qt object is touched off the GUI thread anywhere in the app — checked, and
`goat_app`, `local_llm`, `local_hands` and `screen_*` import no PySide6 at all,
while every UI callback crosses via `emit` to `event_sig`. So the stale-wrapper
path is the remaining candidate rather than a threading violation. If it
recurs, `goat-crash.log` now names the file and line.

## Collapse to a bubble (2026-09-14 night, his order: messenger-style bubble)
Minimize used to send GOAT to the taskbar, which hides the only thing worth
seeing — whether it is listening. Now the window folds into a round
always-on-top dot: `Bubble` in `ui_qt.py` (frameless + WA_TranslucentBackground
+ Qt.Tool), driven by `GoatWindow.collapse()` / `expand()` / `toggle_bubble()`.
- Three ways in: the titlebar "–", Ctrl+B, or ANY OS minimize (taskbar, Win+D,
  shake) via `changeEvent`, guarded by `_collapsing` against recursion.
- The ring carries the live state from `hud_tick`, and a reply arriving while
  collapsed ("delta") lights an accent dot — collapsed is not blind.
- The animation timer runs ONLY while busy or unread. A dot nobody is looking
  at must not burn a worn battery.
- Position persists as `cfg["bubble"]` = [x, y] (logical px), saved on drag
  release, clamped to the live work area on every placement so a position from
  an unplugged monitor can't strand it.
- Theme and UI scale reach it through `apply_theme()`, which is already the
  single funnel for both.
- Qt.Tool is deliberate: without it, collapsing leaves a second taskbar button
  next to the window it just replaced.

NOTE ON THE ORDER: it asked not to break "existing tray/notification logic".
There is none — grep for QSystemTrayIcon/notify across the repo returns
nothing; GOAT has never had a tray icon. Nothing to preserve, nothing broken.

TRAPS: the Qt review harness writes preferences, and `ui-config.json` is his
LIVE session — `test_bubble.py` redirects `ui_qt.UI_CONFIG` to a temp file
before any window exists (this bit once already, in the v6 design pass).
Also: his display runs at 125%, so Qt logical pixels (1536x816) and the
physical pixels `screen_hands` clicks in (1920x1080) are different spaces —
do not compare coordinates across them.

VERIFIED: `test_bubble.py` 40/40 (flags, translucency, scale floor, paint,
beat-timer gating, clamping, drag-vs-click, collapse/expand, persistence,
theme+scale propagation, OS-minimize, state mirroring) plus rendered PNGs per
theme for the eye. `test_engine_router.py` 100/100, `test_screen.py` 65/65,
preflight PASS. Driven live afterwards with GOAT's own new hands: collapse
button, Ctrl+B, drag, and click-to-open all confirmed on the running app.

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

## Screen control (2026-09-14 evening, his goal, in his words: computer-use /
## screen-automation — "ხედავს ეკრანს და მართავს მაუს/კლავიატურას პირდაპირ")
GOAT could drive anything with a CLI and nothing else; it had to tell him so.
Now it sees the screen and uses it. Four new modules under `python/`:
- `screen_hands.py` — mss/PIL capture + Win32 SendInput. Screenshots carry a
  grid whose labels are REAL screen pixels (the model reads a number instead
  of undoing the downscale in its head) and a crosshair on the cursor.
  Mouse, keyboard (Unicode, so Georgian types on an English layout), windows,
  and stand_aside/step_back_in.
- `screen_browser.py` — CDP. Tabs and DOM elements as objects. Chrome 136+
  refuses remote debugging on the default profile, so GOAT drives its own
  profile at port 9333; `tab_search` (Ctrl+Shift+A) steers the live window he
  is actually using.
- `screen_policy.py` — the gate. Two triggers: WHAT (label/keys/card numbers)
  and WHERE (a banking or checkout window makes every action confirm-tier).
  Plain word lists, editable without touching logic. Nothing is refused.
- `screen_tools.py` — publishes `computer` and `browser` as in-process MCP
  tools on the work lane, next to Bash and Read. Gate + JSONL ledger +
  live emit to the left panel live here.

TRAPS HIT, all fixed — do not re-learn these:
- The SDK's shorthand `{name: type}` schema marks EVERY field required, and
  the model dutifully filled x=0, y=0 — a click on the screen corner. Real
  JSON Schema with `required: ["action"]` fixed it AND cut the probe cost 63%.
- GOAT's own always-on-top window ate the clicks aimed at the app underneath,
  and screenshots showed GOAT instead of the work. Hence stand_aside; the
  persona now requires hide_self before driving another app.
- A browser launched from a shell dies with that shell (job object). Needs
  CREATE_BREAKAWAY_FROM_JOB, not just DETACHED_PROCESS.
- `"://" in url` is not a scheme test: it turned `data:text/html,...` into
  `https://data:text/html,...`. Match `^[a-z][a-z0-9+.-]*:` instead.
- Chrome's `window.screenY` is the WINDOW top, not the viewport's, and the
  chrome above the page is a different height everywhere. element_coords
  calibrates against the Win32 client rect instead of doing that arithmetic.
- Arrows/Delete/Home/End need KEYEVENTF_EXTENDEDKEY or the app receives the
  numpad twin. Tested.
- Tk (and anything not DPI aware) reports click coordinates in a scaled space
  that does not match the physical pixel sent — the live test opts in to
  per-monitor DPI so the two agree.

VERIFIED, not assumed: `test_screen.py` 67/67 (fires nothing, safe to run
while he works), `test_screen_live.py` 9/9 (opens its own window and really
drives it — Georgian text arrives exactly, click lands 0px off, chords and
wheel all arrive), `test_engine_router.py` still 100/100, preflight PASS.
End-to-end through the real SDK: the model called `computer`, got the image
back, and read his screen correctly.


## 2026-09-27 — daily self-update (his order: autonomous, tell me only when updated)
- python/self_update.py: 24h check of claude-agent-sdk (engine, py3.13), global Claude Code CLI (`claude update`), and new Opus/Sonnet/Fable ids on docs.claude.com (probed via bundled CLI; announced, NOT auto-switched).
- goat_app._update_watch: every 30 min; pending engine update → restarts via restart-goat.ps1 after 20 min idle. restart-goat installs between kill and relaunch; watchdog reverts engine on boot crash.
- Results queued in python/update-notice.txt → prepended to his next turn once.
- Lesson: Python urllib rejected docs.claude.com cert (expired chain) — use curl.exe there.

- 2026-09-27 00:45 LESSON: never send alt+f4 via computer tool to close an app - it closed GOAT itself. Close apps by process/taskkill. Also: start-goat-app.vbs relaunch silently failed twice (restart-goat logged BOOT CRASH + rollback, no python spawned); direct 'py -3.13 -u ui_qt.py' worked. Launcher needs a look.
- 2026-09-27 00:56 FIXED (launcher + alt+f4): root cause of the failed relaunch = orphaned engine claude.exe held goat-app.log (inherited >> handle) -> launcher MoveFile 'Permission denied' -> vbs died before launch -> watchdog rolled back innocent code. Now: child_guard.py puts every engine in a kill-on-close Job Object (engine dies with GOAT, apps it opened survive); launcher kills orphan engines + logs to goat-app.<stamp>.log if the log is still held; restart-goat.ps1 kills orphans, treats 'never started' as a launch failure (retry, no rollback), verifies the rollback relaunch; screen_hands refuses alt+f4/ctrl+f4 while GOAT's own window has focus, even with confirm=true. Tests: test_child_guard.py.
