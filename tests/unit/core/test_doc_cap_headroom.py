"""Durable-doc caps must leave room for the next owner rule (2026-10-02).

Prod at 18:49Z: owner.md 3928/4000, self.md 2149/2200. The owner said "add the
house-style rule" and BOTH writes were refused over-cap; the owner's /dev reply
was "we need to fix stupid small limits". A cap that a working agent fills in
two weeks turns every new owner instruction into an eviction of an older one.
"""
from core.instance import (OWNER_DOC_MAX_CHARS, SELF_CONTEXT_PER_DOC_MAX_CHARS,
                           SELF_DOC_MAX_CHARS)


def test_owner_doc_cap_has_headroom_over_prod_fill():
    assert OWNER_DOC_MAX_CHARS >= 8000


def test_self_doc_cap_has_headroom_over_prod_fill():
    assert SELF_DOC_MAX_CHARS >= 6000


def test_caps_stay_within_the_per_doc_prompt_bound():
    """Raising a writer cap past the injection bound would just move the refusal."""
    assert OWNER_DOC_MAX_CHARS <= SELF_CONTEXT_PER_DOC_MAX_CHARS
    assert SELF_DOC_MAX_CHARS <= SELF_CONTEXT_PER_DOC_MAX_CHARS


def test_tool_descriptions_quote_the_live_caps():
    """The agent plans its edits from the description; a stale number misleads it."""
    import inspect
    import tools.controller.doc_authoring as da
    src = inspect.getsource(da)
    assert "≤2200" not in src and "≤4000" not in src
    import agents.task.agent.core.background_review as br
    assert "≤2200" not in inspect.getsource(br)
