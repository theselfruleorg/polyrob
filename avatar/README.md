# avatar/ — POLYROB agent avatars (Mindprint)

Deterministic generative agent **face + voice signature**. One seed → one identity,
rendered the same everywhere: CLI, webview, social profile, and (later) an external
NFT service. No crypto here.

## Files

| Path | Role |
|---|---|
| `mindprint.js` | **The engine — SSOT for pixels.** A classic script (sets `window.Mindprint` + `SHAPES`/`MODES`/`TIERS`/`TRAITS`/`cyrb128`/`sfc32`/`hsl2rgb`). Extracted verbatim; **do not edit casually** — a change alters every existing face (bump the `generator` string and re-render). Guarded by `tests/unit/avatar/` (Node determinism + portal-copy equality). |
| `studio.html` | The 4-step browser picker (① name ② look ③ voice ④ save). Loads `mindprint.js`; **Copy config JSON** emits the frozen blob. Open via `polyrob pfp studio`. |
| `config/rob.json` | The frozen STOCK identity blob (Rob #1's face; reproducibility SSOT): `{generator, seed, variant, size, override, ...}`. `pfp generate --stock` / `--config` reproduce it — the default `generate` mints a RANDOM identity instead. |
| `renders/rob.png` | Committed reference still of the STOCK identity — the last-resort fallback for `pfp generate --stock` and the drift golden. `rob.meta.json` carries its traits/voice. Never substituted for a randomized identity (the native mesh renderer covers headless). |
| `webview/avatar-live.js` | Read-only live embed for the console (fetch `/pfp.json` → animate the canvas). |

The Python side lives in `modules/pfp/` (`config`, `mesh` = pure-Python field port,
`renderer` = headless Chromium still, `still` = native Pillow/numpy dot renderer,
`store`, `push`) and `core/instance.py`
(instance-scoped `pfp_path`/`load_pfp_meta`/`voice_signature`).

## One-time setup: generate → randomize → keep

Avatar setup happens ONCE per instance. `generate` mints a random DRAFT; while it is
a draft you can re-roll it as often as you like; `keep` (or `pick`'s save) accepts it
and locks the identity PERMANENTLY — after that no verb can change it (`generate
--force` only re-renders the same identity's pixels, and the deliberate escape hatch
is deleting the instance `identity/{id}/pfp/` directory by hand). `push` requires a
kept identity. A pre-lock-era `pfp.json` (no `locked` key) is treated as kept.

```bash
polyrob pfp generate                 # start setup: mint a RANDOM draft (--stock for Rob #1's face)
polyrob pfp randomize [face|voice]   # re-roll the draft: everything / face-only / voice-only
polyrob pfp say [text]               # HEAR the voice signature (native TTS: say / espeak / SAPI)
polyrob pfp keep                     # accept — freeze the identity forever
polyrob pfp pick                     # interactive setup (shuffle + preview; enter = keep forever)
polyrob pfp show --animate           # watch the face live in the terminal (truecolor)
polyrob pfp push                     # set the kept avatar on X/Discord (live) + Telegram (BotFather steps), flag-gated
```

Every setup step SHOWS the face inline (truecolor TTY) and the report links the
hear/re-roll/keep verbs, so roll → see → hear → keep needs no extra commands.
(`/pfp generate`, `/pfp randomize [face|voice]`, `/pfp say`, `/pfp keep` and
`/pfp show` do the same inside the REPL.)

**Web setup:** the webview **/identity** page runs the whole flow too — live animated
face, DRAFT/KEPT state, a 🔊 hear-voice button (browser `speechSynthesis`, same
clear-English timbre mapping as the studio), and draft-only re-roll/keep controls
backed by `POST /api/pfp/{generate,randomize,keep}` (403 on a read-only console; the
store enforces the once-only lock contract regardless of caller). The still PNG render chain: the exact JS engine headlessly via the `[browser]`
extra (`pip install '.[browser]' && playwright install chromium`) → the native
Pillow/numpy dot renderer (`modules/pfp/still.py` — same face from the parity-tested
field port, no browser) → `renders/rob.png` (stock identity only). The CLI live render
runs the pure-Python field port (`modules/pfp/mesh.py`), parity-tested against
`mindprint.js`.

## Voice signature

`{pitch, rate, timbre}` — engine-agnostic scalars (timbre is an abstract `0–1`, NOT a
browser voice index), seeded per agent and persisted in the blob so a future
voice-interface app can speak in the agent's own voice.

## Brand

The canonical **polyrob logo** (square-headed Mindprint mark + wordmark) is generated
by the same engine — see `web/portal/brand/`.
