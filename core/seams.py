"""Core ⇄ platform seam contracts.

This module is the single home for the dependency-inversion seams between polyrob-core
(the agent framework) and polyrob-platform (auth/billing/metering). Core declares the
Protocols + permissive defaults; platform injects real implementations.

This commit seeds ONLY the usage carrier type. The five Protocols (UsageRecorder,
SessionAdmissionPolicy, PaymentVerifier, plus the auth/admission defaults) land in the
seam-inversion plan. Keep this module free of any platform import (modules.credits/
payments/x402, api.* platform modules).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMUsage:
    """One LLM call's billable usage, carried across the UsageRecorder seam.

    Field names mirror the kwargs the agent loop already passes to the platform usage
    tracker (agents/task/agent/core/next_action_internal.py). The platform recorder maps
    this onto modules.credits.usage_tracker.UsageRecord; core never imports that type.
    """

    user_id: str
    session_id: str
    agent_id: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    duration_seconds: float
    component: str
    purpose: str
