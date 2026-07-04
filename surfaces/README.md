# Surfaces Package — chat-surface adapters

_Last reviewed: 2026-06-30. For the access model see the "Chat-surface access model" section of ../AGENTS.md; run via `polyrob telegram` / `polyrob email`._

## Overview

The `surfaces` package holds inbound/outbound adapters for multi-user chat
channels. Each adapter implements the common **`Surface` contract**
(`core/surfaces/surface.py`) so the same Task agent core powers every channel and
the heavy machinery (streaming state machine, delta buffering, live-edit) is shared
in the base class — a surface adds only what is genuinely transport-specific.

Both surfaces are **off by default**. When enabled they treat inbound messages from
non-owners as **untrusted data** via the three-tier OWNER / CORRESPONDENT / DENIED
access model resolved upstream at `core/surfaces/dispatcher.py`.

## The Surface contract

A surface MUST implement: `surface_id`, `capabilities`, `send()`, `start()`,
`stop()`. It gets for free from the base class: `stream()` (buffer deltas, flush on
finalize, or — if it advertises `supports_edit` + opts into incremental streaming —
the generic live-edit engine that opens one message and edits it in place),
`identify()`, and the turn lifecycle plumbing. Incremental streaming plugs in four
transport primitives (`_open_stream_message`, `_edit_stream_message`,
`_send_stream_overflow`, `_stream_target`) plus two policy hooks.

## Package structure

```
surfaces/
├── telegram/             # Telegram bot surface (aiogram, optional `telegram` extra)
│   ├── harness.py        # long-poll harness + decision→action dispatch + lifecycle
│   ├── inbound.py        # inbound normalize/route → agent
│   ├── surface.py        # Surface impl: outbound send + live-edit streaming
│   ├── dedup.py          # update/message-id dedup
│   ├── rate_limit.py     # per-chat send rate limiting
│   ├── markdown.py       # Telegram-flavored markdown rendering
│   ├── voice.py          # voice-note transcription (optional `voice` extra)
│   └── __init__.py
└── email/                # Email surface (IMAP poll + SMTP, EMAIL_SURFACE_ENABLED)
    ├── harness.py        # IMAP poll loop over the transport-free inbound spine
    ├── inbound.py        # normalize_email_message → process_email; quoted-history
    │                     #   truncation; Message-ID / surrogate dedup; marks \Seen
    ├── surface.py        # buffered SMTP outbound (Surface impl)
    ├── dedup.py          # Message-ID / surrogate dedup store
    ├── seed.py           # correspondent auto-seed helper
    └── __init__.py
```

## Key invariants

- **Tier = authenticated sender, never thread membership.** A correspondent's reply
  is delivered to the originating session as `MessageOrigin.CORRESPONDENT` *data*
  (wrapped untrusted), never to the user "obey" queue.
- **Owner-by-email is off in v1** — a `From:` header is forgeable, so every email
  sender is correspondent or denied. Owners are seeded manually
  (`polyrob owner invite`).
- **Transport only.** Adapters here handle fetch/send/dedup/rate-limit; access
  policy and the capability gate live in `core/surfaces/` and
  `agents/task/agent/core/correspondent_gate.py`.
- Telegram inbound's deterministic helpers (`derive_webhook_path`,
  `act_on_inbound`, `normalize_email_message`) are pure and unit-tested without a
  live mailbox/bot.

## Related

- Access tiers + dispatcher: `core/surfaces/` (`dispatcher.py`, `access.py`,
  `correspondents.py`)
- CLI/local surface: [`../cli/`](../cli/README.md)
- The agent core every surface drives: [`../agents/`](../agents/README.md)
