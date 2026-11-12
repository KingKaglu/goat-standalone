# GOAT audit, 2026-11-10 (Phases 1, 2 and 2B plan; read-only)

Scope: HEAD `08e6e4c` on branch `refactor/audit-cleanup`, the running config (`ui-config.json`: `lang=en`, worker `opus 5.5` at `high`), `python/metrics.jsonl` (2026-11-06 to 11-09, 1,455 events), the two app logs and `restart-goat.log`. No app code was changed. The only new file is `scripts/stt_bench.py`. The 2026-11-06 audit is kept unchanged below this one; items it fixed are not repeated.

**How much was read.** In full: `stt_whisper.py`, `stt_live.py`, `stt_gladia.py`, `endpoint.py`, `run_tests.py`, `requirements.txt`. The voice and phone paths: `goat_app.py` 2174-3300 and 5701-5850, `phone_server.py` 60-130, 860-1460, 1516-1940, 1978-2110, `phone.html` 1080-1240 and 1600-1940, `stt_realtime.py` 1-200, the voice prompt in `talk_brain.py`. Only scanned by script or grep: `ui_qt.py`, `screen_*`, `game_*`, `reflex*.py`, `local_llm.py`, `voice_hands.py`, `task_router.py`, `tts_*.py`, `live_talk.py`, `audio_io.py` (constants only). Not available to read: the Android bubble app (`workspace/goat-bubble`, gitignored). So the bug and race review is solid for the hearing path and thin elsewhere.

**Basis:** FACT = seen in code, log, metrics or a run today. ASSUMPTION = plausible, not proven. Every number names its source.

## 1. Architecture

**Entry points.** `GOAT.bat` / `start-goat.vbs` → `python/start-goat-app.vbs` → `ui_qt.py` (PySide6 window) → `goat_app.GoatApp.run()` on an asyncio loop. `python/restart-goat.ps1` = preflight (`self_check.py`), kill, relaunch, roll back if the engine never comes up. `phone_qr.py` = phone login codes. `run_tests.py`, `metrics.py summary`.

**Modules by job.**

| Job | Files |
|---|---|
| Audio in/out, echo cancel, VAD, endpoint timing | `audio_io.py` (livekit AEC3, Silero VAD through onnxruntime), `endpoint.py` (does the text sound finished), `dsp.py` |
| Hearing (STT) | `stt_whisper.py` (local whisper.cpp server + the Georgian chain), `stt_live.py` (Gemini Live and REST transcription), `stt_gladia.py` (ElevenLabs Scribe batch + Gladia), `stt_realtime.py` (ElevenLabs Scribe realtime) |
| Turn logic | `goat_app.py` (`_handle_utterance` → `_heard` → `_talk`), `side_talk.py`, `reflex.py`, `task_router.py`, `close_guard.py` |
| Voice brain | `live_talk.py` (Gemini Live, speaks its own audio), `talk_brain.py` (REST fallback, prompt, tools), `voice_hands.py` |
| Worker brain | Claude Agent SDK client in `goat_app.run()`, `screen_tools.py`, `brain_route.py` (one-brain mode only) |
| Speaking (TTS) | `tts_edge.py` (edge-tts), `tts_piper.py` (offline fallback), `TtsPipeline` in `goat_app.py` |
| Phone | `phone_server.py` (aiohttp, HTTPS + WebSocket on :8797), `phone.html`, `phone_hands.py`, `phone_ctl.py`, `phone_qr.py` |

**Text turn, laptop.** Window input → `GoatApp.submit_text` (`goat_app.py:2539`) → `_talk` (`:3135`) → stop/close-guard checks → `reflex.match` (device command, no model) → else `_route_big` (`task_router`, big jobs straight to Claude) → else `_front_start` (voice brain answers or calls `hand_to_worker`) → reply text → `TtsPipeline` → speaker.

**Text turn, phone.** `phone.html` form → WebSocket `{"type":"text"}` → `PhoneServer._command` (`phone_server.py:1984`) → the same `submit_text(origin="phone")`. Shared from there on. Reply text and audio go back over the socket.

**Voice turn, laptop.**
1. Mic → `audio_io.DuplexAudio`: 16 kHz mono, 10 ms blocks, AEC3, Silero VAD (threshold 0.35), 300 ms pre-roll.
2. Speech start → `_on_utt_start` (`goat_app.py:2295`) opens a streaming ear: in English mode `stt_whisper.Stream` (local Whisper), in `ka`/`auto` mode `stt_realtime` (ElevenLabs).
3. Endpointing: 200 ms of quiet → soft commit (the ear transcribes what it has). The turn ends after 500 ms if the partial text sounds finished, 1200 ms if not **or if the transcript isn't back yet** (`_sentence_state`, `:2265`). Cap 30 s.
4. STT → `_handle_utterance` (`:2644`) awaits the stream; batch fallback if it missed.
5. `_heard` (`:2934`): voice lock, phantom-phrase filter, echo checks, name gate, hold of a dangling sentence (1.6 s), merge window (1.5 s).
6. `_talk` → reflex / router / voice brain / Claude, as for text.
7. TTS: Gemini Live's own audio, else edge-tts sentence by sentence, else Piper.

**Voice turn, phone.**
1. `getUserMedia` with the browser's echo cancel, noise suppression and auto gain **off** (`phone.html:1808-1812`), AudioWorklet tap, resampled in JS to 16 kHz mono (`:1652-1668`).
2. The page's **own** echo canceller and its **own** energy VAD (`frame()`, `:1712-1788`): starts on 3 loud frames, ends after 800 ms of quiet or 20 s.
3. While he talks: 100 ms chunks as base64 JSON (`:1637-1641`). At the end: the whole clip again as binary int16 (`:1795-1797`).
4. Laptop: `PhoneServer._utterance` (`phone_server.py:1642`) lifts the gain, applies echo/overlap rules, then calls the same `GoatApp._handle_utterance(source="phone")`.
5. Reply audio returns over the socket and plays through Web Audio.

The phone and the laptop share everything from `_handle_utterance` on. They do **not** share VAD, endpointing, gain or echo cancelling: those exist a second time in `phone.html` (and, by the code comments, a third time in the bubble app).

## 2. Which STT engine, exactly

**The phone does not use the browser's speech recognition.** There is no `SpeechRecognition` / `webkitSpeechRecognition` anywhere in `phone.html` (grep, FACT). It sends raw audio and the laptop transcribes it.

GOAT runs in **English mode** (`ui-config.json` `lang: "en"`), although he mostly speaks Georgian. In that mode:

| | Laptop | Phone page |
|---|---|---|
| First engine | whisper.cpp server, CUDA build, `ggml-large-v3-turbo-q8_0.bin` on the RTX 3050, port 3781 | the same server |
| Server flags | `-t 4 -nt -bs 5 -nf -l en` (`stt_whisper.py:133-135`), language overridden per request | same |
| Audio | 16 kHz mono float → 16-bit WAV per request | 16 kHz mono int16, resampled in the page |
| Streaming or batch | Batch requests on growing audio: one at each 200 ms pause (`Stream`), newest wins | **Batch only**, after the whole clip arrived: with the GPU ear on, no stream is opened for the phone (`phone_server.py:1561-1567`) |
| Language | `auto` + language probabilities; pinned `en` with no check when "sticky English" holds (`stt_whisper.py:312-319`) | always `auto` (never sticky) |
| Vocabulary prompt | none on the GPU ear (`stt_whisper.py:62-63`) | none |
| Georgian | Whisper can't write it. If its English score is under 0.35 the clip goes to **Gemini Live input transcription** (`gemini-3.8-live`, `languageCodes ka-GE, en-US`, 200 ms PCM chunks, `stt_live.py:101-122`), raced against Whisper once Georgian was heard in the last 5 min. Then REST `gemini-3.5-flash-lite` (4 s wait), then Gladia `solaria-1` pinned `ka` | same chain, but one after the other, no race |
| Text fixes | `stt-fixes.json` (55 entries) applied to every transcript | same |

Other paths that exist but are not what runs today: ElevenLabs Scribe v2 realtime (`ka`/`auto` modes; out of credits since 2026-10-24, `python/.stt-rt-broke.json`), Scribe v2 batch with 90 keyterms (`stt_gladia.py:125-152`), CPU Whisper `base.en`/`small` (`GOAT_STT_GPU=off`).

Offline, English still works (local Whisper). **Georgian has no offline engine at all.**

## 3. Run and test

- Start `GOAT.bat`; restart `python\restart-goat.ps1`; always `py -3.13`.
- Tests: `cd python; py -3.13 run_tests.py`: 40 script suites, no pytest, 109 s today. Flags `--qt`, `--slow`, `--live`.
- **Baseline today (main checkout, with his uncommitted edits): 38 of 40 pass.**
  - `test_voice_id`: 2 failures ("his held-out clips: none rejected", "barge-in: his voice not vetoed"). It reads his real voiceprint in `voiceid/`.
  - `test_side_talk`: 1 failure (42 % of lines dropped mid-call, limit 40 %). The same test passes 37/37 in a clean worktree, so it depends on machine state (it loads his file index), not only on code.
- There is no test of recognition quality in any language, and no test that drives `phone.html`'s VAD (two small JS tests cover barge and park).

## 4. Baselines

| What | Value | Source |
|---|---|---|
| Startup | **26 s median** from "preflight passed" to "engine up" (27 restarts, range 17-39 s). Includes the kill, a fixed 8 s wait and 3 s polling, so it is an upper bound | `restart-goat.log` |
| Time to first word, warm, laptop | **p50 2.71 s, p90 4.97 s** (146 turns) | `metrics.jsonl`, `latency` events |
| of which endpoint wait | p50 1216 ms, p90 1216 ms | same |
| of which ear after the endpoint | p50 32 ms, p90 1664 ms | same |
| of which voice brain to first audio | p50 1245 ms, p90 3216 ms | same |
| STT job, English pinned | p50 775 ms, p90 1734 ms from his pause (77 jobs) | `ear` events |
| STT job, language auto | p50 1462 ms, p90 3139 ms (164 jobs) | `ear` events |
| Whisper server alone | 508 ms for a 3 s clip, auto language | `stt_bench.py` run today |
| Georgian ear (Gemini Live) | 0.9-4.0 s, median 1.7 s (13 turns), **on top of** the Whisper pass | `goat-app.log`, `[stt] Georgian via Live` |
| Phone | **no data**: all 202 `heard` events are `source=laptop` | `metrics.jsonl` |
| Voice brain prompt | 14,244 chars of rules + 14,000 chars of memory + 8,807 chars of tool schemas ≈ 37,000 chars per Live session and per REST fallback request, plus up to 24 history messages. Not counted in tokens | `talk_brain.py:132-178`, `:253`, measured |
| Worker (Claude) per job | fresh input p50 58 tokens, cache read p50 202k (p90 2.7M), cache write p50 27k, output p50 2.1k; cache hit 95 % (55 jobs) | `claude_usage` events |
| False barge-ins | 59 "echo, not you" against 58 confirmed interrupts | `barge` events |
| Split turns | 17 split candidates, 3 merged | `metrics.py summary` |

Hardware: i5-12450H, **8 GB RAM**, RTX 3050 Laptop **4 GB VRAM**. This limits which local models can sit beside Whisper turbo.

## 5. Problems, by priority

Risk = risk of the fix. Line numbers are at `08e6e4c`.

### P0

| ID | Area | File:line | Evidence | Risk | Expected gain |
|---|---|---|---|---|---|
| A1 | Georgian quality and latency | `stt_whisper.py:359-401`, `:465-526`; `ui-config.json` | Every Georgian sentence first goes through an English-first Whisper pass that cannot write Georgian, then to a chat model's side transcript (Gemini Live, free-tier quota, `stt_live.py:43-48`), then REST Gemini, then Gladia, which his own notes call garbage (`stt_live.py:278-280`). No engine built for Georgian is in the chain. Log: 0.9-4.0 s for the Live step alone. | Medium: touches the hearing path. Behind one interface with the old chain kept as fallback it is reversible by config | The benchmark decides. This is the main item |
| A2 | Latency, endpoint | `goat_app.py:2274-2276`, `audio_io.py:54-68` | The long 1.2 s wait is still the normal case: endpoint p50 = 1216 ms; 142 of 295 endpoints ended in state `local-pending` (transcript not back at the 500 ms mark). The 11-06 fix only covers runs of confident English: 164 of 241 ear jobs were still `auto`, p50 1462 ms. FACT (metrics) | Medium: ending turns earlier can cut him off; the merge window is the net. It is a tuned value, so only with before/after numbers | Up to ~700 ms on about half of all turns |
| A3 | Phone parity | `phone_server.py:1561-1567`, `:1704-1706`; `phone.html:1712-1788` | (a) No streaming ear for phone clips when the GPU ear is on: the clip is transcribed only after it has fully arrived. (b) Phone Georgian runs Whisper then Live in sequence, never the race the laptop has (`stt_whisper.py:720-732` is `Stream` only), never sticky English. (c) VAD, endpointing (fixed 800 ms, 20 s cap against 500/1200 ms, 30 s), gain and echo cancel are separate JS code. (d) No phone turn in four days of metrics, so none of this is measured. FACT (code) | Medium-high for (c): moving endpointing to the laptop changes how the phone feels; (a)(b) are small | Phone and laptop behave the same; est. 0.5-1 s on phone turns (ASSUMPTION until measured) |
| A4 | Blocking, silent wait | `stt_live.py:333`, `stt_whisper.py:497-526`, `stt_gladia.py:167-196` | Worst case for one Georgian clip when the cloud is slow: Live up to 14 s, then REST Gemini and Gladia together for up to 30 s more (about 44 s in all), with nothing said to him. FACT (timeouts in code); frequency unknown | Low: one overall deadline | Bounded wait and a spoken "didn't catch that" instead of silence |

### P1

| ID | Area | File:line | Evidence | Risk | Expected gain |
|---|---|---|---|---|---|
| B1 | Conversation quality, memory | `talk_brain.py:98`, `:253` | The voice gets the first 14,000 characters of `workspace/memory.md`. The file is 42,602 characters, so it never sees the last 67 %. FACT (measured) | Low-medium: sending all of it triples that part of the prompt; better to pick what to send | The voice stops "forgetting" things Claude wrote down |
| B2 | Security, privacy | `phone_server.py:1340` | A personal email address is hard-coded as the web-push contact, in a public repo. FACT | Low: read it from the secrets file | Personal data out of the code (it stays in git history unless rewritten) |
| B3 | Security | `phone_server.py:1019-1020` | The bubble app still logs in by sending the master token as its cookie on every request (open since the 11-06 audit, S3) | Medium: needs an app build | A stolen phone session can't become full access |
| B4 | Latency | `stt_live.py:307` | REST Georgian transcription builds a new HTTP client per call. The repo's own measurement of that cost is 0.25-0.8 s (`stt_whisper.py:106-110`) | Low | 0.25-0.8 s on every REST Georgian turn |
| B5 | Phone audio quality | `phone.html:1652-1668` | 48 kHz → 16 kHz by averaging 3 samples, no real low-pass filter, so content above 8 kHz folds back into the speech band. ASSUMPTION that it hurts recognition: the phone clips in the benchmark will show it | Low if the laptop resamples (`dsp.resample_poly` exists) | Cleaner phone audio; measured in 2B |
| B6 | Phone, network | `phone.html:1121`, `:1797-1799`, `:1637-1641` | Reconnect is a fixed 1.5 s retry. If the socket is down when he stops talking, the clip is dropped ("not connected"), not kept and resent. Audio is sent twice (base64 chunks + full clip) | Low | No lost sentence after a network blip; less upload |
| B7 | Phone, screen lock | `phone.html:1931-1935`, `:1848` | Hidden page = mic off, by design; the mic watchdog does not run while hidden. Hands-free with the screen off is only possible in the bubble app | n/a | Listed so the checklist tests it; no change proposed |
| B8 | Conversation quality | `talk_brain.py:171`, `:158` | The prompt orders every reply to open with "Sir," and every hand-off to say a fixed line. `ui-config.json` holds GOAT's own note that these hand-off lines left him "waiting blind". These are his standing orders | Behaviour change: his decision | Less robotic replies |
| B9 | Tokens | `talk_brain.py:132-178` | ~37,000 characters ride on every REST fallback request; the rules contain dated incident stories. Open since 11-06 (T2). A "quick" Claude job still reads ~200k cached tokens (p50) | Medium: prompt edits change behaviour | Smaller prompt; exact saving needs token counts |
| B10 | Tests | `run_tests.py`, `test_side_talk.py`, `test_voice_id.py` | 2 of 40 suites red before any change; one depends on machine state | Low | A green baseline to refactor against |
| B11 | Logging | all of `python/` | 290 `print()` calls, no `logging`, no timestamps in `goat-app.log`, log replaced on every launch | Low-medium (many lines) | Problems can be dated and traced |
| B12 | Barge-in | `audio_io.py` | As many echo false alarms (59) as real interrupts (58). Tuned thresholds: not touched without a recording-based test | n/a now | Listed only |

### P2

| ID | Area | File:line | Evidence | Risk | Expected gain |
|---|---|---|---|---|---|
| C1 | Dead code, no reference anywhere in the repo (name scan over .py/.html/.ps1/.vbs/.json) | `goat_app.py:2428` `set_talk_brain`, `:2592` `_ack`, `:4437` `_work_status_line`; `local_llm.py:169` `note_exchange`; `tts_edge.py:390` `_get_live`; `ui_qt.py:771` `paint_scene`, `:963` `Backdrop.set_dim`, `:1109` `GoatMark`, `:1506` `VoiceOrb`, `:2057` `Clickable.set_on`, `:2062` `ActionCard`, `:5621` `GoatWindow._act_talk` | Zero references besides the definition | Low; each one re-checked by hand before removal | Roughly 300-500 lines (not counted yet) |
| C2 | Used by tests only | `dsp.py:136` `wav_bytes16`; `goat_app.py:844` `is_escalation`, `:855` `outcome_line`; `tts_piper.py:17` `synth_to_16k` | Only test files reference them | Don't delete without his OK (tests would go too) | small |
| C3 | Old experiments, **listed, not to delete** | one-brain mode (`GOAT_TWO_BRAINS=off`, `brain_route.py`), `GOAT_STT_KA` experiment (`goat_app.py:321`), ElevenLabs realtime ear (`stt_realtime.py`, no credits), CPU Whisper path, `set_talk_brain` stub, `make_icon.py`, six `probe_*.py`, three `voice-knowledge-*.json` data files | Reachable by a switch or run by hand | n/a | Decision for him |
| C4 | Duplicates | Georgian-letters regex written out 16 times; three junk-phrase filters (`stt_whisper.py:95`, `:233`, `goat_app._PHANTOM`); two secrets readers (`stt_gladia.py:78`, `talk_brain.py:117`); two vocabulary lists (`stt_whisper.py:76`, `stt_gladia.py:103`); two gain lifts (`phone_server.py:1704`, `audio_io.py:99-103`); three copies of the echo matching loop (`phone_server.py:1828-1869`); 24 local `from aiohttp import web` | FACT | Low | One place to change each rule |
| C5 | Dependencies | `requirements.txt` | `scipy` is imported only by tests. `pywin32` (`desk_hands.py`) and `py_vapid` (`phone_server.py`) are imported but not declared. `silero-vad` is needed only for its model file (`voice_id.py:128`) | Low | A clean install works |
| C6 | Code health | — | `ui_qt.py` 6,577 lines, `goat_app.py` 5,766, `phone_server.py` 2,109, `phone.html` 1,902 in one file; 62 `GOAT_*` environment switches spread over the modules; 338 `except Exception`, 122 of them `pass`; 93 lazy `getattr(self, "_x", default)`; 19 hard-coded user paths | Medium-high for big moves | Only what Phase 3 touches |
| C7 | Security, low | `stt_live.py:103`, `:308` | The Gemini key travels in the URL query. Normal for that API, but an exception text could carry it into the log; today's handlers print only the error type or 120 chars | Low | Header instead of query |

### Security checks that passed

- All 21 routes: token or session check in every handler that returns data or acts (`_authed` / `_check`); only the manifest, icons and service worker are open, and they hold no data. The network gate middleware runs before all of them (`phone_server.py:1041-1058`).
- WebSocket: auth, then Origin must match the host (`:1090-1094`). A request with no Origin header passes; that is how non-browser clients connect, and it still needs the cookie.
- TLS on the only listener (`:913`). Self-signed for bare IPs (the phone must trust it once), a real certificate for the Tailscale name. Binds all interfaces and relies on the gate plus the Private-only firewall rule.
- Upload: file names are generated on the server, only a cleaned extension is kept, 40 MB cap (`:1386-1400`). No path from the client is ever joined.
- No shell command is built from phone input in `phone_server.py`; phone text becomes a normal turn, so it has the same power as his voice.
- Git history: 0 hits for API-key and private-key patterns over all commits. `.goat-secrets.json`, `.phone/`, `voiceid/`, `workspace/`, metrics and mic captures are gitignored.

## 6. Phase 2B: Georgian STT benchmark

`scripts/stt_bench.py` is written and self-tested (scoring maths, audio preparation, one real clip through the local server). It reports WER, CER, exact matches, p50/p90 latency and real-time factor per engine, overall and per tag (phone, laptop, mixed, names, numbers …), and saves every hypothesis.

Built in: `current` (today's real chain), `whisper-auto`, `whisper-ka` (forced language), `gemini-live`, `gemini-rest` (+ vocabulary in the prompt), `scribe`, `scribe-ka`, `scribe-vocab` (keyterms), `gladia`, `onnx-ka`, `nemo-ka`, `hf-whisper`. Options: `--prep raw,norm,highpass,denoise,pad`, `--clip-ms 0,100,200` (VAD cutting word edges), `--llm-fix` (correction pass, timed), `--vocab file`.

**Candidates, from the vendors' pages on 2026-11-10** (third-party figures marked):

| Engine | Where | Georgian quality claimed | Streaming | License / cost per audio hour | Fits this laptop |
|---|---|---|---|---|---|
| Current chain | local + Google free tier | not measured | partial | free, quota-limited | yes |
| `nvidia/stt_ka_fastconformer_hybrid_large_pc` | local | WER 5.73 % Common Voice, 13.44 % FLEURS (model card) | no; NeMo lists a separate 80 ms streaming Georgian checkpoint | CC-BY-4.0, free | 115M parameters: yes. Through NeMo it needs `nemo_toolkit` (large install); an ONNX export runs on the already installed onnxruntime through the small `onnx-asr` package (CTC head only) |
| `BuzzASR/georgian` | local | WER 34.5 % Common Voice 25, 43.7 % FLEURS (model card) | no | MIT, free | ~2B parameters, ~3 GB in half precision: only with Whisper turbo unloaded, or slowly on CPU with 8 GB RAM |
| ElevenLabs Scribe v2 | API | his own earlier test, mostly synthesized speech: 7.3 % WER (`stt_gladia.py:13-19`) | batch; realtime is a separate model | $0.22/h batch, $0.39/h realtime, keyterms +$0.05/h (+$0.08/h realtime) | yes; his account has no credits |
| Gemini 3.5 Flash-Lite audio | API | not published | no | $0.30 per 1M tokens, 25 tokens per second of audio ≈ **$0.03/h**; currently on the free tier | yes |
| Google Chirp 3 (`ka-GE`) | API | preview language | yes | about $0.96/h (third-party figure, unverified) | yes; needs a Google Cloud project |
| OpenAI `gpt-4o-transcribe` / mini | API | Georgian support not confirmed | unclear | $0.36/h / $0.18/h (third-party figures, unverified) | yes; needs an OpenAI key |
| Soniox | API | **no Georgian** in its language list | — | — | excluded |

Known limit to test, not assume: the NVIDIA model is Georgian-only, so English words inside Georgian sentences will probably come out in Georgian letters.

Sources: [NVIDIA model card](https://huggingface.co/nvidia/stt_ka_fastconformer_hybrid_large_pc), [ONNX export](https://huggingface.co/OpenVoiceOS/stt_ka_fastconformer_hybrid_large_pc_onnx), [BuzzASR/georgian](https://huggingface.co/BuzzASR/georgian), [ElevenLabs API pricing](https://elevenlabs.io/pricing/api), [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing), [Chirp 3](https://docs.cloud.google.com/speech-to-text/docs/models/chirp-3), [Soniox languages](https://docs.soniox.com/stt/concepts/supported-languages).

**Needed from him before anything is measured:** the recordings, and a yes or no on each install and each paid engine (see the questions in the session).

## 7. Proposed Phase 3 order (for approval; nothing started)

1. Smoke tests for the critical paths (transcribe contract, phone clip → `_handle_utterance`, endpoint decision), and a green baseline (B10).
2. One STT interface, `transcribe(audio) -> text`, engine and fallback chosen by config; today's chain moved behind it unchanged (A1, C4).
3. The benchmark winner as the Georgian engine, today's chain as the fallback, one overall deadline (A1, A4, B4).
4. Phone on the same path: streaming ear for phone clips, same language logic, resampling on the laptop (A3 a-b, B5, B6). Moving the phone's VAD and endpointing to the laptop (A3 c) is a separate, larger step that needs his yes.
5. Endpoint wait (A2), only with before/after metrics.
6. B1, B2, then C1 and C5. B3, B8, B9, B11, C2, C3, C6 only if he says so.

## 8. Results (2026-11-10, after Phase 2B and Phase 3)

### Benchmark

Test set: the first 30 clips of FLEURS Georgian (test split, CC-BY-4.0): 340 s of read speech, 7–18 s per clip, several speakers, 16 kHz. **Not his voice, not his phone mic, no mixed Georgian and English, no names of his**: his own recordings were not available, so those four things stay unmeasured. Machine: his laptop, GOAT not running. Command: `py -3.13 scripts\stt_bench.py <folder> --goat <checkout> --engines ...`.

| Engine | WER % | CER % | Empty answers | p50 ms | p90 ms | Cost per audio hour | Offline |
|---|---:|---:|---:|---:|---:|---|---|
| Gemini Live transcript (today's Georgian ear), 3 runs (raw 11.7 %; the two phone-resampler variants 7.8 % and 5.7 %: its answers vary from run to run) | 5.7–11.7 | 1.7–6.6 | 0–1 of 30 | 4068–4958 | 5428–5937 | free tier | no |
| GOAT's whole chain as shipped before (`current`) | 11.9 | 6.3 | 1 | 5910 | 10532 | free tier | no |
| FastConformer ka, int8, CPU | 17.6 | 5.5 | 0 | 306–339 | 438–497 | free | yes |
| FastConformer ka, full precision, CPU | 18.0 | 5.7 | 0 | 140–155 | 215–250 | free | yes |
| FastConformer + LLM correction (Gemini Flash-Lite) | 14.5 | 4.7 | 0 | 140 + 816 | – | about $0.03 | no |
| REST Gemini Flash-Lite (today's second ear) | 40.7 | 34.0 | 8 | 2064 | 4775 | about $0.03 | no |
| ElevenLabs Scribe v2 batch, auto language (2026-11-11, after he added credits) | 7.1 | 2.0 | 0 | 2882 | 4106 | $0.22 | no |
| ElevenLabs Scribe v2 batch, pinned to Georgian | 8.2 | 2.3 | 0 | 2948 | 4417 | $0.22 | no |
| FastConformer ka, transducer head through NeMo (PyTorch, CPU; run 2026-11-11 in a throwaway venv, another benchmark was using the CPU, so the latency is a ceiling) | 15.3 | 4.5 | 0 | 1638 | 2297 | free | yes |
| FastConformer ka through `onnx-asr` (2026-11-11; same model and numbers as GOAT's own front end, which confirms it) | 18.0 | 5.7 | 0 | 136 | 206 | free | yes |
| Gladia Solaria-1 pinned to Georgian (today's last ear; 2026-11-11) | 75.3 | 25.1 | 0 | 5978 | 8211 | €0.27 (this run: about €0.03) | no |
| Whisper large-v3-turbo, pinned to Georgian | 108.4 | 84.6 | 0 | 5162 | 5345 | free | yes |
| Whisper large-v3-turbo, auto language | 121.6 | 102.5 | 0 | 1310 | 5399 | free | yes |

Scribe is the only engine that matches Gemini Live: 7.1 % WER against Live's 5.7–11.7 %, the same answer on every run, about 1.5 s faster on these long clips, and 9.8 % in the 10 dB noise test (Live 12.0 %). Pinning the language does not help it. It costs $0.22 per audio hour where Live is free; the two Scribe runs here cost about $0.08. Whether to put it ahead of Live is his call on money: nothing in the routing was changed.

Not run: Chirp 3 and OpenAI (no accounts). BuzzASR/georgian (a fine-tuned Whisper, 3 GB) was started on 2026-11-11: this laptop's PyTorch has no GPU build, and one 9 s clip took 91–131 s on the CPU, so the run was stopped and its WER was not measured (its own card reports 34–44 %). It cannot be a voice ear here at that speed.

The NeMo transducer head is 2.7 WER points better than the CTC head GOAT uses (15.3 against 18.0 %), but it needs PyTorch and NeMo inside GOAT and took 1.6 s per clip in this run, so it is not built in. An ONNX export of the transducer head would be the thing to try next.

On the 22 clips whose reference has no numerals or Latin words: Live 12.7 % WER / 7.9 % CER, FastConformer 13.9 % / 3.4 %. FastConformer writes numbers as words ("ათი" for "10"), which costs it on the other 8.

What else was tested (FastConformer unless said otherwise):

| Test | Result |
|---|---|
| Forced language | Whisper pinned to Georgian is no better than auto: both over 100 % WER. Whisper cannot write Georgian. |
| Peak normalisation | 18.0 → 16.8 % WER. Small gain; the phone path already does it. |
| 80 Hz high-pass | 17.8 %. No effect. |
| Noise suppression on clean audio | 18.2 %. No effect. |
| Room noise at 10 dB SNR | 25.8 %; Live 12.0 %. Live is far more robust to noise. |
| The same noise, then noise suppression | 23.9 %. Recovers 2 of the 8 points; not worth a new dependency. |
| phone.html's resampler from 48 kHz and from 44.1 kHz | 17.8 % and 18.0 %. No measurable cost, so audit item B5 is closed without a change. |
| 100 ms cut off both ends (VAD clipping) | 18.2 %. No effect. |
| 200 ms cut off both ends | 20.3 %. Two points: the 300 ms pre-roll matters, keep it. |
| 300 ms of silence added around the clip | 18.2 %. No effect. |
| LLM correction pass | 18.0 → 14.5 % WER for 816 ms more: over the 300 ms budget, so not used. |
| Vocabulary bias | Not measurable on this set (no names or terms of his in it). |
| Sound that is not speech | FastConformer writes a word or two for silence, hum, noise and clicks. Its own certainty separates them (0.39–0.55 against 0.78 and up on speech), so answers under 0.6 are handed to the next ear. |

### Recommendation, and what was built

- **Engine: Gemini Live's transcript stays the Georgian ear.** It is the most exact engine measured (6–12 % WER), robust to noise, free, and already in GOAT.
- **Offline fallback: NVIDIA FastConformer for Georgian** (`python/stt_fastconformer.py`, CC-BY-4.0, int8 ONNX, 131 MB on disk, 266 MB of RAM, about 110 ms for a 4 s clip on the CPU, no new package). It answers when Live has nothing after 4 s or cannot be reached, ahead of REST Gemini and Gladia.
- Latency: no turn gets slower. A Georgian turn that Live used to hold for up to 20 s (and the chain for about 44 s) is now answered after 4.3 s at the latest, and Georgian works with no network (17.6 % WER, p50 295 ms, measured through `stt.transcribe` with the cloud ears switched off; before, there was no Georgian offline at all).
- `GOAT_STT_KA_LOCAL=first` asks the local ear before Live (0.1–0.3 s instead of 1.7 s median, less exact); `off` is the old chain.

The model files live in `stt/fastconformer-ka/` (not in git): `model.int8.onnx` and `vocab.txt` from `OpenVoiceOS/stt_ka_fastconformer_hybrid_large_pc_onnx` on Hugging Face. Without them nothing changes.

### Problems from section 5: status

| Item | Status |
|---|---|
| A1 no Georgian engine, nothing offline | Done: local ear as fallback, one interface (`stt.py`), engine by config. |
| A4 silent wait of up to 44 s | Reduced to about 4.3 s when the model is installed. |
| A3 phone parity | Phone clips go through the same `stt.transcribe` and get the same Georgian ears. The phone's own VAD and endpointing, and the missing streaming ear for phone clips with the GPU ear, are unchanged: both need the phone to test. |
| B2 personal address in the code | Done (`GOAT_PUSH_CONTACT`). |
| B5 phone resampler | Measured, no cost, left alone. |
| B6 phone reconnect | Backoff, reconnect on return, and a clip that ended offline is kept for 15 s. Page logic checked in desktop Chrome on 2026-11-12 with a stand-in socket (the real `phone.html` script, no server): after a drop the retries came at 0, 533, 1017, 2023, 4023, 4021 ms; `online` and return-to-front each retried in 0 ms; an utterance that ended offline (38,400 bytes) went out on the next socket and a 16 s old one was dropped. Still not tested on a phone: screen lock and a real Wi-Fi drop. |
| C1 dead code | 12 definitions, 535 lines removed. |
| C5 dependencies | Declared. |
| A2 endpoint wait, B1 memory cap, B3, B4, B7–B12, C2–C4, C6, C7 | Not touched: tuned values without a measurement, his decision, or outside the approved list. |

### Baselines after (2026-11-11 and 2026-11-12, GOAT running the branch code)

| What | Before | After | Note |
|---|---|---|---|
| Startup to "engine up" | 26 s median (27 restarts) | 24 s and 14 s (2 launches) | Two launches are not a trend. Nothing at boot was changed on purpose. |
| Time to first word, laptop | p50 2.71 s, p90 4.97 s (146 turns) | median 2.50 s, range 1.72–5.07 s (7 turns) | Same within the noise. Seven turns give no p90. |
| Ear after the endpoint | p50 32 ms, p90 1664 ms | 172, 235 and 2293 ms (3 turns with a split) | The 2293 ms one was a batch-ear turn. |
| Voice brain to first audio | p50 1245 ms | 1038, 1469 and 2260 ms | |
| Georgian with no network | did not work | 17.6 % WER, p50 295 ms | FLEURS, through `stt.transcribe`. |
| Longest silent wait on a Georgian turn | about 44 s | about 4.3 s | Chain timing, with the local model installed. |
| Georgian WER, online | 11.9 % (whole chain) | 5.7–11.7 % (Live is still first; nothing changed for a turn that Live answers) | FLEURS. |
| Worker tokens per job | 58 fresh in, 202k cache read, 95 % cache hit | not re-measured | No worker job ran after the launch; prompts and caching were not changed. |
| Phone turns | no data | no data | |
| Offline test suites | pass | pass | `run_tests.py`, last run 2026-11-12 on the branch head. |

### What stays unmeasured

On 2026-11-12 he waived the two things that need him: his own recordings and the test on the phone. So:

- WER on his voice, on the phone mic, on mixed Georgian and English and on his names is unknown. Every WER figure here is read speech from FLEURS. `scripts/stt_record.py` records the clips and `scripts/stt_bench.py <folder>` scores them whenever he wants the numbers.
- Vocabulary bias could not be tested for the same reason.
- The phone reconnect change is verified in a desktop browser only (item B6 above).

### Manual checks

Laptop voice:
1. Start with `GOAT.bat`; the window says it is listening within about 30 s.
2. One English and one Georgian sentence: each answer starts within about 3 s.
3. One sentence that mixes Georgian with English product names: the transcript in the window matches what was said.
4. Talk over an answer: it stops. Play its own answer loud: it does not answer itself.
5. Network off, one Georgian sentence: it is still heard (the local ear), within about a second of the pause.
6. `py -3.13 metrics.py summary` in `python\` afterwards shows the turns.

Phone:
1. Open the page from the QR code (`py -3.13 phone_qr.py login --open`), allow the microphone, tap once so sound may play.
2. One typed and one spoken turn: the answer is spoken on the phone.
3. Lock the screen for 30 s, unlock, speak: the dot is green again at once and the turn goes through.
4. Wi-Fi off for 5 s, speak one sentence meanwhile, Wi-Fi on: that sentence is answered.
5. Speaker loud next to the mic: GOAT does not answer itself.
6. Portrait on a small screen: nothing is cut off, the text box stays above the keyboard.

---

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
| S3 | Fixed (browsers); app built | `248f3a5`: hashed server-side sessions, 30 days idle, `logout-all`; one-time pairing codes in QR/links. Bubble app: pairing build ready (`phone_qr.py pair`, see PLAN roadmap 3.1); until he installs it and pairs, the app uses the master token. |
| S4 | Fixed (app layer) | `306073e`: LAN only via Private or trusted networks. Firewall NOT changed (needs admin): see S12. |
| S5, S6, S7 | Fixed | `fdae7ee`: no shell for model text, `secret_paths`, launchable types refused |
| S8 | Fixed | `b6f7309`: worker tool hook, his one-shot yes for private files |
| S10 | Fixed | `.phone/url.txt` no longer repeats the token; it says how to log a phone in |
| S12 (new) | Fixed (his yes, one admin run) | Windows had an *allow all inbound* rule for `python.exe` on **Public** networks (Python's first run) and the GOAT rule was profile Any. Done: home Wi-Fi marked Private, GOAT rule → Private, the two python.exe Public rules disabled (not deleted; undo `Enable-NetFirewallRule`). `phone_server.ensure_firewall` and `setup.ps1` now create the rule Private-only. |
| V3 / Phase 5 | His choice: A | Confirmed by him after the fact: keep option A (filler after 2.5 s, everything else as he ordered). |
| B1 | Fixed | `2b89fa3`: `_connect_engine` retries forever, boot without engine, no crash-loop raise |
| B2, B3, B5 | Fixed | `087f590` (B2 reproduced on the old code: 1600 stale samples queued after cancel) |
| B4 | Open (low) | Live repeat-failure note not added |
| B6 (new) | Fixed (test bug) | The check looked at the collapsed dot's on-top flag; collapsed, `stand_aside` hides the dot by design. The test now checks the main window when there is one, the dot's visibility otherwise. 65/65 pass. |
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
