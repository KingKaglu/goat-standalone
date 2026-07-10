# GOAT 🐐

**English** | [ქართული](#goat--ქართულად)

A JARVIS-style AI desktop assistant for Windows, powered by Claude. Voice-first: you talk to it, it talks back — while having full access to your machine (files, shell, web) to actually build things with you, not just chat.

Built by [KingKaglu](https://github.com/KingKaglu) as a personal assistant. It's a real, working app — but it was built for one person's machine, so read [Can *you* run it?](#can-you-run-it) before cloning.

---

## What it does

- **Voice-first conversation** — always listening (with echo cancellation, so it doesn't hear itself), transcribes locally with Whisper, answers out loud with a natural voice.
- **Voice barge-in** — interrupt it mid-sentence to steer or stop it, like a real conversation.
- **Two-brain model router** — a fast "talking brain" (Claude Sonnet) handles conversation; real work automatically escalates to the full "working brain" (Claude's top model). While the working brain is busy, a "front desk" brain keeps answering you in parallel.
- **Full agent tools** — read/write files, run shell commands, search the web. It builds projects in its `workspace/` folder.
- **Self-growing skill library** — it can write its own reusable skills into `workspace/.claude/skills/`.
- **Self-edit safety net** — when it edits its own code, a preflight check validates the change and auto-rolls back if it would break the app.
- **STT that learns** — mishearings you correct are saved to `stt-fixes.json` and fed back into Whisper's vocabulary prompt, so recognition improves over time.
- **Word-synced text reveal** — the on-screen text appears word-by-word in sync with the actual speech.

## How it works

```
 🎤 mic ──► WebRTC AEC3 echo cancel ──► Silero VAD ──► whisper.cpp server (local, :3781)
                                                            │ text
                                                            ▼
                                          Claude Agent SDK (uses your Claude Code login)
                                          ├─ talking brain: claude-sonnet-5 (fast turns)
                                          └─ working brain: claude-fable-5 (tool use)
                                                            │ reply
                                                            ▼
 🔊 speaker ◄── Edge TTS "Ava" (online) or Piper (local, offline fallback)
```

1. **Hearing**: the mic runs through WebRTC AEC3 echo cancellation and voice-activity detection, then audio goes to a local `whisper-server.exe` (whisper.cpp) that stays resident so transcription takes ~1 second.
2. **Thinking**: text goes to Claude via the **Claude Agent SDK**. Every turn starts on the cheap/fast model; if the request needs real work (tools, files, code), it escalates itself to the full model. There is **no API key in this repo** — the SDK uses your own local Claude Code sign-in.
3. **Speaking**: replies stream to Microsoft Edge TTS (the "Ava" voice) when online, or fall back to Piper, a fully local TTS, when offline.
4. **UI**: a minimal native window (PySide6/Qt) with live captions, Ctrl+K to type instead of talk, and drag-and-drop files for analysis.

### Repo layout

```
goat-standalone/
├── python/              # The app
│   ├── ui_qt.py         # Desktop window (PySide6) — the normal entry point
│   ├── goat_app.py      # Brain-stem: Claude SDK, model router, persona, TTS/STT glue
│   ├── audio_io.py      # Duplex audio + WebRTC AEC3 echo cancellation
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
- **Model IDs may need updating.** `MODEL_FULL` / `MODEL_FAST` at the top of `goat_app.py` name specific Claude models; if your account doesn't have them, set ones you do have.

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
- **Ctrl+K** — type instead of talking. Drop or paste files into the window for analysis.
- **"stop" / "cancel" / "hold on"** — kills the current task.
- **"restart GOAT"** — fresh session (context is otherwise kept for the whole session).
- **"run diagnostics" / "are you okay"** — GOAT runs `goat_doctor.py` and reports.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `[WinError 10061]` / STT connection refused | Whisper server isn't up. Check `stt/bin/Release/whisper-server.exe` and the model file exist; the app auto-starts it on port **3781**. |
| GOAT hears itself / echoes | AEC needs mic and speakers on the same clock — use the laptop's own mic+speakers or a headset; check `python/aec_run.log`. |
| No voice output | Online? Edge TTS needs internet. Offline fallback needs `tts/piper/piper.exe` + the voice model. |
| "No mind" / auth errors | Claude Code not signed in on this machine, or your subscription lacks the configured models — edit `MODEL_FULL`/`MODEL_FAST` in `goat_app.py`. |
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

- **ხმოვანი საუბარი** — მუდმივად გისმენს (ექოს გაუქმებით, საკუთარ თავს არ ისმენს), მეტყველებას ლოკალურად შიფრავს Whisper-ით და ხმით გპასუხობს.
- **საუბრის შეწყვეტა (barge-in)** — შუა წინადადებაში შეგიძლია შეაწყვეტინო და მიმართულება შეუცვალო, როგორც ცოცხალ საუბარში.
- **ორტვინიანი როუტერი** — სწრაფი „მოსაუბრე ტვინი" (Claude Sonnet) საუბარს უძღვება; რეალური სამუშაო ავტომატურად გადადის სრულ „მუშა ტვინზე" (Claude-ის უმძლავრესი მოდელი). სანამ მუშა ტვინი დაკავებულია, „მისაღების" ტვინი პარალელურად გპასუხობს.
- **სრული აგენტური ხელსაწყოები** — ფაილების კითხვა/წერა, ტერმინალის ბრძანებები, ვებ-ძიება. პროექტებს `workspace/` საქაღალდეში აშენებს.
- **თვითმზარდი უნარების ბიბლიოთეკა** — საკუთარ განმეორებად უნარებს თვითონვე წერს `workspace/.claude/skills/`-ში.
- **თვითრედაქტირების დამცავი ბადე** — როცა საკუთარ კოდს ასწორებს, წინასწარი შემოწმება ცვლილებას ამოწმებს და გაფუჭების შემთხვევაში ავტომატურად აბრუნებს.
- **მეტყველების ამოცნობა, რომელიც სწავლობს** — შესწორებული შეცდომები ინახება `stt-fixes.json`-ში და Whisper-ის ლექსიკონს უბრუნდება, ასე რომ ამოცნობა დროთა განმავლობაში უმჯობესდება.
- **სიტყვა-სიტყვით სინქრონული ტექსტი** — ეკრანზე ტექსტი ზუსტად ისე ჩნდება, როგორც ხმა წარმოთქვამს.

## როგორ მუშაობს

1. **სმენა**: მიკროფონი გადის WebRTC AEC3 ექოს გაუქმებას და ხმის აქტივობის დეტექციას, შემდეგ აუდიო მიდის ლოკალურ `whisper-server.exe`-ზე (whisper.cpp), რომელიც მუდმივად ჩართულია — ტრანსკრიფცია ~1 წამში.
2. **აზროვნება**: ტექსტი Claude-ს მიეწოდება **Claude Agent SDK**-ით. ყოველი სვლა იაფ/სწრაფ მოდელზე იწყება; თუ თხოვნას რეალური სამუშაო სჭირდება (ხელსაწყოები, ფაილები, კოდი), თვითონვე გადადის სრულ მოდელზე. **ამ რეპოზიტორიაში API გასაღები არ არის** — SDK შენს ლოკალურ Claude Code ავტორიზაციას იყენებს.
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
- **მოდელების ID-ები შეიძლება შესაცვლელი იყოს.** `goat_app.py`-ის თავში `MODEL_FULL`/`MODEL_FAST` კონკრეტულ Claude მოდელებს ასახელებს; თუ შენს ანგარიშს ისინი არ აქვს, ჩაწერე ის მოდელები, რომლებიც გაქვს.

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
- **Ctrl+K** — წერა ლაპარაკის ნაცვლად. ფაილები ჩააგდე ან ჩასვი ფანჯარაში ანალიზისთვის.
- **"stop" / "cancel" / "hold on"** — მიმდინარე დავალებას აჩერებს.
- **"restart GOAT"** — ახალი სესია (სხვა შემთხვევაში კონტექსტი მთელი სესიის განმავლობაში ინახება).
- **"run diagnostics" / "are you okay"** — GOAT უშვებს `goat_doctor.py`-ს და გატყობინებს.

## პრობლემების მოგვარება

- **`[WinError 10061]` / STT connection refused** — Whisper-სერვერი არ არის ჩართული. შეამოწმე, არსებობს თუ არა `stt/bin/Release/whisper-server.exe` და მოდელის ფაილი; აპი მას **3781** პორტზე თვითონ უშვებს.
- **საკუთარ თავს ისმენს / ექო აქვს** — გამოიყენე ლეპტოპის საკუთარი მიკროფონი+დინამიკები ან ყურსასმენი; ნახე `python/aec_run.log`.
- **ხმა არ ისმის** — ინტერნეტი გაქვს? Edge TTS-ს ქსელი სჭირდება. ოფლაინ-სათადარიგოს სჭირდება `tts/piper/piper.exe` + ხმის მოდელი.
- **„გონება არ აქვს" / ავტორიზაციის შეცდომები** — Claude Code ამ კომპიუტერზე ავტორიზებული არ არის, ან შენს გამოწერას მითითებული მოდელები არ აქვს — შეასწორე `MODEL_FULL`/`MODEL_FAST` `goat_app.py`-ში.
- **სხვა ყველაფერი** — `cd python && python goat_doctor.py` — ამოწმებს პროცესს, სმენას, ხმის არხს, ლოგებს, სესიას.

## ლიცენზია და მადლობები

პირადი პროექტია — MIT-ის სულისკვეთებით: რაც გინდა, ის უქენი, გარანტიის გარეშე.

აგებულია: [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview) (Anthropic) · [PySide6](https://doc.qt.io/qtforpython/) · [whisper.cpp](https://github.com/ggml-org/whisper.cpp) · [Piper](https://github.com/rhasspy/piper) · [Silero VAD](https://github.com/snakers4/silero-vad) · WebRTC AEC3 · [Edge TTS](https://github.com/rany2/edge-tts)
