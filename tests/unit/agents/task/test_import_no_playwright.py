"""agents.task_agent_lite must import and initialize without playwright installed.

Fresh-install finding (2026-07-19): `pip install -r requirements.txt`
does not install playwright (it's the `[browser]` extra by design), yet importing/
initializing the task package failed with "Task package not available: No module
named 'playwright'" — curator + delegation sweep also failed as a result. Root cause
traced (2026-08-18) to FOUR unconditional top-level imports of playwright-backed
browser classes, used only as type hints, sitting on the import path of nearly all of
`agents.task.agent`:
  - agents/task/agent/orchestrator.py (Browser, BrowserContext, BrowserManager)
  - tools/controller/registry/service.py (BrowserContext) — the actual first hit, via
    agents.task.agent.__init__ -> service.Agent -> message_manager -> views ->
    tools.controller.registry.__init__ -> registry.service -> tools.browser.context
  - tools/controller/execution.py (BrowserContext, unused import)
  - tools/controller/service.py (BrowserContext, entirely unused import)
All four moved the import under `if TYPE_CHECKING:` (or removed it if dead), with the
type hints quoted as forward references where no `from __future__ import annotations`
is present in that file — the browser TOOL is still fully functional when playwright
IS installed (nothing changed for the normal path).
"""
import subprocess
import sys


def test_task_agent_initializes_when_playwright_absent():
    code = (
        "import sys, asyncio\n"
        "sys.modules['playwright'] = None\n"  # any `import playwright*` raises ImportError
        "from core.config import BotConfig\n"
        "from core.container import DependencyContainer\n"
        "from agents.task_agent_lite import TaskAgent\n"
        "config = BotConfig()\n"
        "container = DependencyContainer.get_instance(config)\n"
        "agent = TaskAgent(config=config, container=container)\n"
        "asyncio.run(agent._initialize())\n"
        "assert agent.task_available is True, 'task package did not initialize without playwright'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0 and "OK" in result.stdout, result.stderr


def test_orchestrator_module_imports_when_playwright_absent():
    code = (
        "import sys\n"
        "sys.modules['playwright'] = None\n"
        "import agents.task.agent.orchestrator\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0 and "OK" in result.stdout, result.stderr
