"""The typed facts of a ``<state-delta>`` part: what decides a new push.

A part is pushed again only when its facts changed. A render clock, a read's age, a
delivery counter, the amount spent inside a cap and the run times of a job are not
facts for this purpose: each moves with no change the agent must act on, and each push
brings compaction nearer. ``agent_status`` reads the live figures.

An unknown line is one whole fact, so a new line can only add a push, never hide one.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Tuple

from core.money.limits import daily_cap_fact
from core.status_delivery import delivery_fact
from core.status_render import job_fact
from core.wallet.balance_cache import wallet_read_fact

_RENDER_HEADER = re.compile(r"^Live (?:health|harness state) as of ")


@dataclass(frozen=True)
class StateFact:
	"""One fact: ``kind`` names the rule that read it, ``value`` holds what counts."""
	kind: str
	value: str


#: Each renderer owns its line's format, and reads the fact back out of it.
_FACT_READERS = (("delivery", delivery_fact), ("spend_cap", daily_cap_fact),
                 ("job", job_fact), ("read", wallet_read_fact))


def _fact(line: str) -> StateFact:
	for kind, read in _FACT_READERS:
		value = read(line)
		if value is not None:
			return StateFact(kind, value)
	return StateFact("line", line)


def state_facts(text: str) -> Tuple[StateFact, ...]:
	"""The facts of a part, in the order the part states them."""
	lines = (raw.strip() for raw in str(text).splitlines())
	return tuple(_fact(line) for line in lines if line and not _RENDER_HEADER.match(line))


def state_hash(text: str) -> str:
	"""The hash of a part's facts."""
	rows = [[fact.kind, fact.value] for fact in state_facts(text)]
	return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
