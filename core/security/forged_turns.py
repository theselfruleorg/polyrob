"""Forged-turn kind constants (SSOT — R-4 promotion from agents self_wake, 2026-07-17).

The kinds a producer uses to forge a non-user re-entry into a session (self-wake
W1, async-delegation-result UP-12). Stdlib-only on purpose: the ingress producer
(agents.task.agent.core.user_ingress), the async-delegation producer
(agents.task.agent.orchestrator), the security-gate consumer
(tools.controller.action_registration) and the core posture gate
(core.config_policy.policy) all import these without any cycle risk.
"""

SELF_WAKE_KIND = "self_wake"
DELEGATION_RESULT_KIND = "delegation_result"

FORGED_TURN_KINDS = (SELF_WAKE_KIND, DELEGATION_RESULT_KIND)
