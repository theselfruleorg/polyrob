"""Location: core/bot.py"""

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
from core.initialization import (
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

        try:
            self._lock = asyncio.Lock()
            self.is_running = False

        except Exception as e:
            self.logger.error(f"Bot initialization failed: {e}")
            raise BotError(f"Failed to initialize bot: {e}")

    async def _initialize(self) -> None:
        """Implementation-specific initialization."""
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
            # explicitly (api/app.py does this via core.bootstrap.build_server_bot).
            # Core/CLI mode runs phases 1-5 only.

            self.logger.info("")
            self.logger.info("="*50)
            self.logger.info(f"{self.TECH_SYMBOLS['success']} CORE INITIALIZATION COMPLETE (phases 1-5)")
            self.logger.info("="*50)
            self.logger.info("")

            self.is_running = True

        except Exception as e:
            self.logger.error(f"Bot initialization failed: {e}", exc_info=True)
            await self.cleanup()
            raise BotError(f"Failed to initialize bot: {e}")

    async def initialize_server_services(self) -> None:
        """Register server-only services (auth, billing, payments) — phase 6.

        Idempotent: safe to call once after initialize(). Internally checks
        config.enable_auth and gracefully no-ops in core mode. The server
        entry point (api/app.py via core.bootstrap.build_server_bot) invokes
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
        """Clean up implementation-specific resources."""
        try:
            self.logger.info("Starting application cleanup...")
            
            # Set a total cleanup timeout to prevent hanging
            cleanup_timeout = 60.0  # 60 seconds total cleanup time
            start_time = asyncio.get_event_loop().time()

            async def cleanup_with_timeout(cleanup_func, name: str, timeout: float = 10.0):
                """Helper to run cleanup with timeout."""
                try:
                    await asyncio.wait_for(cleanup_func(self.container), timeout=timeout)
                    self.logger.info(f"✓ {name} cleanup completed")
                except asyncio.TimeoutError:
                    self.logger.warning(f"⚠ {name} cleanup timed out after {timeout}s")
                except Exception as e:
                    self.logger.error(f"✗ Error during {name} cleanup: {e}")

            # Stop payment services first (they have background tasks)
            self.logger.info("Stopping payment services...")
            deposit_monitor = self.container.get_service('deposit_monitor')
            if deposit_monitor:
                try:
                    await asyncio.wait_for(deposit_monitor.stop(), timeout=5.0)
                    self.logger.info("  ✓ Deposit monitor stopped")
                except Exception as e:
                    self.logger.warning(f"  ⚠ Error stopping deposit monitor: {e}")

            treasury_sweeper = self.container.get_service('treasury_sweeper')
            if treasury_sweeper:
                try:
                    await asyncio.wait_for(treasury_sweeper.stop(), timeout=5.0)
                    self.logger.info("  ✓ Treasury sweeper stopped")
                except Exception as e:
                    self.logger.warning(f"  ⚠ Error stopping treasury sweeper: {e}")

            # Cleanup agents with timeout
            self.logger.info("Cleaning up agents...")
            from core.initialization import cleanup_agents
            await cleanup_with_timeout(cleanup_agents, "agents", 15.0)

            # Check if we're running out of time
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > cleanup_timeout * 0.8:  # 80% of timeout used
                self.logger.warning("Cleanup taking too long, forcing remaining cleanup")
                self.is_running = False
                return

            # Cleanup managers with timeout
            self.logger.info("Cleaning up managers...")
            from core.initialization import cleanup_managers
            await cleanup_with_timeout(cleanup_managers, "managers", 10.0)

            # Cleanup tools with timeout
            self.logger.info("Cleaning up tools...")
            from core.initialization import cleanup_tools
            await cleanup_with_timeout(cleanup_tools, "tools", 15.0)

            # Check timeout again
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > cleanup_timeout * 0.9:  # 90% of timeout used
                self.logger.warning("Cleanup timeout approaching, skipping remaining cleanup")
                self.is_running = False
                return

            # Cleanup modules with timeout
            self.logger.info("Cleaning up modules...")
            from core.initialization import cleanup_modules
            await cleanup_with_timeout(cleanup_modules, "modules", 10.0)

            # Cleanup core with timeout
            self.logger.info("Cleaning up core components...")
            from core.initialization import cleanup_core
            await cleanup_with_timeout(cleanup_core, "core", 5.0)

            self.is_running = False
            self.logger.info("Application cleanup completed")

        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}", exc_info=True)
            self.is_running = False

    async def start(self) -> None:
        """Start the application."""
        async with self._lock:
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
        """Stop the application."""
        async with self._lock:
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

