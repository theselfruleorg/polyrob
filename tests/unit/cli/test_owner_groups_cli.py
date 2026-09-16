"""`polyrob owner groups mode|set|role|tail|service` — CLI parity with the
Telegram `/groups` seat (044 T18). Both render through `core.surfaces.group_admin`
— except `service` (044 T20), whose helper is `cron.room_service` because core
may not import cron; the ONE-helper-per-verb contract is unchanged."""
from click.testing import CliRunner

from cli.commands.owner import owner
from core.instance import DEFAULT_INSTANCE_ID


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", DEFAULT_INSTANCE_ID)


def test_mode_round_trips(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    run = CliRunner()
    run.invoke(owner, ["groups", "allow", "telegram", "100"])
    res = run.invoke(owner, ["groups", "mode", "telegram", "100", "active"])
    assert res.exit_code == 0 and "active" in res.output


def test_mode_bad_value_names_the_vocabulary(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(owner, ["groups", "mode", "telegram", "100", "loud"])
    assert res.exit_code == 0
    assert "mention, active, listen, off" in res.output


def test_set_and_unset(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    run = CliRunner()
    res = run.invoke(owner, ["groups", "set", "telegram", "100", "chat.tone", "playful"])
    assert res.exit_code == 0 and "playful" in res.output
    from core.surfaces.chat_policy import load
    assert load(tmp_path, "alice", "telegram", "100").tone == "playful"
    res = run.invoke(owner, ["groups", "set", "telegram", "100", "chat.tone", "unset"])
    assert res.exit_code == 0 and "unset" in res.output.lower()
    assert load(tmp_path, "alice", "telegram", "100").tone == ""


def test_role_round_trips(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(owner, ["groups", "role", "telegram", "100", "9911", "admin"])
    assert res.exit_code == 0 and "admin" in res.output
    from core.surfaces.group_roles import GroupRoles
    import os
    roles = GroupRoles(os.path.join(str(tmp_path), "surfaces.db"))
    assert roles.role("telegram", "100", "9911", is_owner=False) == "admin"


def test_role_bad_value_names_the_vocabulary(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(owner, ["groups", "role", "telegram", "100", "9911", "superadmin"])
    assert res.exit_code == 0
    assert "member" in res.output and "blocked" in res.output


def test_tail_empty(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(owner, ["groups", "tail", "telegram", "100"])
    assert res.exit_code == 0 and "no ledger rows" in res.output.lower()


def test_service_creates_and_off_stops_the_job(tmp_path, monkeypatch):
    """044 T20: the CLI seat renders the SAME `cron.room_service.service`
    sentence the Telegram `/groups service` seat does, and really schedules."""
    _env(tmp_path, monkeypatch)
    run = CliRunner()
    # 044 T20 fix round 1 (Minor 10): a room the agent is not IN is refused.
    res = run.invoke(owner, ["groups", "service", "telegram", "100"])
    assert res.exit_code == 0 and "not an allowed room" in res.output
    run.invoke(owner, ["groups", "allow", "telegram", "100"])

    res = run.invoke(owner, ["groups", "service", "telegram", "100",
                             "--every", "1h", "--max", "5"])
    assert res.exit_code == 0, res.output
    assert "Servicing telegram:100 every 1h" in res.output
    from cron.jobs import CronJobStore
    jobs = CronJobStore(str(tmp_path / "cron.db")).list()
    assert len(jobs) == 1 and jobs[0].payload["max_replies"] == 5

    res = run.invoke(owner, ["groups", "service", "telegram", "100", "--every", "off"])
    assert res.exit_code == 0 and "stopped" in res.output.lower()
    assert [j.status for j in CronJobStore(str(tmp_path / "cron.db")).list()] == ["cancelled"]


# ---------------------------------------------------------------------------
# 044 T18 fix round 1 (Important 5) — a real (negative) Telegram chat id
# ---------------------------------------------------------------------------

def test_groups_help_documents_the_dash_workarounds(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(owner, ["groups", "--help"])
    assert res.exit_code == 0
    assert "--" in res.output and "_1001234567890" in res.output


def test_every_groups_subcommand_help_mentions_the_workaround(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    run = CliRunner()
    for cmd in ("allow", "deny", "mode", "set", "role", "tail", "service"):
        res = run.invoke(owner, ["groups", cmd, "--help"])
        assert res.exit_code == 0, (cmd, res.output)
        assert "--" in res.output, f"{cmd} --help does not mention --"


def test_a_bare_negative_chat_id_gets_a_helpful_error_naming_dash_dash(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(owner, ["groups", "mode", "telegram", "-1001234", "active"])
    assert res.exit_code != 0
    assert "--" in res.output
    assert "_1001234567890" in res.output  # the fixed illustrative example
    assert "No such option" not in res.output


def test_dash_dash_lets_the_real_negative_id_through(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(owner, ["groups", "mode", "telegram", "--",
                                     "-1001234567890", "active"])
    assert res.exit_code == 0 and "active" in res.output


def test_underscore_alias_round_trips_to_the_real_negative_id(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    run = CliRunner()
    res = run.invoke(owner, ["groups", "mode", "telegram", "_1001234567890", "active"])
    assert res.exit_code == 0
    assert "-1001234567890" in res.output
    from core.surfaces.chat_policy import load
    assert load(tmp_path, "alice", "telegram", "-1001234567890").mode == "active"


def test_a_genuine_bad_flag_is_unaffected(tmp_path, monkeypatch):
    """The Important-5b interception is scoped to a `-<digits>` shape only —
    an actual typo'd option must still fail exactly as before."""
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(owner, ["groups", "mode", "--bogus-flag", "x"])
    assert res.exit_code != 0
    assert "No such option" in res.output


# ---------------------------------------------------------------------------
# 044 C7 — allow/deny/list must go through group_admin's normalizer too
# ---------------------------------------------------------------------------

def test_allow_with_the_underscore_alias_really_allows_the_negative_room(tmp_path, monkeypatch):
    """`allow`/`deny`/`list` used to talk to `GroupAllowlist` DIRECTLY, skipping
    `_norm_chat_id`. The alias this very command group advertises therefore wrote
    a row under the LITERAL `_100…` string — a row routing (which sees `-100…`)
    can never match, while `list` reported the room as active. The owner was told
    a room was allowed while every line from it was dropped."""
    _env(tmp_path, monkeypatch)
    run = CliRunner()
    res = run.invoke(owner, ["groups", "allow", "telegram", "_1001234567890",
                             "--note", "dev room"])
    assert res.exit_code == 0, res.output
    assert "-1001234567890" in res.output

    from core.surfaces.group_allowlist import GroupAllowlist
    import os
    store = GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db"))
    assert store.is_allowed("telegram", "-1001234567890") is True
    assert store.is_allowed("telegram", "_1001234567890") is False


def test_list_renders_the_same_text_the_telegram_seat_renders(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    run = CliRunner()
    run.invoke(owner, ["groups", "allow", "telegram", "_1001234567890", "--note", "dev room"])
    res = run.invoke(owner, ["groups", "list"])
    assert res.exit_code == 0
    assert "-1001234567890" in res.output
    assert "dev room" in res.output          # the note became the room's chat.name
    assert "mode=" in res.output             # group_admin's own rendering

    from core.surfaces import group_admin
    import types
    container = types.SimpleNamespace(
        config=types.SimpleNamespace(data_dir=str(tmp_path)),
        get_service=lambda _n: None)
    assert group_admin.list_rooms(container, "alice").strip() in res.output


def test_deny_with_the_alias_revokes_the_room_it_allowed(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    run = CliRunner()
    run.invoke(owner, ["groups", "allow", "telegram", "_1001234567890"])
    res = run.invoke(owner, ["groups", "deny", "telegram", "_1001234567890"])
    assert res.exit_code == 0 and "Denied" in res.output

    from core.surfaces.group_allowlist import GroupAllowlist
    import os
    store = GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db"))
    assert store.is_allowed("telegram", "-1001234567890") is False


def test_list_says_default_deny_when_empty(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(owner, ["groups", "list"])
    assert res.exit_code == 0 and "default-DENY" in res.output
