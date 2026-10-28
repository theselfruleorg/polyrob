"""093 D1: the owner's /status names every gated tool (the snapshot's `harness` slot)."""
import tools  # noqa: F401  (registers the slot)
import core.status_sections as S
import tools.harness_view as hv
import tools.status_harness as H
from core.status_render import render_section_lines
from core.status_snapshot import STATE_OK, STATE_UNAVAILABLE, build_status_snapshot


def _snap(tmp_path, container):
    return build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False,
                                 container=container)


def test_the_tools_tier_fills_the_slot():
    prov = S.provider("harness")
    assert prov is not None and prov.source == "tools.status_harness" and not prov.tenant


def test_no_container_is_stated_not_omitted(tmp_path):
    sec = _snap(tmp_path, None).sections["harness"]
    assert (sec.state, sec.lines) == (STATE_OK, [H.NO_CONTAINER])


def test_nothing_gated_is_one_line(monkeypatch, tmp_path):
    monkeypatch.setattr(hv, "gated_tools_block", lambda controller, ids=None: "")
    sec = _snap(tmp_path, object()).sections["harness"]
    assert sec.lines == [H.ALL_USABLE] and sec.data["gated"] == 0


def test_the_owner_seat_view_and_grouped_lines(monkeypatch, tmp_path):
    seen = {}

    def block(controller, ids=None):
        seen["c"] = controller
        return ("harness (gated tools; agent_status(tool=\"<id>\") for one tool's remedy):\n"
                "- x402_pay: gated:autonomous-ceiling · who=owner-verb · when=now\n"
                "- shell: gated:backend-unavailable · who=operator-env · when=restart\n"
                "- mcp: gated:unavailable-on-this-deploy · who=operator-env · when=restart\n"
                "- goal: gated:unavailable-on-this-deploy · who=operator-env · when=restart\n"
                "- odd: unavailable (RuntimeError)\n"
                "- money tools (defi_trade, x402_pay): gated:money · who=owner-verb · "
                "when=new-session")
    monkeypatch.setattr(hv, "gated_tools_block", block)
    container = object()
    snap = _snap(tmp_path, container)
    sec = snap.sections["harness"]
    c = seen["c"]
    assert c.container is container and c.list_tools() == []
    assert c._is_sub_agent is False and c.session_id is None
    assert sec.state == STATE_OK and not sec.health
    assert sec.lines == [
        "7 tool(s) gated (reason · who can fix it · when the fix applies)",
        "shell: backend-unavailable · who=operator-env · when=restart",
        "mcp, goal: unavailable-on-this-deploy · who=operator-env · when=restart",
        "odd: unavailable (RuntimeError)",
        # policy, not a deploy problem: last, one line per reason
        "x402_pay: autonomous-ceiling · who=owner-verb · when=now",
        "defi_trade, x402_pay: money · who=owner-verb · when=new-session",
    ]
    rendered = render_section_lines(snap, "harness", prefix="- ")
    assert rendered[0].startswith("- Tool gates: 7 tool(s) gated")


def test_a_failing_read_is_reported_unavailable(monkeypatch, tmp_path):
    def boom(controller, ids=None):
        raise RuntimeError("catalog unreadable")
    monkeypatch.setattr(hv, "gated_tools_block", boom)
    snap = _snap(tmp_path, object())
    sec = snap.sections["harness"]
    assert sec.state == STATE_UNAVAILABLE and "catalog unreadable" in sec.reason
    assert any(u.startswith("harness (") for u in snap.unavailable_sources)


def test_the_real_resolver_with_an_empty_container(tmp_path):
    """No stub: an empty container gates the unconstructed tools; money is one line."""
    class _Empty:
        config = None

        def get_service(self, name):
            return None

        def has_service(self, name):
            return False
    sec = _snap(tmp_path, _Empty()).sections["harness"]
    assert sec.state == STATE_OK and sec.data["gated"] > 0
    money = [ln for ln in sec.lines if ": money · " in ln]
    assert len(money) <= 1
