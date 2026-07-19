# GOAT UI — "Instrument, refined" (2026-07-20)

Direction chosen by Giorgi from A/B/C (A ⭐). Goal: keep GOAT's identity —
the string, one accent per theme, quiet type, no boxes — and modernize the
craft. References: Rams/Teenage-Engineering instrument panels (zones, tracked
caps, hairline rules), Linear/Raycast desktop polish (type scale, kbd hints,
quiet separators). No new dependencies: Windows-stock fonts only.

## Changes (all in `python/ui_qt.py`)

### Typography
- Replies (`replyNow`/`replyOld`): `Segoe UI Variable Display` with `Segoe UI`
  fallback (Georgian glyphs fall back automatically).
- Machine voice → `Cascadia Mono` (fallback `Consolas`): state word, clock,
  tool lines, work model/steps/idle, footer, usage meter, send/work chips,
  theme/mic buttons.
- Eyebrows (`paneltitle`, wordmark): tracked caps kept, sizes tuned.

### Structure
- Hairline rules at ~35% alpha of the theme's `faint`:
  vertical rule between work lane and talk lane; horizontal rule above the
  input rail.
- Work panel as ledger: eyebrow + live mono `mm:ss` elapsed timer while
  running (`done · mm:ss` after), rule under the header, then task/steps.

### Rhythm
- Transcript column spacing 6 → 14 px; `youNow`/`youOld` top margins up.

### Bottom rail
- `❯` accent prompt glyph before the input; placeholder shortened to
  "say the word — or type"; shortcut hints live ONLY in the footer line.
- talk/work/hard buttons → compact mono chips (`TALK ↵ · WORK ^↵ · HARD ^⇧↵`).
- Footer single line: shortcuts left, `<talk model> · <usage>` right.

### Small
- Scrollbar 6 → 3 px, 40%-alpha handle. State word gets a `●` accent dot.

### Explicitly untouched
String widget, 4 theme palettes, layout skeleton, engine events/API,
`_s()` scale system (wraps every new px), settings drawer behavior,
window geometry/config keys.

## Verification
- Offscreen render (WA_DontShowOnScreen) of all 4 themes + drawer,
  before/after compare by eye.
- `py -3.13 test_scroll.py`, `test_statusword.py`, `self_check.py preflight`.
