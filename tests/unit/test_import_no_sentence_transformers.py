"""The task package must initialize under MEMORY_BACKEND=sqlite (the server default)
with sentence_transformers/torch completely absent — the whole point of moving
sentence-transformers to the opt-in `memory-vector` extra (see
tests/unit/test_requirements_no_sentence_transformers.py) only holds if nothing on
the default path actually needs it.
"""
import subprocess
import sys


def test_task_agent_initializes_when_sentence_transformers_absent():
    code = (
        "import sys, asyncio, os\n"
        "sys.modules['sentence_transformers'] = None\n"
        "sys.modules['torch'] = None\n"
        "os.environ['MEMORY_BACKEND'] = 'sqlite'\n"
        "from core.config import BotConfig\n"
        "from core.container import DependencyContainer\n"
        "from agents.task_agent_lite import TaskAgent\n"
        "config = BotConfig()\n"
        "container = DependencyContainer.get_instance(config)\n"
        "agent = TaskAgent(config=config, container=container)\n"
        "asyncio.run(agent._initialize())\n"
        "assert agent.task_available is True, "
        "'task package did not initialize without sentence_transformers/torch'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0 and "OK" in result.stdout, result.stderr
