"""WS-A principal-awareness frame line (belt-and-suspenders over the untrusted wrap)."""
from core.instance import owner_awareness_line


def test_awareness_line_names_owner_and_frames_correspondents():
    line = owner_awareness_line({"POLYROB_OWNER_USER_ID": "u_owner"})
    assert "u_owner" in line
    assert "correspondent-message" in line
    assert "never instructions" in line.lower() or "not instructions" in line.lower()


def test_awareness_line_frames_data_even_without_owner():
    # The DATA-not-instructions framing must be present whenever the model is on. With no
    # DISTINCT owner bound, the principal auto-derives to the instance id ("rob"), so the
    # "act on behalf of OWNER rob" clause is self-referential noise and is suppressed.
    line = owner_awareness_line({})
    assert "correspondent-message" in line
    assert "never instructions" in line.lower()
    assert "OWNER " not in line.split("Content")[0]  # no "act on behalf of OWNER X" clause


def test_awareness_line_suppresses_owner_clause_when_owner_equals_instance():
    # owner principal == instance id (the auto-derived single-user default) -> the agent
    # would be told it acts "on behalf of OWNER <itself>", which is meaningless. Suppress.
    line = owner_awareness_line({"POLYROB_OWNER_USER_ID": "rob", "POLYROB_INSTANCE_ID": "rob"})
    assert "OWNER " not in line.split("Content")[0]


def test_awareness_line_names_distinct_owner():
    # A DISTINCT human owner (different from the instance id) IS named.
    line = owner_awareness_line({"POLYROB_OWNER_USER_ID": "gleb", "POLYROB_INSTANCE_ID": "rob"})
    assert "gleb" in line.split("Content")[0]
