"""093 D6 — `/update now`: the owner lets a waiting deploy skip the owner-quiet window.

Quote → card → `go` writes ONE marker (`core.lifecycle.request_owner_deploy_now`)
that `scripts/deploy_when_idle.sh` reads; the verb stays an owner admin verb,
refused in a room. The waiter side is tested in
`ops/tests/deployment/test_deploy_when_idle_owner_now.py`.
"""
import asyncio
import json
import time

import pytest

from core import lifecycle as lc
from core.surfaces import cards

ORIGIN = {"surface": "telegram", "chat_id": "42", "user_id": "u1"}


def _never_checked(channel):
    raise AssertionError("/update now never runs the release check")


def _reply(home, args):
    from surfaces.telegram.lifecycle_ops import update_reply
    return asyncio.run(update_reply("u1", str(home), args, ORIGIN, check=_never_checked))


def test_update_now_quotes_first_and_writes_nothing(tmp_path):
    text = _reply(tmp_path, ["now"])
    assert "does not wait for your chat to be quiet" in text
    assert "running goal" in text and "cron" in text  # the other checks stay, and it says so
    assert text.rstrip().endswith("/update now go")
    assert lc.read_owner_deploy_now(data_home=tmp_path) is None


def test_update_now_card_confirms_with_the_now_line(tmp_path):
    st = cards.CardStore(str(tmp_path / "c.db"))
    _, card = cards.quote_card("u1", "/update", ["now"], _reply(tmp_path, ["now"]), st=st)
    assert card is not None and card.confirm_line == "/update now go"


def test_update_now_go_writes_the_owner_marker(tmp_path):
    out = _reply(tmp_path, ["now", "go"])
    assert out.startswith("OK — a deploy no longer waits for your chat to go quiet")
    rec = lc.read_owner_deploy_now(data_home=tmp_path)
    assert rec["requested_by"] == "owner" and rec["origin"]["chat_id"] == "42"
    raw = json.loads((tmp_path / lc.OWNER_DEPLOY_NOW_FILE).read_text())
    assert raw["expires_at"] - raw["at"] == pytest.approx(lc.OWNER_DEPLOY_NOW_TTL_SEC)
    # the order of the words does not matter
    assert _reply(tmp_path, ["go", "now"]).startswith("OK")


def test_an_expired_marker_reads_as_none(tmp_path):
    lc.request_owner_deploy_now(data_home=tmp_path, ttl_sec=-1)
    assert lc.read_owner_deploy_now(data_home=tmp_path) is None


@pytest.mark.parametrize("args", [["now", "pre"], ["later"], ["now", "x"]])
def test_bad_update_args_give_the_usage(tmp_path, args):
    from surfaces.telegram.lifecycle_ops import UPDATE_USAGE
    assert _reply(tmp_path, args) == UPDATE_USAGE
    assert "/update now" in UPDATE_USAGE


def test_update_stays_an_owner_verb_refused_in_a_room():
    from surfaces.telegram import harness
    assert "/update" in harness._OWNER_ADMIN_COMMANDS
    assert "/update" in harness._ROOM_REFUSED_COMMANDS


def test_the_verb_row_names_update_now():
    from core.verbs import _CORE_TABLE, room_refused
    row = next(v for v in _CORE_TABLE if v.name == "/update")
    assert "/update now" in row.help and room_refused("/update")
