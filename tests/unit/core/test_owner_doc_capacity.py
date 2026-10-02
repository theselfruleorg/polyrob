"""The owner-facts doc must hold a working owner's standing rules (2026-09-21).

At ``OWNER_DOC_MAX_CHARS = 1600`` the doc was a FORGETTING mechanism. Prod,
2026-09-20, recording ONE new owner rule:

    07:18:02 Owner-facts doc rejected: owner-facts doc is 1695/1600 chars …
    07:19:11 Owner-facts doc rejected: owner-facts doc is 1612/1600 chars …
    07:19:39 owner-doc rob written (pending)

It only fit by deleting text from the 09-17 and 09-18 rules; their enforcement
anchors ("self-test #11 + cron 41370b02653a") are gone from prod's owner.md.
Prod then sat at 1563/1600 — one rule from the next eviction.
"""
from core.instance import (CONTRACT_DOC_MAX_CHARS, OWNER_DOC_MAX_CHARS,
                           load_owner_doc)
from core.owner_doc_writer import OwnerDocWriter

#: Six rules of the shape prod actually writes (dated, quoted, with a rationale).
_RULE = ("RULE NAME {n} (2026-09-{n:02d}): a standing owner rule with its date, "
         "its scope, the rails it binds, and the rationale that stops a later run "
         "from re-deriving the opposite. Enforced by check #{n} of the self-test "
         "and cron 41370b02653a.\n\n")


def test_cap_holds_a_realistic_owner_rule_set():
    doc = "".join(_RULE.format(n=i) for i in range(1, 11))
    assert len(doc) > 1600, "fixture must exceed the old cap to be meaningful"
    assert len(doc) <= OWNER_DOC_MAX_CHARS, (
        f"{len(doc)} chars of ordinary owner rules still do not fit in "
        f"{OWNER_DOC_MAX_CHARS} — the doc still evicts a rule per arrival")


def test_cap_is_not_below_the_contract_doc():
    """Owner facts are always-injectable owner INTENT — never the tightest cap."""
    assert OWNER_DOC_MAX_CHARS >= CONTRACT_DOC_MAX_CHARS


def test_over_cap_error_forbids_deleting_a_standing_rule():
    hint = OwnerDocWriter._CAP_HINT.lower()
    assert "never delete" in hint
    assert "ask the owner" in hint


def test_an_over_cap_doc_blocks_the_whole_block_not_a_truncation(tmp_path):
    """⚠️ The read side BLOCKS rather than truncates, so the cap governs BOTH.

    An over-cap owner.md does not lose its tail — it loses every rule it has.
    """
    from core.instance import self_tier_root
    root = self_tier_root(tmp_path, "rob", "rob")
    root.mkdir(parents=True, exist_ok=True)
    (root / "owner.md").write_text("Owner prefers short answers.\n" * 400,
                                   encoding="utf-8")
    assert "[BLOCKED" in load_owner_doc(tmp_path, "rob", "rob")
    (root / "owner.md").write_text("Owner prefers short answers.\n", encoding="utf-8")
    assert "short answers" in load_owner_doc(tmp_path, "rob", "rob")
