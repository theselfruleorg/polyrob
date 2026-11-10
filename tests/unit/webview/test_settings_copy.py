"""113 W5 item 1 (CP-1) — Agent › Settings speaks the owner's words.

Settings showed the developer notes of ``core/prefs.py`` as labels ("(018 P0.2)",
"wired into PolicyGate via load_wallet_config()"). Every setting now has an owner
label and a help line in the copy table (``webview/copy/en_prefs.py``); the API
sends those and no developer note; this test fails on a setting with no words.
"""
import re
from pathlib import Path

import pytest

from core.prefs import PREF_SCHEMA
from webview.copy import STRINGS, t

WEBVIEW = Path(__file__).resolve().parents[3] / "webview"

#: Words that belong to a developer note, never to an owner label.
_DEV_NOTE = re.compile(r"\b\d{3}\b|\bP\d\.\d\b|_|PolicyGate|load_wallet_config|env\b|pref\b")


@pytest.mark.parametrize("key", sorted(PREF_SCHEMA))
def test_every_setting_has_an_owner_label_and_help(key):
    label = STRINGS.get(f"pref.{key}.label")
    help_ = STRINGS.get(f"pref.{key}.help")
    assert label, f"pref.{key}.label is missing from webview/copy/en_prefs.py"
    assert help_, f"pref.{key}.help is missing from webview/copy/en_prefs.py"
    assert label != PREF_SCHEMA[key].description
    assert not _DEV_NOTE.search(label), (key, label)
    assert len(label) <= 60, (key, label)


def test_no_words_for_a_setting_that_does_not_exist():
    keys = {k[len("pref."):].rsplit(".", 1)[0] for k in STRINGS if k.startswith("pref.")}
    assert keys == set(PREF_SCHEMA), keys ^ set(PREF_SCHEMA)


def test_the_groups_say_when():
    assert t("agent.set_group_now") == "Takes effect now"
    assert t("agent.set_group_next_chat") == "In the next chat"
    agent = (WEBVIEW / "templates" / "agent.html").read_text()
    for key in ("set_group_now", "set_group_next_chat", "set_group_restart"):
        assert f"data-{key}=\"{{{{ t('agent.{key}') }}}}\"" in agent


def test_the_api_sends_the_label_and_not_the_developer_note():
    src = (WEBVIEW / "pages.py").read_text()
    body = src.split("async def api_preferences(", 1)[1].split("return JSONResponse", 1)[0]
    assert 't(f"pref.{key}.label")' in body and 't(f"pref.{key}.help")' in body
    assert "spec.description" not in body
