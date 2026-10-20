"""An agent write may not silently retire a block of live owner rules.

Prod 2026-10-15 (twice: 04:55 draft, 08:06 write): Rob's revision put a
`## Superseded` heading ABOVE live rules, so the writer recorded 27 current
rules as retired and Rob read only 16. The writer now refuses (a) any write
with more than one Superseded heading and (b) an agent write that would
retire more than a handful of active rules at once. The owner's own writes
(promote, owner turn) are not limited.
"""
from core.owner_doc_writer import OwnerDocWriter

RULES = [f"RULE {i} (2026-10-0{1 + i % 9}): owner said keep rule {i}." for i in range(10)]
BASE = "\n\n".join(RULES) + "\n"


def _seed(tmp_path):
    w = OwnerDocWriter(tmp_path)
    assert w.propose(BASE, user_id="u1", created_by="user", pending=False).ok
    return w


def test_agent_write_that_retires_many_rules_is_refused(tmp_path):
    w = _seed(tmp_path)
    broken = "\n\n".join(RULES[:3]) + "\n\n## Superseded\n\n" + "\n\n".join(RULES[3:]) + "\n"
    res = w.propose(broken, user_id="u1", created_by="agent")
    assert not res.ok and "retire" in " ".join(res.errors)
    assert w.list_pending("u1") is None


def test_agent_write_retiring_one_rule_is_fine(tmp_path):
    w = _seed(tmp_path)
    res = w.propose("\n\n".join(RULES[1:]) + "\n", user_id="u1", created_by="agent")
    assert res.ok


def test_two_superseded_headings_refused_for_anyone(tmp_path):
    w = _seed(tmp_path)
    body = BASE + "\n## Superseded\n\n- old\n\n## Superseded\n\n- older\n"
    res = w.propose(body, user_id="u1", created_by="user", pending=False)
    assert not res.ok and "Superseded" in " ".join(res.errors)


def test_owner_may_retire_many(tmp_path):
    w = _seed(tmp_path)
    res = w.propose(RULES[0] + "\n", user_id="u1", created_by="user", pending=False)
    assert res.ok
