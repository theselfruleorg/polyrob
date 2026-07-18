"""Guarded pref changes ride the self-evolution pending/approve flow (owner-UX P2 T3).

Producer: ``core.prefs.propose_pref_change`` — validates FIRST via ``validate_pref``
(unknown key / malformed value refused before anything is quarantined), then writes a
``{user_id, key, value}`` proposal that ``core.self_evolution``'s ``pref_change`` kind
lists/promotes/rejects alongside skills/self-context/owner-facts/the operating
contract. Approval applies via ``write_preference`` from the CORE ``self_evolution``
seam — the same function every surface (REPL `/pending`, `polyrob owner
promote/reject`, Telegram `/approve <id>`) calls, so driving it directly here proves
every surface inherits the apply-on-approve wiring.
"""
from core import self_evolution as se
from core.prefs import load_preferences, propose_pref_change


# --- propose ------------------------------------------------------------------

def test_propose_lists_as_pending_pref_change(tmp_path):
    ok, proposal_id = propose_pref_change(
        "gleb", "budget.wallet_daily_usd", 5.0, tmp_path, instance_id="rob")
    assert ok
    assert proposal_id == "budget.wallet_daily_usd"

    items = se.list_pending("gleb", home_dir=tmp_path, instance_id="rob")
    assert len(items) == 1
    assert items[0]["kind"] == se.KIND_PREF_CHANGE
    assert items[0]["id"] == "budget.wallet_daily_usd"
    assert "5.0" in items[0]["preview"]

    # not written yet — the pref file doesn't exist until approved
    assert load_preferences(tmp_path, "gleb", instance_id="rob") == {}


def test_propose_unknown_key_refused_at_propose_time(tmp_path):
    ok, err = propose_pref_change("gleb", "bogus.nonexistent", "x", tmp_path, instance_id="rob")
    assert not ok
    assert "unknown" in err.lower()
    assert se.list_pending("gleb", home_dir=tmp_path, instance_id="rob") == []


def test_propose_invalid_value_refused_at_propose_time(tmp_path):
    ok, err = propose_pref_change(
        "gleb", "budget.wallet_daily_usd", "not-a-number", tmp_path, instance_id="rob")
    assert not ok
    assert se.list_pending("gleb", home_dir=tmp_path, instance_id="rob") == []


def test_propose_tenant_scoped(tmp_path):
    propose_pref_change("gleb", "budget.wallet_daily_usd", 5.0, tmp_path, instance_id="rob")
    assert se.list_pending("mallory", home_dir=tmp_path, instance_id="rob") == []


def test_propose_refuses_unsafe_user_id(tmp_path):
    ok, err = propose_pref_change(
        "../evil", "budget.wallet_daily_usd", 5.0, tmp_path, instance_id="rob")
    assert not ok


# --- approve via the CORE seam (proves every surface inherits it) -------------

def test_approve_via_core_seam_writes_pref_and_resolves_proposal(tmp_path):
    """Drives core.self_evolution.promote() directly — NOT a CLI/Telegram handler —
    proving the apply happens in the shared core function that every surface
    (REPL /pending, polyrob owner promote, Telegram /approve) calls."""
    propose_pref_change("gleb", "budget.wallet_daily_usd", 5.0, tmp_path, instance_id="rob")

    ok, msg = se.promote(
        se.KIND_PREF_CHANGE, "budget.wallet_daily_usd", user_id="gleb",
        home_dir=tmp_path, instance_id="rob")

    assert ok
    assert "5.0" in msg
    prefs = load_preferences(tmp_path, "gleb", instance_id="rob")
    assert prefs["budget.wallet_daily_usd"] == 5.0
    # proposal resolved — no longer pending
    assert se.list_pending("gleb", home_dir=tmp_path, instance_id="rob") == []


def test_guarded_key_round_trip(tmp_path):
    ok, _ = propose_pref_change("gleb", "budget.wallet_daily_usd", 5.0, tmp_path, instance_id="rob")
    assert ok
    ok, _ = se.promote(se.KIND_PREF_CHANGE, "budget.wallet_daily_usd", user_id="gleb",
                       home_dir=tmp_path, instance_id="rob")
    assert ok
    assert load_preferences(tmp_path, "gleb", instance_id="rob")["budget.wallet_daily_usd"] == 5.0


# --- reject ---------------------------------------------------------------------

def test_reject_never_writes_pref(tmp_path):
    propose_pref_change("gleb", "budget.wallet_daily_usd", 5.0, tmp_path, instance_id="rob")

    ok, msg = se.reject(se.KIND_PREF_CHANGE, "budget.wallet_daily_usd", user_id="gleb",
                        home_dir=tmp_path, instance_id="rob")

    assert ok
    assert load_preferences(tmp_path, "gleb", instance_id="rob") == {}
    assert se.list_pending("gleb", home_dir=tmp_path, instance_id="rob") == []
    # archived (recoverable), not silently gone
    archived = list((tmp_path / "identity" / "rob" / "user_gleb" / ".archived").glob("*.json"))
    assert archived


def test_reject_unknown_proposal_errors(tmp_path):
    ok, msg = se.reject(se.KIND_PREF_CHANGE, "no.such.key", user_id="gleb",
                        home_dir=tmp_path, instance_id="rob")
    assert not ok


# --- write-failure path: unsafe uid smuggled into the proposal record -----------

def test_write_failure_leaves_proposal_pending(tmp_path):
    """A corrupted/hand-edited proposal record (bypassing propose_pref_change's own
    front door) whose embedded user_id is unsafe must fail the apply via
    write_preference's OWN re-validation — and the proposal must NOT be silently
    dropped; it stays pending so the owner can see the failure and retry/reject."""
    import json
    from core.prefs import _pref_proposal_path

    # Seed a legit-looking pending directory under a SAFE tenant ("gleb"), but the
    # record's own embedded user_id is smuggled as unsafe.
    path = _pref_proposal_path(tmp_path, "gleb", "budget.wallet_daily_usd", instance_id="rob")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "user_id": "../evil", "key": "budget.wallet_daily_usd", "value": 5.0,
    }), encoding="utf-8")

    ok, err = se.promote(se.KIND_PREF_CHANGE, "budget.wallet_daily_usd", user_id="gleb",
                         home_dir=tmp_path, instance_id="rob")

    assert not ok
    # never silently lost — still pending after the failed apply
    items = se.list_pending("gleb", home_dir=tmp_path, instance_id="rob")
    assert len(items) == 1
    assert items[0]["id"] == "budget.wallet_daily_usd"
    # and, of course, never actually written anywhere
    assert load_preferences(tmp_path, "../evil", instance_id="rob") == {}


# --- cross-tenant write via a tampered embedded user_id (review fix) -------------

def test_tampered_embedded_tenant_mismatch_refused(tmp_path):
    """Review fix (P2 T3): a tampered pending file under tenant A's .pending/prefs/
    whose EMBEDDED user_id names a different (safe!) tenant must NOT promote into
    that other tenant's preferences.toml — the embedded id passes is_safe_tenant_id,
    so write_preference alone can't catch it. promote must assert embedded ==
    caller-scoped tenant, refuse on mismatch, and leave the proposal pending."""
    import json
    from core.prefs import _pref_proposal_path

    # Legit propose as gleb...
    propose_pref_change("gleb", "budget.wallet_daily_usd", 5.0, tmp_path, instance_id="rob")
    # ...then tamper the pending record's embedded user_id to another SAFE tenant.
    path = _pref_proposal_path(tmp_path, "gleb", "budget.wallet_daily_usd", instance_id="rob")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["user_id"] = "mallory"
    path.write_text(json.dumps(payload), encoding="utf-8")

    ok, err = se.promote(se.KIND_PREF_CHANGE, "budget.wallet_daily_usd", user_id="gleb",
                         home_dir=tmp_path, instance_id="rob")

    assert not ok
    assert "mismatch" in err.lower()
    # NEITHER tenant got a write
    assert load_preferences(tmp_path, "gleb", instance_id="rob") == {}
    assert load_preferences(tmp_path, "mallory", instance_id="rob") == {}
    # proposal still pending — never silently lost
    items = se.list_pending("gleb", home_dir=tmp_path, instance_id="rob")
    assert len(items) == 1 and items[0]["id"] == "budget.wallet_daily_usd"


# --- operation-based removals (owner-UX P2 T4 review fix) -----------------------
#
# A remove queued as a FULL-LIST SNAPSHOT clobbers any entry added between propose
# and promote. ``op="remove_entry"`` stores the OPERATION (which entry to drop) and
# promote recomputes against the CURRENT list, so a later add survives.


def test_propose_remove_entry_preview_renders_operation(tmp_path):
    from core.prefs import write_preference
    write_preference(tmp_path, "gleb", "approvals.require", ["A", "B"], instance_id="rob")
    ok, pid = propose_pref_change("gleb", "approvals.require", None, tmp_path,
                                  instance_id="rob", op="remove_entry", entry="A")
    assert ok and pid == "approvals.require"
    items = se.list_pending("gleb", home_dir=tmp_path, instance_id="rob")
    assert len(items) == 1
    assert "remove 'A' from approvals.require" in items[0]["preview"]


def test_remove_entry_promote_recomputes_current_list(tmp_path):
    """The reviewer's exact scenario: queue remove A -> owner adds C -> promote
    the removal -> C SURVIVES and A is gone (the old full-list snapshot would
    have silently erased C)."""
    from core.prefs import write_preference
    write_preference(tmp_path, "gleb", "approvals.require", ["A", "B"], instance_id="rob")
    ok, _ = propose_pref_change("gleb", "approvals.require", None, tmp_path,
                                instance_id="rob", op="remove_entry", entry="A")
    assert ok
    # Owner adds C AFTER queuing the removal (e.g. /approve add C).
    write_preference(tmp_path, "gleb", "approvals.require", ["A", "B", "C"], instance_id="rob")

    ok, msg = se.promote(se.KIND_PREF_CHANGE, "approvals.require", user_id="gleb",
                         home_dir=tmp_path, instance_id="rob")

    assert ok
    got = load_preferences(tmp_path, "gleb", instance_id="rob")["approvals.require"]
    assert "C" in got and "B" in got and "A" not in got
    assert se.list_pending("gleb", home_dir=tmp_path, instance_id="rob") == []


def test_remove_entry_already_gone_resolves_gracefully(tmp_path):
    from core.prefs import write_preference
    write_preference(tmp_path, "gleb", "approvals.require", ["A"], instance_id="rob")
    propose_pref_change("gleb", "approvals.require", None, tmp_path,
                        instance_id="rob", op="remove_entry", entry="A")
    # The entry disappears before promote (owner hand-edited / another surface).
    write_preference(tmp_path, "gleb", "approvals.require", [], instance_id="rob")

    ok, msg = se.promote(se.KIND_PREF_CHANGE, "approvals.require", user_id="gleb",
                         home_dir=tmp_path, instance_id="rob")

    assert "already removed" in msg.lower() and "nothing to apply" in msg.lower()
    # resolved — NOT left dangling in the pending queue
    assert se.list_pending("gleb", home_dir=tmp_path, instance_id="rob") == []


def test_remove_entry_refused_on_non_list_key(tmp_path):
    ok, err = propose_pref_change("gleb", "budget.wallet_daily_usd", None, tmp_path,
                                  instance_id="rob", op="remove_entry", entry="A")
    assert not ok
    assert se.list_pending("gleb", home_dir=tmp_path, instance_id="rob") == []


def test_remove_entry_refused_on_empty_entry(tmp_path):
    ok, err = propose_pref_change("gleb", "approvals.require", None, tmp_path,
                                  instance_id="rob", op="remove_entry", entry="")
    assert not ok
    assert se.list_pending("gleb", home_dir=tmp_path, instance_id="rob") == []


def test_unknown_op_refused(tmp_path):
    ok, err = propose_pref_change("gleb", "approvals.require", None, tmp_path,
                                  instance_id="rob", op="bogus_op", entry="A")
    assert not ok


def test_legacy_set_proposal_without_op_field_still_applies(tmp_path):
    """Backward compat: a pending proposal written BEFORE the op field existed
    (bare {user_id, key, value}) must still promote as a plain set."""
    import json
    from core.prefs import _pref_proposal_path

    path = _pref_proposal_path(tmp_path, "gleb", "approvals.require", instance_id="rob")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"user_id": "gleb", "key": "approvals.require",
                                "value": ["X"]}), encoding="utf-8")

    ok, msg = se.promote(se.KIND_PREF_CHANGE, "approvals.require", user_id="gleb",
                         home_dir=tmp_path, instance_id="rob")

    assert ok
    assert load_preferences(tmp_path, "gleb", instance_id="rob")["approvals.require"] == ["X"]


# --- promote/reject unknown kind still errors cleanly ---------------------------

def test_promote_unknown_pref_change_id_errors(tmp_path):
    ok, msg = se.promote(se.KIND_PREF_CHANGE, "no.such.key", user_id="gleb",
                         home_dir=tmp_path, instance_id="rob")
    assert not ok
    assert "no pending" in msg.lower()


# --- contract kind rides the same pending/approve pipeline (carried from T1) ----

def test_contract_pending_draft_appears_in_listing(tmp_path):
    from core.contract_writer import ContractWriter, PROVENANCE_BACKGROUND

    ContractWriter(tmp_path, instance_id="rob").propose(
        "Never spend more than the daily budget without approval.",
        user_id="gleb", created_by=PROVENANCE_BACKGROUND)

    items = se.list_pending("gleb", home_dir=tmp_path, instance_id="rob")
    kinds = {i["kind"] for i in items}
    assert se.KIND_CONTRACT in kinds
    contract_item = next(i for i in items if i["kind"] == se.KIND_CONTRACT)
    assert "daily budget" in contract_item["preview"]


def test_contract_approve_promotes_to_active(tmp_path):
    from core.contract_writer import ContractWriter, PROVENANCE_BACKGROUND
    from core.instance import load_self_doc

    writer = ContractWriter(tmp_path, instance_id="rob")
    writer.propose("Never spend more than the daily budget without approval.",
                   user_id="gleb", created_by=PROVENANCE_BACKGROUND)

    ok, msg = se.promote(se.KIND_CONTRACT, "gleb", user_id="gleb",
                         home_dir=tmp_path, instance_id="rob")

    assert ok
    contract_path = tmp_path / "identity" / "rob" / "user_gleb" / "contract.md"
    assert "daily budget" in contract_path.read_text(encoding="utf-8")
    assert se.list_pending("gleb", home_dir=tmp_path, instance_id="rob") == []


def test_contract_reject_never_activates(tmp_path):
    from core.contract_writer import ContractWriter, PROVENANCE_BACKGROUND

    writer = ContractWriter(tmp_path, instance_id="rob")
    writer.propose("Draft rule to be discarded.", user_id="gleb",
                   created_by=PROVENANCE_BACKGROUND)

    ok, _ = se.reject(se.KIND_CONTRACT, "gleb", user_id="gleb",
                      home_dir=tmp_path, instance_id="rob")

    assert ok
    contract_path = tmp_path / "identity" / "rob" / "user_gleb" / "contract.md"
    assert not contract_path.exists()
    assert se.list_pending("gleb", home_dir=tmp_path, instance_id="rob") == []
