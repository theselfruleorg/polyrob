"""060 §6 ratchet — the instruction-surface count does not grow.

Six places can hold an instruction (060 §2.1). The defect 060 names is that
RULES live in all six; a seventh place to put a rule is the accretion this
file refuses. Add one only by deleting one, or by changing this pin in the
same commit with the reason.
"""
from core.instruction_surfaces import (INSTRUCTION_SURFACES, SCOPED, STANDING,
                                       SURFACE_NAMES, TIERS, VOLATILE)

CEILING = 6


def test_the_surface_count_does_not_grow():
	assert len(INSTRUCTION_SURFACES) <= CEILING, (
		f"{len(INSTRUCTION_SURFACES)} instruction surfaces vs {CEILING}: a new place "
		"to put a rule — fold it into an existing surface instead")


def test_names_unique_and_tiers_known():
	assert len(set(SURFACE_NAMES)) == len(SURFACE_NAMES)
	for s in INSTRUCTION_SURFACES:
		assert s.tier in TIERS and s.tier != VOLATILE, s


def test_owner_reviewed_surfaces_are_the_standing_ones():
	"""Rules belong in the two owner-reviewed surfaces (060 §8)."""
	reviewed = {s.name for s in INSTRUCTION_SURFACES if s.owner_reviewed}
	assert reviewed == {s.name for s in INSTRUCTION_SURFACES if s.tier == STANDING}
	assert {s.name for s in INSTRUCTION_SURFACES if s.tier == SCOPED}
