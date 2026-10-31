# GOAT roadmap

Features Giorgi asked for that are not built yet. GOAT's voice reads this when
he asks "where's the roadmap" (find_files "roadmap"); Claude moves an item to
**Done** when it ships.

## Next update

- **Image, video and 3D generation** (asked 2026-10-29 00:58: "I want you to
  give me a 3D design image for this Volvo YCC car ... schedule that build for
  the next GOAT update"). GOAT should make pictures, short videos and 3D models
  on request. Open questions for Giorgi: which service (Gemini image models on
  the existing key, a paid video API, a local Stable Diffusion / TripoSR on the
  RTX 3050), and the budget.

## Waiting on Giorgi (money or a decision)

- **Gemini paid tier** — the free tier gives 20 voice requests per model per
  day and Live refuses under load; "voice link dropped" moments go away with
  billing.
- **ElevenLabs or OpenAI credits** — the Georgian ear falls back to weaker
  engines when the free quota is out.
- **Fable 5.1 for Claude's work** — needs prepaid credits (currently $0).

## Done

- 2026-10-31 — the voice's own desk hands: notes and text files, saving the chat
  log, reading and logging work hours; it tries instead of saying "that needs
  Claude". Name-only mode. "Shut down yourself". "Open my email" opens Gmail.
  The usage-limit retry no longer announces a failure every 20 minutes and
  quotes the real reset day and time.
