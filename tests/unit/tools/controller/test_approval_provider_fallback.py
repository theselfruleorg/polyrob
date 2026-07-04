"""H9: a misconfigured APPROVAL_PROVIDER must never silently leave gated tools
ungated. get_approval_provider() raises on an unknown name; the Controller wiring
caught that and skipped hook registration (fail-OPEN). get_approval_provider_or_deny
resolves the provider or falls back to DenyByDefaultApprover (fail-CLOSED).
"""
from tools.controller.approval import (
    get_approval_provider_or_deny,
    AutoApprover,
    DenyByDefaultApprover,
)


def test_known_provider_resolves():
    assert isinstance(get_approval_provider_or_deny("auto"), AutoApprover)


def test_unknown_provider_falls_back_to_deny_not_auto():
    p = get_approval_provider_or_deny("does-not-exist")
    assert isinstance(p, DenyByDefaultApprover)


def test_none_resolves_to_auto_default():
    assert isinstance(get_approval_provider_or_deny(None), AutoApprover)
