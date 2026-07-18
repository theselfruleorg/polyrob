"""P1 (proposal 018): core/config_service.py — the ONE config control plane.

describe/effective/explain/search/set_value over the EXISTING stores (flags
catalog + PREF_SCHEMA + env files); nothing replaced. Every surface renders
from this service so display and mutation semantics can never drift per
surface again. Security invariants pinned here: secrets never readable back,
guarded prefs keep the confirm/queue pipeline, unknown keys hard-refuse.
"""
import pytest

from core import config_service as cs


@pytest.fixture()
def home(tmp_path):
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in ("GOAL_DAILY_QUOTA", "GOALS_ENABLED", "POLYROB_LOCAL", "ROB_LOCAL",
                "AUTONOMY_POSTURE", "ANYSITE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "polyrob-home"))
    monkeypatch.chdir(tmp_path)


# --- describe -----------------------------------------------------------------

def test_describe_pref_carries_schema_and_effective(home):
    info = cs.describe("goals.daily_quota", user_id="u1", home_dir=home)
    assert info.namespace == "pref"
    assert info.kind == "int"
    assert info.effective == 6 and info.source == "built-in"
    assert info.applies == "live"
    assert info.enforcement == "enforced"
    assert info.secret is False


def test_describe_flag_typed_default_and_secret_masking(home):
    info = cs.describe("GOALS_ENABLED", user_id="u1", home_dir=home)
    assert info.namespace == "flag"
    assert info.kind == "bool"
    assert info.effective in (True, False)

    sec = cs.describe("ANYSITE_API_KEY", user_id="u1", home_dir=home)
    assert sec.secret is True
    assert sec.effective in ("(unset)", "(set, masked)")


def test_describe_unknown_key_raises():
    with pytest.raises(KeyError):
        cs.describe("no.such_key")


# --- explain (provenance chain) ----------------------------------------------

def test_explain_pref_chain_shows_merge_inputs(home, monkeypatch):
    from core.prefs import write_preference
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "10")
    write_preference(home, "u1", "goals.daily_quota", 4)
    info = cs.explain("goals.daily_quota", user_id="u1", home_dir=home)
    origins = [s.origin for s in info.chain]
    assert any(o.startswith("pref") for o in origins)
    assert any(o.startswith("env") for o in origins)
    assert info.effective == 4  # min-merge


def test_explain_flag_chain_attributes_env_file(home, monkeypatch, tmp_path):
    envfile = tmp_path / ".polyrob" / ".env"
    envfile.parent.mkdir(parents=True, exist_ok=True)
    envfile.write_text("GOALS_ENABLED=true\n")
    monkeypatch.setenv("GOALS_ENABLED", "true")
    info = cs.explain("GOALS_ENABLED", user_id="u1", home_dir=home)
    origins = [s.origin for s in info.chain]
    assert "env:process" in origins
    assert any(o.startswith("env-file:") for o in origins)
    assert any(o.startswith("built-in") or o.startswith("default") for o in origins)


def test_explain_never_echoes_secret_values(home, monkeypatch, tmp_path):
    envfile = tmp_path / ".polyrob" / ".env"
    envfile.parent.mkdir(parents=True, exist_ok=True)
    envfile.write_text("ANYSITE_API_KEY=sk-supersecretvalue\n")
    monkeypatch.setenv("ANYSITE_API_KEY", "sk-supersecretvalue")
    info = cs.explain("ANYSITE_API_KEY", user_id="u1", home_dir=home)
    blob = repr(info)
    assert "supersecret" not in blob


# --- search -------------------------------------------------------------------

def test_search_spans_both_namespaces(home):
    hits = cs.search("wallet", user_id="u1", home_dir=home)
    keys = [i.key for i in hits]
    assert "budget.wallet_daily_usd" in keys
    assert any(k.startswith("WALLET_") for k in keys)


def test_search_finds_flag_by_fragment(home):
    keys = [i.key for i in cs.search("GOAL_DAILY", user_id="u1", home_dir=home)]
    assert "GOAL_DAILY_QUOTA" in keys


# --- set_value ----------------------------------------------------------------

def test_set_pref_safe_writes_immediately(home):
    res = cs.set_value("goals.daily_quota", "3", scope="user",
                       user_id="u1", home_dir=home)
    assert res.ok and res.outcome == "written"
    assert cs.describe("goals.daily_quota", user_id="u1", home_dir=home).effective == 3


def test_set_pref_guarded_queues_without_confirm(home):
    res = cs.set_value("budget.wallet_daily_usd", "5", scope="user",
                       user_id="u1", home_dir=home)
    assert res.ok and res.outcome == "queued"
    # Nothing written to the active pref store.
    assert cs.describe("budget.wallet_daily_usd", user_id="u1",
                       home_dir=home).source != "pref"


def test_set_pref_guarded_confirm_writes(home):
    res = cs.set_value("budget.wallet_daily_usd", "5", scope="user",
                       user_id="u1", home_dir=home, confirm=True)
    assert res.ok and res.outcome == "written"


def test_set_flag_shape_checked_project_scope(home, tmp_path):
    res = cs.set_value("GOALS_ENABLED", "on", scope="project",
                       user_id="u1", home_dir=home)
    assert res.ok and res.outcome == "written"
    assert "restart" in res.applies
    assert "GOALS_ENABLED=on" in (tmp_path / ".polyrob" / ".env").read_text()

    bad = cs.set_value("GOAL_DAILY_QUOTA", "not-a-number", scope="project",
                       user_id="u1", home_dir=home)
    assert not bad.ok and bad.outcome == "invalid"


def test_set_flag_global_scope_writes_home_env(home, tmp_path):
    res = cs.set_value("GOALS_ENABLED", "off", scope="global",
                       user_id="u1", home_dir=home)
    assert res.ok
    assert "GOALS_ENABLED=off" in (tmp_path / "polyrob-home" / ".env").read_text()


def test_set_unknown_key_refused(home):
    res = cs.set_value("TOTALLY_UNKNOWN_FLAG", "1", scope="project",
                       user_id="u1", home_dir=home)
    assert not res.ok and res.outcome == "refused"


def test_set_import_frozen_flag_warns_restart_semantics(home):
    res = cs.set_value("AGENT_COMPUTE_POSTURE", "1", scope="project",
                       user_id="u1", home_dir=home)
    assert res.ok
    assert "frozen at import" in res.message


def test_set_pref_requires_user(home):
    res = cs.set_value("goals.daily_quota", "3", scope="user",
                       user_id=None, home_dir=home)
    assert not res.ok and res.outcome == "refused"


# --- full-registry contract ---------------------------------------------------

def test_every_known_key_is_describable_and_secrets_stay_masked(home, monkeypatch):
    from core.flags import is_secret_flag
    # Plant a sentinel secret in the env: no SettingInfo may ever echo it.
    sentinel = "sk-SENTINEL-NEVER-SHOWN"
    monkeypatch.setenv("ANYSITE_API_KEY", sentinel)
    keys = cs.known_keys()
    assert len(keys) > 400  # 25 prefs + ~409 catalog flags
    for key in keys:
        info = cs.describe(key, user_id="u1", home_dir=home)
        assert info.namespace in ("pref", "flag"), key
        if info.namespace == "flag" and is_secret_flag(key):
            assert info.secret, key
            assert sentinel not in repr(info), key
