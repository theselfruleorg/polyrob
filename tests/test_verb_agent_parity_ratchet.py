"""Every owner verb names its agent twin, or the reason the agent has none (120 W9).

The incident this guards against (proposal 120 §0): the avatar push had an owner
command and no agent action, so the agent told the owner to run a shell command.
Each row of the ONE verb table (``core.verbs``: the core rows, the in-core money
verbs and every first-party pack row) carries EXACTLY ONE of

* ``agent`` — a registered action name or a classified tool id that gives the
  agent the same reach (``agent_partial`` says what it does not reach), or
* ``owner_only`` — one line: why the agent has nothing (120 §8, or
  ``core.verbs.SEAT`` for a seat control).

A part with no twin and no stated reason reads ``gap:`` and is counted here:
:data:`GAP_ROWS` may only shrink. Close a gap by building the agent's reach, then
lower the number; never write a reason to make a gap disappear.
"""
from pathlib import Path

import pytest

from tests.unit.core.test_action_name_parity import runtime_action_names  # noqa: F401 (fixture)

REPO = Path(__file__).resolve().parents[1]

#: The rows that name a gap today (120 W9): /lp (seed, move), /offer (resume,
#: retire, quote), /paid (enable, disable, price, asset), /trade (the mandate's
#: pause, resume, stop). Shrink only.
GAP_ROWS = 4


def _pack_rows() -> list:
    """The rows every first-party pack contributes through its ``owner.verbs`` hook.
    Only the X pack has one; a new pack with the hook must be added here."""
    hooked = sorted(p.parent.parent.name for p in REPO.glob("packs/*/*/__init__.py")
                    if "owner.verbs" in p.read_text(encoding="utf-8"))
    assert hooked == ["x"], f"a pack contributes owner verbs; add its rows here: {hooked}"
    from polyrob_x.owner_verbs import OWNER_VERBS
    return list(OWNER_VERBS["rows"])


def _rows() -> list:
    from core.verbs import VERB_TABLE, pack_verb_names
    core_rows = [v for v in VERB_TABLE if v.name not in pack_verb_names()]
    assert len(core_rows) > 90, "the verb table did not load its money rows"
    return core_rows + _pack_rows()


def test_every_verb_names_its_twin_or_its_reason_and_never_both():
    bad = [v.name for v in _rows() if bool(v.agent.strip()) == bool(v.owner_only.strip())]
    assert not bad, (
        f"{bad}: set exactly one of Verb.agent (the agent's twin) and Verb.owner_only "
        "(why the agent has none; 'gap: …' when nobody stated a reason)")


def test_the_fields_are_one_line_and_a_partial_rides_on_a_twin():
    for v in _rows():
        for field in ("agent", "owner_only", "agent_partial"):
            text = getattr(v, field)
            assert "\n" not in text and text == text.strip(), (v.name, field)
        assert not v.agent_partial or v.agent, f"{v.name}: agent_partial needs an agent twin"


def test_a_seat_control_carries_the_one_reason():
    from core.verbs import SEAT, verb_for
    for name in ("/clear", "/exit", "/chats"):
        assert verb_for(name).owner_only == SEAT, name


def test_every_twin_is_a_registered_action_or_a_classified_tool(runtime_action_names):  # noqa: F811
    from core.tool_capabilities import classified_ids
    known = set(runtime_action_names) | set(classified_ids())
    dead = sorted(f"{v.name} -> {v.agent}" for v in _rows() if v.agent and v.agent not in known)
    assert not dead, (
        f"{dead}: Verb.agent names no registered action and no classified tool id. "
        "A container-tool action registers as {tool_id}_{action}.")


def test_the_gap_rows_only_shrink():
    gaps = sorted(v.name for v in _rows()
                  if v.owner_only.startswith("gap:") or "gap:" in v.agent_partial)
    assert len(gaps) <= GAP_ROWS, (
        f"a new owner verb (or part of one) has no agent reach and no reason: {gaps}. "
        "Build the agent's twin, or state the §8-style reason; never raise GAP_ROWS.")
    assert len(gaps) == GAP_ROWS, (
        f"a gap closed ({gaps}): lower GAP_ROWS to {len(gaps)}")


@pytest.mark.parametrize("name", ["/avatar", "/job", "/why", "/usage", "/allowlist"])
def test_the_incident_shapes_have_their_twin(name):
    """The abilities 120 built for the agent stay named (W1-W5)."""
    from core.verbs import verb_for
    assert verb_for(name).agent, name
