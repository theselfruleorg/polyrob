"""WS-2: foreground/background discipline.

A foreground `shell_run` that blocks forever (a server, a `sleep`, a trailing `&`
or `nohup`) would hang the turn. The tool detects those patterns and refuses,
nudging the model to pass `background=True` instead (Hermes parity).
"""
import pytest

from tools.shell.discipline import background_nudge


@pytest.mark.parametrize("cmd", [
    "flask run",
    "python -m http.server 8000",
    "npm run dev",
    "uvicorn app:app",
    "gunicorn app:app",
    "python app.py &",
    "nohup ./server",
    "tail -f /var/log/x",
    "sleep 600",
    # newly-covered blocking/server patterns (review LOW-6)
    "python app.py",
    "python3 server.py --port 8000",
    "journalctl -u polyrob -f",
    "watch -n1 ls",
    "sleep 5m",
    "tail -n 100 -f app.log",
    "ping example.com",
])
def test_server_and_backgrounding_patterns_are_nudged(cmd):
    nudge = background_nudge(cmd, background=False)
    assert nudge is not None
    assert "background" in nudge.lower()


@pytest.mark.parametrize("cmd", [
    "ls -la",
    "pytest -q",
    "pip install flask",
    "echo hello && cat file.txt",
    "python build.py",          # a build script, not a server
    "grep -r TODO .",
    "python setup.py sdist",
    "ping -c 3 example.com",     # bounded ping is fine
    "sleep 5",                    # short sleep is fine
])
def test_normal_commands_pass_foreground(cmd):
    assert background_nudge(cmd, background=False) is None


def test_background_true_never_nudges_even_for_a_server():
    assert background_nudge("flask run", background=True) is None


def test_background_true_never_nudges_a_normal_command():
    assert background_nudge("ls", background=True) is None
