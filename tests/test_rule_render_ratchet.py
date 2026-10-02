"""060 §6 ratchet — every STANDING and SCOPED instruction surface is reachable
from the ONE status snapshot (the `rules` section), so a rule that is written
somewhere always has a seat that can say whether it is in effect."""
from core.instruction_surfaces import INSTRUCTION_SURFACES, SCOPED, STANDING
from core.status_rules import rules_section


def test_standing_and_scoped_surfaces_render(tmp_path):
	sec = rules_section("u1", str(tmp_path), str(tmp_path / "cron.db"))
	reported = set(sec.data["surfaces"])
	need = {s.name for s in INSTRUCTION_SURFACES if s.tier in (STANDING, SCOPED)}
	assert need <= reported, f"not reported by the rules section: {sorted(need - reported)}"
	for name in need:
		assert sec.data[name]["surface"] == name
