"""A money-gated tool must redirect the OWNER to their own verbs (2026-09-12).

Live failure: the owner asked the agent to proceed with a bridge. The agent
replied that no bridge verb was in its catalog, that defi_trade would not load,
and asked the owner to GRANT defi_trade to the session — while `/bridge` was
deployed and working. Naming a capability you cannot reach as though the system
lacked it is a capability denial, and the owner reasonably concluded a shipped
feature was broken.
"""
from tools.tool_disclosure import resolve_tool_status


def _money_remedy():
    st = resolve_tool_status("defi_trade", container=None, is_leaf=False,
                             loaded_ids=frozenset())
    assert st.status == "gated" and st.reason == "money"
    return st.remedy


def test_the_remedy_names_the_owner_chat_verbs():
    remedy = _money_remedy()
    for verb in ("/trade", "/bridge", "/wallet"):
        assert verb in remedy, f"the money remedy never mentions {verb}"


def test_it_says_the_capability_is_not_missing_only_not_the_agents():
    remedy = _money_remedy()
    assert "not YOURS" in remedy or "not missing" in remedy


def test_it_forbids_answering_an_owner_with_a_list_of_grants():
    """The exact shape of the live failure."""
    remedy = _money_remedy()
    assert "list of grants" in remedy
    assert "DENIAL" in remedy


def test_the_no_self_grant_warning_survives():
    """The older, load-bearing half must not be lost to the new half: a goal the
    agent creates can never carry a money tool, and saying otherwise sent the
    prod agent round a ~50-iteration loop."""
    remedy = _money_remedy()
    assert "NEVER carry one" in remedy
    assert "goal_create strips money tools" in remedy
