"""096 finish: email names chat taps, inbox bodies keep their lines, the job
list tail is a /more tap."""
from core.surfaces import inbox_render, tappable


def test_email_footer_added_once_when_a_tap_is_present():
    body = "1 change\n/approve_p_62f952 · /reject_p_62f952 · read it: /pending_p_62f952"
    out = tappable.email_taps_footer(body)
    assert out.startswith(body) and tappable.EMAIL_TAP_FOOTER in out
    assert tappable.email_taps_footer(out) == out


def test_email_footer_absent_without_taps():
    assert tappable.email_taps_footer("Goal done. See /status? no") == "Goal done. See /status? no"


def test_inbox_wrap_keeps_lines_and_hangs_bullets():
    text = "Skill 'x' — update\nWhy: the run posted without approval\n• " + "word " * 20
    lines = inbox_render._wrap(text, "    ", 40)
    assert lines[0].strip() == "Skill 'x' — update"
    assert lines[1].strip().startswith("Why:")
    bullet = [l for l in lines if l.strip().startswith("•")][0]
    cont = lines[lines.index(bullet) + 1]
    assert cont.startswith("      ") and not cont.strip().startswith("•")


def test_job_list_tail_is_a_more_tap(monkeypatch):
    from surfaces.telegram import job_ops

    class J:
        def __init__(self, i):
            self.id, self.status, self.task = f"job{i:04d}abcd", "scheduled", f"task {i}"

    class Svc:
        def list_jobs(self, user_id):
            return [J(i) for i in range(job_ops._LIST_LIMIT + 3)]

    monkeypatch.setattr(job_ops, "_row", lambda j: j.id)  # row shape not under test
    monkeypatch.setattr(job_ops, "_stopped", lambda svc, uid: "")
    out = job_ops._list(Svc(), "owner")
    assert "/more_" in out and "see the console" not in out
