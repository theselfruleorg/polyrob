"""The autonomous tool grant (A-14): what an autonomous goal/cron run requests.

Moved from ``agents/task/constants.py`` (which re-exports every name) so the core
tier — the status rails, the tool grants — reads the ONE rule without an upward
import. The tool ids live in ``core/config_policy/profiles.py`` (067 P1).
"""
from core.config_policy.profiles import profile as _profile

# The full autonomous grant under AUTONOMY_MODE=autonomous (proposal 013 §2.3):
# every research/content/comms/coding/receivables tool. NEVER money-spend
# (x402_pay/wallet/hyperliquid/polymarket) and NEVER host/compute tools
# (code_execution/shell/self_env — those ride AGENT_COMPUTE_POSTURE).
# The ids live in core/config_policy/profiles.py (067 P1): `grant:autonomous`.
# It includes `x_browser` — build-in-public: the real-account X browser rail
# (x_post is owner-approval-gated → auto+notify under autonomous mode).
# Social-write, NOT money/host, so the MONEY_AND_HOST exclusion invariant
# (test_autonomous_toolset) is intact.

AUTONOMOUS_MODE_TOOLS = _profile("grant:autonomous")

#: The on-chain trading rail, added to the autonomous grant ONLY when the
#: operator arms it. Default OFF, so the shipped posture is unchanged.
#:
#: ⚠️ OWNER DIRECTIVE, 2026-09-12: *"we should GIVE AGENT TOOLS TO DO EVERY DEFI
#: ACTION WITHOUT USER"* — the "caps not taps" posture asked for repeatedly
#: (2026-07-15 autonomy-by-default, 2026-08-21 full-autonomy payment posture).
#:
#: The failure this fixes was live and total: with defi absent from the grant,
#: the OWNER'S OWN CHAT SESSION held neither tool, so when he asked his agent to
#: bridge, it truthfully answered "no bridge verb in the tool catalog" and asked
#: him to grant `defi_trade` — about a rail that was deployed and working. An
#: agent that cannot reach the capability its owner is asking about is not
#: safer; it teaches the owner that a shipped feature is broken.
#:
#: What bounds it — the reason arming this is a posture change and not a hole:
#: every verb simulates and asserts its own deltas before broadcast (`tx_guard`,
#: and the mirrored guard on the Solana path), the PolicyGate per-tx ceiling and
#: rolling daily cap apply, the kill-switch and the 031 pause apply, a
#: correspondent-tainted session can never reach it, and anything over
#: `DEFI_AUTONOMOUS_MAX_USD` still routes to the owner queue. **The bound on an
#: injected goal is the CAP, not the toolset** — which is why widening the
#: toolset while keeping the caps is the right trade and removing the caps would
#: not be.
#:
#: Still excluded even when armed: `wallet`, `hyperliquid`, `polymarket` and
#: every host tool (`MONEY_AND_HOST`). `x402_pay` rides the same key since 068
#: (below). This widens the on-chain trading rail, not host access.
DEFI_AUTONOMOUS_TOOLS = _profile("grant:defi_autonomous")

#: 068 X4: x402 auto-pay under the same armed key. Until 2026-09-26 no prod
#: session ever held `x402_pay` — every goal run's `load_tool` answered
#: "gated: money" — so the agent could not pay for one API call. What bounds it:
#: `tools/x402/spend_gate.py` admits only the owner and a goal/cron run on the
#: main agent (never a leaf, a self-wake or a tainted turn); a payment above
#: `X402_AUTONOMOUS_MAX_USD` waits in the owner queue; the PolicyGate holds the
#: per-tx ceiling and `WALLET_VENUE_DAILY_CAP_X402_USD`; the asset/network pins
#: bind it to canonical Base USDC. The tool itself only exists with
#: `X402_CLIENT_ENABLED` + `AGENT_WALLET_ENABLED` (`provided_only` drops it).
X402_AUTONOMOUS_TOOLS = _profile("grant:x402_autonomous")

#: 042: the launchpad and the injected dapp wallet. They ride `DEFI_AGENT_AUTONOMY`
#: alongside the trading rail — for the SAME reason it exists. An agent asked to
#: launch a token, or to use a dapp its owner is looking at, that answers "I have
#: no such tool" about a rail that is deployed and working teaches the owner that
#: a shipped feature is broken.
#:
#: Each is independently flag-gated on top (`LAUNCHPAD_ENABLED`,
#: `DAPP_BROWSER_ENABLED`, both default OFF), so arming `DEFI_AGENT_AUTONOMY`
#: alone grants a tool that still refuses every call with its own remedy. Two
#: deliberate keys, not one: reaching a capability and arming it are different
#: decisions.
DEFI_LAUNCH_AUTONOMOUS_TOOLS = _profile("grant:defi_launch_autonomous")

#: 050/069, 123 S1: the agent_nft tool rides the same key when `AGENT_NFT_ENABLED`
#: is on — the flag that registers the tool, so the grant never names a tool the
#: deploy did not construct (a false `[tool gap]` line, the 056 WS4 lesson). The
#: optional drop package is not a condition: the core verbs work without it.
AGENT_NFT_AUTONOMOUS_TOOLS = _profile("grant:agent_nft")


def _agent_nft_registered() -> bool:
    """`agent_nft` is registered only with `AGENT_NFT_ENABLED`
    (`tools/agent_nft/registration.py::agent_nft_enabled`; this tier may not
    import `tools`, so it reads the same flag)."""
    from core.env import bool_env
    return bool_env("AGENT_NFT_ENABLED", False)


def _x402_pay_constructed() -> bool:
    """`x402_pay` is registered only with BOTH flags (tools/x402/__init__.py +
    the wallet factory); requesting it otherwise writes a false tool gap into
    every goal record (the 056 WS4 lesson, as for agent_nft above)."""
    from core.env import bool_env
    return (bool_env("X402_CLIENT_ENABLED", False)
            and bool_env("AGENT_WALLET_ENABLED", False))


def autonomous_mode_tools() -> tuple:
    """`AUTONOMOUS_MODE_TOOLS`, plus the defi rail when `DEFI_AGENT_AUTONOMY` is
    armed. Resolved at CALL time so an operator can arm it without a redeploy."""
    from core.env import bool_env
    from core.config_policy.profiles import provided_only
    if bool_env("DEFI_AGENT_AUTONOMY", False):
        tools = (AUTONOMOUS_MODE_TOOLS + DEFI_AUTONOMOUS_TOOLS
                 + DEFI_LAUNCH_AUTONOMOUS_TOOLS
                 + (X402_AUTONOMOUS_TOOLS if _x402_pay_constructed() else ())
                 + (AGENT_NFT_AUTONOMOUS_TOOLS if _agent_nft_registered() else ()))
    else:
        tools = AUTONOMOUS_MODE_TOOLS
    # 067 P3: the views are import-time; a pack disabled at phase 2 drops out here.
    return tuple(provided_only(tools))


def stable_autonomous_toolset() -> bool:
    """057 WS-A: freeze the REQUESTED autonomous toolset so the emitted tool
    schemas (part of the cached prompt prefix) do not flip on a credential
    verdict's TTL. Default ON since 2026-09-22 (the harness/cache review:
    four distinct tool counts in one prod day, each a cold cache); ``false``
    restores the 056 WS4 drop behaviour. `core/status_rails.py` reads this
    function, so the status line and the dispatchers share one default."""
    from core.env import bool_env
    return bool_env("STABLE_AUTONOMOUS_TOOLSET", True)


def effective_autonomous_tools() -> tuple:
    """056 WS4: `autonomous_mode_tools()` minus the tools this deploy cannot
    actually serve right now — today `email` when the transport is SMTP and the
    credentials were rejected within the backoff window (`tools.email_tool`
    remembers a 535 process-wide). The CONSTANT is untouched; only the set the
    goal/cron dispatchers REQUEST moves, so a goal record stops carrying a false
    `[tool gap] email …` line (330 failed loads in 48 h, 2026-09-17→19). An
    AgentMail transport is unaffected by an SMTP rejection. Fail-open: any probe
    error keeps the tool in."""
    tools = tuple(autonomous_mode_tools())
    if stable_autonomous_toolset():
        # 057 WS-A: the drop below is a CACHE BUST. The emitted tool schemas are
        # part of the prompt's stable prefix, so dropping and re-adding `email`
        # on a 900 s TTL changed the prefix bytes between sessions — four
        # distinct tool counts (175/176/177/180) were seen in one prod day, each
        # one a cold cache at 50x the cached input price. Under this flag the
        # REQUESTED set is frozen and the rejection is told honestly instead:
        # the tool catalog renders `gated:credentials-rejected` with the remedy
        # (tools/tool_disclosure.py) and the tool itself already refuses at call
        # time with the same remedy. Same information, stable bytes.
        return tools
    try:
        from core.config_policy.capability_toggles import email_provider
        if "email" in tools and email_provider() == "smtp":
            from core.credential_verdicts import rejected_within, SMTP_TTL_SEC
            if rejected_within("smtp", SMTP_TTL_SEC):
                tools = tuple(t for t in tools if t != "email")
    except Exception:
        pass
    return tools
