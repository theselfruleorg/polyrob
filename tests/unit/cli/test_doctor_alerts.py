"""116 W5.10/W7.4 — doctor's own alerts join the snapshot's health list
before the headline: one fact is one item, and no alert follows an OK headline."""
from core.status_types import HealthItem, SEVERITY_CRIT, Section, StatusSnapshot

from cli.commands.doctor_alerts import fold_doctor_alerts

_NONE = ("  ! no provider credential found (no API key, no connected account) — "
         "run `polyrob setup` or `polyrob auth add <provider>`")
_BAD = "  ! no usable provider credential — malformed key"


def _snap(*items):
    return StatusSnapshot(user_id="u1", generated_at=0.0, window_sec=86400,
                          sections={"providers": Section(name="providers")},
                          health=list(items), unavailable_sources=[])


def _no_provider():
    return HealthItem(key="no_provider", severity=SEVERITY_CRIT,
                      text="NO LLM provider is configured", remedy="`polyrob auth add`")


def test_a_fact_the_snapshot_carries_is_not_repeated():
    snap = _snap(_no_provider())
    assert fold_doctor_alerts(snap, [_NONE], []) is snap


def test_an_unreadable_provider_read_is_not_contradicted():
    snap = _snap(HealthItem(key="providers_unreadable", text="could not read"))
    assert fold_doctor_alerts(snap, [_NONE], []) is snap


def test_the_reason_a_present_key_is_unusable_rides_the_snapshot_item():
    snap = _snap(_no_provider())
    out = fold_doctor_alerts(snap, [_BAD], [])
    assert [h.key for h in out.health] == ["no_provider"]
    assert "not usable: malformed key" in out.health[0].text
    assert snap.health[0].text == "NO LLM provider is configured"   # the cache is unchanged


def test_a_fact_only_doctor_knows_is_a_crit_item_before_the_headline():
    snap = _snap()
    out = fold_doctor_alerts(snap, [_NONE], ["CRITICAL data home split: x"])
    assert out.overall == "degraded" and snap.overall == "ok"
    keys = [h.key for h in out.health]
    assert keys == ["doctor_credentials", "host:0"]
    assert out.health[0].remedy.startswith("run `polyrob setup`")
    assert out.health[1].text == "data home split: x"
    assert all(h.severity == SEVERITY_CRIT for h in out.health)
