"""Location: polyrob/app/bot.py"""

"""Main application initialization and management - Platform agnostic version."""

import logging
import time
import asyncio
import os
from typing import Optional, Dict, Any

from core.config import BotConfig
from core.exceptions import (
    BotError,
    ConfigurationError,
    ComponentInitializationError
)
from core.logging import setup_logging, get_component_logger
from polyrob.app.initialization import (
    initialize_core,
    cleanup_core,
    initialize_tools,
    cleanup_tools,
    initialize_managers,
    initialize_agents,
    initialize_modules,
    initialize_auth_services
)
from core.base_component import BaseComponent
from core.container import DependencyContainer
from core.state.machine import StateMachine

#: 098 CL-17: one lifecycle instead of is_running + _initialized.
BOT = StateMachine(
    "app.bot",
    states={"stopped", "starting", "running", "stopping", "failed", "stopped_degraded"},
    transitions={"stopped": {"starting"}, "starting": {"running", "failed"},
                 "running": {"stopping"}, "stopping": {"stopped", "stopped_degraded"},
                 "failed": {"starting"}, "stopped_degraded": {"starting"}},
    initial={"stopped"},
)

logger = logging.getLogger('Bot.bot')

class Bot(BaseComponent):
    """Main application class managing initialization and lifecycle.

    In this architecture, this class handles:
    - Component initialization
    - Service registration with DependencyContainer
    - Application lifecycle management

    The HTTP server and API endpoints are handled by FastAPI.
    """

    # Tech symbols for logging
    TECH_SYMBOLS = {
        'start': '»',
        'success': '✓',
        'arrow': '→',
        'bullet': '∙',
        'separator': '┊',
    }

    def __init__(self, config: BotConfig, *args, **kwargs):
        """Initialize bot with required parameters"""
        super().__init__(name="MainBot", config=config, *args, **kwargs)
        self.container = DependencyContainer.get_instance(config)
        self.logger = get_component_logger("Bot")

        self.state = "stopped"
        #: phase -> "ok" | "timeout" | "error" for the last cleanup (098 CL-17).
        self.cleanup_report: Dict[str, str] = {}

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    def _move(self, to: str) -> None:
        if self.state != to and BOT.check(self.state, to, ctx={"bot": self.name}):
            self.state = to

    async def _initialize(self) -> None:
        """Implementation-specific initialization."""
        self._move("starting")
        try:
            # Add a single clear marker for initialization start
            self.logger.info("")
            self.logger.info("="*50)
            self.logger.info("🚀 APPLICATION INITIALIZATION SEQUENCE STARTED")
            self.logger.info("="*50)

            # Initialize core first
            self.logger.info("🔍 [STEP 1/6] Initializing core components...")
            await initialize_core(self.container)

            # Initialize modules (database, memory, LLM, etc.)
            self.logger.info("🔍 [STEP 2/6] Initializing modules...")
            await initialize_modules(self.container)

            # Initialize tools (browser, filesystem, MCP, etc.)
            self.logger.info("🔍 [STEP 3/6] Initializing tools...")
            await initialize_tools(self.container)

            # Initialize managers
            self.logger.info("🔍 [STEP 4/6] Initializing managers...")
            await initialize_managers(self.container)

            # Initialize agents
            self.logger.info("🔍 [STEP 5/5] Initializing agents...")
            await initialize_agents(self.container)

            # Auth/billing/payment services (phase 6) are SERVER-SCOPE and no
            # longer initialized by CoreBot.initialize(). Callers that need
            # the full server stack must invoke initialize_server_services()
            # explicitly (api/app.py does this via polyrob.app.bootstrap.build_server_bot).
            # Core/CLI mode runs phases 1-5 only.

            self.logger.info("")
            self.logger.info("="*50)
            self.logger.info(f"{self.TECH_SYMBOLS['success']} CORE INITIALIZATION COMPLETE (phases 1-5)")
            self.logger.info("="*50)
            self.logger.info("")

            self._move("running")

        except Exception as e:
            self._move("failed")
            self.logger.error(f"Bot initialization failed: {e}", exc_info=True)
            # 098 CL-32: tear down what the failed phases started. cleanup() is
            # guarded by _initialized (only success sets it), so call the
            # implementation; a cleanup fault never masks the original error.
            try:
                await self._cleanup()
            except Exception:
                self.logger.error("cleanup after a failed init also failed", exc_info=True)
            raise BotError(f"Failed to initialize bot: {e}")

    async def initialize_server_services(self) -> None:
        """Register server-only services (auth, billing, payments) — phase 6.

        Idempotent: safe to call once after initialize(). Internally checks
        config.enable_auth and gracefully no-ops in core mode. The server
        entry point (api/app.py via polyrob.app.bootstrap.build_server_bot) invokes
        this; the CLI bootstrap does not.
        """
        try:
            self.logger.info("🔍 [PHASE 6] Initializing auth & payment services...")
            await initialize_auth_services(self.container)
            self.logger.info(f"{self.TECH_SYMBOLS['success']} Server services initialized")
        except ImportError as e:
            # modules.auth / modules.payments / etc. unavailable in this install.
            self.logger.info(f"  ⏩ Server services unavailable in this install: {e}")
        except Exception as e:
            self.logger.error(f"❌ Server service initialization failed: {e}", exc_info=True)
            raise

    async def _cleanup(self) -> None:
        """Clean up implementation-specific resources. Each phase is bounded and
        its outcome recorded; any phase that is not ``ok`` ends the bot in
        ``stopped_degraded`` (098 CL-17), never a plain "completed"."""
        from polyrob.app import initialization as init
        if self.state == "running":
            self._move("stopping")
        report: Dict[str, str] = {}
        self.cleanup_report = report
        self.logger.info("Starting application cleanup...")

        async def phase(name: str, coro_fn, timeout: float) -> None:
            try:
                await asyncio.wait_for(coro_fn(), timeout=timeout)
                report[name] = "ok"
                self.logger.info(f"✓ {name} cleanup completed")
            except asyncio.TimeoutError:
                report[name] = "timeout"
                self.logger.warning(f"⚠ {name} cleanup timed out after {timeout}s")
            except Exception as e:
                report[name] = "error"
                self.logger.error(f"✗ Error during {name} cleanup: {e}")

        # Stop payment services first (they have background tasks)
        for svc_name in ("deposit_monitor", "treasury_sweeper"):
            svc = self.container.get_service(svc_name)
            if svc:
                await phase(svc_name, svc.stop, 5.0)
        for name, fn, timeout in (("agents", init.cleanup_agents, 15.0),
                                  ("managers", init.cleanup_managers, 10.0),
                                  ("tools", init.cleanup_tools, 15.0),
                                  ("modules", init.cleanup_modules, 10.0),
                                  ("core", init.cleanup_core, 5.0)):
            await phase(name, lambda fn=fn: fn(self.container), timeout)

        bad = [f"{k} ({v})" for k, v in report.items() if v != "ok"]
        if self.state == "stopping":
            self._move("stopped_degraded" if bad else "stopped")
        if bad:
            self.logger.warning("Application cleanup incomplete: " + ", ".join(bad))
        else:
            self.logger.info("Application cleanup completed")

    async def start(self) -> None:
        """Start the application. initialize() holds the component lock; this
        must not (an asyncio lock is not re-entrant)."""
        if self.is_running:
            self.logger.warning("Application is already running")
            return
        try:
            await self.initialize()
            self.logger.info("Application started successfully")
        except Exception as e:
            self.logger.error(f"Failed to start application: {e}")
            raise

    async def stop(self) -> None:
        """Stop the application (cleanup() serialises with initialize())."""
        if not self.is_running:
            self.logger.warning("Application is not running")
            return
        try:
            await self.cleanup()
            self.logger.info("Application stopped successfully")
        except Exception as e:
            self.logger.error(f"Error stopping application: {e}")
            raise

    def get_status(self) -> Dict[str, Any]:
        """Get application status information."""
        return {
            "running": self.is_running,
            "state": self.state,
            "components": {
                "services": len(self.container.services),
                "managers": len(self.container.managers),
                "agents": len(self.container.agents)
            }
        }

    async def handle_request(self, request_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a generic request - can be HTTP, WebSocket, etc.

        Args:
            request_type: Type of request (message, command, etc.)
            data: Request data including user info, message, etc.

        Returns:
            Response dict
        """
        try:
            if request_type == "message":
                # Chat is served by the unified task agent via the HTTP
                # /api/chat/message endpoint (legacy ChatAgent retired, HANDOFF-C).
                return {
                    "text": "Send chat messages to the /api/chat/message endpoint.",
                    "parse_mode": "markdown"
                }

            elif request_type == "command":
                command = data.get("command")
                # Handle basic commands directly
                if command == "health":
                    return {
                        "text": "✅ System is operational",
                        "parse_mode": "markdown"
                    }
                elif command == "status":
                    status = self.get_status()
                    return {
                        "text": f"📊 System Status:\n" +
                               f"Running: {status['running']}\n" +
                               f"Services: {status['components']['services']}\n" +
                               f"Managers: {status['components']['managers']}\n" +
                               f"Agents: {status['components']['agents']}",
                        "parse_mode": "markdown"
                    }
                else:
                    return {
                        "text": "Command not recognized",
                        "parse_mode": "markdown"
                    }

            return {
                "text": f"Request type '{request_type}' not supported yet",
                "parse_mode": "markdown"
            }

        except Exception as e:
            self.logger.error(f"Error handling request: {e}", exc_info=True)
            return {
                "text": f"❌ Error: {str(e)}",
                "parse_mode": "markdown"
            }

