"""032 — AppServiceRegistry: tenant-keyed rows, address-sticky approval, CAS claims,
caps counters, secret-shaped env refused at the registry."""
import os
import time

import pytest

from core.app_service.registry import (
    HOLDS_ADDRESS, STATUS_APPROVED, STATUS_DEPLOYING, STATUS_FAILED, STATUS_LIVE,
    STATUS_PAUSED, STATUS_PENDING, STATUS_STOPPED, AppServiceRegistry,
    default_app_services_db, screen_env,
)


def _req(r, slug="st", uid="u1", **over):
    kw = dict(source_dir="rob-status", cmd=["python", "server.py"], container_port=8765,
              health_path="/api/status.json", egress="none", egress_allow=[],
              env={"PORT": "8765"}, workspace_digest="d1")
    kw.update(over)
    return r.upsert_request(slug, uid, **kw)


def test_request_pending_then_approve_then_redeploy_unattended(tmp_path):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    row = _req(r)
    assert row["status"] == STATUS_PENDING and row["approved_at"] is None
    assert row["cmd"] == ["python", "server.py"] and row["env"] == {"PORT": "8765"}
    assert row["egress_allow"] == [] and row["container_port"] == 8765
    assert r.mark_approved("st", "u1") is True
    assert r.mark_approved("st", "u1") is False  # CAS: only pending -> approved
    assert r.get("st", "u1")["status"] == STATUS_APPROVED
    r.record_live("st", "u1", host_port=18000, container_name="polyrob-app-u1-st",
                  public_url="https://st.apps.example.test")
    live = r.get("st", "u1")
    assert live["status"] == STATUS_LIVE and live["host_port"] == 18000
    assert live["public_url"] == "https://st.apps.example.test"
    again = _req(r, workspace_digest="d2")
    # approval sticks to the ADDRESS *and its approved CONFIG*; only the tree moved
    assert again["status"] == STATUS_APPROVED
    assert again["workspace_digest"] == "d2" and again["health_path"] == "/api/status.json"
    assert again["approved_at"] is not None and again["approval_change"] == []


def test_pending_request_updates_params_and_stays_pending(tmp_path):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    _req(r)
    row = _req(r, container_port=9000)
    assert row["status"] == STATUS_PENDING and row["container_port"] == 9000


def test_cross_tenant_slug_rejects(tmp_path):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    _req(r)
    r.mark_approved("st", "u1")
    assert r.slug_holder("st") == "u1"
    with pytest.raises(PermissionError):
        _req(r, uid="u2")
    # a PENDING (never approved) slug does not hold the address yet
    _req(r, slug="other", uid="u1")
    assert r.slug_holder("other") is None
    _req(r, slug="other", uid="u2")  # allowed: first approval wins the address


def test_claim_is_cas(tmp_path):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    _req(r)
    r.mark_approved("st", "u1")
    assert r.claim("st", "u1", STATUS_APPROVED, STATUS_DEPLOYING) is True
    assert r.claim("st", "u1", STATUS_APPROVED, STATUS_DEPLOYING) is False
    assert r.get("st", "u1")["status"] == STATUS_DEPLOYING
    with pytest.raises(ValueError):
        _req(r)  # a redeploy request mid-deploy is refused, never silently lost


def test_failed_increments_and_redeploy_resets(tmp_path):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    _req(r)
    r.mark_approved("st", "u1")
    r.record_failed("st", "u1", error="health check failed")
    r.record_failed("st", "u1", error="health check failed again")
    row = r.get("st", "u1")
    assert row["status"] == STATUS_FAILED and row["consecutive_failures"] == 2
    assert "again" in row["last_failure_error"]
    row = _req(r, workspace_digest="d3")
    assert row["status"] == STATUS_APPROVED and row["consecutive_failures"] == 0
    assert row["last_failure_error"] is None


def test_health_stop_pause_transitions(tmp_path):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    _req(r)
    r.mark_approved("st", "u1")
    r.record_live("st", "u1", host_port=18001, container_name="c", public_url="u")
    r.record_health("st", "u1", ok=False)
    assert r.get("st", "u1")["consecutive_failures"] == 1
    r.record_health("st", "u1", ok=True)
    assert r.get("st", "u1")["consecutive_failures"] == 0
    assert r.set_status("st", "u1", STATUS_PAUSED) is True
    assert r.get("st", "u1")["status"] == STATUS_PAUSED
    assert r.set_status("st", "u1", STATUS_STOPPED, error="owner kill") is True
    assert r.get("st", "u1")["last_failure_error"] == "owner kill"
    assert r.get("st", "u1")["container_name"] == "c"  # kept until the supervisor cleans up
    r.record_stopped("st", "u1")
    row = r.get("st", "u1")
    assert row["container_name"] is None and row["host_port"] is None and row["public_url"] is None
    assert r.set_status("nope", "u1", STATUS_STOPPED) is False


def test_caps_counters_and_listing(tmp_path):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    _req(r)
    _req(r, slug="two")
    r.mark_approved("st", "u1")
    assert r.holding_count("u1") == 1 and r.holding_count("u2") == 0
    r.record_attempt("st", "u1")
    r.record_attempt("two", "u1")
    assert r.deploys_in_last_day("u1") == 2 and r.deploys_in_last_day("u2") == 0
    assert r.last_attempt_epoch("st", "u1") is not None
    assert r.last_attempt_epoch("st", "u1") <= time.time()
    assert r.last_attempt_epoch("none", "u1") is None
    r.record_live("st", "u1", host_port=18000, container_name="c", public_url="u")
    assert r.used_host_ports() == {18000}
    assert [x["slug"] for x in r.list_for("u1")] == ["st", "two"]
    assert [x["slug"] for x in r.list_by_status((STATUS_LIVE,))] == ["st"]
    assert len(r.list_all()) == 2
    assert set(HOLDS_ADDRESS) == {STATUS_APPROVED, STATUS_DEPLOYING, STATUS_LIVE, STATUS_PAUSED}


def test_secret_shaped_env_is_refused(tmp_path):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    for bad in ({"OPENAI_API_KEY": "x"}, {"DB_PASSWORD": "x"}, {"hf_token": "x"},
                {"seed_phrase": "x"}, {"lower-bad key": "x"}):
        with pytest.raises(ValueError):
            screen_env(bad)
        with pytest.raises(ValueError):
            _req(r, env=bad)
    assert screen_env({"PORT": "80", "LOG_LEVEL": "info"}) == {"PORT": "80", "LOG_LEVEL": "info"}
    assert screen_env(None) == {}


def test_secret_regex_matches_the_env_policy_samples():
    """Parity with tools/code_exec/env_policy.SECRET_PAT (core cannot import tools)."""
    from tools.code_exec.env_policy import SECRET_PAT
    from core.app_service.registry import _SECRET_KEY_RE
    for k in ("API_KEY", "MY_SECRET", "AUTH_TOKEN", "PASSWORD", "PRIVATE_KEY", "ACCESS_KEY",
              "MNEMONIC", "WALLET_SEED", "CREDENTIAL", "port"):
        assert bool(SECRET_PAT.search(k)) == bool(_SECRET_KEY_RE.search(k)), k


def test_default_db_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_SERVICES_DB_PATH", str(tmp_path / "x.db"))
    assert default_app_services_db() == str(tmp_path / "x.db")
    monkeypatch.delenv("APP_SERVICES_DB_PATH", raising=False)
    assert default_app_services_db().endswith("app_services.db")


def test_invalid_request_shapes(tmp_path):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    with pytest.raises(ValueError):
        _req(r, cmd=[])
    with pytest.raises(ValueError):
        _req(r, cmd="python server.py")
    with pytest.raises(ValueError):
        _req(r, egress="everything")
    with pytest.raises(ValueError):
        _req(r, container_port=0)
    with pytest.raises(ValueError):
        _req(r, health_path="status")


# --- approval binds to the approved CONFIG, not just the address -------------

def test_changed_config_returns_an_approved_slug_to_pending(tmp_path):
    """The P0 this closes: an owner-approved `hello-world` must not be silently
    redeployed with `egress='open'` and an exfiltration cmd."""
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    _req(r)
    r.mark_approved("st", "u1")
    approved_fp = r.get("st", "u1")["approved_fingerprint"]
    assert len(approved_fp) == 64
    row = _req(r, cmd=["python", "exfiltrate.py"], egress="open")
    assert row["status"] == STATUS_PENDING
    assert row["approval_change"] == ["cmd", "egress"]
    assert r.approval_change_reason(row) == "the start command and the egress mode changed"
    # the approval record still names what the owner said yes to
    assert row["approved_fingerprint"] == approved_fp
    assert row["approved_config"]["cmd"] == ["python", "server.py"]


@pytest.mark.parametrize("over,fields", [
    ({"cmd": ["sh", "-c", "curl evil"]}, ["cmd"]),
    ({"container_port": 9000}, ["container_port"]),
    ({"health_path": "/other"}, ["health_path"]),
    ({"egress": "open"}, ["egress"]),
    ({"egress": "allowlist", "egress_allow": ["evil.example.com"]}, ["egress", "egress_allow"]),
    ({"env": {"PORT": "8765", "EXTRA": "1"}}, ["env_keys"]),
])
def test_every_security_relevant_field_needs_reapproval(tmp_path, over, fields):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    _req(r)
    r.mark_approved("st", "u1")
    row = _req(r, **over)
    assert row["status"] == STATUS_PENDING and row["approval_change"] == fields


def test_inert_changes_stay_unattended(tmp_path):
    """A code bump (digest), an env VALUE edit, a moved source dir and a reordered
    allow-list are not a new capability — they redeploy on the approved address."""
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    _req(r, egress="allowlist", egress_allow=["a.example.com", "b.example.com"])
    r.mark_approved("st", "u1")
    row = _req(r, workspace_digest="d9", env={"PORT": "9999"}, source_dir="elsewhere",
               egress="allowlist", egress_allow=["B.example.com", "a.example.com"])
    assert row["status"] == STATUS_APPROVED and row["approval_change"] == []


def test_reverting_to_the_approved_config_is_unattended_again(tmp_path):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    _req(r)
    r.mark_approved("st", "u1")
    assert _req(r, egress="open")["status"] == STATUS_PENDING
    assert _req(r)["status"] == STATUS_APPROVED


def test_reapproval_stamps_the_new_fingerprint(tmp_path):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    _req(r)
    r.mark_approved("st", "u1")
    _req(r, egress="open")
    assert r.mark_approved("st", "u1") is True
    row = r.get("st", "u1")
    assert row["status"] == STATUS_APPROVED and row["approval_change"] == []
    assert _req(r, egress="open")["status"] == STATUS_APPROVED  # now the approved config
    assert _req(r)["status"] == STATUS_PENDING                  # the OLD config is not


def test_pre_fingerprint_row_is_migrated_not_crashed(tmp_path):
    """An approved row written before the fingerprint column existed: derive it
    from what the row currently runs, so the owner's app keeps redeploying."""
    import sqlite3
    db = str(tmp_path / "a.db")
    r = AppServiceRegistry(db)
    _req(r)
    r.mark_approved("st", "u1")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE app_services SET approved_fingerprint=NULL, approved_config=NULL")
    conn.commit()
    conn.close()
    row = r.get("st", "u1")
    assert row["approved_fingerprint"] and row["approval_change"] == []
    assert _req(r, workspace_digest="d7")["status"] == STATUS_APPROVED   # same config
    assert _req(r, egress="open")["status"] == STATUS_PENDING            # changed config


def test_schema_upgrade_adds_the_columns_to_an_existing_db(tmp_path):
    """The ALTER path: a database created without the columns opens fine."""
    import sqlite3
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE app_services (
        slug TEXT NOT NULL, user_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        source_dir TEXT NOT NULL, cmd TEXT NOT NULL, container_port INTEGER NOT NULL,
        health_path TEXT NOT NULL DEFAULT '/', egress TEXT NOT NULL DEFAULT 'none',
        egress_allow TEXT, env_json TEXT, workspace_digest TEXT, host_port INTEGER,
        container_name TEXT, public_url TEXT, approved_at REAL, last_deploy REAL,
        last_health REAL, consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_failure_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY (slug, user_id))""")
    conn.commit()
    conn.close()
    r = AppServiceRegistry(db)
    _req(r)
    assert r.get("st", "u1")["status"] == STATUS_PENDING
    assert r.mark_approved("st", "u1") is True
    assert r.get("st", "u1")["approved_fingerprint"]


# --- the declared env is screened by VALUE too -------------------------------

@pytest.mark.parametrize("value", [
    "sk-proj-AbCdEf0123456789AbCdEf0123456789",
    "ghp_" + "A" * 36,
    "github_pat_" + "a" * 60,
    "xoxb-123456789012-abcdefghijkl",
    "AKIAIOSFODNN7EXAMPLE",
    "AIza" + "b" * 35,
    "123456789:AA" + "x" * 33,
    "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n",
    "a" * 63 + "1",
    "0x" + "9f" * 32,
    "Qx7vLm2ZpR9tKw4NcB1sHdJf6YgVaEuT3oPiXl0MnZrQwS8y",  # opaque 48-char token
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVPmB92K27u",
])
def test_credential_shaped_values_are_refused_under_an_innocent_key(tmp_path, value):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    with pytest.raises(ValueError) as e:
        screen_env({"GREETING": value})
    assert "GREETING" in str(e.value) and value not in str(e.value)
    with pytest.raises(ValueError):
        _req(r, env={"GREETING": value})
    assert r.get("st", "u1") is None


@pytest.mark.parametrize("value", [
    "info", "8765", "https://rob-status.apps.example.test/api/status.json",
    "hello world, this is the greeting shown on the status page",
    "MyCompanyProductionEnvironmentNameForApp2026Abcd",
    "postgres-connection-pool-size-is-twenty-please",
    "2026-09-07T12:00:00Z", "true", "en_US.UTF-8",
    "a" * 80,
])
def test_ordinary_config_values_pass(value):
    assert screen_env({"GREETING": value}) == {"GREETING": value}


def test_approval_never_stamps_a_config_the_owner_did_not_see(tmp_path):
    """A request landing between the seat's read and its write must not inherit
    the approval: the CAS fails and the owner is asked again."""
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    _req(r)
    row = r.get("st", "u1")
    _req(r, cmd=["python", "exfiltrate.py"])  # the racing request
    assert r.get("st", "u1")["updated_at"] != row["updated_at"]
    real_get = r.get
    r.get = lambda s, u: row if (s, u) == ("st", "u1") else real_get(s, u)  # stale read
    try:
        assert r.mark_approved("st", "u1") is False
    finally:
        r.get = real_get
    assert real_get("st", "u1")["status"] == STATUS_PENDING
