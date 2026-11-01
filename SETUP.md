# Setting GOAT up on a fresh Windows

Takes a few minutes if you have the Google backup.

## 1. Restore the folder

Restore `projects\goat-standalone-master` from the Google backup. Any path works:
GOAT finds its home from its own location (`python/goat_paths.py`). To point it
somewhere else, set the `GOAT_ROOT` environment variable.

The backup must include these, because they are not in git:

| What | Where |
|---|---|
| API keys | `.goat-secrets.json` (gemini, elevenlabs, gladia, my_email, gmail_account) |
| Ear (whisper.cpp + models) | `stt\` |
| Offline voice (Piper) | `tts\` |
| Your voice print | `voiceid\` |
| Memory, skills, file index, transcript | `workspace\` |
| Learned STT fixes, UI settings | `stt-fixes.json`, `ui-config.json` |
| Phone pairing | `.phone\` |

## 2. Install the basics (once, as yourself)

```powershell
winget install Python.Python.3.13
irm https://claude.ai/install.ps1 | iex   # then run `claude` once and log in
winget install Git.Git                    # asks for admin
```

## 3. Run the setup script

```powershell
cd $HOME\projects\goat-standalone-master
powershell -ExecutionPolicy Bypass -File setup.ps1 -CheckOnly   # report only
powershell -ExecutionPolicy Bypass -File setup.ps1              # fix what it can
```

It installs missing Python packages and restores the autostart shortcut. It
also checks secrets (by name only), models, mic permission and tools, then
runs `goat_doctor`. Run it **once as administrator** to recreate the phone's
firewall rule (`GOAT phone 8797`, TCP inbound). It finishes with a
**Needs you** list for anything it can't do itself.

## 4. Windows settings it can't change

- Settings > Privacy > Microphone: turn on "Let desktop apps access your microphone".
- Pick the right default microphone in Sound settings.

## 5. Start

Double-click `GOAT.bat`, or log out and back in (autostart brings it up asleep).
Say "GOAT, are you working?" or run `py -3.13 python\goat_doctor.py`.

## Optional

| Tool | For | Install |
|---|---|---|
| Node.js | phone page tests (`python\test_phone_*.js`) | `winget install OpenJS.NodeJS.LTS` |
| Ollama + qwen3:4b | the local fallback brain | `winget install Ollama.Ollama`, then `ollama pull qwen3:4b` |
| FFmpeg | media jobs | `winget install Gyan.FFmpeg` |
| NVIDIA driver | GPU ear (`stt\bin-cuda`) | from NVIDIA |

## Switches (environment variables)

| Variable | Default | Effect |
|---|---|---|
| `GOAT_TWO_BRAINS` | on | Gemini talks, Claude works. `off` = one brain. |
| `GOAT_TASK_ROUTER` | on | Big tasks go straight to Claude before the voice answers. `off` = the voice decides. |
| `GOAT_BRAIN_ROUTE` | on | Talk/task/deep tiers in one-brain mode. |
| `GOAT_ROOT` | (auto) | Overrides GOAT's home folder. |
