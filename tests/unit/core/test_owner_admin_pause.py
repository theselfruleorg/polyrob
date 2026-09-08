"""031 T2: the legacy halt/entry/stream writers are facets of the ONE record."""
import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    for k in ("AUTONOMY_HALT", "TREASURY_ENTRY_PAUSE", "STREAM_SEEDING_PAUSE", "DATA_ROOT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    return tmp_path


def test_legacy_halt_api_writes_the_one_record(home):
    from core.surfaces import owner_admin as oa
    from core.autonomy_control import PAUSE_FILENAME
    effective, written = oa.halt_autonomy(str(home), reason="t")
    assert effective is True
    assert written == [str(home / PAUSE_FILENAME)]
    assert not (home / "AUTONOMY_HALT").exists()
    assert oa.halt_active() is True
    still, removed = oa.resume_autonomy(str(home))
    assert still is False and removed == [str(home / PAUSE_FILENAME)]


def test_entry_and_stream_pauses_are_scopes(home):
    from core.surfaces import owner_admin as oa
    from core.autonomy_control import read_state
    oa.pause_entries(str(home))
    oa.pause_stream_seeding(str(home))
    assert set(read_state(str(home)).scopes) == {"trading", "streams"}
    assert oa.entry_pause_active() and oa.stream_seeding_paused_active() and not oa.halt_active()
    oa.resume_entries(str(home))
    assert read_state(str(home)).scopes == ("streams",)


def test_autonomy_config_predicates_delegate(home):
    from core.config_policy import AutonomyConfig
    from core.surfaces import owner_admin as oa
    assert AutonomyConfig.autonomy_halted() is False
    oa.pause_autonomy(str(home), scopes=("trading",))
    assert AutonomyConfig.entry_paused() is True
    assert AutonomyConfig.autonomy_halted() is False
    oa.pause_autonomy(str(home))
    assert AutonomyConfig.autonomy_halted() is True
    assert AutonomyConfig.stream_seeding_paused() is True


def test_legacy_touch_files_still_work_as_facets(home):
    from core.config_policy import AutonomyConfig
    (home / "AUTONOMY_HALT").write_text("")
    assert AutonomyConfig.autonomy_halted() is True
    (home / "AUTONOMY_HALT").unlink()
    (home / "TREASURY_ENTRY_PAUSE").write_text("")
    assert AutonomyConfig.entry_paused() is True
    assert AutonomyConfig.autonomy_halted() is False


def test_render_pause_result_reports_verified_state(home):
    from core.surfaces import owner_admin as oa
    res = oa.pause_autonomy(str(home), scopes=("all",), reason="stop", via="telegram")
    text = oa.render_pause_result(res, resume_hint="/resume")
    assert text.startswith("⏸ Paused everything")
    assert "via telegram" in text and "/resume" in text
    oa.resume_autonomy_scopes(str(home))
    res2 = oa.pause_autonomy(str(home), scopes=("cron",), duration_minutes=90, via="cli")
    text2 = oa.render_pause_result(res2, resume_hint="polyrob autonomy resume")
    assert "cron" in text2 and "90 min" in text2


def test_render_resume_result_states(home, monkeypatch):
    from core.surfaces import owner_admin as oa
    res = oa.resume_autonomy_scopes(str(home))
    assert oa.render_resume_result(res, halt_hint="/pause") == "Autonomy was not paused."
    oa.pause_autonomy(str(home), scopes=("cron", "streams"))
    res = oa.resume_autonomy_scopes(str(home), scopes=("cron",))
    assert "still paused: streams" in oa.render_resume_result(res, halt_hint="/pause")
    res = oa.resume_autonomy_scopes(str(home))
    assert oa.render_resume_result(res, halt_hint="/pause").startswith("▶ Autonomy RESUMED")
    monkeypatch.setenv("AUTONOMY_HALT", "true")
    res = oa.resume_autonomy_scopes(str(home))
    assert "ENVIRONMENT" in oa.render_resume_result(res, halt_hint="/pause")


def test_owner_pause_phrases_reads_the_pref_and_fails_open(home, monkeypatch):
    from core.surfaces import owner_admin as oa
    assert oa.owner_pause_phrases("rob", str(home)) == ()
    monkeypatch.setattr("core.prefs.resolve", lambda key, uid, hd, **kw: ["ghosts", "bots"])
    assert oa.owner_pause_phrases("rob", str(home)) == ("ghosts", "bots")
    monkeypatch.setattr("core.prefs.resolve", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert oa.owner_pause_phrases("rob", str(home)) == ()


def test_apply_owner_intent_is_the_one_decision_path(home):
    """The Telegram gate and the REPL gate are thin over this."""
    from core.surfaces import owner_admin as oa
    from core.surfaces.owner_intent import owner_stop_intent
    from core.autonomy_control import read_state
    dd = str(home)
    assert oa.apply_owner_intent(None, dd, via="t", resume_hint="/r", halt_hint="/p") == (None, None)
    # "continue" with nothing paused is chat
    assert oa.apply_owner_intent(owner_stop_intent("continue"), dd, via="t", resume_hint="/r",
                                 halt_hint="/p") == (None, None)
    # scoped goes to the agent unless a safe-default reason is given
    scoped = owner_stop_intent("stop trading")
    assert oa.apply_owner_intent(scoped, dd, via="t", resume_hint="/r", halt_hint="/p") == (None, None)
    reply, res = oa.apply_owner_intent(scoped, dd, via="t", resume_hint="/r", halt_hint="/p",
                                       force_full_reason="My model is unavailable")
    assert reply.startswith("⚠️ My model is unavailable") and res.effective
    assert read_state(dd).scopes == ("all",) and read_state(dd).reason == "stop trading"
    oa.resume_autonomy_scopes(dd)
    # full stop with a prose duration
    reply, res = oa.apply_owner_intent(owner_stop_intent("stop everything for 2 hours"), dd,
                                       via="repl", resume_hint="/resume", halt_hint="/pause")
    assert reply.startswith("⏸ Paused everything") and "120 min" in reply
    assert read_state(dd).via == "repl"
    # resume lifts it
    reply, res = oa.apply_owner_intent(owner_stop_intent("resume"), dd, via="repl",
                                       resume_hint="/resume", halt_hint="/pause")
    assert reply.startswith("▶ Autonomy RESUMED") and not read_state(dd).paused


def test_render_results_are_seat_aware_and_honest(home, monkeypatch):
    from core.surfaces import owner_admin as oa
    res = oa.pause_autonomy(str(home), scopes=("all",), via="cli")
    text = oa.render_pause_result(res, resume_hint="`polyrob autonomy resume`",
                                  status_hint="`polyrob autonomy status`", chat=False)
    assert "this chat" not in text and "/status" not in text and "`polyrob autonomy status`" in text
    # a scoped resume on a full pause is refused, and says so (never a ▶)
    res = oa.resume_autonomy_scopes(str(home), scopes=("trading",))
    text = oa.render_resume_result(res, halt_hint="/pause")
    assert text.startswith("⚠️ Resume NOT effective") and "full pause" in text
    oa.resume_autonomy_scopes(str(home))
    monkeypatch.setenv("STREAM_SEEDING_PAUSE", "true")
    res = oa.resume_autonomy_scopes(str(home), scopes=("streams",))
    text = oa.render_resume_result(res, halt_hint="/pause")
    assert "ENVIRONMENT" in text and "STREAM_SEEDING_PAUSE" in text


def test_owner_pause_phrases_splits_multi_word_entries(home, monkeypatch):
    from core.surfaces import owner_admin as oa
    monkeypatch.setattr("core.prefs.resolve", lambda key, uid, hd, **kw: ["dev loop", "Bots"])
    assert oa.owner_pause_phrases("rob", str(home)) == ("dev", "loop", "bots")
