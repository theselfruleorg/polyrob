"""What a stored compaction summary must be: complete, bounded, honest about a gap.

A summary that grows to the output cap loses its later sections, and each compaction builds
on the damaged one, so an answer that is not complete is never stored as a good summary.

The completeness check reads the call's own facts and the answer's end line: the provider can
say that it cut the answer, and a model can stop early with no such signal. Pure functions:
no I/O, no LLM.
"""
import re
from typing import Dict, List, Optional, Tuple

from modules.llm.call_result import CallFacts, output_was_truncated

END_LINE = "## End of summary"
#: The size the prompt asks for. Output is the latency cost of a compaction.
SUMMARY_MAX_WORDS = 1500
#: The deterministic limit behind that request, per section. A model that ignores the
#: size cannot make the summary grow compaction after compaction.
SECTION_MAX_CHARS = 4000
#: A retry carries a smaller prior summary, so the answer has room to finish.
RETRY_SECTION_MAX_CHARS = 1000
OLDER_CUT_LINE = "[older items cut to fit the summary size]"
#: The ``core.credential_verdicts`` kind /status shows while summaries are being salvaged.
VERDICT_KIND = "compaction_summary"
#: The anchor block beside the ledger (``compaction_safety``): the values it holds, their
#: summed size, and what one tool result may add. Each compaction merges the last block
#: within these caps, so re-extraction cannot grow it.
ANCHORS_MAX_COUNT = 60
ANCHORS_MAX_CHARS = 3000
ANCHORS_PER_RESULT = 12
#: The part of one tool result the extraction reads, head + tail.
ANCHORS_SCAN_CHARS = 40_000
#: The summary an episode keeps, as JSON characters. The run record's meta budget is 3200.
EPISODE_SUMMARY_MAX_CHARS = 1200

#: Every section but the owner's, which the rebuild lifts out on its own.
REQUIRED_SECTIONS: Tuple[str, ...] = (
	"Active Task", "Goal & Constraints", "Completed Actions", "In Progress",
	"Blocked / Open Questions", "Key Decisions", "Resolved Questions",
	"Pending User Asks", "Relevant Files & Data", "Remaining Work",
)

RETRY_NOTE = (
	"Your previous answer was cut at the output limit, so it was not used. Write a "
	f"SHORTER summary: at most {SUMMARY_MAX_WORDS // 2} words, one short line per item, "
	f"every section present, and the exact last line {END_LINE}.\n\n"
)

_HEADING = re.compile(r"^## +(.+?)\s*$", re.MULTILINE)


def _key(heading: str) -> str:
	return heading.split(" (", 1)[0].strip().lower()


def split_sections(text: str) -> Tuple[str, List[Tuple[str, str]]]:
	"""``(text before the first heading, [(heading, body), ...])``, in order."""
	text = text or ""
	found = list(_HEADING.finditer(text))
	if not found:
		return text.strip(), []
	sections = []
	for i, match in enumerate(found):
		end = found[i + 1].start() if i + 1 < len(found) else len(text)
		sections.append((match.group(1), text[match.end():end].strip()))
	return text[:found[0].start()].strip(), sections


def _join(preamble: str, sections: List[Tuple[str, str]]) -> str:
	parts = [preamble] if preamble else []
	parts += [f"## {heading}\n{body}".rstrip() for heading, body in sections]
	return "\n\n".join(parts).strip()


def is_complete(text: str, facts: Optional[CallFacts] = None) -> bool:
	"""True when the answer reached its end line and its call's *facts* do not say cut."""
	if output_was_truncated(facts):
		return False
	lines = [line.strip() for line in (text or "").strip().splitlines()]
	return bool(lines) and lines[-1] == END_LINE


def strip_end_line(text: str) -> str:
	lines = (text or "").rstrip().splitlines()
	if lines and lines[-1].strip() == END_LINE:
		lines = lines[:-1]
	return "\n".join(lines).rstrip()


def missing_sections(text: str) -> List[str]:
	"""The required sections *text* does not hold, in the template's order."""
	present = {_key(heading) for heading, _ in split_sections(text)[1]}
	return [name for name in REQUIRED_SECTIONS if _key(name) not in present]


def _is_owner_section(heading: str) -> bool:
	return heading.lower().startswith("owner instructions")


def _bound_body(body: str, limit: int) -> str:
	"""Keep the head of a body, on a line boundary. A section is written newest
	first, so the end is the oldest part."""
	if len(body) <= limit:
		return body
	kept: List[str] = []
	used = 0
	for line in body.splitlines():
		if used + len(line) + 1 > limit:
			break
		kept.append(line)
		used += len(line) + 1
	if not kept:
		kept = [body[:limit].rstrip()]
	return "\n".join(kept + [OLDER_CUT_LINE])


def bound_summary(text: str, section_limit: int = SECTION_MAX_CHARS) -> str:
	"""One body per heading, each within the limit. Two sections with one heading
	(the static fallback adds to a prior summary) become one, the newer text first.
	The owner's instructions are quoted rules, so no size limit removes one."""
	preamble, sections = split_sections(text)
	merged: Dict[str, Tuple[str, str]] = {}
	for heading, body in sections:
		key = _key(heading)
		if key in merged:
			merged[key] = (merged[key][0], f"{body}\n{merged[key][1]}".strip())
		else:
			merged[key] = (heading, body)
	bounded = [(heading, body if _is_owner_section(heading) else _bound_body(body, section_limit))
	           for heading, body in merged.values()]
	return _join(_bound_body(preamble, section_limit), bounded)


def salvage(cut: str, prior: str = "", fallback: str = "") -> str:
	"""A summary built from a cut answer, never a cut answer stored as a good one.

	The whole sections of the answer stay, and its last section without its last
	line (the line the cap broke). A section the answer never reached comes from the
	prior summary, else from the deterministic fallback. A final section names what
	came from where, so the agent re-reads the source and does not read a gap as
	"nothing pending".
	"""
	preamble, cut_sections = split_sections(strip_end_line(cut))
	if cut_sections:
		heading, body = cut_sections[-1]
		kept = "\n".join(body.splitlines()[:-1]).strip()
		cut_sections = cut_sections[:-1] + ([(heading, kept)] if kept else [])
	have = {_key(heading) for heading, _ in cut_sections}
	sections = list(cut_sections)
	from_prior: List[str] = []
	lost: List[str] = []
	prior_sections = split_sections(prior)[1]
	fallback_sections = split_sections(fallback)[1]
	for source, note in ((prior_sections, from_prior), (fallback_sections, None)):
		for heading, body in source:
			if _key(heading) in have or not body:
				continue
			have.add(_key(heading))
			sections.append((heading, body))
			if note is not None and not _is_owner_section(heading):
				note.append(heading)
	lost = [name for name in REQUIRED_SECTIONS if _key(name) not in have]
	note_lines = ["The summary was cut at the output limit, so this one was rebuilt."]
	if from_prior:
		note_lines.append("Older text, from the earlier summary: " + ", ".join(from_prior) + ".")
	if lost:
		note_lines.append("Lost: " + ", ".join(lost) + ".")
	note_lines.append("Before you state a fact from a gap here, re-read its source.")
	sections.append(("Compaction note", " ".join(note_lines)))
	return bound_summary(_join(preamble, sections))


def damaged_prior_note(prior: str) -> str:
	"""The line that names what an earlier cut took from the prior summary. Empty
	for a whole summary, or for text that holds no section at all."""
	if not split_sections(prior)[1]:
		return ""
	gone = missing_sections(prior)
	if not gone:
		return ""
	return ("This prior summary LOST these sections at an earlier compaction: "
	        + ", ".join(gone) + ". Their absence does NOT mean nothing is pending. Rebuild "
	        "each from the conversation where you can; otherwise write \"(lost at an "
	        "earlier compaction: re-read the source)\".\n")
