"""owner-UX P2 T5 — approval ladder: [o]nce / [s]ession / [a]lways-allow /
[d]eny / [n]ever.

Extends the interactive CLI approval provider (``InteractiveCLIApprover``,
``tools/controller/approval_interactive.py``) from a bare yes/no prompt to the
five-way Hermes-style ladder. Drives ``provider.request(...)`` directly via
``pytest.mark.asyncio``, mirroring the harness in
``tests/unit/tools/controller/test_interactive_approver.py``.

Persistence side effects are asserted through the SAME core reader/lister
functions the rest of the prefs system uses (``core.prefs.load_preferences``,
``list_pending_pref_changes``) — never by peeking at file bytes directly.
"""
import pytest

from core.prefs import load_preferences, list_pending_pref_changes, write_preference
from tools.controller.approval_interactive import InteractiveCLIApprover


def _answers(*vals):
    """input_fn stub that yields *vals* in order; a read past the end raises
    StopIteration — which is exactly what we want when a test asserts "no
    further prompt happens" (e.g. session/always auto-approve on repeat)."""
    it = iter(vals)

    def _fn(prompt):
        return next(it)

    return _fn


# --- once ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_once_approves_this_call_only():
    prov = InteractiveCLIApprover(input_fn=_answers("o", "d"))
    assert await prov.request("run_code", {}, None) is True
    # A second request for the SAME action is NOT remembered after 'once'.
    assert await prov.request("run_code", {}, None) is False


# --- session --------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_auto_approves_same_action_only():
    prov = InteractiveCLIApprover(input_fn=_answers("s"))
    assert await prov.request("git_push", {}, None) is True
    # Same action again: auto-approved without consuming a second answer (a
    # second read would raise StopIteration and fail the test).
    assert await prov.request("git_push", {}, None) is True

    prov2 = InteractiveCLIApprover(input_fn=_answers("s", "d"))
    assert await prov2.request("a1", {}, None) is True
    # A DIFFERENT action is not covered by the a1 session-approval.
    assert await prov2.request("a2", {}, None) is False


# --- deny -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deny_denies_this_call_only():
    prov = InteractiveCLIApprover(input_fn=_answers("d", "d"))
    assert await prov.request("shell_run", {}, None) is False
    # No persistence -> a repeat still prompts (and denies again).
    assert await prov.request("shell_run", {}, None) is False


# --- never --------------------------------------------------------------

@pytest.mark.asyncio
async def test_never_persists_to_approvals_deny_and_denies(tmp_path):
    prov = InteractiveCLIApprover(input_fn=_answers("n"), user_id="u1", home_dir=tmp_path)
    approved = await prov.request("dangerous_action", {}, None)
    assert approved is False
    prefs = load_preferences(tmp_path, "u1")
    assert "dangerous_action" in (prefs.get("approvals.deny") or [])


@pytest.mark.asyncio
async def test_never_appends_without_clobbering_existing_deny_list(tmp_path):
    write_preference(tmp_path, "u1", "approvals.deny", ["existing_action"])
    prov = InteractiveCLIApprover(input_fn=_answers("n"), user_id="u1", home_dir=tmp_path)
    await prov.request("dangerous_action", {}, None)
    prefs = load_preferences(tmp_path, "u1")
    deny_list = prefs.get("approvals.deny") or []
    assert "existing_action" in deny_list
    assert "dangerous_action" in deny_list


# --- always-allow -------------------------------------------------------

@pytest.mark.asyncio
async def test_always_allow_on_pref_added_action_queues_removal_and_approves(tmp_path):
    write_preference(tmp_path, "u1", "approvals.require", ["git_push"])
    prov = InteractiveCLIApprover(input_fn=_answers("a"), user_id="u1", home_dir=tmp_path)
    approved = await prov.request("git_push", {}, None)
    assert approved is True
    pending = list_pending_pref_changes("u1", tmp_path)
    assert any(p["id"] == "approvals.require" for p in pending)
    assert any("git_push" in p["preview"] for p in pending)


@pytest.mark.asyncio
async def test_always_allow_on_env_gated_action_approves_without_proposal(tmp_path):
    # No approvals.require pref names "shell_run" -> it's env/posture-gated,
    # not pref-added -> approve for this session only, no removal proposal.
    prov = InteractiveCLIApprover(input_fn=_answers("a"), user_id="u1", home_dir=tmp_path)
    approved = await prov.request("shell_run", {}, None)
    assert approved is True
    assert list_pending_pref_changes("u1", tmp_path) == []


# --- no tenant context -> degrade, never crash --------------------------

@pytest.mark.asyncio
async def test_always_allow_with_no_tenant_context_degrades_to_session():
    prov = InteractiveCLIApprover(input_fn=_answers("a"))
    assert await prov.request("git_push", {}, None) is True
    # Degraded to session-scoped: repeat is auto-approved without a 2nd answer.
    assert await prov.request("git_push", {}, None) is True


@pytest.mark.asyncio
async def test_never_with_no_tenant_context_degrades_to_plain_deny():
    prov = InteractiveCLIApprover(input_fn=_answers("n", "d"))
    assert await prov.request("git_push", {}, None) is False
    # Degraded deny is NOT remembered either -> a repeat still prompts.
    assert await prov.request("git_push", {}, None) is False


# --- unrecognized input: reprompt once, then fail-closed -----------------

@pytest.mark.asyncio
async def test_unrecognized_input_reprompts_then_denies():
    prov = InteractiveCLIApprover(input_fn=_answers("bogus", "still-bogus"))
    assert await prov.request("git_push", {}, None) is False


@pytest.mark.asyncio
async def test_unrecognized_input_reprompt_recovers_on_valid_second_answer():
    prov = InteractiveCLIApprover(input_fn=_answers("bogus", "o"))
    assert await prov.request("git_push", {}, None) is True


# --- fail-open on a raising prefs write -----------------------------------

@pytest.mark.asyncio
async def test_write_preference_failure_fails_open_to_the_deny_decision(tmp_path, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("core.prefs.write_preference", _boom)
    prov = InteractiveCLIApprover(input_fn=_answers("n"), user_id="u1", home_dir=tmp_path)
    # The decision itself (deny) must still be honored even though persisting
    # it to approvals.deny blew up.
    approved = await prov.request("git_push", {}, None)
    assert approved is False


@pytest.mark.asyncio
async def test_propose_pref_change_failure_fails_open_to_the_approve_decision(tmp_path, monkeypatch):
    write_preference(tmp_path, "u1", "approvals.require", ["git_push"])

    def _boom(*a, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("core.prefs.propose_pref_change", _boom)
    prov = InteractiveCLIApprover(input_fn=_answers("a"), user_id="u1", home_dir=tmp_path)
    approved = await prov.request("git_push", {}, None)
    assert approved is True
