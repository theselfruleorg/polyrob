"""112 W2: /status shows the last model tiers chosen, and why."""
from core.status_types import Section
from core.status_work import _model_route_lines


def test_last_three_routes_newest_first():
    rows = [{"kind": "model_routed", "attrs": {"kind": "child", "tier": "heavy",
                                                "model": f"m{i}", "why": f"why {i}"}}
            for i in range(5)]
    tele = Section(name="telemetry", data={"rows": rows})
    sec = Section(name="work")
    _model_route_lines(sec, tele)
    assert [r["model"] for r in sec.data["model_routes"]] == ["m0", "m1", "m2"]
    assert sec.lines == ["model choices (newest first): child → heavy m0 (why 0); "
                         "child → heavy m1 (why 1); child → heavy m2 (why 2)"]


def test_no_routes_no_line():
    sec = Section(name="work")
    _model_route_lines(sec, Section(name="telemetry", data={"rows": []}))
    assert sec.lines == [] and sec.data["model_routes"] == []
