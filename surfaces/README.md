# Surfaces Package — chat-surface adapters

_Last reviewed: 2026-09-23 (064 S2b wave F). The access model is
`docs/architecture/components/chat-access-model.md`; the owner-seat contracts are
`docs/architecture/components/interface-contracts.md`._

## Overview

The `surfaces` package holds one adapter per chat platform. Each adapter
implements the common **`Surface` contract** (`core/surfaces/surface.py`), so the
same Task agent powers every channel and the shared machinery (streaming state
machine, delta buffering, live edit) lives in the base class — a surface adds only
what is genuinely transport-specific.

Every surface is **off by default** (its `*_SURFACE_ENABLED` flag). Inbound
messages resolve to a tier (OWNER / CORRESPONDENT / GROUP_MEMBER / DENIED) upstream
in `core/surfaces/dispatcher.py`; a surface is an adapter, never a policy.

## The surface catalog — one row per surface

`core/surfaces/catalog.py::SURFACES` is the SSOT for which surfaces exist and
what each one is: its package, enable flag, owner-address env, whether the owner
can be reached there (`owner_seat`), whether its sender id is forgeable (email),
its transport, pip extra, cron-target status, message limit, credentials, the
standalone `polyrob <id>` command and its state databases.

Every list that used to name surfaces by hand derives from it: owner fan-out
order, owner admin summary, the session creator label, cron delivery targets, the
forgeable set, the owner alias set, the db manifest, the gateway, the CLI map and
help group, the doctor lines and the update process guard.
`tests/unit/core/surfaces/test_surface_catalog.py` fails on a new literal list of
surface ids in any of those modules.

### Adding a surface

1. One catalog row in `core/surfaces/catalog.py`.
2. One package `surfaces/<id>/` with:
   - `surface.py` — the `Surface` subclass with honest `capabilities`;
   - `harness.py` — the transport (`surfaces/_shared.py::BaseHarness` for a
     poll/WS loop, `core/surfaces/inbound_webhook.py::WebhookSurface` for push);
   - `launch.py` — `async def launch(ctx) -> Launched | None` (contract in
     `surfaces/_launch.py`): check its credentials (WARN + `None` = skipped), build
     the harness, return it. `polyrob gateway` does the rest; webhook surfaces
     share one HTTP server at `/webhooks/<id>`.
   - `probe.py` — `async def probe(env) -> ProbeResult` (contract in
     `surfaces/_probe.py`): prove the credential with a READ, never a send.
3. The flag rows in `docs/CONFIGURATION.md` for the enable flag, the owner env and
   every credential (+ both generators + the flag-count ratchet).
4. If it needs a vendor SDK: a pip extra in `pyproject.toml` (and in `all`),
   `requirements.lock` regenerated with the documented `--python-version` line,
   rows in `core/optional_extras.py`, and the SDK imported INSIDE the function
   that uses it — never at module top. To run it on prod, the extra also joins
   `PROD_EXTRAS` in `scripts/deploy_prod.sh` and `requirements.txt`.

Enforced: `tests/unit/core/surfaces/test_surface_catalog.py` (one row + one package
is enough; every row ships launch/probe; limits agree; no hand-kept surface list) and
`tests/unit/core/surfaces/test_surface_package_conventions.py` (the extra is real and
has a pip hint; the env names are documented flags; every package imports with its
SDK blocked).

## The shared inbound executor

A surface routes each inbound (`core.surfaces.dispatcher.route_inbound`) and
hands the decision to `surfaces/_actor.py::act_on_inbound`. That is the ONE
import a surface makes for the shared RouteDecision → TaskAgent executor. Its body
still lives in `surfaces/telegram/harness.py` (with the owner-verb `*_ops`
modules); no surface imports Telegram to reach it.

## Owner-verb parity (064 E-S2-01)

`tests/unit/test_surface_verb_parity.py` has one column per catalog row: every owner verb in
`core/verbs.py` reaches the surface (an authenticated owner seat + a `_COMMANDS` verb routed by
the shared executor) or is a documented `None` with a reason. A new catalog row without a
column fails; a `None` that starts reaching fails. Today: every owner verb reaches every
non-forgeable surface except the two REPL-local panes (`/gates`, `/meter`); email is
correspondent-only (`*`). Native menus/buttons per platform are separate stream orders.

## Package structure

```
surfaces/
├── _actor.py      # the shared inbound executor seam
├── _launch.py     # the gateway launch contract (LaunchContext, Launched)
├── _shared.py     # BaseHarness / route_and_act / TextSink
├── telegram/      # long polling (aiogram, `telegram` extra); owner verbs, rooms
├── email/         # IMAP poll + SMTP; correspondent-only (forgeable From:)
├── slack/         # Socket Mode
├── discord/       # gateway WS (hand-rolled)
├── signal/        # signal-cli SSE
├── whatsapp/      # Meta Cloud API webhook
├── x/             # X DM polling
├── feishu/        # Feishu / Lark WS long connection (lark-oapi, `feishu` extra)
└── dingtalk/      # DingTalk Stream Mode (hand-rolled aiohttp); session-webhook replies
```

## Key invariants

- **Tier = authenticated sender, never thread membership.** A correspondent's
  reply reaches the originating session as `MessageOrigin.CORRESPONDENT` data,
  never the owner "obey" queue.
- **Owner-by-email is off** — a `From:` header is forgeable (`forgeable=True` in
  the catalog), so every email sender is correspondent or denied.
- **Owner alias** (the configured owner id IS the owner principal, no pairing
  row) is Telegram only (`alias_owner=True`). On the other surfaces the owner is
  recognised through a pairing row. Widening that is a reviewed security change.
- **Transport only.** Adapters handle fetch/send/dedup/rate-limit; access policy
  and the capability gate live in `core/surfaces/` and
  `agents/task/agent/core/correspondent_gate.py`.
