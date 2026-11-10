"""F21 ratchet — only the sanctioned sites may cut the conversation deque.

A prompt cache is a PREFIX cache: anything that clears, splices or left-evicts
``MessageManager.history.messages`` invalidates every cached byte behind the cut.
That is sometimes right (compaction, an emergency prune, a restore from disk),
but it must stay a short, named list — the four routine rewrites the 2026-09-22
prompt/cache review found were exactly what happens when it is not.

This is a SOURCE grep, like the other repo ratchets: it does not run the code,
it pins WHERE the mutation verbs appear. Adding a new site fails this test; the
fix is either to reuse an existing seam or to add the site here deliberately,
with the reason, in the same commit.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.support.repo_index import repo_index

REPO_ROOT = Path(__file__).resolve().parents[1]

# The verbs that cut the deque or rewrite a stored row under a cached prefix.
_PATTERNS = (
	re.compile(r"history\.messages\.clear\(\)"),
	re.compile(r"history\.remove_message\("),
	re.compile(r"history\.messages\.popleft\("),
	re.compile(r"self\.messages\.popleft\("),
	re.compile(r"\b(?:managed|holder)\.message\s*=[^=]"),
	re.compile(r"history\.messages\s*=\s*deque\("),
)

# path -> (expected occurrence count, why it is allowed)
ALLOWED: dict[str, tuple[int, str]] = {
	"agents/task/agent/messages/compactor.py": (
		4,
		"clear_history_keep_system / emergency_context_prune / _rebuild_with_summary "
		"— the three sanctioned compaction boundaries; age_old_tool_results, after the "
		"guards decided to compact",
	),
	"agents/task/agent/messages/persistence.py": (
		2,
		"restore from a file checkpoint clears the deque; restore_from_checkpoint rewraps it",
	),
	"agents/task/agent/message_manager/service.py": (
		3,
		"F10 batched left-eviction at saturation (one log line, pair-safe); its deque "
		"rebuild for a positional insert; the <state-delta> scrub of a part that no "
		"longer applies",
	),
	"agents/task/agent/messages/filters.py": (
		1,
		"_retire_stored_media: the batch retirement of old image bytes",
	),
}

# The module that DEFINES the verbs is not a caller.
_DEFINITION_SITES = {"agents/task/agent/message_manager/views.py"}

_SKIP_DIRS = {
	".git", "__pycache__", "node_modules", "venv", ".venv", "build", "dist",
	"data", "tests", "docs", "scripts",
}


def _scan() -> dict[str, int]:
	found: dict[str, int] = {}
	for path in REPO_ROOT.rglob("*.py"):
		rel = path.relative_to(REPO_ROOT).as_posix()
		if any(part in _SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts[:-1]):
			continue
		if rel in _DEFINITION_SITES:
			continue
		try:
			text = repo_index().text(path, errors="ignore")
		except OSError:
			continue
		hits = sum(len(p.findall(text)) for p in _PATTERNS)
		if hits:
			found[rel] = hits
	return found


def test_only_sanctioned_sites_cut_the_conversation_deque():
	found = _scan()
	unexpected = sorted(set(found) - set(ALLOWED))
	assert not unexpected, (
		"new history-mutation site(s) outside the sanctioned list — a cut here "
		"colds the whole prompt prefix behind it:\n  " + "\n  ".join(unexpected)
	)


def test_the_patterns_see_each_rewrite_form():
	"""A form the scan cannot see is a rewrite with no ratchet."""
	for form in ("self.history.messages.clear()", "managed.message = new",
	             "holder.message = after", "self.history.messages = deque(rows, maxlen=9)"):
		assert any(p.search(form) for p in _PATTERNS), form
	assert not any(p.search("managed.message == other") for p in _PATTERNS)


def test_sanctioned_sites_did_not_multiply():
	found = _scan()
	for rel, (expected, why) in ALLOWED.items():
		actual = found.get(rel, 0)
		assert actual <= expected, (
			f"{rel} now cuts the deque {actual} times (allowed {expected}: {why}). "
			"Extend the existing seam instead of adding another cut."
		)
