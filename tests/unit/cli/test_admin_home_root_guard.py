"""057 WS-G: the owner-CLI euid guard.

A root-run owner verb writes root-owned rows into a home three de-rooted units
share; the service meets them hours later as "attempt to write a readonly
database" in a unit that did nothing wrong. The refusal lives at the ONE data-
home seam, and it applies ONLY on a box that actually has a deployed instance —
a dev checkout shares nothing.
"""
import click
import pytest

import cli._admin_home as ah


@pytest.fixture(autouse=True)
def _clean():
    ah.reset_root_guard_state()
    yield
    ah.reset_root_guard_state()


@pytest.fixture
def as_root(monkeypatch):
    monkeypatch.setattr(ah, "_is_root", lambda: True)
    monkeypatch.setattr(ah, "_deployed_box", lambda: True)


def test_root_write_on_a_deployed_box_refuses_with_the_remedy(as_root):
    with pytest.raises(click.ClickException) as e:
        ah.check_root_write("/var/lib/polyrob", True)
    msg = str(e.value)
    assert "sudo -u polyrob-agent" in msg
    assert "--as-root" in msg and ah.ALLOW_ROOT_ENV in msg


def test_root_read_is_never_refused(as_root, capsys):
    ah.check_root_write("/var/lib/polyrob", False)   # doctor and friends
    assert capsys.readouterr().err == ""


def test_unknown_intent_warns_once_and_proceeds(as_root, capsys):
    ah.check_root_write("/var/lib/polyrob", None)
    ah.check_root_write("/var/lib/polyrob", None)
    err = capsys.readouterr().err
    assert err.count("running as root") == 1
    assert "cannot tell whether this verb writes" in err


def test_as_root_flag_lifts_the_refusal(as_root, capsys):
    ah._set_as_root(None, None, True)
    ah.check_root_write("/var/lib/polyrob", True)
    ah.check_root_write("/var/lib/polyrob", None)
    assert capsys.readouterr().err == ""


def test_env_escape_hatch_lifts_the_refusal(as_root, monkeypatch):
    monkeypatch.setenv(ah.ALLOW_ROOT_ENV, "1")
    ah.check_root_write("/var/lib/polyrob", True)


def test_a_dev_checkout_is_never_guarded(monkeypatch, capsys):
    """No deployed instance means no shared home and nothing to protect."""
    monkeypatch.setattr(ah, "_is_root", lambda: True)
    monkeypatch.setattr(ah, "_deployed_box", lambda: False)
    ah.check_root_write("/tmp/whatever", True)
    assert capsys.readouterr().err == ""


def test_a_non_root_user_is_never_guarded(monkeypatch, capsys):
    monkeypatch.setattr(ah, "_is_root", lambda: False)
    monkeypatch.setattr(ah, "_deployed_box", lambda: True)
    ah.check_root_write("/var/lib/polyrob", True)
    assert capsys.readouterr().err == ""


def test_deployed_box_probe_is_the_031_evidence(monkeypatch):
    import core.admin_data_home as adh
    monkeypatch.setattr(adh, "_deployment_evidence", lambda: [])
    assert ah._deployed_box() is False
    monkeypatch.setattr(adh, "_deployment_evidence", lambda: ["polyrob.service"])
    assert ah._deployed_box() is True


def test_admin_data_dir_applies_the_guard(monkeypatch):
    monkeypatch.setattr(ah, "_is_root", lambda: True)
    monkeypatch.setattr(ah, "_deployed_box", lambda: True)
    monkeypatch.setattr("core.admin_data_home.admin_data_home",
                        lambda echo=None: "/var/lib/polyrob")
    assert ah.admin_data_dir(write=False) == "/var/lib/polyrob"
    with pytest.raises(click.ClickException):
        ah.admin_data_dir(write=True)


def test_pause_verb_declares_a_write_and_doctor_declares_a_read():
    """The wiring is the point: a guard no verb consults protects nothing."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    autonomy = (root / "cli/commands/autonomy.py").read_text()
    assert "pause_autonomy(_data_dir(write=True)" in autonomy
    assert "resume_autonomy_scopes(_data_dir(write=True)" in autonomy
    assert "_data_dir(write=False)" in autonomy          # status stays readable
    assert "@as_root_option" in autonomy
    doctor = (root / "cli/commands/doctor.py").read_text()
    assert "admin_data_dir(write=False)" in doctor
