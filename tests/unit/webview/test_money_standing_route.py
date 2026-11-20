"""Money > Limits reads the owner's standing rules through the status sections."""
import os

from webview.money_standing_routes import STANDING_SECTIONS, standing_body


def test_the_body_is_the_two_status_sections(tmp_path):
    body = standing_body(str(tmp_path))
    assert [s["name"] for s in body["sections"]] == list(STANDING_SECTIONS)
    assert all(s["state"] == "ok" for s in body["sections"])


def test_an_unreadable_store_is_the_sections_unavailable_state(tmp_path):
    os.makedirs(tmp_path / "wallet")
    (tmp_path / "wallet" / "mandates.json").write_text("{")
    by = {s["name"]: s for s in standing_body(str(tmp_path))["sections"]}
    assert by["mandates"]["state"] == "unavailable" and by["mandates"]["reason"]
    assert by["income_rule"]["state"] == "ok"
