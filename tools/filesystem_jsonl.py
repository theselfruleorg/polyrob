"""One JSON-lines record check, and a line count that holds one line at a time."""
import json
from typing import Any, Optional, Tuple

from core.security.workspace_io import open_read


def jsonl_record(line: str) -> Tuple[Any, Optional[str]]:
    """(the object, None), or (None, why this line is not a JSON object)."""
    try:
        obj = json.loads(line)
    except ValueError as e:
        return None, str(e)[:60]
    return (obj, None) if isinstance(obj, dict) else (None, "not a JSON object")


def jsonl_tally(path: str, root: str) -> Tuple[int, int, Optional[int]]:
    """(total_lines, bad_lines, first_bad_lineno)."""
    total, bad, first_bad = 0, 0, None
    with open_read(path, root) as stream:
        for n, raw in enumerate(stream, 1):  # a binary stream splits on b'\n' only
            line = raw.decode("utf-8", errors="replace")
            if not line.strip():
                continue
            total += 1
            if jsonl_record(line)[1]:
                bad += 1
                first_bad = n if first_bad is None else first_bad
    return total, bad, first_bad
