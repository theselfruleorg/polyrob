"""The per-chat preference rows (``chat.*``): one more file, the same machinery.

Split out of ``core/prefs.py`` (101): that module holds the validator. Built by a
call so this module imports nothing from it.
"""
from __future__ import annotations


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
def build_chat_schema(_spec, SENSITIVITY_SAFE, ENFORCEMENT_ADVISORY) -> dict:
    return dict((
        # --- 101: what the owner granted this room --------------------------------
        # ⚠️ Owner only: `/groups set` and `/groups tools` are owner verbs, and the
        # read intersects with the closed sets (core/surfaces/room_grants.py).
        _spec("chat.room_tools", "list", SENSITIVITY_SAFE, "override", "next-turn",
              default_display=[],
              description="Extra tools for every turn in this room. Owner only. Only "
                          "tools the capability table names room-grantable count; "
                          "see /groups tools here"),
        _spec("chat.admin_tools", "list", SENSITIVITY_SAFE, "override", "live",
              default_display=[],
              description="Bundles a room admin may run with /admin: moderation, files, "
                          "http, browser. Owner only. Empty = /admin is off in this room"),
        _spec("chat.admin_turns_per_day", "int", SENSITIVITY_SAFE, "override", "live",
              min_value=0, max_value=500, default_display=20,
              description="How many /admin turns this room may run per day; each spends "
                          "the owner's LLM credit"),
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
                          "agent in this room without an @mention, e.g. ['hey agent']. "
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
