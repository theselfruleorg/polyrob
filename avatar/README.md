# avatar/ — POLYROB agent avatars (Mindprint)

Deterministic generative agent **face + voice signature**. One seed → one identity,
rendered the same everywhere: CLI, webview, social profile, and (later) an external
NFT service. No crypto here.

## Files

| Path | Role |
|---|---|
| `mindprint.js` | **The engine — SSOT for pixels.** A classic script (sets `window.Mindprint` + `SHAPES`/`MODES`/`TIERS`/`TRAITS`/`cyrb128`/`sfc32`/`hsl2rgb`). Extracted verbatim; **do not edit casually** — a change alters every existing face (bump the `generator` string and re-render). Guarded by `tests/unit/avatar/` (Node determinism + portal-copy equality). |
| `studio.html` | The 4-step browser picker (① name ② look ③ voice ④ save). Loads `mindprint.js`; **Copy config JSON** emits the frozen blob. Open via `polyrob pfp studio`. |
| `config/rob.json` | The frozen identity blob (reproducibility SSOT): `{generator, seed, variant, size, override, ...}`. |
| `renders/rob.png` | Committed reference still — the headless-free fallback for `pfp generate` and the drift golden. `rob.meta.json` carries its traits/voice. |
| `webview/avatar-live.js` | Read-only live embed for the console (fetch `/pfp.json` → animate the canvas). |

The Python side lives in `modules/pfp/` (`config`, `mesh` = pure-Python field port,
`renderer` = headless Chromium still, `store`, `push`) and `core/instance.py`
(instance-scoped `pfp_path`/`load_pfp_meta`/`voice_signature`).

## Reproduce / re-pick

```bash
polyrob pfp show --animate          # watch the face live in the terminal (truecolor)
polyrob pfp pick                     # shuffle face + voice independently → config/rob.json
polyrob pfp generate                 # freeze into the instance identity home (idempotent)
polyrob pfp push --twitter           # set the X avatar (needs PFP_PUSH_TWITTER=true)
```

The still PNG is rendered headlessly via the `[browser]` extra
(`pip install '.[browser]' && playwright install chromium`); without it `generate`
copies `renders/rob.png`. The CLI live render needs neither — it runs the pure-Python
field port (`modules/pfp/mesh.py`), parity-tested against `mindprint.js`.

## Voice signature

`{pitch, rate, timbre}` — engine-agnostic scalars (timbre is an abstract `0–1`, NOT a
browser voice index), seeded per agent and persisted in the blob so a future
voice-interface app can speak in the agent's own voice.

## Brand

The canonical **polyrob logo** (square-headed Mindprint mark + wordmark) is generated
by the same engine — see `web/portal/brand/`.
