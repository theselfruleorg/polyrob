"""Flag-count ratchet (058 WS-2, 2026-09-21).

736 documented knobs on 2026-09-21, up from 408 on 2026-07-21 — +71% in two
months with nothing holding the line. This tree ratchets file size, layering,
path use, rate limiters, bool-env parsing, status silence, shipped assets, nav
count and inline scripts; the one surface an OPERATOR actually has to read had
no ratchet at all.

This does NOT forbid a new flag. It forbids a NET new flag: add one and retire
one, or raise/lower ``CEILING`` in the same commit and say why in the message.
The five named default bundles (``POLYROB_LOCAL`` / ``AUTONOMY_MODE`` /
``AUTONOMY_POSTURE`` / ``AGENT_COMPUTE_POSTURE`` / ``AGENT_BUILDER_MODE``) are
the intended operator surface — a sixth individual knob that only a bundle
should move is the thing this catches.

SHRINK-ONLY, mirroring ``tests/test_file_size_ratchet.py``, with the same two
guards: no growth, and a ceiling that may not drift above the real count (so a
retirement that forgets to lower the row stops the ratchet tightening).

⚠️ Reads ``core/flags_catalog.py`` — the checked-in literal that ships in the
WHEEL — and never ``docs/CONFIGURATION.md``, which ships only in the sdist
(``MANIFEST.in``). A test that read the doc would fail in an installed tree.
"""
from core.flags_catalog import CATALOG

# Frozen 2026-09-21 at the live count. Lower it when flags are retired.
# 2026-09-22: 736 -> 738 — LAZY_DEPS_ENABLED + TOOL_USAGE_TELEMETRY (058 T4.3), none retired.
# 2026-09-23 (064 factory): +1 — ONE <SURFACE>_SURFACE_ENABLED pattern row, so a new
# surface needs no enable-flag row of its own (configured = on).
# 2026-09-30 (069 P0): the four pre-069 agent-NFT rows renamed AGENT_NFT_* — net zero, no aliases.
# 2026-09-30 (069 v4 A1): 773 -> 771 — AGENT_NFT_GAS_REFILL_FLOOR_ETH and
# AGENT_NFT_GAS_REFILL_CAP_ETH retired with the account gas refill (no operator key to refill).
# 2026-10-02: 771 -> 772 — FEISHU_WEBHOOK_ALLOW_UNSIGNED, the explicit opt-in for an
# unsigned (token-only) Feishu webhook, which is now refused by default.
# 2026-10-03: 772 -> 773 — VALUELESS_CHAIN_MONEY (C13), the owner's opt-in to a core run on the
# Robinhood Chain testnet (native at $0, fee bounded in wei); off by default.
CEILING = 773  # 2026-09-29 impl handoff E (collection revealer): +2 — CRON_WRITE_JOBS_ENABLED
               # (the one scheduled write verb, off by default), the reveal gas cap.
               # 771: 2026-09-29 core handoff W12 / 080 D42a: +2 — the two account gas-refill
               # rows (retired again by 069 v4, above).
               # 769: 2026-09-29 core handoff W6 / 090 R4: +3 — LP_ETH_CAP, LP_ETH_DAILY_CAP,
               # LP_ETH_FLOOR (the v4 LP program's caps, in config, not in prose).
               # 766: X eval option A: +1 X_OAUTH2_REDIRECT_URI (the console callback `/x login`
               # sends X to; nothing is derived when unset).
               # 765: 2026-09-24: 067 P2 +2 — POLYROB_PACKS, POLYROB_PACKS_DISABLED (the pack
               # loader's enabled set; the proposal's two flags for the whole feature).
               # 763: 2026-09-24: 067 F4b -4 — HITL_MODE, DESTRUCTIVE_ACTION_POLICY, API_HOST,
               # API_PORT: their only reader was an unread ServerConfig field (deleted),
               # so the rows documented knobs that never had an effect.
               # 767: 2026-09-24: 067 F4 +8 — the reverse contract's READ_RE now sees the
               # aliased _core_int_env/_core_float_env reads in agents/task/constants.py:
               # the 7 per-tool timeouts (MCP/BROWSER/FILESYSTEM/POLYMARKET/SHELL/
               # CODE_EXEC/DEFAULT_TOOL_TIMEOUT_SECONDS) and SCREENSHOT_JPEG_QUALITY were
               # live but undocumented; the 4 dead close timeouts became constants.
               # 759: 2026-09-24: 067 F2 -21 — deleted proven waste: 3 dead rows
               # (POLYROB_SUPERVISED, SURFACE_ADMIN_USER_IDS, PAYMENT_ASSETS_ENABLED);
               # 6 named *_SURFACE_ENABLED rows folded into the <SURFACE>_SURFACE_ENABLED
               # pattern (EMAIL_SURFACE_ENABLED stays: an AUTONOMY_MODE bundle member);
               # 7 OWNER_*_ID rows -> OWNER_<SURFACE>_ID; 9 AUX slot rows ->
               # AUX_MODEL_<SLOT> / AUX_PROVIDER_<SLOT> / AUX_FALLBACK_<SLOT>. No reader
               # changed; the reverse contract matches the patterns.
               # Previous: 780. 2026-09-23: +1 — DEFI_PORTFOLIO_PRICE_BUDGET_SEC (bounds the portfolio
               # pricing loop after it outlived the 600s stall limit twice and blinded
               # the SAFETY monitor). Previous: 779 (050 +1, the agent-NFT enable flag).
#              2026-09-23: 025 +2 — MEMORY_SCOPES_ENABLED (the master switch, default
#              false) + AUTONOMY_MEMORY_REGIME (shared|scoped|sealed, default scoped);
#              promotion policy / row cap / retention are module constants, not knobs.
#              776: 2026-09-23: 033 +3 — EXTERNAL_WRITE_TELEMETRY + EXTERNAL_WRITE_PAUSE_GATE
#              (default on) + EXTERNAL_WRITE_STRICT (default off): the effect
#              recorder, its pause gate, and the unclassified-writer refusal.
#              773: 2026-09-23: 066 P2 +1 — WALLET_SIGNER (local|shadow|remote, default local:
#              where the agent's signatures come from; polyrob-signer); none retired.
#              772: 2026-09-23: 064 order 0007 +3 — the DingTalk surface: DINGTALK_CLIENT_ID,
#              DINGTALK_CLIENT_SECRET, OWNER_DINGTALK_ID (enable flag = the pattern row).
#              769: 2026-09-23: 064 order 0003 +3 — the Feishu webhook fallback: FEISHU_TRANSPORT
#              (ws|webhook), FEISHU_ENCRYPT_KEY, FEISHU_VERIFICATION_TOKEN; none retired.
#              766: 2026-09-23: 022 +1 — FINANCIAL_CLAIM_GATE_ENABLED (the emergency off switch
#              for the send-time financial-claim gate, default ON); none retired.
#              765: 2026-09-23: 064 order 0001 +4 — the Feishu / Lark surface: FEISHU_APP_ID,
#              FEISHU_APP_SECRET, FEISHU_DOMAIN, OWNER_FEISHU_ID (its enable flag is the
#              <SURFACE>_SURFACE_ENABLED pattern row); none retired.
#              761: 2026-09-23: 066 P1 +1 — LAZY_DEPS_MODE (off|trusted|legacy; the old
#              boolean LAZY_DEPS_ENABLED stays as its deprecated alias, one release).
#              760: 2026-09-23: 060 WS-8 -3 — VOICE_TRANSCRIPT_ECHO, TELEGRAM_INCREMENTAL_STREAM
#              and USER_DELIVERY_LIFECYCLE_DAILY_CAP retired into PREF_SCHEMA
#              (voice.transcript_echo / stream.telegram / delivery.lifecycle_daily_cap):
#              the owner sets them from chat. Net 060: +3 -3 = 0.
#              763: 2026-09-23: 060 WS-3 DOC_KIND_ENFORCED (the revert for the RECORD guard
#              on workspace documents; undeclared = record).
#              762: 2026-09-23: 060 WS-6 OWNER_RULES_SUPERSEDE (the revert for supersede,
#              never evict — a dropped owner rule is kept, dated, and not injected).
#              761: 2026-09-23: 060 WS-1 SELF_CONTEXT_COMBINED (the revert that re-joins the
#              unwelded self-context blocks, byte-identical; WS-8 retires owner knobs
#              into PREF_SCHEMA to pay it back).
#              760: 2026-09-23: 063 F9 ANTHROPIC_DEFERRED_TOOLS (shape (b), Anthropic's
#              native mid-conversation tool changes — the default late-tool path
#              on a capable Anthropic model; TOOL_SCHEMAS_FROZEN flipped to
#              default OFF in the same commit and stays as the opt-in shape (a)).
#              759: 2026-09-23: 063 F15 TOOL_RESULT_AGE_CHARS (demote long OLD tool
#              results to one line + the offload pointer at the compaction
#              boundary; a 40k web_fetch used to ride every request verbatim).
#              757: 2026-09-23: 063 F11 COMPACTION_WARM_PREFIX (the compaction
#              summariser replays the cached prefix and appends the instruction,
#              instead of sending the whole middle again as cold text).
#              756: 2026-09-23: 063 F16(ii) STATE_MESSAGE_EPHEMERAL (the per-step
#              state message rides the one-shot ephemeral rail instead of being
#              appended into history and spliced back out mid-deque every step).
#              The count also carries two rows that landed in this shared working
#              tree from the parallel 063 streams: F9 TOOL_SCHEMAS_FROZEN and
#              F13 FOUNDATION_REPLAY. All three retire one minor release later.
#              754: 2026-09-23: 063 F13 FOUNDATION_REPLAY (replay the PERSISTED
#              foundation on a reload instead of re-rendering every block from
#              the live env; a deploy re-wrote the whole cached prefix).
#              753: 2026-09-23: 063 F8 TOOL_CATALOG_TAIL_UPDATES (the tool catalog
#              becomes a pinned baseline + tail deltas; the in-place rewrite of
#              foundation [7] re-billed the whole conversation on any status flip).
#              752: 2026-09-22: F7 MEDIA_KEEP_MAX / MEDIA_RETIRE_BATCH / MEDIA_KEEP_FLOOR
#              (the image-retirement step function; the old rule re-wrote the
#              previously-newest screenshot turn on every step of a browser run).
#              749: F4 ANTHROPIC_CACHE_TTL (the Anthropic prompt-cache window
#              by session class: 1h interactive, 5m autonomous) and F26
#              MAX_EXTRACTED_CONTENT_TURN_SIZE (the per-turn tool-result offload
#              budget).
#              747: 062 POLYROB_BOOTSTRAP_STAGES (the installer's handoff to
               # `polyrob setup`; an env var shipped code READS must be documented, and it
               # is not a knob an operator sets)
               # 2026-09-22: +1 KB_AUTO_INGEST_ARTIFACTS (WS-K3) — the ONE revert for
               # indexing what the agent writes. Prod held 76 indexed sources against 473
               # produced documents; a fix that ships default-off fixes nothing.
               # 2026-09-22: +1 SESSION_RETENTION_DAYS (WS-K2) — the third retention
               # window. `memories` and `episodes` had one; the largest store on disk
               # (2,449 trees, 5.0 GB) had none. Offsets the DEAD
               # AUTO_KNOWLEDGE_RETENTION_DAYS field deleted in the same commit, which
               # had a default of 7 and zero consumers (it was never in the catalog).
               # 2026-09-22: +1 MEMORY_CONSOLIDATE (WS-K4) — the revert for distilling
               # repeated recall. 19,001 raw rows against 0 curated notes on prod.
# A flag row is a deliberate act — unlike file line counts there is no ordinary
# churn to absorb, so the ceiling tracks the real count exactly.
SLACK = 0

# 067 F1: the PUBLIC tier (the 5th catalog field, from PUBLIC_FLAGS in
# scripts/gen_flags_catalog.py) is what `doctor --flags` shows by default — the
# operator surface. Same exact-tracking rule, slack 0: a new public flag is a
# deliberate act, so raise this in the same commit and say why.
PUBLIC_CEILING = 88  # 067 F2: +OWNER_<SURFACE>_ID (the pattern replaced named rows)


def test_flag_count_does_not_grow():
    assert len(CATALOG) <= CEILING, (
        f"{len(CATALOG)} flags vs ceiling {CEILING}. Retire a flag, fold it into "
        "one of the five named bundles, or raise CEILING here with the reason in "
        "the commit message — 736 -> 408 took two months in the other direction."
    )


def test_ceiling_tracks_actual():
    drift = CEILING - len(CATALOG)
    assert drift <= SLACK, (
        f"CEILING {CEILING} sits {drift} above the real count {len(CATALOG)} — a "
        "retirement forgot to lower this row, so the ratchet stopped tightening."
    )


def _public_count() -> int:
    return sum(1 for row in CATALOG if row[4] == "public")


def test_public_tier_does_not_grow():
    assert _public_count() <= PUBLIC_CEILING, (
        f"{_public_count()} public flags vs PUBLIC_CEILING {PUBLIC_CEILING}. The "
        "public tier is the operator surface; demote a flag (drop it from "
        "PUBLIC_FLAGS in scripts/gen_flags_catalog.py) or raise the ceiling "
        "with the reason in the commit message."
    )


def test_public_ceiling_tracks_actual():
    assert PUBLIC_CEILING - _public_count() <= 0, (
        f"PUBLIC_CEILING {PUBLIC_CEILING} sits above the real public count "
        f"{_public_count()} — lower it."
    )


def test_every_row_has_a_known_tier():
    bad = [row[0] for row in CATALOG if len(row) < 5
           or row[4] not in ("public", "advanced", "internal")]
    assert not bad, f"catalog rows without a valid tier: {bad[:10]}"
