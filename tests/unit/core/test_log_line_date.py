"""Each log line carries its date. The rotated files reach back about 14 days, and
a line with a time alone cannot be placed in a review of a past incident."""
import logging
import re
from pathlib import Path

from core.logging import LOG_DATEFMT, ComfyFormatter, HTTPFormatter

REPO = Path(__file__).resolve().parents[3]
FMT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"


def _line(formatter):
    return formatter.format(logging.LogRecord("m.n", logging.ERROR, "x.py", 1, "boom", (), None))


def test_a_file_line_has_the_date_and_keeps_its_shape(monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: False, raising=False)
    for cls in (ComfyFormatter, HTTPFormatter):
        line = _line(cls(FMT, datefmt=LOG_DATEFMT))
        assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ERROR m\.n: boom", line)
        assert " ERROR " in line  # scripts/watch_tick.sh counts on this text


def test_no_sink_uses_a_time_only_stamp():
    src = (REPO / "core/logging.py").read_text(encoding="utf-8")
    assert "datefmt='%H:%M:%S'" not in src
    assert src.count("datefmt=LOG_DATEFMT") == 2
