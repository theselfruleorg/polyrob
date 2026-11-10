"""``last_response`` lives on a client that sessions share (106 S2), so it is only the one
transfer from a client to the adapter, inside one tick. The adapter builds ``CallFacts`` from
it, and every later consumer reads ``message.call_facts``."""
import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
_READ = re.compile(r"""\.last_response\b|['"]last_response['"]""")
_SCANNED = ("agents", "api", "cli", "core", "cron", "modules", "polyrob", "surfaces", "tools", "webview")
_THE_TRANSFER = {"modules/llm/adapters.py"}


def _is_client(rel: str) -> bool:
    return rel in {"modules/llm/llm_client.py", "modules/llm/compat_anthropic.py"} or (
        rel.startswith("modules/llm/") and rel.endswith("_client.py"))


def test_no_reader_outside_the_clients_and_the_adapter():
    readers = set()
    for top in _SCANNED:
        for path in (REPO / top).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if not _is_client(rel) and _READ.search(path.read_text(errors="replace")):
                readers.add(rel)
    assert readers <= _THE_TRANSFER, sorted(readers - _THE_TRANSFER)


def _function(name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse((REPO / "modules/llm/adapters.py").read_text())
    # The last definition: the abstract base comes first.
    return [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name][-1]


def _awaits_between(func, first_line: int, last_line: int) -> list:
    return [n.lineno for n in ast.walk(func) if isinstance(n, ast.Await) and first_line < n.lineno < last_line]


def test_the_batch_transfer_has_no_await_after_the_client_returned():
    func = _function("_agenerate")
    awaits = [n for n in ast.walk(func) if isinstance(n, ast.Await)]
    calls = [n.lineno for n in awaits if "_client" in ast.unparse(n)]
    read = next(n.lineno for n in ast.walk(func) if isinstance(n, ast.Constant) and n.value == "last_response")
    assert calls and not _awaits_between(func, max(calls), read)
    assert all(line < read for line in calls)


def test_the_stream_transfer_reads_on_the_final_event():
    source = ast.unparse(_function("_astream_true"))
    final = source.index("final_event = event")
    assert source.index("last_response") - final < 80, "read it in the tick of the final event"
