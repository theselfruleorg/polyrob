"""116 W8 — the owner's clock: one zone (``owner.timezone``), one formatter.

The agent told the owner yesterday's date because every clock it read was UTC and no
zone was used anywhere but quiet hours (116 P15)."""
import time
from datetime import datetime, timezone

import pytest

from core import owner_time
from core.owner_time import owner_clock, owner_day, owner_zone, stamp
from core.prefs import PREF_SCHEMA, load_preferences, preferences_path, write_preference

# 2026-11-11 18:03:00Z — 01:03 the next day in Asia/Bangkok (UTC+7).
TS = datetime(2026, 11, 11, 18, 3, tzinfo=timezone.utc).timestamp()


def test_stamp_shows_both_clocks_and_names_the_owner_date_when_it_differs():
    assert stamp(TS, "Asia/Bangkok") == "11-11 18:03Z (11-12 01:03 +07)"
    noon = TS - 8 * 3600  # 10:03Z = 17:03 the same day
    assert stamp(noon, "Asia/Bangkok") == "11-11 10:03Z (17:03 +07)"
    assert stamp(noon, "Asia/Kolkata") == "11-11 10:03Z (15:33 +05:30)"
    assert stamp(noon, "America/Sao_Paulo") == "11-11 10:03Z (07:03 -03)"


def test_stamp_without_a_zone_is_byte_equal_to_the_utc_form():
    for zone in (None, "", "Mars/Olympus", "UTC", "Etc/UTC"):
        assert stamp(TS, zone) == time.strftime("%m-%d %H:%MZ", time.gmtime(TS))
    assert stamp(TS, None, utc_format="%H:%M UTC") == "18:03 UTC"
    assert stamp(TS, "Asia/Bangkok", utc_format="%H:%M UTC") == "18:03 UTC (11-12 01:03 +07)"


@pytest.mark.parametrize("bad", [None, "", 0, "x", object()])
def test_stamp_of_a_missing_or_bad_time_is_the_missing_text(bad):
    assert stamp(bad, "Asia/Bangkok") == "?"
    assert stamp(bad, None, missing="-") == "-"


def test_a_naive_datetime_is_the_server_clock_and_an_aware_one_is_kept():
    aware = datetime.fromtimestamp(TS, timezone.utc)
    assert stamp(aware, "Asia/Bangkok") == stamp(TS, "Asia/Bangkok")
    naive_local = datetime.fromtimestamp(TS)  # what cron stores
    assert stamp(naive_local, "Asia/Bangkok") == stamp(TS, "Asia/Bangkok")


def test_owner_clock_is_the_prompt_form_and_empty_without_a_zone():
    assert owner_clock(TS, "Asia/Bangkok") == "2026-11-12 01:03 Asia/Bangkok (UTC+7)"
    assert owner_clock(TS, "Asia/Kolkata") == "2026-11-11 23:33 Asia/Kolkata (UTC+5:30)"
    assert owner_clock(TS, None) == "" and owner_clock(TS, "Mars/Olympus") == ""


def test_owner_day_is_the_owner_calendar_day_and_utc_without_a_zone():
    day, start, end = owner_day(TS, "Asia/Bangkok")
    assert day == "2026-11-12"
    assert start == datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc).timestamp()
    assert end - start == 86400
    day, start, end = owner_day(TS, None)
    assert (day, end - start) == ("2026-11-11", 86400)
    # A DST day is 25 h (Europe/Berlin, 2026-10-25).
    _d, s, e = owner_day(datetime(2026, 10, 25, 12, tzinfo=timezone.utc).timestamp(),
                         "Europe/Berlin")
    assert e - s == 25 * 3600


def test_owner_zone_reads_the_new_key_and_the_old_name(tmp_path):
    assert owner_zone("u1", tmp_path) is None
    ok, err = write_preference(tmp_path, "u1", "owner.timezone", "Asia/Bangkok")
    assert ok, err
    assert owner_zone("u1", tmp_path) == "Asia/Bangkok"
    # D5: the old name still writes — to the new key.
    ok, err = write_preference(tmp_path, "u1", "digest.timezone", "Europe/Berlin")
    assert ok, err
    assert owner_zone("u1", tmp_path) == "Europe/Berlin"
    text = preferences_path(tmp_path, "u1").read_text()
    assert "[owner]" in text and "digest" not in text


def test_a_file_that_holds_the_old_name_is_read_as_the_new_key(tmp_path):
    path = preferences_path(tmp_path, "u1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[digest]\ntimezone = "Asia/Bangkok"\n')
    assert load_preferences(tmp_path, "u1") == {"owner.timezone": "Asia/Bangkok"}
    assert owner_zone("u1", tmp_path) == "Asia/Bangkok"
    # Both names on disk: the new one wins, whatever the order.
    path.write_text('[owner]\ntimezone = "Europe/Berlin"\n\n[digest]\ntimezone = "Asia/Bangkok"\n')
    assert load_preferences(tmp_path, "u1") == {"owner.timezone": "Europe/Berlin"}


def test_the_old_name_finds_the_spec_but_is_not_listed():
    assert PREF_SCHEMA["digest.timezone"] is PREF_SCHEMA["owner.timezone"]
    assert "digest.timezone" in PREF_SCHEMA
    assert PREF_SCHEMA.get("digest.timezone").key == "owner.timezone"
    assert "digest.timezone" not in list(PREF_SCHEMA) and "owner.timezone" in list(PREF_SCHEMA)


def test_owner_zone_with_no_scope_reads_the_owner_scope(tmp_path, monkeypatch):
    from core import prefs
    write_preference(tmp_path, "owner1", "owner.timezone", "Asia/Bangkok")
    monkeypatch.setattr(prefs, "owner_pref_scope", lambda: ("owner1", str(tmp_path)))
    assert owner_zone() == "Asia/Bangkok"
    assert stamp(TS) == "11-11 18:03Z (11-12 01:03 +07)"  # zone=AUTO resolves it
    monkeypatch.setattr(prefs, "owner_pref_scope", lambda: (None, ""))
    assert owner_zone() is None


def test_quiet_hours_reads_the_moved_resolver():
    from core.surfaces import quiet_hours
    assert quiet_hours.effective_timezone is owner_time.owner_zone
    assert quiet_hours.TIMEZONE_PREF == "owner.timezone"
