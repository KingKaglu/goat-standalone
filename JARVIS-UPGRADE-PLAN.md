# GOAT → JARVIS Upgrade Plan

Study of how JARVIS behaves with Tony Stark, mapped onto GOAT as a concrete roadmap.
Written 2026-07-08. Phase 1 is implemented; later phases are the build queue.

## What makes JARVIS a great teammate (trait analysis)

| # | JARVIS trait | Example with Tony | GOAT translation |
|---|---|---|---|
| 1 | Always on, ambient | No "opening the app" — he's just there | Persistent session, auto-revive ✅ |
| 2 | Instant, terse acknowledgment | "Right away, sir." then does it | "On it." then work + narrate |
| 3 | Narrates operations | "Power at 400% capacity" | Voice narration of tools ✅ |
| 4 | Interruptible mid-task | Tony talks over him constantly | Barge-in + mid-turn interjection ✅ |
| 5 | Anticipates needs | Preloads suits, runs diagnostics unasked | After each task, offer the one obvious next step |
| 6 | Honest pushback | "I advise against it" — but complies after | Flag flaws once, plainly, then follow Giorgi's call |
| 7 | Dry wit | Deadpan jokes, never forced | Humor allowed, sparing, never blocking clarity |
| 8 | Total recall | Remembers every project, preference | Memory files + learn-giorgi skill ✅ |
| 9 | Decodes terse/unclear commands | Fills gaps with judgment | Pattern file + stt-fixes dictionary ✅ |
| 10 | Protects his human | Safety before obedience | Never act on garbled destructive commands ✅ |

## Phase 1 — Personality core (DONE tonight)
- Rewrote GOAT's persona around traits 2, 5, 6, 7: terse acks, anticipation,
  honest pushback, dry wit, copilot-style status reports.
- Infrastructure for 1, 3, 4, 8, 9, 10 was built earlier tonight.

## Phase 2 — Sharper senses
- **Wake word** ✅ DONE (Node 2026-07-08; PORTED to Python app 2026-07-10):
  when idle, GOAT only engages if addressed by name (garble variants:
  goat/goad/ghost/god/coat/go at...). 2-min active-conversation window after
  each exchange — no name needed mid-flow. Typed input and mid-task
  interjections never gated. Disable via env GOAT_WAKE=off.
- **Better voice** ✅ DONE (2026-07-08 afternoon): Piper neural TTS, resident
  process with en_GB-alan (British) loaded — ~200ms per sentence. Default voice
  "GOAT Neural (British)" in the picker; browser voices remain as options and
  automatic fallback. Files in goat/tts/.
- **Voice ID**: react only to Giorgi's voice (speaker embedding, local). TODO —
  last remaining Phase 2 item; research project.

## Phase 3 — Proactive teammate
- **Boot briefing** ✅ DONE (Node 2026-07-08; PORTED to Python app 2026-07-10):
  opening GOAT after 6+ hours away (session-file mtime) triggers an unprompted
  JARVIS-style briefing — time-aware greeting, where we left off, what's first.
  Runs on the fast model ([boot-briefing] tag, no tools) — near-zero cost.
- **Personality v2** ✅ DONE (2026-07-10): PERSONA rewritten around a real
  JARVIS character — calm under fire, dry deadpan wit, signature acks,
  situational awareness (clock / 4am grind), confidence-with-receipts.
  Identity hardened: GOAT is its own AI; Claude/Anthropic never mentioned
  unprompted (MODEL TRUTH kept). Boot greeting now time-of-day aware.

- **Watchers**: GOAT monitors things Giorgi cares about (a site's uptime, a repo,
  a price) and speaks up when something changes. TODO.
- **Idle usefulness**: during long quiet periods, tidy workspace, update memory,
  pre-research the current project's next step. TODO.

## Phase 3.5 — Receptionist ✅ DONE (Node 2026-07-08; PORTED to Python 2026-07-10)
- Python port ("front desk"): second Sonnet 5 session answers instantly while
  Fable works ([main-status] feed, FORWARD to interject, no tools, pre-warmed
  at boot). Barge-in over a work turn now mutes the voice but keeps the work;
  short stop-orders ("stop"/"cancel"/"hold on") brake the work turn.
  Node original, for history:
- Second GOAT session on Haiku fields questions INSTANTLY (~5s incl. voice)
  while the main brain deep-works. Watches a live status feed of main-brain
  activity; answers status/small-talk itself, replies FORWARD to interject
  heavy asks into the main brain. Tested e2e (test-recep.mjs, RECEP-PASS).
- Same patch: silent pre-warm ping at boot — first reply after restart no
  longer pays the load-the-history tax; latency cut via settingSources: []
  (user plugins/hooks no longer load into GOAT's sessions); instant "heard
  you" ack chime in the UI.

## Phase 4 — Hands ✅ SEEDED (2026-07-10)
- **App control** ✅: PERSONA "MACHINE CONTROL" section — GOAT acts directly on
  voice commands (open/close/focus apps, volume/mute, media, kill process,
  Wi-Fi, clipboard, files) through its existing tools; voice-sized confirms.
- **Deep OS integration** ✅ partial: "look at my screen" = GOAT screenshots the
  virtual screen itself (PowerShell → inbox/screen.png) and Reads it. File
  drag-in / paste already live. Remaining: passive clipboard awareness.

## Phase 5 — Growth ✅ SEEDED (2026-07-10)
- **Skill library** ✅: workspace\.claude\skills\ loads at session start
  (setting_sources=["project"]). GOAT writes its own skills when a procedure
  repeats; Giorgi grants them with "learn this as a skill". Meta-skill
  skill-creator defines the format; screen-look is the first ability.
- **Full access** ✅: PERSONA grants the whole machine (workspace = home, not
  cage) and the whole web (WebSearch/WebFetch, check-don't-recall).

Giorgi picks what Phase 2+ item comes first. GOAT keeps this file updated as phases land.
