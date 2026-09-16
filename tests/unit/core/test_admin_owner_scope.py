"""035 P0-3 — an owner-control listing must not be confidently wrong.

Measured on prod 2026-09-10: `polyrob owner pending` printed "no pending
proposals" while four existed, and three env combinations gave three different
answers. `_data_dir()` adopts the DEPLOYED data home (031's `admin_data_home`
seam) and says so; `_instance_id()` and `_owner_tenant()` did not, so the
command resolved a correct home under a wrong instance/tenant and reported an
empty result under a note claiming it looked in the right place.
"""
import core.admin_data_home as adh


def _fake_env_file(tmp_path, body):
    f = tmp_path / "polyrob.env"
    f.write_text(body)
    return str(f)


def test_deployed_env_value_reads_one_key(tmp_path, monkeypatch):
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", _fake_env_file(tmp_path, (
        "POLYROB_OWNER_TELEGRAM_ID=28436760\n"
        "export POLYROB_INSTANCE_ID=rob\n"
        'POLYROB_OWNER_USER_ID="rob"\n'
        "POLYROB_DATA_DIR=/var/lib/polyrob\n")))
    assert adh.deployed_env_value("POLYROB_INSTANCE_ID") == "rob"
    assert adh.deployed_env_value("POLYROB_OWNER_USER_ID") == "rob"
    assert adh.deployed_env_value("POLYROB_DATA_DIR") == "/var/lib/polyrob"
    assert adh.deployed_env_value("NOPE") is None


def test_deployed_env_value_last_assignment_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", _fake_env_file(
        tmp_path, "POLYROB_INSTANCE_ID=first\nPOLYROB_INSTANCE_ID=second\n"))
    assert adh.deployed_env_value("POLYROB_INSTANCE_ID") == "second"


def test_deployed_env_value_never_partial_matches(tmp_path, monkeypatch):
    """``POLYROB_INSTANCE_ID_EXTRA`` must not answer for ``POLYROB_INSTANCE_ID``."""
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", _fake_env_file(
        tmp_path, "POLYROB_INSTANCE_ID_EXTRA=wrong\n"))
    assert adh.deployed_env_value("POLYROB_INSTANCE_ID") is None


def test_deployed_env_value_unreadable_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", str(tmp_path / "absent.env"))
    assert adh.deployed_env_value("POLYROB_INSTANCE_ID") is None


def test_admin_instance_id_prefers_the_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", _fake_env_file(
        tmp_path, "POLYROB_INSTANCE_ID=deployed\n"))
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "shell")
    assert adh.admin_instance_id() == "shell"


def test_admin_instance_id_adopts_the_deployment(tmp_path, monkeypatch):
    """THE prod case: nothing in the shell, a deployed instance names itself."""
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", _fake_env_file(
        tmp_path, "POLYROB_INSTANCE_ID=rob\n"))
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    monkeypatch.delenv("BOT_INSTANCE_ID", raising=False)
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    assert adh.admin_instance_id() == "rob"


def test_admin_instance_id_refuses_an_unsafe_name(tmp_path, monkeypatch):
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", _fake_env_file(
        tmp_path, "POLYROB_INSTANCE_ID=../../etc\n"))
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    monkeypatch.delenv("BOT_INSTANCE_ID", raising=False)
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    assert adh.admin_instance_id() == "polyrob", "a path-unsafe id is never adopted"


def test_admin_owner_principal_adopts_the_deployment(tmp_path, monkeypatch):
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", _fake_env_file(
        tmp_path, "POLYROB_INSTANCE_ID=rob\nPOLYROB_OWNER_USER_ID=rob\n"))
    for k in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID", "POLYROB_INSTANCE_ID",
              "BOT_INSTANCE_ID", "POLYROB_PROFILE", "SURFACE_SUPER_ADMIN_USER_IDS"):
        monkeypatch.delenv(k, raising=False)
    assert adh.admin_owner_principal() == "rob"


def test_a_declared_instance_is_not_an_owner_tenant(tmp_path, monkeypatch):
    """⚠️ Reversed 2026-09-15 (043 residue). This used to adopt the deployment's
    INSTANCE id as the owner tenant when no owner was declared, on the reading
    that a single-instance deploy IS owned by its instance. That made
    `polyrob owner …` answer `rob` on a box whose REPL, goals, memory and
    identity docs all answered `local` — the four-resolver divergence. An
    instance id names the identity-doc tier and the avatar, never an owner
    tenant, so an undeclared owner now falls to the ONE resolver's answer.
    A deployment that declares an OWNER is still adopted (test above)."""
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", _fake_env_file(
        tmp_path, "POLYROB_INSTANCE_ID=rob\n"))
    for k in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID", "POLYROB_INSTANCE_ID",
              "BOT_INSTANCE_ID", "POLYROB_PROFILE", "SURFACE_SUPER_ADMIN_USER_IDS",
              "POLYROB_LOCAL_OWNER"):
        monkeypatch.delenv(k, raising=False)
    assert adh.admin_owner_principal() == "local"
    assert adh.admin_instance_id() == "rob", "the instance axis is untouched"


def test_no_deployment_is_byte_identical(tmp_path, monkeypatch):
    """A developer box must see no change at all: the same answers the ordinary
    resolvers give. (The owner side reads `resolve_owner_user_id` — the ONE
    owner-tenant resolver — not the instance-defaulting `resolve_owner_principal`
    it used before 2026-09-15.)"""
    from core.instance import resolve_instance_id, resolve_owner_user_id
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setattr(adh, "UNIT_DIRS", (str(tmp_path / "no-units"),))
    assert adh.admin_instance_id() == resolve_instance_id()
    assert adh.admin_owner_principal() == resolve_owner_user_id()
