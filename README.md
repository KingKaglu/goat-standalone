# GOAT 🐐

**English** | [ქართული](#goat--ქართულად)

A JARVIS-style AI desktop assistant for Windows, powered by Claude. Voice-first: you talk to it, it talks back — while having full access to your machine (files, shell, web) to actually build things with you, not just chat.

Built by [KingKaglu](https://github.com/KingKaglu) as a personal assistant. It's a real, working app — but it was built for one person's machine, so read [Can *you* run it?](#can-you-run-it) before cloning.

---

## What it does

- **Voice-first conversation** — always listening (with echo cancellation, so it doesn't hear itself), transcribes locally with Whisper, answers out loud with a natural voice. Typing is first-class too: a permanent input line at the bottom.
- **Voice barge-in** — interrupt it mid-sentence to steer or stop it, like a real conversation. If it's mid-task, your interruption mutes the voice while the work continues, and a "front desk" brain answers you in parallel.
- **It transcribes while you are still talking** — the ear is a streaming WebSocket (ElevenLabs Scribe v2 Realtime), not an upload-and-wait, so the transcript is essentially finished the moment you stop. Measured on the same audio: batch **1089–2594ms** after the last word, streaming **134–554ms**; live turns here commit in **194–240ms**. How long GOAT waits before deciding you're done adapts to what you said — a short command gets 480ms of quiet, a long thought keeps the full 700ms so a mid-sentence pause is never cut off — and the ear is told to start wrapping up 200ms into that silence, so the two waits overlap instead of stacking. Every voice turn prints its own budget (`endpoint + ear + think/voice = total to first sound`) and the total shows on the panel. `GOAT_STT_REALTIME=off` returns to the batch ear; failure there falls back to it automatically, so the worst case is the old latency, never a lost sentence.
- **Two-engine model router with token economy** — casual conversation runs on **Google's Gemini Flash** (free tier — zero Claude usage for chat) and can even fire off quick machine actions itself (open an app, a short shell command); anything needing real tools or heavy reasoning it hands to Claude by replying `ESCALATE`. Work verbs route straight to the Claude **working brain (Fable 5)**. If Gemini is down or out of quota, chat falls back to Claude automatically — an optimization, never a single point of failure. Heavy sessions stick to one model (prompt-cache friendly) and self-compact past 60k tokens.
- **Screen control (eyes and hands)** — GOAT sees your actual screen and drives the real mouse and keyboard, so anything that only exists as pixels is reachable: a GUI app, a settings toggle, a dialog with no command-line equivalent. Screenshots come back with a coordinate grid whose labels are real screen pixels, so aiming is read off the picture rather than guessed. Its own always-on-top window steps aside while it works and returns afterwards. A safety gate holds back the irreversible and the outward-facing — payments, deletions, sending, and *everything* inside a banking or checkout window — until you say yes; nothing is forbidden, it just asks first. Every action is logged to `python/screen-actions.jsonl` and shown live on the work panel. `GOAT_SCREEN=off` parks the hands (eyes stay open).
- **Browser control** — tabs and page elements as objects, not pixels: list, open, close, switch, navigate, read page text, run JavaScript, click and fill by CSS selector, and get an element's real screen coordinates when only a physical click will do. It drives a browser on its own profile over the Chrome DevTools Protocol (Chrome forbids debugging the everyday profile); for a tab in the window you are already using, it uses the browser's own tab search instead.
- **Full agent tools** — read/write files, run shell commands, search the web. It builds projects in its `workspace/` folder.
- **Self-growing skill library** — it writes its own reusable skills into `workspace/.claude/skills/` (long-term memory, machine health, file map, self-upgrade procedure…).
- **Self-edit safety net** — when it edits its own code, a preflight check validates the change and auto-rolls back if it would break the app.
- **Watchers** — a background power watcher speaks up when AC drops or the battery runs low (`GOAT_WATCH=off` to disable).
- **Orders are obeyed, not discussed** — say "fix the build", "deploy fasmetri", "გაასწორე ბილდი" and it goes straight to the working brain; GOAT acknowledges out loud instantly (pre-synthesized, ~0ms) and says one line when the job lands. Questions, opinions and trivia ("what time is it", "how do I fix this?") stay on the fast talking lane.
- **It knows when it can't hear you** — a quiet microphone used to produce confident nonsense rather than silence: at −26 dBFS the Georgian ear returns nothing and the English one invents fluent English over Georgian speech. Now every utterance is lifted to a level the models can actually read (ears only — the voice detector and noise gate still see the raw signal, so a quiet mic costs nothing), and below −18 dBFS a transcript with no Georgian in it is dropped instead of answered, with GOAT saying once that the mic is too quiet. Answering a hallucination is worse than admitting deafness.
- **Bilingual hearing** — speak Georgian or English and GOAT answers in the language you used, sentence by sentence. The Georgian ear is ElevenLabs Scribe (`scribe_v2`, auto-detect + keyterms); English can stay fully local. Pick `english` / `ქართული` / `ორივე` (both) in the drawer or with Ctrl+L.
- **Collapse to a bubble** — minimize, press the – button, or hit Ctrl+B and the window folds into a small round always-on-top dot in the corner, messenger style. The ring breathes with the live state (listening, thinking, speaking) and marks a reply you haven't seen, so collapsed doesn't mean blind. Drag it anywhere — it remembers where you put it — and click it to open the full window again.
- **Settings drawer** — ≡ or Ctrl+, : talking/working/hard brain, **thinking depth (low…max)**, **language (english / ქართული / ორივე)**, four themes (ember/paper/phosphor/graphite), interface + text size, voice on/off + level, **language (English / ქართული)**, wake word, mic mute, always-on-top, new chat/restart. Rows wrap, so every switch stays reachable at any interface scale. Preferences persist in `ui-config.json`.
- **Georgian mode** — GOAT answers in Georgian, spoken with Microsoft's ka-GE neural voice, and **hears Georgian speech** through Gladia's cloud STT (~4s per utterance, free tier 10h/month) — put your key in `.goat-secrets.json` as `{"gladia_api_key": "..."}` (gitignored). Without a key, voice input stays English and typed Georgian works. Note: in Georgian mode utterance audio goes to Gladia's servers; English mode is 100% local. (Local Whisper Georgian was measured unusable — romanization/hallucinations; `GOAT_STT_KA=on` re-enables that experiment.)
- **Continuity** — recent exchanges persist to `workspace/transcript.jsonl` and repaint (dimmed) after a restart; the Claude session itself resumes too.
- **STT that learns** — mishearings you correct are saved to `stt-fixes.json` and fed back into Whisper's vocabulary prompt, so recognition improves over time.
- **Word-synced text reveal** — the on-screen text appears word-by-word in sync with the actual speech.

## How it works

```
 🎤 mic ──► WebRTC AEC3 echo cancel ──► Silero VAD ──► streaming ear (Scribe v2 Realtime, WebSocket)
                       (adaptive endpoint: 480ms command / 700ms thought)   └─ batch ear (whisper.cpp :3781 · Scribe) on failure
                                                            │ text
                                                            ▼
                                          intent router
                                          ├─ REFLEX lane: device commands, no model at all (~0.2ms match, ~100ms to act)
                                          ├─ talking brain: Gemini Flash (casual chat, free) or Sonnet 5 — replies ESCALATE for real work
                                          └─ working brain: claude-opus-5 (tools)  ·  claude-fable-5-1 (optional)
                                             thinking: adaptive, effort low…max (default max)
                                          (Claude via the Agent SDK — your Claude Code login)
                                                            │ reply
                                                            ▼
 🔊 speaker ◄── Edge TTS "Ava" (online) or Piper (local, offline fallback)
```

1. **Hearing**: the mic runs through WebRTC AEC3 echo cancellation and voice-activity detection, and every captured block is fed *as it arrives* to a streaming ear (`stt_realtime.py` — Scribe v2 Realtime over a WebSocket), so the sentence is being transcribed while it is still being said. Two rules were learned the hard way and are baked in: the realtime language must be **pinned** (on auto-detect it heard Georgian as Russian and returned Cyrillic transliteration — the opposite of the batch endpoint, where auto-detect is the accurate mode), and the turn boundary belongs to **GOAT's own VAD**, not the server's (`commit_strategy=manual`; the server's own VAD split one sentence into several fragments). In bilingual mode two pinned ears run side by side and the **alphabet** decides which one answered — an alphabet is a fact, a confidence score is an opinion. Anything that goes wrong returns nothing and the batch ear (resident `whisper-server.exe`, or Scribe for Georgian) transcribes the full audio GOAT already holds, so a failure costs latency, never a sentence.
2. **Hearing Georgian**: local whisper cannot do Georgian — it romanizes it into English-looking nonsense — so in Georgian or bilingual mode each utterance goes to **ElevenLabs Scribe** (`scribe_v2`) instead, with auto-detect and keyterm hints; Gladia is the fallback route. Measured 2026-09-14: ~1.5s, 7.3% WER on Georgian, and identical to local whisper on English. The language of each utterance is decided by its **alphabet**, not by a confidence score, and it drives the reply language and the voice (`ka-GE-EkaNeural` / `en-US-AvaMultilingualNeural`). Plain English mode never sends audio anywhere.
3. **Reflexes** (`reflex.py`): before any model sees it, an utterance is matched against a deterministic on-device intent table — open a site / app / file / folder, volume, media transport, brightness, lock, window state, screenshot, time, date, battery, in English and Georgian. A match executes straight away through Win32 and the shell, and the acknowledgement is a **pre-synthesised** TTS clip, so the whole round trip is *~0.2ms to decide, ~100ms to act* instead of two cloud round trips. Files and apps resolve through `reflex_index.py`, a name→path index of his Desktop / Downloads / Documents / Start Menu built on a background thread at boot (1144 entries in ~0.1s here) and cached to `workspace/file-index.json`; the folders come from the Windows known-folder API, so OneDrive redirection is followed instead of guessed. A name the index doesn't know triggers a depth-1 re-scan of Desktop and Downloads (~4ms) before giving up, so a file saved or downloaded *after* GOAT booted still opens. Anything the matcher isn't certain about returns nothing and falls through to the brains untouched — a reflex never guesses. Turn the whole lane off with `GOAT_REFLEX=off`.
4. **Thinking**: casual chat is answered by **Google's Gemini Flash** (`local_llm.py` — `gemini-3.8-flash`, free tier, keeps its own memory of the conversation, and can even do quick machine actions like opening an app). It replies `ESCALATE` for anything needing real tools or heavy work, which re-runs the message on Claude via the **Claude Agent SDK**; obvious work (fix/build/search/run…) skips straight there. The Claude side runs **Opus 5** for tool work, with **Fable 5.1** selectable in the drawer, and thinks adaptively at the effort you pick (low…max, default **max**) — the reasoning summary streams into the left panel as it works. Past 60k tokens it compacts itself and carries on. There is **no Claude API key in this repo** — the SDK uses your own local Claude Code sign-in. The Gemini key goes in `.goat-secrets.json` as `{"gemini_api_key": "..."}` (or the `GEMINI_API_KEY` env var); without it, chat simply falls back to Claude. Swap the fast model with `GOAT_GEMINI_MODEL`. **Latency guard**: Gemini 3 models always think — Google's docs state reasoning cannot be disabled on them, so the `reasoning_effort: "none"` GOAT sends is accepted and ignored. Measured here 2026-09-15 on "say ok": `gemini-3.8-flash` **19.0s**, `gemini-3.5-flash` **1.3s**, `gemini-3.5-flash-lite` **0.7s**. A talking brain that blows `GOAT_GEMINI_SLOW_TTFT` (default 6s) to first token twice in a row demotes itself to `GOAT_GEMINI_FALLBACK` for 15 minutes, and a model that has sent nothing after `GOAT_GEMINI_TTFT_ABORT` (default 9s) is abandoned mid-request for the fallback. Your configured pick is never rewritten — the demotion is runtime only and lapses on its own.
5. **Speaking**: replies stream to Microsoft Edge TTS (the "Ava" voice) when online, or fall back to Piper, a fully local TTS, when offline. The *first* breath goes out on a clause boundary rather than a full stop (`GOAT_FIRST_CLAUSE_MIN`, 28 characters — a fragment shorter than that waits, because "Yes," alone sounds like a glitch); later sentences are never split at their commas. If the answer itself is slow, one short listening noise covers the gap after `GOAT_BACKCHANNEL_AFTER` (0.9s) — once per turn, never over a reply that has already started, and pre-synthesised like the acknowledgements so it costs no network. `GOAT_BACKCHANNEL=off` silences it.
6. **Measuring it**: the target is under 500ms from "he stopped talking" to "GOAT made a sound", because human conversational gaps average ~200ms. A target nobody measures is a wish, so every voice turn prints its own ledger — `endpoint + ear + think/voice = total`, back-dated to the moment the sound actually stopped so the hangover he sat through is counted, not hidden — marked ✓ under 500ms, · under a second, ! beyond. Where it stands: reflex commands land inside the budget, and on a chat turn the ear is no longer the cost (endpoint 480ms + ear ~200ms) — the talking brain's first token and the first TTS clip are, at ~900ms on a warm turn.
7. **UI**: a minimal native window (PySide6/Qt) with live captions, Ctrl+K to type instead of talk, and drag-and-drop files for analysis.

### Repo layout

```
goat-standalone/
├── python/              # The app
│   ├── ui_qt.py         # Desktop window (PySide6) — the normal entry point
│   ├── goat_app.py      # Brain-stem: Claude SDK, model router, persona, TTS/STT glue
│   ├── screen_hands.py  # Eyes + hands: screenshots with a real-pixel grid, Win32 SendInput mouse/keyboard
│   ├── screen_browser.py# Browser control over the Chrome DevTools Protocol (tabs, DOM, element coords)
│   ├── screen_policy.py # Which screen actions need a yes first (editable word lists)
│   ├── screen_tools.py  # Publishes `computer` + `browser` to the working brain as in-process MCP tools
│   ├── reflex.py        # Reflex lane: deterministic device commands, zero model, EN + KA
│   ├── reflex_index.py  # Name→path index of his files, folders and installed apps
│   ├── local_llm.py     # Fast talking brain: Google Gemini Flash (free chat, quick hands, ESCALATE protocol)
│   ├── audio_io.py      # Duplex audio + WebRTC AEC3 echo cancellation
│   ├── stt_realtime.py  # Streaming ear: Scribe v2 Realtime over a WebSocket, one pinned session per language
│   ├── stt_whisper.py   # Whisper server client (spawns it if not running)
│   ├── tts_edge.py      # Edge TTS (Ava voice, online)
│   ├── tts_piper.py     # Piper TTS (local fallback)
│   ├── self_check.py    # Self-edit safety gate (validate + auto-rollback)
│   ├── goat_doctor.py   # Diagnostics — run this when something's wrong
│   └── requirements.txt
├── stt/                 # ← you put whisper.cpp binaries + models here (not in repo)
├── tts/                 # ← you put Piper + voice model here (not in repo)
├── workspace/           # Where GOAT builds projects and grows skills
├── stt-fixes.json       # Learned speech-recognition corrections
├── GOAT.bat             # Windows launcher
└── STATE.md             # Dev journal / handoff notes (how it evolved)
```

## Can *you* run it?

Honest checklist — all of these are **required**:

| Requirement | Why |
|---|---|
| **Windows 10/11** | Launchers, audio stack, and the prebuilt whisper/piper binaries are Windows-only. |
| **Python 3.11+** | The app is Python (PySide6 + asyncio). |
| **[Claude Code](https://claude.com/claude-code) installed & signed in** | The brain. The Agent SDK piggybacks on your Claude Code login — **no API key needed, but you need your own paid Claude subscription**. Without it, GOAT has no mind. |
| **A microphone + speakers** | It's voice-first. (Ctrl+K typing works too.) |
| **Internet** | For Claude and the Ava voice. (Piper covers voice offline, but the brain needs the network.) |
| **~750 MB disk for models** | Whisper + Piper models, downloaded separately (below). |

Also know:

- **English only (speech)**: Whisper runs the `base.en` English model. It will not transcribe Georgian or other languages out of the box (you can swap in a multilingual `ggml` model yourself).
- **The persona is personal.** `PERSONA` in `python/goat_app.py` is written for Giorgi by name, and `stt_whisper.py`'s `SEED_VOCAB` biases recognition toward his vocabulary. **Edit both before using it as your own** — replace the name, tweak the character, change the vocab.
- **It has real hands.** GOAT can read/write files and run shell commands on your machine. That's the point, but understand it before you run it.
- **Model IDs may need updating.** `MODEL_FULL` (Opus 5, the working brain) and `MODEL_FABLE` at the top of `goat_app.py` name specific Claude models; if your account doesn't have them, set ones you do have. Note that the Fable tier bills from its own credit bucket — on an account without those credits it answers *"You're out of usage credits"*, which is why Opus 5 is the default and `fallback_model` is wired to it.
- **Free chat brain is optional but recommended.** Drop a Google [Gemini](https://ai.google.dev) key into `.goat-secrets.json` as `{"gemini_api_key": "..."}` — casual conversation then runs on Gemini Flash's free tier and costs zero Claude usage. No key = GOAT quietly uses Claude for everything, as before.

## Install

### 1. Clone + Python deps

```bash
git clone https://github.com/KingKaglu/goat-standalone.git
cd goat-standalone/python
pip install -r requirements.txt
```

### 2. Download the voice stack (not in the repo — too big for GitHub)

**Whisper (hearing):**

1. Download a Windows x64 release of [whisper.cpp](https://github.com/ggml-org/whisper.cpp/releases) (you need `whisper-server.exe` and its DLLs).
2. Put the binaries in `stt/bin/Release/` so that `stt/bin/Release/whisper-server.exe` exists.
3. Download the model [`ggml-base.en.bin`](https://huggingface.co/ggerganov/whisper.cpp/tree/main) (~148 MB) into `stt/`.
   - Optional: also grab `ggml-small.en.bin` and set `GOAT_STT_MODEL=small` for higher accuracy at ~3× the latency.

**Piper (offline voice fallback):**

1. Download a Windows release of [Piper](https://github.com/rhasspy/piper/releases) and extract it into `tts/piper/` so that `tts/piper/piper.exe` exists.
2. Download the voice [`en_GB-alan-low.onnx` + its `.json`](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_GB/alan/low) into `tts/`.
   - Piper is only the fallback — the primary "Ava" voice is Microsoft Edge TTS and needs no download, just internet.

### 3. Make sure Claude Code is signed in

```bash
claude --version   # should print a version; sign in if you haven't
```

### 4. Run

```bash
cd python
python ui_qt.py
```

Or double-click **`GOAT.bat`** in the project root (silent launch, single-instance aware).

Headless mode (no window, console only): `python goat_app.py`.

## Using it

- **Just talk.** It's always listening. Speak normally; pause; it answers.
- **Interrupt it** by talking over it — it stops (or, if it's mid-task, mutes the voice and keeps working while the front-desk brain answers you).
- **Type any time** — the input line at the bottom is always there (Ctrl+K focuses it). Drop or paste files into the window for analysis.
- **≡ or Ctrl+,** — settings drawer: brains, thinking depth, themes, text size, voice/level, wake word, mic mute, always-on-top, copy reply, new chat, restart.
- **Ctrl+T** — cycle themes without opening the drawer.
- **Ctrl+E** — step the working brain's thinking depth (low → medium → high → xhigh → max). The engine reopens its session at the new depth and keeps the conversation.
- **Ctrl+L** — cycle the language: english → ქართული → ორივე (bilingual).
- **"stop" / "cancel" / "hold on"** — kills the current task.
- **"restart GOAT"** — fresh session (context is otherwise kept for the whole session).
- **"run diagnostics" / "are you okay"** — GOAT runs `goat_doctor.py` and reports.

## Developing

```bash
cd python
python self_check.py preflight   # compile + import gate (GOAT's own safety net)
python test_engine_router.py     # router / compaction / front-desk suite — no audio, no API cost
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `[WinError 10061]` / STT connection refused | Whisper server isn't up. Check `stt/bin/Release/whisper-server.exe` and the model file exist; the app auto-starts it on port **3781**. |
| GOAT hears itself / echoes | AEC needs mic and speakers on the same clock — use the laptop's own mic+speakers or a headset; check `python/aec_run.log`. |
| No voice output | Online? Edge TTS needs internet. Offline fallback needs `tts/piper/piper.exe` + the voice model. |
| "No mind" / auth errors | Claude Code not signed in on this machine, or your subscription lacks the configured models — edit `MODEL_FULL`/`MODEL_OPUS` in `goat_app.py`. |
| Anything else | `cd python && python goat_doctor.py` — checks process, hearing, voice route, logs, session. |

## License & credits

Personal project — MIT-spirit: do what you want with it, no warranty.

Built on: [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview) (Anthropic) · [PySide6](https://doc.qt.io/qtforpython/) · [whisper.cpp](https://github.com/ggml-org/whisper.cpp) · [Piper](https://github.com/rhasspy/piper) · [Silero VAD](https://github.com/snakers4/silero-vad) · WebRTC AEC3 · [Edge TTS](https://github.com/rany2/edge-tts)

---

---

# GOAT 🐐 — ქართულად

JARVIS-ის სტილის AI დესკტოპ-ასისტენტი Windows-ისთვის, Claude-ზე აგებული. ხმით მუშაობს: შენ ელაპარაკები — ის გპასუხობს ხმით, და ამავდროულად აქვს სრული წვდომა შენს კომპიუტერზე (ფაილები, ტერმინალი, ინტერნეტი), რომ რეალურად ააწყოს პროექტები შენთან ერთად და არა უბრალოდ ისაუბროს.

შექმნილია [KingKaglu](https://github.com/KingKaglu)-ს მიერ პირად ასისტენტად. მუშა აპლიკაციაა, მაგრამ ერთი ადამიანის კომპიუტერისთვის აიგო — ამიტომ კლონირებამდე წაიკითხე [გაგიშვებს თუ არა შენთან?](#გაგიშვებს-თუ-არა-შენთან)

## რას აკეთებს

- **ეკრანის კონტროლი (თვალები და ხელები)** — GOAT ხედავს შენს ნამდვილ ეკრანს და მართავს ნამდვილ მაუსსა და კლავიატურას, ასე რომ ყველაფერი, რაც მხოლოდ პიქსელებად არსებობს, ხელმისაწვდომია: GUI აპლიკაცია, პარამეტრის გადამრთველი, დიალოგი, რომელსაც ბრძანების ხაზი არ აქვს. სქრინშოტს თან ახლავს კოორდინატების ბადე, რომლის წარწერები რეალური ეკრანის პიქსელებია — მიზანში ამოღება სურათიდან იკითხება და არა გამოცნობით. მისივე ყოველთვის-ზემოთა ფანჯარა მუშაობისას განზე დგება და შემდეგ ბრუნდება. უსაფრთხოების ზღვარი აკავებს შეუქცევადსა და გარეთ მიმართულს — გადახდას, წაშლას, გაგზავნას და *ყველაფერს* საბანკო ან გადახდის ფანჯარაში — სანამ არ დაეთანხმები; არაფერია აკრძალული, უბრალოდ ჯერ იკითხავს. ყოველი მოქმედება იწერება `python/screen-actions.jsonl`-ში და ჩანს სამუშაო პანელზე. `GOAT_SCREEN=off` ხელებს აჩერებს (თვალები ღია რჩება).
- **ბაბლში ჩაკეცვა** — მინიმიზაცია, – ღილაკი ან Ctrl+B და ფანჯარა იკეცება პატარა მრგვალ, ყოველთვის-ზემოთა წერტილად კუთხეში, მესენჯერის სტილში. რგოლი სუნთქავს მიმდინარე მდგომარეობასთან ერთად (გისმენს, ფიქრობს, ლაპარაკობს) და ნიშნავს პასუხს, რომელიც ჯერ არ გინახავს — ჩაკეცილი არ ნიშნავს ბრმას. გადაათრიე სადაც გინდა (პოზიციას იმახსოვრებს) და დააწკაპუნე, რომ სრული ფანჯარა დაბრუნდეს.
- **ბრაუზერის კონტროლი** — ტაბები და გვერდის ელემენტები ობიექტებად, პიქსელებად კი არა: სია, გახსნა, დახურვა, გადართვა, ნავიგაცია, გვერდის ტექსტის წაკითხვა, JavaScript-ის გაშვება, CSS სელექტორით დაჭერა და შევსება, და ელემენტის რეალური ეკრანული კოორდინატები, როცა მხოლოდ ფიზიკური დაჭერა შველის. მუშაობს საკუთარ პროფილზე Chrome DevTools Protocol-ით (Chrome ყოველდღიური პროფილის დებაგს კრძალავს); იმ ტაბისთვის, რომელსაც უკვე იყენებ, ბრაუზერის საკუთარ tab search-ს იყენებს.
- **ხმოვანი საუბარი** — მუდმივად გისმენს (ექოს გაუქმებით, საკუთარ თავს არ ისმენს), მეტყველებას ლოკალურად შიფრავს Whisper-ით და ხმით გპასუხობს. წერაც სრულფასოვანია: მუდმივი შესაყვანი ველი ეკრანის ბოლოში.
- **გისმენს მაშინ, როცა ჯერ კიდევ ლაპარაკობ** — ყური სტრიმინგია (ElevenLabs Scribe v2 Realtime, WebSocket), ანუ აღარ ელოდება სრულ ჩანაწერს: ტექსტი პრაქტიკულად მზადაა იმ წამს, როცა ჩუმდები. იმავე აუდიოზე გაზომილი: batch **1089–2594მწმ** ბოლო სიტყვის შემდეგ, სტრიმინგი **134–554მწმ**; ცოცხალ საუბარში — **194–240მწმ**. რამდენს დაელოდება, სანამ გადაწყვეტს რომ დაასრულე, შენს ნათქვამზეა დამოკიდებული: მოკლე ბრძანებას 480მწმ სიჩუმე ჰყოფნის, გრძელ ფიქრს სრული 700მწმ რჩება, რომ შუა წინადადებაში არ გაწყვეტინოს; ყურს კი სიჩუმის 200-ე მილიწამზე ეუბნება „შეიძლება დაასრულა", ასე რომ ორი ლოდინი ერთმანეთს ედება და არა ერთმანეთს ემატება. ყოველი ხმოვანი დიალოგი თავის ბიუჯეტს ბეჭდავს (`endpoint + ear + think/voice`) და ჯამი პანელზე ჩანს. `GOAT_STT_REALTIME=off` აბრუნებს ძველ ყურს; შეცდომისას თვითონვე გადადის მასზე — ყველაზე ცუდი შემთხვევა ძველი სიჩქარეა და არა დაკარგული წინადადება.
- **საუბრის შეწყვეტა (barge-in)** — შუა წინადადებაში შეგიძლია შეაწყვეტინო; თუ დავალებაზე მუშაობს, ხმა ჩუმდება, მუშაობა გრძელდება და „მისაღების" ტვინი პარალელურად გპასუხობს.
- **ორძრავიანი როუტერი ტოკენების ეკონომიით** — ჩვეულებრივ საუბარს **Google-ის Gemini Flash** უძღვება (უფასო დონე — Claude-ის ლიმიტს საერთოდ არ ხარჯავს) და სწრაფი მოქმედებებიც შეუძლია (აპლიკაციის გახსნა, მოკლე ბრძანება); რეალურ სამუშაოს ან მძიმე ფიქრს კი `ESCALATE` პასუხით Claude-ს გადასცემს. სამუშაო ზმნები პირდაპირ Claude-ის **მუშა ტვინთან (Fable 5)** მიდის. თუ Gemini მიუწვდომელია ან ლიმიტი ამოეწურა, საუბარი ავტომატურად Claude-ზე გადადის — ეს ოპტიმიზაციაა, კრიტიკული წერტილი არა. მძიმე სესია ერთ მოდელზე რჩება (ქეშისთვის) და 60k ტოკენის შემდეგ თვითონვე იკუმშება.
- **სრული აგენტური ხელსაწყოები** — ფაილების კითხვა/წერა, ტერმინალის ბრძანებები, ვებ-ძიება. პროექტებს `workspace/` საქაღალდეში აშენებს.
- **თვითმზარდი უნარების ბიბლიოთეკა** — საკუთარ განმეორებად უნარებს თვითონვე წერს `workspace/.claude/skills/`-ში (გრძელვადიანი მეხსიერება, ლეპტოპის ჯანმრთელობა, ფაილების რუკა, თვითგანახლების პროცედურა…).
- **თვითრედაქტირების დამცავი ბადე** — როცა საკუთარ კოდს ასწორებს, წინასწარი შემოწმება ცვლილებას ამოწმებს და გაფუჭების შემთხვევაში ავტომატურად აბრუნებს.
- **მეთვალყურეები** — ფონური კვების მეთვალყურე ხმამაღლა გაფრთხილებს, როცა დენი წყდება ან ბატარეა იწურება (`GOAT_WATCH=off` თიშავს).
- **ბრძანებას ასრულებს, არ განიხილავს** — თქვი "გაასწორე ბილდი" ან "deploy fasmetri" და პირდაპირ მუშა ტვინთან მიდის; GOAT მაშინვე ხმამაღლა დაგიდასტურებს და დასრულებისას ერთ წინადადებას გეტყვის. კითხვები და წვრილმანი ("რა დროა") სწრაფ ლეინზე რჩება.
- **ორენოვანი სმენა** — ილაპარაკე ქართულად ან ინგლისურად და GOAT იმავე ენაზე გიპასუხებს, წინადადება-წინადადებაზე. ქართულ ყურს ElevenLabs Scribe (`scribe_v2`) აკეთებს; პანელში ან Ctrl+L-ით აირჩიე `english` / `ქართული` / `ორივე`.
- **პარამეტრების პანელი** — ≡ ან Ctrl+, : მოსაუბრე/მუშა/მძიმე ტვინი, **ენა (english / ქართული / ორივე)**, **აზროვნების სიღრმე (low…max)**, ოთხი თემა (ember/paper/phosphor/graphite), ინტერფეისისა და ტექსტის ზომა, ხმა ჩართვა/გამორთვა + სიმაღლე, **ენა (English / ქართული)**, გამოღვიძების სიტყვა, მიკროფონის დადუმება, ყოველთვის-ზემოთ, ახალი საუბარი/გადატვირთვა. პარამეტრები ინახება `ui-config.json`-ში.
- **ქართული რეჟიმი** — GOAT ქართულად გპასუხობს Microsoft-ის ka-GE ნეირონული ხმით და **ქართულ მეტყველებასაც ისმენს** Gladia-ს ღრუბლოვანი STT-ით (~4წმ ფრაზაზე, უფასო 10სთ/თვეში) — გასაღები ჩაწერე `.goat-secrets.json`-ში: `{"gladia_api_key": "..."}` (git-ში არ ხვდება). გასაღების გარეშე ხმოვანი შეყვანა ინგლისურად რჩება, ქართულად წერა კი მუშაობს. გაითვალისწინე: ქართულ რეჟიმში ხმის ჩანაწერები Gladia-ს სერვერებზე მიდის; ინგლისური რეჟიმი 100% ლოკალურია.
- **უწყვეტობა** — ბოლო საუბრები ინახება `workspace/transcript.jsonl`-ში და გადატვირთვის შემდეგ ეკრანზე ბრუნდება (მიმქრალებული); Claude-სესიაც გრძელდება.
- **მეტყველების ამოცნობა, რომელიც სწავლობს** — შესწორებული შეცდომები ინახება `stt-fixes.json`-ში და Whisper-ის ლექსიკონს უბრუნდება, ასე რომ ამოცნობა დროთა განმავლობაში უმჯობესდება.
- **სიტყვა-სიტყვით სინქრონული ტექსტი** — ეკრანზე ტექსტი ზუსტად ისე ჩნდება, როგორც ხმა წარმოთქვამს.

## როგორ მუშაობს

1. **სმენა**: მიკროფონი გადის WebRTC AEC3 ექოს გაუქმებას და ხმის აქტივობის დეტექციას, შემდეგ აუდიო მიდის ლოკალურ `whisper-server.exe`-ზე (whisper.cpp), რომელიც მუდმივად ჩართულია — ტრანსკრიფცია ~1 წამში.
2. **აზროვნება**: ჩვეულებრივ საუბარს **Google-ის Gemini Flash** პასუხობს (`local_llm.py` — უფასო დონე, საკუთარ მეხსიერებას ინახავს, სწრაფი მოქმედებებიც შეუძლია). რეალურ სამუშაოზე ის `ESCALATE`-ს პასუხობს და შეტყობინება Claude-ზე გადადის **Claude Agent SDK**-ით; აშკარა სამუშაო (fix/build/search/run…) პირდაპირ მიდის. Claude-ის მხარე **Opus 5**-ს იყენებს ხელსაწყოებისთვის (**Fable 5.1** პანელიდან ირჩევა) და ფიქრობს ადაპტურად შენ მიერ არჩეული სიღრმით (low…max, ნაგულისხმევი **max**) — მსჯელობის შეჯამება მარცხენა პანელში იშლება. **ამ რეპოზიტორიაში Claude-ის API გასაღები არ არის** — SDK შენს ლოკალურ Claude Code ავტორიზაციას იყენებს. Gemini-ს გასაღები ჩაწერე `.goat-secrets.json`-ში: `{"gemini_api_key": "..."}`; მის გარეშე საუბარი Claude-ზე გადადის.
3. **ლაპარაკი**: პასუხები Microsoft Edge TTS-ით („Ava"-ს ხმა) ჟღერს, ინტერნეტის გარეშე კი Piper-ზე — სრულად ლოკალურ TTS-ზე — გადადის.
4. **ინტერფეისი**: მინიმალისტური ნატიური ფანჯარა (PySide6/Qt): ცოცხალი სუბტიტრები, Ctrl+K ტექსტით მისაწერად, ფაილების ჩაგდება ანალიზისთვის.

## გაგიშვებს თუ არა შენთან?

გულწრფელი ჩამონათვალი — ყველა პუნქტი **აუცილებელია**:

- **Windows 10/11** — გამშვებები, აუდიო-სისტემა და whisper/piper-ის ბინარები Windows-ისთვისაა.
- **Python 3.11+**
- **[Claude Code](https://claude.com/claude-code) დაყენებული და ავტორიზებული** — ეს არის ტვინი. API გასაღები არ გჭირდება, მაგრამ **გჭირდება საკუთარი ფასიანი Claude გამოწერა**. მის გარეშე GOAT-ს გონება არ აქვს.
- **მიკროფონი და დინამიკები** — ხმოვანი აპლიკაციაა (Ctrl+K-თი წერაც შეიძლება).
- **ინტერნეტი** — Claude-სა და Ava-ს ხმისთვის.
- **~750 MB ადგილი მოდელებისთვის** — Whisper და Piper ცალკე იტვირთება (ქვემოთ).

ასევე გაითვალისწინე:

- **მეტყველება მხოლოდ ინგლისურად**: Whisper-ს ინგლისური `base.en` მოდელი უზის. ქართულს (და სხვა ენებს) პირდაპირ ვერ გაშიფრავს — შეგიძლია თვითონ ჩაანაცვლო მრავალენოვანი `ggml` მოდელით.
- **პერსონა პირადულია.** `python/goat_app.py`-ში `PERSONA` გიორგისთვისაა დაწერილი სახელით, ხოლო `stt_whisper.py`-ის `SEED_VOCAB` მის ლექსიკაზეა მორგებული. **სანამ საკუთარ ასისტენტად გამოიყენებ, ორივე შეცვალე** — სახელი, ხასიათი, ლექსიკა.
- **ნამდვილი ხელები აქვს.** GOAT-ს შეუძლია შენს კომპიუტერზე ფაილების წერა და ბრძანებების გაშვება. ეს მისი დანიშნულებაა, მაგრამ გაშვებამდე ეს კარგად გქონდეს გააზრებული.
- **მოდელების ID-ები შეიძლება შესაცვლელი იყოს.** `goat_app.py`-ის თავში `MODEL_FULL` (Opus 5, მუშა ტვინი)/`MODEL_FABLE` კონკრეტულ Claude მოდელებს ასახელებს; თუ შენს ანგარიშს ისინი არ აქვს, ჩაწერე ის მოდელები, რომლებიც გაქვს.

## დაყენება

### 1. კლონირება + Python-ის პაკეტები

```bash
git clone https://github.com/KingKaglu/goat-standalone.git
cd goat-standalone/python
pip install -r requirements.txt
```

### 2. ხმის კომპონენტების ჩამოტვირთვა (რეპოში არ დევს — GitHub-ისთვის ზედმეტად დიდია)

**Whisper (სმენა):**

1. ჩამოტვირთე [whisper.cpp](https://github.com/ggml-org/whisper.cpp/releases)-ის Windows x64 რელიზი (გჭირდება `whisper-server.exe` და მისი DLL-ები).
2. ბინარები ჩადე `stt/bin/Release/`-ში ისე, რომ არსებობდეს `stt/bin/Release/whisper-server.exe`.
3. ჩამოტვირთე მოდელი [`ggml-base.en.bin`](https://huggingface.co/ggerganov/whisper.cpp/tree/main) (~148 MB) და ჩადე `stt/`-ში.
   - სურვილისამებრ: `ggml-small.en.bin`-იც აიღე და დააყენე `GOAT_STT_MODEL=small` — მეტი სიზუსტე, ~3-ჯერ ნელი.

**Piper (ხმის ოფლაინ-სათადარიგო):**

1. ჩამოტვირთე [Piper](https://github.com/rhasspy/piper/releases)-ის Windows რელიზი და ამოალაგე `tts/piper/`-ში ისე, რომ არსებობდეს `tts/piper/piper.exe`.
2. ჩამოტვირთე ხმა [`en_GB-alan-low.onnx` + მისი `.json`](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_GB/alan/low) და ჩადე `tts/`-ში.
   - Piper მხოლოდ სათადარიგოა — მთავარი „Ava"-ს ხმა Edge TTS-ია, ჩამოტვირთვა არ სჭირდება, მხოლოდ ინტერნეტი.

### 3. დარწმუნდი, რომ Claude Code ავტორიზებულია

```bash
claude --version   # ვერსია უნდა დაბეჭდოს; თუ არა — გაიარე ავტორიზაცია
```

### 4. გაშვება

```bash
cd python
python ui_qt.py
```

ან პროექტის ძირში ორჯერ დააწკაპუნე **`GOAT.bat`**-ზე.

Headless რეჟიმი (ფანჯრის გარეშე, კონსოლში): `python goat_app.py`.

## გამოყენება

- **უბრალოდ ელაპარაკე.** მუდმივად გისმენს. ილაპარაკე ჩვეულებრივ, გაჩერდი — გიპასუხებს.
- **შეაწყვეტინე** ლაპარაკით — გაჩერდება (ან, თუ დავალებაზე მუშაობს, ხმას ჩაიდუმებს და მუშაობას გააგრძელებს, სანამ „მისაღების" ტვინი გპასუხობს).
- **წერე ნებისმიერ დროს** — შესაყვანი ველი ეკრანის ბოლოში ყოველთვის დგას (Ctrl+K აფოკუსებს). ფაილები ჩააგდე ან ჩასვი ფანჯარაში ანალიზისთვის.
- **≡ ან Ctrl+,** — პარამეტრები: თემები, ტექსტის ზომა, ხმა/სიმაღლე, გამოღვიძების სიტყვა, მიკროფონი, ყოველთვის-ზემოთ, პასუხის კოპირება, ახალი საუბარი, გადატვირთვა.
- **Ctrl+T** — თემების ცვლა პანელის გახსნის გარეშე.
- **Ctrl+E** — მუშა ტვინის აზროვნების სიღრმის ცვლა (low → medium → high → xhigh → max).
- **Ctrl+L** — ენის გადართვა: english → ქართული → ორივე.
- **"stop" / "cancel" / "hold on"** — მიმდინარე დავალებას აჩერებს.
- **"restart GOAT"** — ახალი სესია (სხვა შემთხვევაში კონტექსტი მთელი სესიის განმავლობაში ინახება).
- **"run diagnostics" / "are you okay"** — GOAT უშვებს `goat_doctor.py`-ს და გატყობინებს.

## პრობლემების მოგვარება

- **`[WinError 10061]` / STT connection refused** — Whisper-სერვერი არ არის ჩართული. შეამოწმე, არსებობს თუ არა `stt/bin/Release/whisper-server.exe` და მოდელის ფაილი; აპი მას **3781** პორტზე თვითონ უშვებს.
- **საკუთარ თავს ისმენს / ექო აქვს** — გამოიყენე ლეპტოპის საკუთარი მიკროფონი+დინამიკები ან ყურსასმენი; ნახე `python/aec_run.log`.
- **ხმა არ ისმის** — ინტერნეტი გაქვს? Edge TTS-ს ქსელი სჭირდება. ოფლაინ-სათადარიგოს სჭირდება `tts/piper/piper.exe` + ხმის მოდელი.
- **„გონება არ აქვს" / ავტორიზაციის შეცდომები** — Claude Code ამ კომპიუტერზე ავტორიზებული არ არის, ან შენს გამოწერას მითითებული მოდელები არ აქვს — შეასწორე `MODEL_FULL`/`MODEL_OPUS` `goat_app.py`-ში.
- **სხვა ყველაფერი** — `cd python && python goat_doctor.py` — ამოწმებს პროცესს, სმენას, ხმის არხს, ლოგებს, სესიას.

## ლიცენზია და მადლობები

პირადი პროექტია — MIT-ის სულისკვეთებით: რაც გინდა, ის უქენი, გარანტიის გარეშე.

აგებულია: [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview) (Anthropic) · [PySide6](https://doc.qt.io/qtforpython/) · [whisper.cpp](https://github.com/ggml-org/whisper.cpp) · [Piper](https://github.com/rhasspy/piper) · [Silero VAD](https://github.com/snakers4/silero-vad) · WebRTC AEC3 · [Edge TTS](https://github.com/rany2/edge-tts)
