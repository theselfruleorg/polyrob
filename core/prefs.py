"""Typed per-user preferences: schema, storage, resolver (owner-UX wave §3).

A curated, schema-validated layer of user-tunable knobs stored per-tenant at
``identity/{instance_id}/user_{uid}/preferences.toml`` and resolved as
pref > env > default — EXCEPT guarded keys, which merge most-restrictive
(min()/union/narrow_list/AND/stricter-provider) so a preference can tighten
operator policy but never widen it. Polarity note: ``union`` tightens a
deny/require list (either side adding an entry restricts more); ``narrow_list``
tightens an ALLOWLIST-shaped list (a pref can only intersect the operator set,
never add to it — union would invert the polarity there). No file present ==
byte-identical legacy behavior.

Schema invariant: NO secret-typed keys, ever (credentials stay in env files).
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SENSITIVITY_SAFE = "safe"
SENSITIVITY_GUARDED = "guarded"

# 018 P0.4 — enforcement honesty: "enforced" = a runtime path resolves this
# pref and behavior changes; "advisory" = it only steers the prompt (the
# style.* design). The panel renders the label so an owner never sets a key
# and wonders why nothing hard-changes.
ENFORCEMENT_ENFORCED = "enforced"
ENFORCEMENT_ADVISORY = "advisory"

# Format rules for structured str prefs that are rendered VERBATIM into the
# SELF_CONTEXT style line (P2 T1 review fix). Format validation is STRONGER
# than threat-scanning here: a structured field is simply incapable of
# carrying injected prose. Because ``load_preferences`` re-validates every
# entry via ``validate_pref``, a hand-edited preferences.toml is neutralized
# on load too (the bad entry is dropped), not only at write time.
_QUIET_HOURS_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")
_LANGUAGE_RE = re.compile(r"^[A-Za-z][A-Za-z-]{0,31}$")
#: Characters that would break a value rendered INLINE into a prompt block
#: (the room name lands in an XML-ish attribute and in one prose sentence).
_CTRL_CHARS = re.compile(r"[\r\n\t\x00-\x1f\x7f]+")

# APPROVAL_PROVIDER strictness ladder for the stricter_provider merge.
# `auto_notify` (013 T4: allow + audit + owner notify — the act-and-report default
# under AUTONOMY_MODE=autonomous) sits between allow-all `auto` and the blocking
# `interactive_cli`, so an owner pref of interactive_cli/deny still tightens over it.
_PROVIDER_ORDER = ("auto", "auto_notify", "interactive_cli", "deny")


@dataclass(frozen=True)
class PrefSpec:
    key: str
    type: str                       # bool|int|float|str|list|enum
    sensitivity: str                # safe|guarded
    merge: str                      # override|min|union|narrow_list|and|stricter_provider|stricter_policy
    applies: str                    # live|next-turn|next-session|restart
    env_flag: str | None = None
    enum_values: tuple[str, ...] = ()
    min_value: float | None = None
    max_value: float | None = None
    description: str = ""
    # Display-only fallback for keys with NO env flag (018 P0.1): the value the
    # enforcement site uses when nothing is configured. Env-backed keys resolve
    # their honest default through the flags catalog instead — see
    # _builtin_default(). Never consulted by the resolver/merge path.
    default_display: object = None
    # 018 P0.4: enforced (a runtime path consumes it) | advisory (prompt-only).
    enforcement: str = ENFORCEMENT_ENFORCED


def _spec(key: str, type: str, sensitivity: str, merge: str, applies: str,
          env_flag: str | None = None, **kw) -> tuple[str, PrefSpec]:
    return key, PrefSpec(key=key, type=type, sensitivity=sensitivity,
                         merge=merge, applies=applies, env_flag=env_flag, **kw)


PREF_SCHEMA: dict[str, PrefSpec] = dict((
    _spec("ui.theme", "enum", SENSITIVITY_SAFE, "override", "live",
          enum_values=("auto", "dark", "light"), default_display="auto",
          description="Console appearance: follow the system, dark, or light"),
    _spec("approvals.require", "list", SENSITIVITY_GUARDED, "union", "next-session",
          "APPROVAL_REQUIRED_TOOLS",
          description="Action names that need owner approval (add=safe, remove=guarded)"),
    _spec("approvals.provider", "enum", SENSITIVITY_GUARDED, "stricter_provider",
          "next-session", "APPROVAL_PROVIDER", enum_values=_PROVIDER_ORDER,
          description="Approval provider; effective = stricter of env/pref"),
    _spec("approvals.deny", "list", SENSITIVITY_GUARDED, "union", "next-session",
          "POLYROB_TOOL_DENYLIST",
          description="Actions the agent may never run (add=safe, remove=guarded)"),
    _spec("budget.wallet_daily_usd", "float", SENSITIVITY_GUARDED, "min", "live",
          "WALLET_DAILY_CAP_USD", min_value=0.0,
          description="Wallet daily cap; effective = min(pref, env), wired into "
                      "PolicyGate via load_wallet_config() (G-13)"),
    # OWNER-OVERRIDE, not min-merged (owner decision 2026-09-18, after the
    # owner hit the min-merge wall for the second time: an approved raise
    # sat on disk as a tap that changed nothing, and the agent sent the owner
    # to edit the env file and restart). The env value is the DEFAULT; an
    # owner-approved pref replaces it in either direction. The operator's hard
    # envelope is the DAILY cap, which stays min-merged and env-only: the read
    # site (`core/wallet/config.py::effective_max_per_tx_usd`) clamps this to
    # it, so a single transaction can never exceed what a day may lose and
    # the maximum daily loss is unchanged by any raise made from chat.
    _spec("budget.wallet_per_tx_usd", "float", SENSITIVITY_GUARDED, "override", "live",
          "AGENT_WALLET_MAX_PER_TX_USD", min_value=0.0,
          description="Wallet per-transaction cap; an owner-approved pref replaces "
                      "the env default, clamped to the daily cap (never above "
                      "WALLET_DAILY_CAP_USD); wired into PolicyGate via "
                      "load_wallet_config() (G-13)"),
    # NOT min-merged, unlike the two wallet caps above, and deliberately so.
    # This is not a loss ceiling -- it is how much runs WITHOUT interrupting the
    # owner, and the catastrophic backstop (AGENT_WALLET_MAX_PER_TX_USD) still
    # binds above it. Raising it cannot widen maximum loss, only reduce how
    # often the owner is asked, so the owner may raise it from any seat. The
    # clamp lives at the read site (tx_guard.autonomous_max_usd).
    _spec("budget.defi_autonomous_usd", "float", SENSITIVITY_GUARDED, "override", "live",
          "DEFI_AUTONOMOUS_MAX_USD", min_value=0.0,
          description="Spend that executes without owner approval; effective = "
                      "min(pref or env, wallet per-tx ceiling)"),
    _spec("goals.daily_quota", "int", SENSITIVITY_SAFE, "min", "live",
          "GOAL_DAILY_QUOTA", min_value=1, max_value=100,
          description="Autonomous goal runs per day (capped by env)"),
    _spec("goals.max_concurrent", "int", SENSITIVITY_SAFE, "min", "live",
          "GOAL_MAX_CONCURRENT", min_value=1, max_value=10,
          description="Concurrent goal runs (capped by env)"),
    _spec("goals.notify_on_done", "bool", SENSITIVITY_SAFE, "override", "live",
          "GOAL_NOTIFY_ON_DONE",
          description="Notify owner when a goal completes (consumed by the goal "
                      "dispatcher's completion push, 018 P0.2)"),
    _spec("progress.telegram", "bool", SENSITIVITY_SAFE, "override", "live",
          "TELEGRAM_PROGRESS_EDITS",
          description="Live Telegram progress bubble: throttled edits showing the "
                      "current step/tool/wait state during a turn (019 P2; consumed "
                      "by the telegram harness per-turn tracker)"),
    _spec("digest.enabled", "bool", SENSITIVITY_SAFE, "override", "live",
          "OWNER_DIGEST_ENABLED", description="Daily owner digest on/off"),
    _spec("digest.channel", "enum", SENSITIVITY_SAFE, "override", "live",
          enum_values=("telegram", "email"), default_display="telegram",
          description="Digest delivery channel"),
    _spec("digest.quiet_hours", "str", SENSITIVITY_SAFE, "override", "live",
          description="No proactive delivery window as HH-HH, hours 0-23 "
                      "(local time), e.g. '23-08'. Enforced on the user-delivery "
                      "rail: sends inside the window are held and released at "
                      "window-end (018 P0.3)"),
    _spec("pause.phrases", "list", SENSITIVITY_SAFE, "union", "live",
          description="Extra object-free WORDS for the deterministic owner stop gate "
                      "(031): 'stop <these words>' still means stop EVERYTHING, e.g. "
                      "['ghosts', 'bots'] (a multi-word entry is split into words)"),
    _spec("delivery.rate_per_hour", "int", SENSITIVITY_SAFE, "min", "live",
          "USER_DELIVERY_RATE_PER_HOUR", min_value=1,
          description="Proactive messages/hour; the pref WINS unless the operator "
                      "set USER_DELIVERY_RATE_PER_HOUR, in which case that is a "
                      "ceiling and effective = min(pref, env)"),
    _spec("delivery.daily_cap", "int", SENSITIVITY_SAFE, "min", "live",
          "USER_DELIVERY_DAILY_CAP", min_value=1,
          description="Proactive messages/day; the pref WINS unless the operator "
                      "set USER_DELIVERY_DAILY_CAP, in which case that is a "
                      "ceiling and effective = min(pref, env)"),
    _spec("style.verbosity", "enum", SENSITIVITY_SAFE, "override", "next-turn",
          enum_values=("terse", "normal", "detailed"),
          enforcement=ENFORCEMENT_ADVISORY,
          description="Reply verbosity (rendered into the SELF_CONTEXT style line)"),
    _spec("style.language", "str", SENSITIVITY_SAFE, "override", "next-turn",
          enforcement=ENFORCEMENT_ADVISORY,
          description="Preferred reply language name/tag (letters/hyphens, "
                      "max 32 chars), e.g. 'en' or 'en-GB'"),
    _spec("style.tone", "str", SENSITIVITY_SAFE, "override", "next-turn",
          enforcement=ENFORCEMENT_ADVISORY,
          description="Free-text tone hint (threat-scanned at write, capped 200 chars; "
                      "rendered into the SELF_CONTEXT style line)"),
    _spec("ui.show_avatar", "bool", SENSITIVITY_SAFE, "override", "live",
          default_display=True,
          description="Show the agent's avatar in web views (identity page)"),
    _spec("session.toolset", "str", SENSITIVITY_SAFE, "override", "next-session",
          "POLYROB_AGENT_TOOLSET", description="Default toolset for new sessions"),
    _spec("session.persona", "str", SENSITIVITY_SAFE, "override", "next-session",
          "POLYROB_PERSONA",
          description="Default persona (template key or literal text; threat-scanned)"),
    _spec("autonomy.self_wake", "bool", SENSITIVITY_SAFE, "and", "next-session",
          "SELF_WAKE_ENABLED",
          description="Self-wake loop; pref can only disable (consumed by "
                      "deliver_self_wake, 018 P0.2)"),
    _spec("autonomy.background_review", "bool", SENSITIVITY_SAFE, "and", "next-session",
          "BACKGROUND_REVIEW_ENABLED",
          description="Background review loop; pref can only disable (consumed "
                      "by the reviewer's should-fire gate, 018 P0.2)"),
    _spec("outbound.policy", "enum", SENSITIVITY_GUARDED, "stricter_policy", "live",
          "OUTBOUND_POLICY", enum_values=("open", "domains", "allowlist", "off"),
          description="Outbound contact policy (013 T5); effective = stricter of "
                      "env/mode-default and pref on the open>domains>allowlist>off ladder"),
    _spec("outbound.domains", "list", SENSITIVITY_GUARDED, "narrow_list", "live",
          "OUTBOUND_DOMAINS",
          description="Outbound domain allowlist (013 T5 review); an allowlist-shaped "
                      "set can only NARROW — the pref intersects the operator env set "
                      "when one is set, or defines the set from scratch when env is "
                      "empty (there is no operator ceiling to widen past)"),
    _spec("outbound.max_new_recipients_per_day", "int", SENSITIVITY_GUARDED, "min", "live",
          "CORRESPONDENT_MAX_NEW_PER_DAY", min_value=0,
          description="New-correspondent seeding cap; effective = min(pref, env)"),
    _spec("outbound.daily_send_cap", "int", SENSITIVITY_GUARDED, "min", "live",
          "OUTBOUND_DAILY_SEND_CAP", min_value=1,
          description="Outbound sends/day under an open/domains policy; effective = "
                      "min(pref, env), enforced via resolve_outbound_daily_cap "
                      "(email/message send paths)."),
))


# --- 044 T17: the per-CHAT overlay (`chat.*`) --------------------------------
#
# A room's behaviour is configuration the owner writes, so it is validated by
# the SAME machinery (`validate_pref`/`_coerce`) as a tenant preference. It is
# deliberately NOT a member of PREF_SCHEMA: a `chat.*` value lives in a per-chat
# file (`chat_preferences_path`, read by `core/surfaces/chat_policy.py`), never
# in `preferences.toml`. Putting these rows in PREF_SCHEMA would list them on
# `/config`, `polyrob config` and the webview panel as tenant prefs and let
# `write_preference` store them where NOTHING reads them — the write-only trap
# `tests/unit/core/test_prefs_no_dead_keys_ratchet.py` exists to prevent. The
# same ratchet covers these rows through their own consumer proof.
CHAT_PREF_SCHEMA: dict[str, PrefSpec] = dict((
    _spec("chat.mode", "enum", SENSITIVITY_SAFE, "override", "next-turn",
          enum_values=("mention", "active", "listen", "off"),
          default_display="mention",
          description="When the agent answers in this room: mention (an addressed "
                      "line from anyone — the owner included), active (every "
                      "message), listen (an addressed owner/admin only), off "
                      "(never). Addressed = an @mention, a reply to the agent, "
                      "or a chat.wake_words hit"),
    _spec("chat.name", "str", SENSITIVITY_SAFE, "override", "next-turn",
          default_display="",
          description="What this room is CALLED in the agent's context block "
                      "(max 64 chars); empty = the chat title, else the chat id"),
    _spec("chat.instructions", "str", SENSITIVITY_SAFE, "override", "next-turn",
          enforcement=ENFORCEMENT_ADVISORY, default_display="",
          description="Standing instructions for this room, rendered into the "
                      "<surface> block (threat-scanned at write, capped 1500 chars)"),
    _spec("chat.verbosity", "enum", SENSITIVITY_SAFE, "override", "next-turn",
          enum_values=("terse", "normal", "detailed"),
          enforcement=ENFORCEMENT_ADVISORY, default_display="",
          description="Reply verbosity in this room (overrides the tenant's "
                      "style.verbosity for the room session)"),
    _spec("chat.tone", "str", SENSITIVITY_SAFE, "override", "next-turn",
          enforcement=ENFORCEMENT_ADVISORY, default_display="",
          description="Free-text tone hint for this room (threat-scanned at "
                      "write, capped 200 chars)"),
    _spec("chat.language", "str", SENSITIVITY_SAFE, "override", "next-turn",
          enforcement=ENFORCEMENT_ADVISORY, default_display="",
          description="Reply language for this room (letters/hyphens, max 32 "
                      "chars), e.g. 'en' or 'pt-BR'"),
    _spec("chat.wake_words", "list", SENSITIVITY_SAFE, "override", "next-turn",
          default_display=[],
          description="Extra case-insensitive regex patterns that address the "
                      "agent in this room without an @mention, e.g. ['hey rob']. "
                      "Matched with re.search anywhere in the line, so anchor a "
                      "whole word with \\b (['\\brob\\b']) or 'problem' wakes it. "
                      "At most 10 patterns, each at most 64 chars"),
    _spec("chat.context_lines", "int", SENSITIVITY_SAFE, "override", "next-turn",
          min_value=0, max_value=100, default_display=30,
          description="How many unanswered room lines the agent is shown as "
                      "<group-context>; 0 = answer with no room context"),
    _spec("chat.reply_cap_per_hour", "int", SENSITIVITY_SAFE, "override", "next-turn",
          min_value=0, max_value=200, default_display=20,
          description="Max replies the agent sends into THIS room per rolling "
                      "hour (overrides GROUP_REPLY_CAP_PER_HOUR)"),
    _spec("chat.member_cooldown_sec", "int", SENSITIVITY_SAFE, "override", "next-turn",
          min_value=0, max_value=600, default_display=20,
          description="Minimum seconds between two triggers from the same member "
                      "in THIS room (overrides GROUP_MEMBER_COOLDOWN_SEC)"),
    _spec("chat.quiet_hours", "str", SENSITIVITY_SAFE, "override", "next-turn",
          default_display="",
          description="Hours this room is owner/admin-only, as HH-HH (0-23, "
                      "local time), e.g. '23-08'"),
    _spec("chat.mute_until", "float", SENSITIVITY_SAFE, "override", "next-turn",
          min_value=0.0, default_display=0,
          description="Unix epoch until which this room is owner/admin-only "
                      "(written by /mute); 0 = not muted"),
    _spec("chat.member_verbs", "list", SENSITIVITY_SAFE, "override", "live",
          default_display=["help"],
          description="Slash verbs a PLAIN MEMBER may invoke in this room "
                      "(046). Only verbs in the closed grantable set are "
                      "honoured — today `help`, `mute`, `unmute`, `ban` and "
                      "`unban`; everything else is ordinary room chatter. "
                      "Granting one is what lets a member BUY it (a member "
                      "pays; an owner or room admin acts free). See chat.paid_*"),
    # --- 046: paid room actions -------------------------------------------
    # ⚠️ FLAT names, not dotted. `chat_policy.load()` computes
    # `field = key.split(".", 1)[1]` and skips anything that is not a flat
    # ChatPolicy dataclass field, so `chat.paid.mute.price_usd` would validate,
    # write, and then be SILENTLY DROPPED at load — a setting an owner can
    # change and nothing reads.
    #
    # SAFE, like every other chat key, even though these price money. In this
    # schema `guarded` means one thing: `core/config_service.py` and the webview
    # panel demand an explicit confirm before writing a TENANT preference. The
    # chat overlay does not go through either — `chat_policy.set` never reads
    # `sensitivity` at all — so `guarded` here would be decoration that READS as
    # protection. The real authority gate is the SEAT: `/groups set` is
    # owner-only and `/paid` is owner-or-room-admin.
    _spec("chat.paid_enabled", "bool", SENSITIVITY_SAFE, "override", "live",
          default_display=False,
          description="Offer PAID moderation actions in this room (046). OFF "
                      "means a member's /mute is refused, not priced"),
    _spec("chat.paid_asset", "str", SENSITIVITY_SAFE, "override", "live",
          default_display="",
          description="The asset_id members pay in (see `polyrob wallet asset "
                      "list`); empty = the instance default"),
    _spec("chat.paid_mute_usd", "float", SENSITIVITY_SAFE, "override", "live",
          min_value=0.0, default_display=0.0,
          description="Price of a paid mute in this room (USD, sized into the "
                      "asset at mint)"),
    _spec("chat.paid_mute_min_usd", "float", SENSITIVITY_SAFE, "override",
          "live", min_value=0.0, default_display=0.0,
          description="Floor of the band the agent's chosen price is clamped to"),
    _spec("chat.paid_mute_max_usd", "float", SENSITIVITY_SAFE, "override",
          "live", min_value=0.0, default_display=0.0,
          description="Ceiling of the band the agent's chosen price is clamped to"),
    _spec("chat.paid_mute_max_duration", "str", SENSITIVITY_SAFE, "override",
          "live", default_display="24h",
          description="Longest mute this room sells, e.g. 30m/2h/7d (never "
                      "above the catalog cap of 30d)"),
    _spec("chat.paid_unmute_usd", "float", SENSITIVITY_SAFE, "override",
          "live", min_value=0.0, default_display=0.0,
          description="Counter-pay price: what the TARGET pays to end a mute "
                      "early"),
    _spec("chat.paid_ban_usd", "float", SENSITIVITY_SAFE, "override", "live",
          min_value=0.0, default_display=0.0,
          description="Price of a paid ban in this room (USD, sized into the "
                      "asset at mint). Unset = ban is not for sale here"),
    _spec("chat.paid_ban_min_usd", "float", SENSITIVITY_SAFE, "override",
          "live", min_value=0.0, default_display=0.0,
          description="Floor of the ban price band"),
    _spec("chat.paid_ban_max_usd", "float", SENSITIVITY_SAFE, "override",
          "live", min_value=0.0, default_display=0.0,
          description="Ceiling of the ban price band"),
    _spec("chat.paid_ban_max_duration", "str", SENSITIVITY_SAFE, "override",
          "live", default_display="24h",
          description="Longest ban this room sells, e.g. 30m/2h/7d (never "
                      "above the catalog cap of 30d)"),
    _spec("chat.paid_unban_usd", "float", SENSITIVITY_SAFE, "override",
          "live", min_value=0.0, default_display=0.0,
          description="Counter-pay price: what the TARGET pays to end a ban "
                      "early"),
    _spec("chat.paid_mute_units", "str", SENSITIVITY_SAFE, "override",
          "live", default_display="",
          description="Price of a mute in TOKEN units of the room's asset "
                      "(e.g. 1500). Set, it decides the amount and no price "
                      "quote is needed; the USD price stays the declared value"),
    _spec("chat.paid_ban_units", "str", SENSITIVITY_SAFE, "override",
          "live", default_display="",
          description="Price of a ban in TOKEN units of the room's asset "
                      "(e.g. 1500). Set, it decides the amount and no price "
                      "quote is needed; the USD price stays the declared value"),
    _spec("chat.paid_unmute_units", "str", SENSITIVITY_SAFE, "override",
          "live", default_display="",
          description="Price of ending a mute early in TOKEN units of the room's asset "
                      "(e.g. 1500). Set, it decides the amount and no price "
                      "quote is needed; the USD price stays the declared value"),
    _spec("chat.paid_unban_units", "str", SENSITIVITY_SAFE, "override",
          "live", default_display="",
          description="Price of ending a ban early in TOKEN units of the room's asset "
                      "(e.g. 1500). Set, it decides the amount and no price "
                      "quote is needed; the USD price stays the declared value"),
    _spec("chat.paid_max_per_payer_day", "int", SENSITIVITY_SAFE, "override",
          "live", min_value=0, max_value=100, default_display=3,
          description="Paid actions one payer may buy in this room per day"),
    _spec("chat.paid_max_per_target_day", "int", SENSITIVITY_SAFE, "override",
          "live", min_value=0, max_value=100, default_display=2,
          description="Times one member may be targeted in this room per day"),
    _spec("chat.paid_offer_ttl", "str", SENSITIVITY_SAFE, "override", "live",
          default_display="30m",
          description="How long an unpaid offer stays payable, e.g. 30m/2h"),
))


def _spec_for(key: str) -> PrefSpec | None:
    """The spec for a tenant preference OR a ``chat.*`` overlay key."""
    return PREF_SCHEMA.get(key) or CHAT_PREF_SCHEMA.get(key)


def _redacted_repr(value: object) -> str:
    """``repr(value)`` for a validation-error message, but never echo a long
    string verbatim (owner-UX P1 final review, item 6): ``find_invalid_preferences``/
    ``/config check`` surface these error strings, and a hand-pasted secret
    (mistakenly written to a str-typed pref key) would otherwise leak in full
    via the ``got {value!r}`` mismatch errors. A str value longer than 12 chars
    is truncated to a 4-char prefix; short values (and every non-str type)
    render exactly as before — the full repr is genuinely useful feedback for
    e.g. a mistyped boolean (`got 'yse'`)."""
    if isinstance(value, str) and len(value) > 12:
        return repr(value[:4] + "…")
    return repr(value)


def _coerce(spec: PrefSpec, value: object) -> tuple[bool, object, str]:
    """Coerce ``value`` to the spec's type. Returns (ok, coerced, error)."""
    if spec.type == "bool":
        if isinstance(value, bool):
            return True, value, ""
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True, True, ""
        if s in ("0", "false", "no", "off"):
            return True, False, ""
        return False, None, f"{spec.key}: expected a boolean, got {_redacted_repr(value)}"
    if spec.type in ("int", "float"):
        try:
            num = int(value) if spec.type == "int" else float(value)
        except (TypeError, ValueError):
            return False, None, f"{spec.key}: expected a {spec.type}, got {_redacted_repr(value)}"
        if spec.min_value is not None and num < spec.min_value:
            return False, None, f"{spec.key}: must be at least {spec.min_value}"
        if spec.max_value is not None and num > spec.max_value:
            return False, None, f"{spec.key}: must be at most {spec.max_value}"
        return True, num, ""
    if spec.type == "enum":
        s = str(value).strip().lower()
        if s in spec.enum_values:
            return True, s, ""
        return False, None, f"{spec.key}: must be one of {', '.join(spec.enum_values)}"
    if spec.type == "list":
        if isinstance(value, (list, tuple)):
            items = [str(v).strip() for v in value if str(v).strip()]
        else:
            items = [p.strip() for p in str(value).split(",") if p.strip()]
        if spec.key == "chat.wake_words":
            # Every pattern is re.search-ed against every line of a busy room,
            # so the list is bounded HERE — at the writer AND at load-time
            # re-validation, which is what stops a hand-edited file from
            # carrying 500 patterns past the writer's back.
            if len(items) > 10:
                return False, None, "chat.wake_words: at most 10 patterns"
            if any(len(w) > 64 for w in items):
                return False, None, "chat.wake_words: each pattern is capped at 64 chars"
        return True, items, ""
    # str
    s = str(value).strip()
    if spec.key in ("style.tone", "chat.tone") and len(s) > 200:
        return False, None, f"{spec.key}: capped at 200 chars"
    if spec.key == "chat.instructions" and len(s) > 1500:
        return False, None, "chat.instructions: capped at 1500 chars"
    if spec.key == "chat.name":
        # A room NAME is rendered inline (the <group-context> head attribute and
        # the <surface> paragraph), so a control character would forge a second
        # line. Coerced to one line rather than refused — the owner typed a
        # name, not a policy value.
        s = _CTRL_CHARS.sub(" ", s).strip()[:64]
    if spec.key in ("digest.quiet_hours", "chat.quiet_hours"):
        m = _QUIET_HOURS_RE.fullmatch(s)
        if not m or not (0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 23):
            return False, None, f"{spec.key}: expected HH-HH (0-23), e.g. 23-08"
    if spec.key in ("style.language", "chat.language") and not _LANGUAGE_RE.fullmatch(s):
        return False, None, (f"{spec.key}: expected a language name/tag "
                             "(letters/hyphens, max 32 chars), e.g. 'en' or 'en-GB'")
    return True, s, ""


def validate_pref(key: str, value: object) -> tuple[bool, object, str]:
    """Validate + coerce a preference (tenant or ``chat.*``).

    Unknown keys get a closest-match hint drawn from BOTH namespaces, so a
    mistyped ``chat.mod`` is told about ``chat.mode`` rather than being sent
    looking for a tenant preference that does not exist.
    """
    spec = _spec_for(key)
    if spec is None:
        pool = list(PREF_SCHEMA.keys()) + list(CHAT_PREF_SCHEMA.keys())
        hint = difflib.get_close_matches(key, pool, n=1)
        suffix = f" (did you mean {hint[0]}?)" if hint else ""
        return False, None, f"unknown preference key: {key}{suffix}"
    return _coerce(spec, value)


# --- storage (identity tier, per-tenant) -------------------------------------
import os
import tomllib
from pathlib import Path
from typing import Optional

from core.instance import (DEFAULT_INSTANCE_ID, is_safe_tenant_id, resolve_instance_id,
                           self_tier_root)

_PREFS_NAME = "preferences.toml"
# path -> (mtime_ns, parsed dict); tiny process-local cache
_CACHE: dict[str, tuple[int, dict]] = {}

# Free-text prompt-level str keys threat-scanned at write time (owner-UX P1
# final review, item 2): these values are injected verbatim into the running
# agent's prompt/identity next session (style.tone -> reply-tone hint,
# session.persona -> the session's <identity> source), so — unlike the other
# schema keys, which are bool/int/enum/list-shaped and can't carry free-form
# injected text — they are a genuine prompt-injection **persistence** vector.
# Scanned with the SAME stricter identity scanner as the evolving SELF/owner
# docs (`modules.memory.task.threat_scan.is_identity_suspicious`), fail-CLOSED
# on a scan hit, a scan error, or the scanner being unavailable at all. Kept
# OFF the hot path for every other (non-prompt, non-str, or str-but-not-listed)
# key — this is not a blanket str-value scan.
_THREAT_SCANNED_PREF_KEYS = frozenset({"style.tone", "session.persona",
                                       # 044 T17: both land verbatim in a room
                                       # session's prompt, exactly like their
                                       # style.* twins.
                                       "chat.tone", "chat.instructions"})


def preferences_path(home_dir: Path | str, user_id: Optional[str],
                     instance_id: Optional[str] = None) -> Optional[Path]:
    """Path to the tenant's ``preferences.toml``.

    ``instance_id=None`` (the default) resolves via
    :func:`core.instance.resolve_instance_id` — the SAME resolution every
    other instance-scoped seam (agent construction, telegram, init) uses — so
    a non-default ``POLYROB_INSTANCE_ID`` is honored even when a call site
    never threads an explicit instance id through (owner-UX P2-4 final review,
    item 1). Pass an explicit ``instance_id`` to pin a specific instance.
    """
    instance_id = instance_id or resolve_instance_id()
    uid = (user_id or "").strip()
    if not uid or not is_safe_tenant_id(uid):
        return None
    return self_tier_root(home_dir, uid, instance_id) / _PREFS_NAME


def chat_preferences_path(home_dir: Path | str, user_id: Optional[str],
                          surface: str, chat_id: object,
                          instance_id: Optional[str] = None) -> Optional[Path]:
    """Path to ONE room's ``chat.*`` overlay, under the OWNER tenant.

    ``identity/{instance}/user_{uid}/chats/{surface}_{chat_id}.toml``. The chat
    id arrives from the network and NAMES A FILE, so every character outside
    ``[A-Za-z0-9_-]`` is replaced — a traversal attempt becomes an ordinary
    (ugly) name rather than a path. Returns None for an empty or unsafe tenant
    id, exactly like :func:`preferences_path`.
    """
    instance_id = instance_id or resolve_instance_id()
    uid = (user_id or "").strip()
    if not uid or not is_safe_tenant_id(uid):
        return None
    safe_surface = re.sub(r"[^A-Za-z0-9_-]", "_", str(surface or "")) or "unknown"
    safe_chat = re.sub(r"[^A-Za-z0-9_-]", "_", str(chat_id))
    return self_tier_root(home_dir, uid, instance_id) / "chats" / \
        f"{safe_surface}_{safe_chat}.toml"


def _flatten(nested: dict, prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    for k, v in nested.items():
        dotted = f"{prefix}{k}"
        if isinstance(v, dict):
            flat.update(_flatten(v, f"{dotted}."))
        else:
            flat[dotted] = v
    return flat


def load_preferences(home_dir: Path | str, user_id: Optional[str],
                     instance_id: Optional[str] = None) -> dict[str, object]:
    """Flat {dotted_key: coerced_value}. Missing/broken file fails OPEN to {}.

    ``instance_id=None`` resolves via :func:`core.instance.resolve_instance_id`
    (owner-UX P2-4 final review, item 1)."""
    instance_id = instance_id or resolve_instance_id()
    path = preferences_path(home_dir, user_id, instance_id)
    if path is None or not path.is_file():
        return {}
    key = str(path)
    try:
        mtime = path.stat().st_mtime_ns
        cached = _CACHE.get(key)
        if cached and cached[0] == mtime:
            return dict(cached[1])
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # malformed TOML / unreadable — never break the agent
        logger.warning("preferences.toml unreadable (%s) — ignoring prefs: %s", path, e)
        return {}
    prefs: dict[str, object] = {}
    for dotted, value in _flatten(raw).items():
        # 044 T17: a `chat.*` key is per-ROOM configuration. It validates through
        # the same machinery, but it belongs in a per-chat file that
        # core/surfaces/chat_policy.py reads — accepting one here would hand the
        # resolver a value no enforcement site ever looks at.
        if dotted in CHAT_PREF_SCHEMA and dotted not in PREF_SCHEMA:
            logger.warning("preferences.toml: dropping %s (per-room configuration, "
                           "not a tenant preference)", dotted)
            continue
        ok, coerced, err = validate_pref(dotted, value)
        if ok:
            prefs[dotted] = coerced
        else:
            logger.warning("preferences.toml: dropping %s (%s)", dotted, err)
    _CACHE[key] = (mtime, dict(prefs))
    return prefs


def _to_toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_to_toml_value(i) for i in v) + "]"
    # Escape for TOML basic string: backslash first (already done), then quotes,
    # then control chars per TOML spec (newline, tab, CR, and other < U+0020).
    s = str(v)
    s = s.replace("\\", "\\\\")  # backslash first
    s = s.replace('"', '\\"')    # double-quote
    s = s.replace("\n", "\\n")   # newline
    s = s.replace("\r", "\\r")   # carriage return
    s = s.replace("\t", "\\t")   # tab
    # Escape any other control character (< U+0020 or U+007F DEL) as \uXXXX
    result = []
    for c in s:
        if (ord(c) < 0x20 and c not in "\n\r\t") or ord(c) == 0x7F:
            result.append(f"\\u{ord(c):04x}")
        else:
            result.append(c)
    return '"' + "".join(result) + '"'


def _render_toml(flat: dict[str, object]) -> str:
    groups: dict[str, dict[str, object]] = {}
    for dotted, v in sorted(flat.items()):
        group, _, leaf = dotted.partition(".")
        groups.setdefault(group, {})[leaf] = v
    out: list[str] = ["# POLYROB per-user preferences — managed via /config, "
                      "`polyrob config`, chat, or hand-edit.", ""]
    for group, leaves in groups.items():
        out.append(f"[{group}]")
        out.extend(f"{leaf} = {_to_toml_value(v)}" for leaf, v in leaves.items())
        out.append("")
    return "\n".join(out)


def _refuse_room_key(key: str) -> str:
    """One sentence when a ``chat.*`` key is aimed at the TENANT store.

    ``preferences.toml`` is not where a room's behaviour lives, and writing it
    there would succeed, display, and change nothing. Returns "" for any key
    that genuinely belongs in the tenant store.
    """
    if key in CHAT_PREF_SCHEMA and key not in PREF_SCHEMA:
        return (f"{key} is per-room configuration, not a tenant preference — "
                f"set it on the room instead (`/groups set <room> {key} <value>`)")
    return ""


def unknown_pref_error(key: str, value: object = None) -> str:
    """The ONE sentence a TENANT-pref surface gives for a key it cannot act on.

    Never empty. ``validate_pref`` now answers for the ``chat.*`` namespace too,
    so a caller that did ``PREF_SCHEMA.get(key) is None`` and then reported
    ``validate_pref``'s error string got ``""`` for a valid room key — a silent
    failure the owner reads as "it worked" (044 T16/17 fix round 1). This names
    the right seat for a room key and falls back to the closest-match hint.
    """
    refusal = _refuse_room_key(key)
    if refusal:
        return refusal
    _ok, _coerced, err = validate_pref(key, value)
    return err or f"unknown preference key: {key}"


def _threat_scan_pref_value(key: str, value: object) -> tuple[bool, str]:
    """Identity-scan a threat-scanned prompt pref value; fail-CLOSED.

    Same posture as ``core.owner_doc_writer``/``core.self_context_writer``:
    a scanner import failure, a scan-time exception, and an actual hit are all
    treated as "reject the write" — never silently let injected content
    through because the scanner happened to be unavailable.
    """
    try:
        from modules.memory.task.threat_scan import is_identity_suspicious
    except Exception as e:
        logger.warning(
            "preference write rejected (scanner unavailable, fail-closed): %s: %s", key, e
        )
        return False, f"{key}: identity scanner unavailable (rejected)"
    try:
        flagged = is_identity_suspicious(str(value))
    except Exception as e:
        logger.warning("preference write rejected (scan error, fail-closed): %s: %s", key, e)
        return False, f"{key}: identity scan error (rejected)"
    if flagged:
        logger.warning("preference write rejected (identity scan): %s", key)
        return False, f"{key}: content failed identity safety scan"
    return True, ""


def write_preference(home_dir: Path | str, user_id: Optional[str], key: str,
                     value: object,
                     instance_id: Optional[str] = None) -> tuple[bool, str]:
    """Validate + upsert one preference; atomic temp+replace; busts the cache.

    ``instance_id=None`` resolves via :func:`core.instance.resolve_instance_id`
    (owner-UX P2-4 final review, item 1)."""
    instance_id = instance_id or resolve_instance_id()
    path = preferences_path(home_dir, user_id, instance_id)
    if path is None:
        return False, "empty or unsafe user_id refused (tenant scope)"
    refusal = _refuse_room_key(key)
    if refusal:
        return False, refusal
    ok, coerced, err = validate_pref(key, value)
    if not ok:
        return False, err
    if key in _THREAT_SCANNED_PREF_KEYS:
        ok, err = _threat_scan_pref_value(key, coerced)
        if not ok:
            return False, err
    flat = load_preferences(home_dir, user_id, instance_id)
    flat[key] = coerced
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(_render_toml(flat), encoding="utf-8")
        os.replace(str(tmp), str(path))
        _CACHE.pop(str(path), None)
        return True, ""
    except Exception as e:
        logger.error("preferences write failed (%s): %s", path, e, exc_info=True)
        return False, f"write failed: {e}"


# --- style-line rendering (owner-UX Phase 2, contract/style injection) ------

# Fixed rendering order: (pref key, display label). Only keys actually present
# in the resolved prefs dict contribute a segment — a deterministic, stable
# one-line summary so the same prefs always render byte-identically.
_STYLE_LINE_FIELDS: tuple[tuple[str, str], ...] = (
    ("style.verbosity", "verbosity"),
    ("style.language", "language"),
    ("style.tone", "tone"),
    ("digest.quiet_hours", "quiet hours"),
)

#: What each ``style.verbosity`` value actually MEANS to the agent.
#:
#: "verbosity terse" is a bare adjective with no operational content — no line
#: budget, no rule about what to do with the detail that does not fit. A model
#: reading it has nothing to comply with (chat-first review 2026-08-22, G7).
#: These phrases give each value a budget and say where the overflow goes.
_VERBOSITY_GUIDANCE: dict[str, str] = {
    "terse": "reply in 5 lines or fewer; attach or link anything longer",
    "normal": "reply in 15 lines or fewer; attach or link anything longer",
    "detailed": "full detail inline is welcome",
}


def render_style_line(prefs: dict[str, object]) -> str:
    """Deterministic one-line style summary from typed prefs.

    Considers only ``style.verbosity``/``style.language``/``style.tone``/
    ``digest.quiet_hours`` (in that fixed order); a key not present in *prefs*
    is simply omitted from the line. Returns ``""`` when none of the four are
    set — the byte-identical default (no line injected) before any style pref
    is written.

    Defense-in-depth (P2 T1 review fix): the rendered line lands VERBATIM in
    the SELF_CONTEXT foundation message, so every value is re-validated via
    :func:`validate_pref` and hard-capped at 200 chars here — even though
    ``load_preferences`` already validates. This keeps the seam safe against a
    hand-constructed prefs dict AND against a future ``_STYLE_LINE_FIELDS``
    addition whose schema forgot a format rule; a failing value is silently
    skipped (the line must never be a place errors surface).

    Load-side scan backstop (owner-UX P2-4 final review, item 2): a field in
    ``_THREAT_SCANNED_PREF_KEYS`` (today only ``style.tone`` — ``session.persona``
    is not a ``_STYLE_LINE_FIELDS`` member) is re-run through the SAME
    fail-closed identity scanner used at write time
    (:func:`_threat_scan_pref_value`) before being rendered. Unlike
    :func:`display_effective` (which substitutes a ``"[BLOCKED: ...]"``
    placeholder for the agent-facing get/list surface), a scan failure here
    just OMITS the field — the style line is a compact rendering convenience,
    not an error-reporting surface, and there is no natural place to show a
    blocked-marker inline in a "verbosity terse · tone ..." summary. A
    hand-edited ``preferences.toml`` (or a pre-scan value already on disk)
    therefore can never smuggle injected prose into SELF_CONTEXT via the style
    line, mirroring the ``session.persona`` load-side scan backstop.

    Example: ``Style: verbosity terse · language en · tone friendly · quiet hours 23-08``.
    """
    parts: list[str] = []
    for key, label in _STYLE_LINE_FIELDS:
        if key not in prefs:
            continue
        value = prefs[key]
        if len(str(value)) > 200:
            continue
        ok, coerced, _err = validate_pref(key, value)
        if not ok:
            continue
        if key in _THREAT_SCANNED_PREF_KEYS:
            scan_ok, _scan_err = _threat_scan_pref_value(key, coerced)
            if not scan_ok:
                continue
        if key == "style.verbosity":
            guidance = _VERBOSITY_GUIDANCE.get(str(coerced))
            if guidance:
                parts.append(f"{label} {coerced} ({guidance})")
                continue
        parts.append(f"{label} {coerced}")
    if not parts:
        return ""
    return "Style: " + " · ".join(parts)


# --- resolver (pref > env > default, guarded keys merge most-restrictive) -----

def prefs_enabled() -> bool:
    from core.env import bool_env
    return bool_env("PREFS_ENABLED", True)


def resolve_with_source(key: str, user_id: Optional[str], home_dir: Path | str,
                        *, env_value: object = None, default: object = None,
                        instance_id: Optional[str] = None) -> tuple[object, str]:
    """Resolve one preference. ``env_value`` is the already-parsed operator value
    from the call site's existing accessor (None == operator did not set it).

    ``instance_id=None`` resolves via :func:`core.instance.resolve_instance_id`
    (owner-UX P2-4 final review, item 1) — this is what makes the many
    enforcement call sites (approval provider, denylist, wallet caps, delivery
    caps, goal/budget caps, digest) honor a non-default
    ``POLYROB_INSTANCE_ID`` even though none of them thread an explicit
    instance id through."""
    instance_id = instance_id or resolve_instance_id()
    spec = PREF_SCHEMA.get(key)
    base, base_src = (env_value, "env") if env_value is not None else (default, "default")
    if spec is None or not prefs_enabled():
        return base, base_src
    pref = load_preferences(home_dir, user_id, instance_id).get(key)
    if pref is None:
        return base, base_src
    if spec.merge == "override":
        return pref, "pref"
    if spec.merge == "min":
        if env_value is None:
            return pref, "pref"
        return min(pref, env_value), "merged(min)"
    if spec.merge == "union":
        merged = sorted(set(env_value or []) | set(pref or []))
        return merged, "merged(union)"
    if spec.merge == "narrow_list":
        # Allowlist-polarity list merge (T5 review fix): union is correct for a
        # deny/require list (approvals.require/deny — either side adding an
        # entry TIGHTENS), but inverts the polarity for an ALLOWLIST-shaped
        # list, where a pref adding an entry the operator never listed would
        # WIDEN the reachable set. Here the pref can only NARROW the operator
        # set (intersection); with no operator set at all (env empty/None),
        # there is no ceiling to widen past, so the guarded+approvable pref
        # channel is how the owner defines the set from scratch.
        if not env_value:
            return list(pref or []), "pref"
        merged = sorted(set(env_value) & set(pref or []))
        return merged, "merged(narrow)"
    if spec.merge == "and":
        env_on = bool(env_value) if env_value is not None else bool(default)
        return env_on and bool(pref), "merged(and)"
    if spec.merge == "stricter_provider":
        env_p = str(env_value or default or "auto")
        # Custom (non-standard) providers are never overridden by a pref
        if env_p not in _PROVIDER_ORDER:
            return base, base_src
        # Both env and pref are in known order; use stricter
        idx = max(_PROVIDER_ORDER.index(env_p),
                  _PROVIDER_ORDER.index(pref) if pref in _PROVIDER_ORDER else 0)
        return _PROVIDER_ORDER[idx], "merged(stricter)"
    if spec.merge == "stricter_policy":
        try:
            from core.surfaces.outbound_policy import POLICY_LADDER
        except Exception:
            return base, base_src
        env_p = str(env_value or default or "allowlist")
        # An unknown env/default policy is never overridden by a pref.
        if env_p not in POLICY_LADDER:
            return base, base_src
        idx = max(POLICY_LADDER.index(env_p),
                  POLICY_LADDER.index(pref) if pref in POLICY_LADDER else 0)
        return POLICY_LADDER[idx], "merged(stricter)"
    return base, base_src  # unreachable: schema test pins merge values


def resolve(key: str, user_id: Optional[str], home_dir: Path | str, *,
            env_value: object = None, default: object = None,
            instance_id: Optional[str] = None) -> object:
    """``instance_id=None`` resolves via
    :func:`core.instance.resolve_instance_id` (owner-UX P2-4 final review,
    item 1); see :func:`resolve_with_source`."""
    return resolve_with_source(key, user_id, home_dir, env_value=env_value,
                               default=default, instance_id=instance_id)[0]


# Placeholder shown instead of a pref-sourced free-text value that fails (or
# cannot complete) the read-time identity re-scan below — mirrors the
# "[BLOCKED: ...]" substitution self_context_manage's read path uses.
_BLOCKED_PREF_DISPLAY = "[BLOCKED: failed identity safety scan]"


def _builtin_default(spec: PrefSpec) -> Optional[tuple]:
    """Display-only honest default for the no-env/no-pref case (018 P0.1).

    Historically ``display_effective`` fell back to ``(None, "default")`` for
    every unconfigured key, rendering the /config panel as a wall of ``None``
    even though each enforcement site has a real operational default. This
    helper surfaces that default: env-backed keys resolve through the flags
    catalog (posture/local-aware via ``dynamic_flag_default`` — same machinery
    as ``doctor --flags``), pure prefs carry :attr:`PrefSpec.default_display`.
    Returns ``None`` when no operational default exists (legacy display).

    Never feeds the resolver — merge semantics are byte-identical; a failure
    here degrades to the legacy ``None`` display (fail-open).
    """
    if spec.env_flag:
        try:
            from core.config_policy.flag_defaults import dynamic_flag_default
            from core.flags import REGISTRY, resolve_flag
            if spec.env_flag in REGISTRY:
                resolved = resolve_flag(spec.env_flag, {}, dynamic_flag_default)
                if resolved.value is not None:
                    source = (resolved.source
                              if str(resolved.source).startswith("default(")
                              else "built-in")
                    return resolved.value, source
        except Exception:
            logger.debug("built-in default resolution failed for %s", spec.key,
                         exc_info=True)
    if spec.default_display is not None:
        return spec.default_display, "built-in"
    return None


def display_effective(key: str, user_id: Optional[str], home_dir: Path | str,
                      instance_id: Optional[str] = None) -> tuple[object, str]:
    """Effective (value, source) for *key* — the actual value in force, not a
    partial view (owner-UX P1 final review, item 4; lifted from
    ``cli/ui/commands/h_config.py::_display_value_source`` in owner-UX P2 T2 so
    both the ``/config`` REPL handler and the agent-callable ``preferences``
    action render identically, without the agent-facing action reaching into
    ``cli/``).

    Two display bugs this fixes (kept from the original CLI-side helper):
      (a) an env var set alongside a pref used to short-circuit and show the
          RAW env string with source ``"env"``, hiding that the two actually
          merge (e.g. an env cap of 10 with a tighter pref of 5 must display
          the merged ``5``, not the raw ``10``).
      (b) with the env var unset, always resolving with a bare
          ``env_value=None, default=None`` makes the "and"-merge keys
          (``autonomy.self_wake``/``autonomy.background_review``) treat "no
          env" as an unconditional env-OFF, so a fresh ``set`` looked
          unchanged. Fix: when a pref exists (env unset), show it directly
          with source ``"pref"`` — correct for every merge kind.

    Read-time identity re-scan (P2 T2 review fix): a PREF-sourced value of a
    threat-scanned free-text key (``_THREAT_SCANNED_PREF_KEYS`` —
    ``style.tone``/``session.persona``) is re-run through the SAME fail-closed
    scanner used at write time (:func:`_threat_scan_pref_value`) before being
    displayed. ``write_preference`` scans at write, but a hand-edited
    ``preferences.toml`` (or a pre-scan / scanner-false-negative value on
    disk) reaches display unscanned — and the agent-facing ``get``/``list``
    output re-enters the model's context (``include_in_memory=True``), so an
    unscanned echo is an injection-laundering channel. On a scan hit, a scan
    error, or the scanner being unavailable, the value is replaced with
    ``"[BLOCKED: failed identity safety scan]"`` (parity with
    ``self_context_manage``'s read guard). Non-pref sources (operator env /
    default) and every other key are never scanned — no cost on the hot path.

    Returns ``(None, "unknown")`` for a key not in :data:`PREF_SCHEMA`.

    ``instance_id=None`` resolves via :func:`core.instance.resolve_instance_id`
    (owner-UX P2-4 final review, item 1).
    """
    instance_id = instance_id or resolve_instance_id()
    spec = PREF_SCHEMA.get(key)
    if spec is None:
        return None, "unknown"
    raw_env = os.environ.get(spec.env_flag) if spec.env_flag else None
    if raw_env is not None:
        ok, parsed, _err = validate_pref(key, raw_env)
        env_value = parsed if ok else raw_env
        value, source = resolve_with_source(key, user_id, home_dir, env_value=env_value,
                                            default=None, instance_id=instance_id)
    else:
        pref = load_preferences(home_dir, user_id, instance_id).get(key)
        if pref is not None:
            value, source = pref, "pref"
        else:
            builtin = _builtin_default(spec)
            if builtin is not None:
                value, source = builtin
            else:
                value, source = resolve_with_source(key, user_id, home_dir,
                                                    env_value=None, default=None,
                                                    instance_id=instance_id)
    if source == "pref" and key in _THREAT_SCANNED_PREF_KEYS:
        ok, _err = _threat_scan_pref_value(key, value)
        if not ok:
            return _BLOCKED_PREF_DISPLAY, source
    return value, source


# --- catalog cross-checking (env-flag / preferences.toml sanity, owner-UX P1 T7) --
#
# Reusable by both the `/config` REPL handler (cli/ui/commands/h_config.py) and
# the planned `polyrob config check` CLI command (P1 T8) — this module stays
# core-layer (no cli/ imports), so callers format the returned strings however
# their surface needs.

import re
from core.flags_catalog import CATALOG

_PLACEHOLDER_RE = re.compile(r"<[^>]+>")
_BOOL_SHAPE_RE = re.compile(r"\b(ON|OFF)\b")
_NUMERIC_SHAPE_RE = re.compile(r"`(-?\d+(?:\.\d+)?)`")


def _pattern_to_regex(name: str) -> "re.Pattern[str]":
    """Turn a dynamic catalog name (e.g. ``POLYROB_<PROVIDER>_MODEL``) into a
    regex that matches any concrete flag name following that shape."""
    parts = _PLACEHOLDER_RE.split(name)
    body = "[A-Z0-9_]+".join(re.escape(p) for p in parts)
    return re.compile(rf"^{body}$")


def catalog_lookup(key: str) -> Optional[tuple[str, str]]:
    """Return ``(group, documented_default)`` for *key* against ``CATALOG``.

    Exact (non-pattern) names are tried first, then the documented ``<...>``
    dynamic-pattern entries (matched by regex, not equality). ``None`` when
    *key* matches nothing in the catalog.
    """
    for name, group, default, *_ in CATALOG:
        if "<" not in name and name == key:
            return group, default
    for name, group, default, *_ in CATALOG:
        if "<" in name and _pattern_to_regex(name).match(key):
            return group, default
    return None


def catalog_names() -> list[str]:
    """Static (non-pattern) CATALOG flag names — for closest-match suggestions."""
    return [name for name, _group, _default, *_ in CATALOG if "<" not in name]


def shape_of_default(documented_default: str) -> str:
    """Classify a CATALOG documented-default string's expected value shape.

    ``'bool'`` when the doc says ON/OFF, ``'numeric'`` when it shows a
    backtick-quoted number (e.g. ``` `30` ```), else ``'free'`` (no shape to
    check — free-form strings, paths, "unset", etc.). Bool is checked first
    since a doc string like ``ON (`"1"`)`` embeds both.
    """
    if _BOOL_SHAPE_RE.search(documented_default):
        return "bool"
    if _NUMERIC_SHAPE_RE.search(documented_default):
        return "numeric"
    return "free"


_BOOLISH_VALUES = ("1", "0", "true", "false", "yes", "no", "on", "off")


def value_matches_shape(value: object, shape: str) -> bool:
    """Does *value* look like the shape ``shape`` (see :func:`shape_of_default`)?

    ``'free'`` always matches (nothing to check).
    """
    v = str(value).strip()
    if shape == "bool":
        return v.lower() in _BOOLISH_VALUES
    if shape == "numeric":
        try:
            float(v)
            return True
        except (TypeError, ValueError):
            return False
    return True


def _iter_env_lines(paths: list[Path]) -> dict[str, str]:
    """Merge ``KEY=VALUE`` lines across *paths* (later paths win on conflict,
    mirroring project-overrides-global elsewhere). Missing/unreadable files are
    silently skipped — never raises."""
    merged: dict[str, str] = {}
    for raw_path in paths:
        try:
            path = Path(raw_path)
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            if key:
                merged[key] = value.strip()
    return merged


def check_env_files(paths: list[Path]) -> list[str]:
    """Validate env-flag files against ``core.flags_catalog.CATALOG``.

    For every ``KEY=VALUE`` line found across *paths* (existing files only;
    later paths override earlier ones for the same key):
      - a name with no catalog match but a close catalog-name match is
        reported as a likely typo (with the suggestion);
      - a name with NO catalog match and no close match is reported at
        info-level ("unknown (not in catalog)") — env files legitimately hold
        provider keys/secrets that aren't behavior flags, so this is not an
        error;
      - a catalog-matched name whose value doesn't look like the documented
        default's shape (boolish for ON/OFF, numeric for a backtick number)
        is reported as a shape mismatch.

    Never includes VALUES in the returned findings (key names only) — env
    files may hold secrets. Never raises.
    """
    merged = _iter_env_lines(paths)
    if not merged:
        return []
    names = catalog_names()
    findings: list[str] = []
    for key in sorted(merged):
        value = merged[key]
        hit = catalog_lookup(key)
        if hit is None:
            hint = difflib.get_close_matches(key, names, n=1)
            if hint:
                findings.append(f"{key}: possible typo (did you mean {hint[0]}?)")
            else:
                findings.append(
                    f"{key}: unknown (not in catalog) — may be a provider key/secret"
                )
            continue
        _group, documented_default = hit
        # 026 P1.4: enum-shaped flags surface an invalid member with the valid
        # set (the resolvers silently degrade typos, so the file check is the
        # place the owner actually hears about it).
        try:
            from core.config_policy.flag_enums import enum_error
            e = enum_error(key, value)
        except Exception:
            e = None
        if e:
            findings.append(e)
            continue
        shape = shape_of_default(documented_default)
        if not value_matches_shape(value, shape):
            findings.append(
                f"{key}: value doesn't match the documented shape "
                f"({shape}; documented default: {documented_default})"
            )
    return findings


def find_invalid_preferences(home_dir: Path | str, user_id: Optional[str],
                             instance_id: Optional[str] = None
                             ) -> list[tuple[str, str]]:
    """Return ``(key, error)`` pairs for preferences.toml entries that
    :func:`load_preferences` silently drops (unknown keys, invalid values).

    Unlike :func:`load_preferences` (fail-open — bad entries are logged and
    skipped so the agent never breaks), this surfaces them for ``/config
    check`` / ``polyrob config check`` to report to the operator. Missing or
    unreadable files return ``[]`` (nothing to report); never raises.

    ``instance_id=None`` resolves via :func:`core.instance.resolve_instance_id`
    (owner-UX P2-4 final review, item 1).
    """
    instance_id = instance_id or resolve_instance_id()
    path = preferences_path(home_dir, user_id, instance_id)
    if path is None or not path.is_file():
        return []
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [("<file>", f"preferences.toml unreadable: {e}")]
    invalid: list[tuple[str, str]] = []
    for dotted, value in _flatten(raw).items():
        ok, _coerced, err = validate_pref(dotted, value)
        if not ok:
            invalid.append((dotted, err))
    return invalid


# --- guarded pref-change proposals (ride self-evolution pending/approve, owner-UX P2 T3) --
#
# A GUARDED-sensitivity key can't be written immediately by an agent-callable `set`
# (Task 2) — it quarantines a `{user_id, key, value}` proposal that rides the SAME
# owner pending/approve pipeline as skills/self-context/owner-facts/the operating
# contract (`core/self_evolution.py`'s ``KIND_PREF_CHANGE``). Kept here (not a new
# core module) so the producer sits next to ``validate_pref``/``write_preference``.
#
# At most one pending proposal per (tenant, key) — a re-propose overwrites the prior
# draft, mirroring the self-context/owner-doc "one draft" idiom used elsewhere in the
# self-evolution pipeline.

import json

_PREF_PROPOSAL_KEY_RE = re.compile(r"[^A-Za-z0-9_.]")


def _pref_proposal_dir(home_dir: Path | str, user_id: Optional[str],
                       instance_id: str = DEFAULT_INSTANCE_ID) -> Optional[Path]:
    uid = (user_id or "").strip()
    if not uid or not is_safe_tenant_id(uid):
        return None
    return self_tier_root(home_dir, uid, instance_id) / ".pending" / "prefs"


def _pref_proposal_path(home_dir: Path | str, user_id: Optional[str], key: str,
                        instance_id: str = DEFAULT_INSTANCE_ID) -> Optional[Path]:
    d = _pref_proposal_dir(home_dir, user_id, instance_id)
    if d is None:
        return None
    # Keys are schema-fixed dotted names, but never trust a caller-supplied
    # string as a raw path component.
    slug = _PREF_PROPOSAL_KEY_RE.sub("_", str(key))
    return d / f"{slug}.json"


def _archive_pref_proposal(home_dir: Path | str, user_id: Optional[str], path: Path,
                          instance_id: str = DEFAULT_INSTANCE_ID) -> None:
    """Best-effort archive of a rejected proposal (archive-never-delete, mirrors the
    other self-evolution writers). Never raises — a failed archive must not block
    the reject itself."""
    uid = (user_id or "").strip()
    if not uid or not is_safe_tenant_id(uid):
        return
    try:
        archive_dir = self_tier_root(home_dir, uid, instance_id) / ".archived"
        archive_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        while (archive_dir / f"pref-rejected.{n}.json").exists():
            n += 1
        import shutil
        shutil.copy2(str(path), str(archive_dir / f"pref-rejected.{n}.json"))
    except Exception:
        pass


def _min_merge_noop(key: str, coerced: object) -> Optional[str]:
    """Why a proposed value for a min-merged key could never take effect, or
    None when it can.

    A min-merged key (the daily wallet cap, the goal quotas) resolves to
    ``min(pref, env)``, so a pref ABOVE the operator's env value changes
    nothing. Until 2026-09-18 such a proposal was queued anyway: the owner saw
    "tap to approve", tapped, and the guard kept reading the env value — a tap
    that lies is worse than a refusal (prod, 2026-09-17: two decorative
    ceiling proposals in one afternoon). Refuse up front and name the one line
    that would actually move it. Fail-open: an unparseable env value queues
    the proposal as before.
    """
    spec = PREF_SCHEMA.get(key)
    if spec is None or spec.merge != "min" or not spec.env_flag:
        return None
    raw_env = os.environ.get(spec.env_flag)
    if raw_env is None:
        return None
    ok, env_value, _err = validate_pref(key, raw_env)
    if not ok:
        return None
    try:
        if float(coerced) <= float(env_value):
            return None
    except (TypeError, ValueError):
        return None
    return (f"{key}={coerced} would change nothing: the effective value is "
            f"min(pref, env) and the operator env {spec.env_flag}={env_value} "
            f"is lower. Raising it is an operator change — set "
            f"{spec.env_flag}={coerced} in the service env file (polyrob.env) "
            f"and restart; a preference can only tighten this one.")


def propose_pref_change(user_id: Optional[str], key: str, value: object,
                        home_dir: Path | str,
                        instance_id: Optional[str] = None,
                        *, op: str = "set", entry: object = None) -> tuple[bool, str]:
    """Quarantine a guarded pref change for owner review.

    ``instance_id=None`` resolves via :func:`core.instance.resolve_instance_id`
    (owner-UX P2-4 final review, item 1).

    Two operation shapes (owner-UX P2 T4 review fix):
      - ``op="set"`` (default, legacy): store ``value`` verbatim; promote writes
        it via :func:`write_preference`. Validates via :func:`validate_pref`
        FIRST — a malformed value or unknown key is refused before anything is
        ever written to disk.
      - ``op="remove_entry"``: store WHICH ``entry`` to drop from a list-typed
        (union-merge) key; promote recomputes against the CURRENT list at apply
        time. This is what makes removals safe against the stale-snapshot
        clobber: a full-list snapshot queued at propose time would silently
        erase any entry the owner ADDED between propose and promote. ``value``
        may be omitted (``None``). Refused unless the key is list-typed with
        union merge and ``entry`` is a non-empty string.

    On success returns ``(True, proposal_id)`` (the proposal id is the pref
    ``key`` itself); on failure returns ``(False, error)``.
    """
    instance_id = instance_id or resolve_instance_id()
    if op not in ("set", "remove_entry"):
        return False, f"unknown pref-change op: {op!r} (known: set, remove_entry)"
    if op == "remove_entry":
        spec = PREF_SCHEMA.get(key)
        if spec is None:
            return False, f"unknown preference key: {key}"
        if spec.type != "list" or spec.merge != "union":
            return False, (f"{key}: op='remove_entry' only applies to list-typed "
                           "(union-merge) preferences")
        if not isinstance(entry, str) or not entry.strip():
            return False, "op='remove_entry' requires a non-empty string entry"
        coerced = None  # the value is recomputed at promote time, never stored
        entry = entry.strip()
    else:
        refusal = _refuse_room_key(key)
        if refusal:
            return False, refusal
        ok, coerced, err = validate_pref(key, value)
        if not ok:
            return False, err
        noop = _min_merge_noop(key, coerced)
        if noop:
            return False, noop
    path = _pref_proposal_path(home_dir, user_id, key, instance_id)
    if path is None:
        return False, "empty or unsafe user_id refused (tenant scope)"
    payload = {"user_id": str(user_id), "key": key, "value": coerced, "op": op,
               "entry": entry if op == "remove_entry" else None}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(str(tmp), str(path))
        return True, key
    except Exception as e:
        logger.error("pref-change proposal write failed (%s): %s", path, e, exc_info=True)
        return False, f"proposal write failed: {e}"


def list_pending_pref_changes(user_id: Optional[str], home_dir: Path | str,
                              instance_id: Optional[str] = None) -> list[dict]:
    """Pending guarded pref-change proposals for one tenant (the self-evolution
    ``list_pending`` seam). Each item: ``{id, preview, chars, path}``.

    ``instance_id=None`` resolves via :func:`core.instance.resolve_instance_id`
    (owner-UX P2-4 final review, item 1)."""
    instance_id = instance_id or resolve_instance_id()
    d = _pref_proposal_dir(home_dir, user_id, instance_id)
    if d is None or not d.is_dir():
        return []
    items: list[dict] = []
    for p in sorted(d.glob("*.json")):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("pref-change proposal unreadable (%s): %s", p, e)
            continue
        key = payload.get("key", p.stem)
        if payload.get("op") == "remove_entry":
            # Operation-based removal (P2 T4 review fix): show the OPERATION,
            # not a raw list snapshot — the applied value is recomputed against
            # the current list at promote time.
            preview = f"remove {payload.get('entry')!r} from {key}"
        else:
            preview = f"{key} = {payload.get('value')!r}"
        items.append({"id": key, "preview": preview, "chars": len(preview), "path": str(p)})
    return items


def promote_pref_change(item_id: str, *, user_id: Optional[str], home_dir: Path | str,
                        instance_id: Optional[str] = None) -> tuple[bool, str]:
    """Apply a pending pref-change proposal (the self-evolution ``promote`` seam).

    ``instance_id=None`` resolves via :func:`core.instance.resolve_instance_id`
    (owner-UX P2-4 final review, item 1).

    Tenant integrity (review fix): the record's embedded ``user_id`` must equal
    the CALLER-scoped tenant that owns the ``.pending/prefs/`` directory — on a
    mismatch (a tampered/hand-edited record naming another, possibly *safe*,
    tenant) the promote is REFUSED; ``write_preference`` alone can't catch that
    case, since a different-but-safe embedded id would pass its validation and
    write cross-tenant. The apply then uses the CALLER-supplied ``user_id``
    (like ContractWriter/OwnerDocWriter), never the file content. On ANY
    failure the proposal is left in place (never silently lost) so the owner
    sees the error and can retry or reject.
    """
    instance_id = instance_id or resolve_instance_id()
    path = _pref_proposal_path(home_dir, user_id, item_id, instance_id)
    if path is None or not path.is_file():
        return False, f"no pending pref change '{item_id}'"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"pref-change proposal unreadable: {e}"
    embedded_uid = payload.get("user_id")
    if str(embedded_uid) != str(user_id):
        logger.warning(
            "pref-change promote refused: embedded tenant %r != caller tenant %r (%s)",
            embedded_uid, user_id, path)
        return False, ("embedded tenant mismatch — possible tampering, refusing "
                       "(proposal left pending)")
    key = payload.get("key", item_id)
    if payload.get("op") == "remove_entry":
        # Operation-based removal (P2 T4 review fix): recompute against the
        # CURRENT list at promote time — a full-list snapshot queued at propose
        # time would clobber any entry the owner added in between.
        entry = payload.get("entry")
        if not isinstance(entry, str) or not entry.strip():
            return False, "remove_entry proposal missing its entry (rejecting is safe)"
        entry = entry.strip()
        current = list(load_preferences(home_dir, user_id, instance_id).get(key, []) or [])
        if entry not in current:
            # Idempotent: the desired end-state already holds. Resolve the
            # proposal (never leave it dangling) and say so honestly.
            try:
                os.remove(str(path))
            except OSError:
                pass
            return True, (f"'{entry}' already removed from {key} — nothing to apply "
                          "(proposal resolved)")
        updated = [a for a in current if a != entry]
        ok, err = write_preference(home_dir, user_id, key, updated, instance_id)
        if not ok:
            return False, err  # proposal stays pending — never silently lost
        try:
            os.remove(str(path))
        except OSError:
            pass
        return True, f"removed '{entry}' from {key} (now {updated!r})"
    value = payload.get("value")
    ok, err = write_preference(home_dir, user_id, key, value, instance_id)
    if not ok:
        return False, err  # proposal stays pending — never silently lost
    try:
        os.remove(str(path))
    except OSError:
        pass
    return True, f"{key} = {value!r} applied"


def reject_pref_change(item_id: str, *, user_id: Optional[str], home_dir: Path | str,
                       instance_id: Optional[str] = None) -> tuple[bool, str]:
    """Discard (archive-then-remove) a pending pref-change proposal — never written.

    ``instance_id=None`` resolves via :func:`core.instance.resolve_instance_id`
    (owner-UX P2-4 final review, item 1)."""
    instance_id = instance_id or resolve_instance_id()
    path = _pref_proposal_path(home_dir, user_id, item_id, instance_id)
    if path is None or not path.is_file():
        return False, f"no pending pref change '{item_id}'"
    _archive_pref_proposal(home_dir, user_id, path, instance_id)
    try:
        os.remove(str(path))
    except OSError as e:
        return False, f"reject failed: {e}"
    return True, f"pref change '{item_id}' rejected (archived)"


def effective_autonomy_switch(pref_key: str, env_on: bool, user_id, home_dir) -> bool:
    """A loop's env/posture default AND-merged with its tenant pref: the pref
    can only DISABLE a loop, never enable one the operator has off (018 P0.2).
    No pref file present => byte-identical to *env_on*; fail-open to it.
    Self-wake and background review each carried this by hand."""
    try:
        return bool(resolve(pref_key, user_id, home_dir, env_value=env_on, default=env_on))
    except Exception:
        return env_on
