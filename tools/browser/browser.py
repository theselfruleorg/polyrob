"""
Playwright browser on steroids.
"""

import asyncio
import gc
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Type, List
import platform
import os
import tempfile
from pathlib import Path
import time

# ProxySettings type for proxy configuration
from playwright.async_api import Browser as PlaywrightBrowser
from playwright.async_api import (
	Playwright,
	async_playwright,
)

from tools.browser.context import BrowserContext, BrowserContextConfig
from tools.base_tool import BaseTool
from tools.controller.types import ActionResult
from tools.browser.actions import (
	SearchGoogleAction, GoToUrlAction, ClickElementAction, InputTextAction,
	SwitchTabAction, OpenTabAction, ScrollAction, SendKeysAction,
	NoParamsAction, ExtractPageContentAction
)
from tools.controller.execution_context import ActionExecutionContext


def _check_url_ssrf(url: str) -> Optional[str]:
	"""SSRF guard for agent-supplied navigation URLs.

	Runs the URL through the existing MCPURLValidator (which resolves the host
	and rejects loopback / RFC1918 / link-local / cloud-metadata IP ranges)
	BEFORE the browser is allowed to navigate. This stops a prompt-injected page
	from steering the agent to http://169.254.169.254/ (instance metadata) or
	internal RFC1918 services and exfiltrating the response back to the model.

	The browser legitimately uses both http and https, so ``allow_http=True`` is
	passed — we only care about the SSRF IP-range checks here, not the scheme.

	Gated by ``BROWSER_ALLOW_PRIVATE_URLS`` (default false = guard active). Set it
	to ``true`` for local/dev where hitting localhost is intentional.

	Residual risk (accepted for this fix): Playwright re-resolves DNS at goto
	time, so a hostname whose A-record flips between this check and the actual
	navigation (DNS rebinding) is a TOCTOU window this does NOT fully close. We
	validate the *currently resolved* IP, which blocks the obvious static
	metadata/RFC1918 SSRF; a separate pin-resolved-IP task covers rebinding.

	Returns:
		An error message string if the URL is blocked, or ``None`` if allowed.
	"""
	allow_private = os.getenv('BROWSER_ALLOW_PRIVATE_URLS', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
	if allow_private:
		return None

	try:
		from tools.mcp.security import MCPURLValidator
		# allow_http=True: the browser uses http+https; we want only the SSRF
		# IP-range checks, not the HTTPS-only restriction MCP applies to servers.
		validator = MCPURLValidator(allow_http=True)
		is_valid, error = validator.validate(url)
		if not is_valid:
			return f"Blocked URL (SSRF protection): {error}. Set BROWSER_ALLOW_PRIVATE_URLS=true to allow private/internal hosts (local/dev only)."
	except Exception as e:
		# Fail closed: if validation itself errors, do not navigate.
		return f"Blocked URL (SSRF validation error): {e}"
	return None


@dataclass
class BrowserConfig:
	r"""
	Configuration for the Browser.

	Default values:
		headless: True
			Whether to run browser in headless mode (no visible window).
			Set to True by default for server compatibility.

		disable_security: False
			Disable browser security features (safe default - opt-in relaxations only)

		use_no_sandbox: False
			Enable --no-sandbox flag (opt-in only, required for some server environments)

		extra_chromium_args: []
			Extra arguments to pass to the browser

		wss_url: None
			Connect to a browser instance via WebSocket

		cdp_url: None
			Connect to a browser instance via CDP

		chrome_instance_path: None
			Path to a Chrome instance to use to connect to your normal browser
			e.g. '/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome'
            
		auto_configure_for_server: True
			Automatically configure browser for server environments.
			This includes checking for X server availability and falling back
			to headless mode if no display is available. It also adds necessary
			flags like --no-sandbox and --disable-dev-shm-usage for stability.
			Should be left enabled unless you're managing these settings manually.
	"""

	headless: bool = True  # Changed default to True for safety and server compatibility
	disable_security: bool = False
	use_no_sandbox: bool = False  # Opt-in --no-sandbox flag for server environments
	extra_chromium_args: list[str] = field(default_factory=list)
	chrome_instance_path: str | None = None
	wss_url: str | None = None
	cdp_url: str | None = None
	auto_configure_for_server: bool = True

	proxy: Dict[str, Any] | None = field(default=None)
	new_context_config: BrowserContextConfig = field(default_factory=BrowserContextConfig)

	_force_keep_browser_alive: bool = False


# @singleton: TODO - think about id singleton makes sense here
# @dev By default this is a singleton, but you can create multiple instances if you need to.
class Browser(BaseTool):
	"""
	Playwright browser on steroids.

	This tool provides browser automation capabilities using Playwright.
	It follows the standard BaseTool pattern with decorated actions.

	Architecture:
	- Extends BaseTool for lifecycle management
	- Actions defined with @BaseTool.action() decorator
	- Auto-discovered by Controller via get_actions()
	- Registered with namespacing (browser_action_name)

	Usage:
		# In Controller/Orchestrator initialization
		browser = Browser(name="browser", config=bot_config)
		await browser.initialize()
		controller.add_tool("browser", browser)

		# Actions are automatically registered and available to LLM

	Note: It is recommended to use only one instance per application
	as each instance consumes significant RAM.
	"""

	def __init__(
		self,
		name: str = "browser",
		config = None,
		container = None,
		# Browser-specific parameters
		headless: bool = True,
		slow_mo: int = 0,
		proxy: Optional[Dict[str, Any]] = None,
		browser_config: Optional[BrowserConfig] = None,
		timeout: int = 30000,
		server_args: Optional[List[str]] = None,
		browser_path: Optional[str] = None,
		env: Optional[Dict[str, str]] = None,
	):
		"""Initialize Browser with configuration.

		Args:
			name: Tool name for registration
			config: Bot configuration (required by BaseTool)
			container: Dependency container (required by BaseTool)
			headless: Whether to run browser in headless mode
			slow_mo: Slow down Playwright operations by specified milliseconds
			proxy: Proxy settings
			browser_config: Custom browser configuration
			timeout: Browser launch timeout in milliseconds
			server_args: Additional arguments for browser launch
			browser_path: Path to browser executable
			env: Environment variables
		"""
		# Initialize BaseTool (this sets up logging, status tracking, etc.)
		if config is None:
			from core.config import BotConfig
			config = BotConfig()

		super().__init__(name=name, config=config, container=container)

		# Initialize state variables (will be set in _initialize())
		self._browser = None
		self._playwright = None
		self._xvfb_process = None
		self._started_xvfb = False

		# Use provided browser config or create default
		self.browser_config = browser_config or BrowserConfig()

		# Override browser config with provided arguments
		if headless is not None:
			self.browser_config.headless = headless
		
		# Store initialization parameters
		self.slow_mo = slow_mo
		self.timeout = timeout
		self.server_args = server_args or []
		self.browser_path = browser_path
		
		# Initialize base environment
		base_env = os.environ.copy()
		if env:
			base_env.update(env)
		
		# Handle server environment if needed
		if self.browser_config.auto_configure_for_server:
			# Configure environment with display setup
			self.env = self._setup_display_for_server(base_env)

			# Check if we started Xvfb (for cleanup)
			if hasattr(self, '_xvfb_process') and self._xvfb_process is not None:
				self._started_xvfb = True

			# Get browser path if possible
			self._detect_browser_on_server()
		else:
			self.env = base_env

		# Disable security if configured
		self.disable_security_args = []
		if self.browser_config.disable_security:
			self.disable_security_args = ['--disable-web-security', '--disable-site-isolation-trials', '--disable-features=IsolateOrigins,site-per-process']

		self.logger.info(f"Browser initialized with headless={self.browser_config.headless}, auto_configure={self.browser_config.auto_configure_for_server}")

	def _setup_display_for_server(self, env=None):
		"""Set up display for server environments, ensuring Xvfb is running if needed."""
		import subprocess
		import shutil
		import time

		if not env:
			env = os.environ.copy()

		# If no DISPLAY, try to check if Xvfb is already running
		if "DISPLAY" not in env:
			logging.info("DISPLAY not set, checking for running Xvfb instances")
			
			# Check if Xvfb is already running on :99
			try:
				xvfb_check = subprocess.run(
					["pgrep", "-f", "Xvfb :99"], 
					stdout=subprocess.PIPE, 
					stderr=subprocess.PIPE, 
					text=True
				)
				
				if xvfb_check.returncode == 0:
					# Xvfb is running, use it
					logging.info("Found running Xvfb on :99, using it")
					env["DISPLAY"] = ":99"
				else:
					# No Xvfb running, check if we can start one
					xvfb_path = shutil.which("Xvfb")
					if xvfb_path and self.browser_config.auto_configure_for_server:
						logging.info("Starting Xvfb on :99")
						try:
							xvfb_process = subprocess.Popen(
								["Xvfb", ":99", "-screen", "0", "1920x1080x24"],
								stdout=subprocess.DEVNULL,
								stderr=subprocess.DEVNULL,
							)
							# Give Xvfb a moment to start
							time.sleep(1)
							
							# Check if it's actually running
							if xvfb_process.poll() is None:
								logging.info("Successfully started Xvfb with PID: %d", xvfb_process.pid)
								# Save PID for potential cleanup
								pid_dir = os.path.dirname(os.path.abspath(__file__))
								with open(os.path.join(pid_dir, "xvfb.pid"), "w") as f:
									f.write(str(xvfb_process.pid))
								env["DISPLAY"] = ":99"
							else:
								logging.warning("Failed to start Xvfb, forcing headless mode")
								self.browser_config.headless = True
						except Exception as e:
							logging.warning(f"Error starting Xvfb: {str(e)}, forcing headless mode")
							self.browser_config.headless = True
					else:
						# No Xvfb available, force headless mode
						logging.warning(
							"DISPLAY not set and Xvfb not available, forcing headless mode"
						)
						self.browser_config.headless = True
			except Exception as e:
				logging.warning(f"Error checking for Xvfb: {str(e)}, forcing headless mode")
				self.browser_config.headless = True
		
		# Even with a DISPLAY set, test if it's actually working
		if "DISPLAY" in env and not self.browser_config.headless:
			try:
				display_check = subprocess.run(
					["xdpyinfo", "-display", env["DISPLAY"]], 
					stdout=subprocess.DEVNULL, 
					stderr=subprocess.DEVNULL
				)
				if display_check.returncode != 0:
					logging.warning(f"Display {env['DISPLAY']} not accessible, forcing headless mode")
					self.browser_config.headless = True
			except Exception:
				logging.warning("Failed to verify display, forcing headless mode")
				self.browser_config.headless = True
		
		return env

	def _configure_server_args(self, server_args):
		"""Configure browser launch arguments for server environments."""
		if server_args is None:
			server_args = []
		
		# Add --no-sandbox only if explicitly opted-in
		if self.browser_config.use_no_sandbox and "--no-sandbox" not in server_args:
			server_args.append("--no-sandbox")

		# Configure server-specific flags
		if self.browser_config.auto_configure_for_server:
			
			# Handle GPU-related flags for server environments
			if "--disable-gpu" not in server_args:
				server_args.append("--disable-gpu")
			
			# Make sure we're handling remote rendering correctly
			if "--disable-dev-shm-usage" not in server_args:
				server_args.append("--disable-dev-shm-usage")
			
			# In non-headless mode on a server, we need to adjust some settings
			if not self.browser_config.headless and "DISPLAY" in os.environ:
				# Add browser flags that work better with Xvfb
				if "--disable-features=VizDisplayCompositor" not in server_args:
					server_args.append("--disable-features=VizDisplayCompositor")
		
		# Add user-specified args
		if self.browser_config.extra_chromium_args:
			server_args.extend(self.browser_config.extra_chromium_args)
		
		return server_args

	def _detect_browser_on_server(self):
		"""Detect installed browsers on server"""
		common_browser_paths = [
			"/usr/bin/chromium-browser",
			"/usr/bin/chromium",
			"/usr/bin/google-chrome",
			"/usr/bin/google-chrome-stable",
			"/snap/bin/chromium",
		]
		
		browser_path = None
		for path in common_browser_paths:
			if os.path.exists(path):
				browser_path = path
				self.logger.info(f"Auto-detected browser at: {browser_path}")
				break
		
		if browser_path is None:
			self.logger.warning("No browser executable found. Install chromium-browser or google-chrome.")
		
		return browser_path

	@property
	def required_services(self) -> Dict[str, str]:
		"""Browser has no required services."""
		return {}

	@property
	def optional_services(self) -> Dict[str, str]:
		"""Browser has no optional services."""
		return {}

	@property
	def required_config(self) -> Dict[str, str]:
		"""Browser has no required config beyond BotConfig."""
		return {}

	async def _initialize(self) -> None:
		"""Initialize browser instance.

		Called automatically by BaseTool.initialize().
		"""
		# Call parent to register decorated actions
		await super()._initialize()

		# Browser-specific initialization happens lazily when first needed
		self.logger.info("Browser tool initialized (browser instance will start on first use)")

	async def _cleanup(self) -> None:
		"""Cleanup browser resources.

		Called automatically by BaseTool.cleanup().
		"""
		try:
			if self._browser:
				await self._browser.close()
				self._browser = None
				self.logger.debug("Browser closed")

			if self._playwright:
				await self._playwright.stop()
				self._playwright = None
				self.logger.debug("Playwright stopped")

			# Handle Xvfb cleanup
			if self._started_xvfb and self._xvfb_process:
				try:
					if self._xvfb_process.poll() is None:
						self._xvfb_process.terminate()
						self.logger.info(f"Terminated Xvfb process (PID: {self._xvfb_process.pid})")
				except Exception as e:
					self.logger.warning(f"Error cleaning up Xvfb: {e}")

			self.logger.info("Browser cleanup completed")
		except Exception as e:
			self.logger.error(f"Error during browser cleanup: {e}")
			raise

	@BaseTool.action(
		'Search the query in Google in the current tab, the query should be a search query like humans search in Google, concrete and not vague or super long.',
		param_model=SearchGoogleAction
	)
	async def search_google(self, params: SearchGoogleAction, execution_context: Optional[ActionExecutionContext]):
		"""Search Google in the current tab."""
		import time
		start_time = time.time()

		if not isinstance(params, SearchGoogleAction):
			from pydantic import TypeAdapter
			params = TypeAdapter(SearchGoogleAction).validate_python(params)

		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		try:
			page = await browser_context.get_current_page()
			search_url = f'https://www.google.com/search?q={params.query}&udm=14'
			self.logger.info(f"🔍 Starting Google search for: {params.query}")

			response = await page.goto(search_url, timeout=30000, wait_until='domcontentloaded')

			if not response or not response.ok:
				status = response.status if response else 'No response'
				duration = time.time() - start_time
				self.logger.error(f"❌ Google search failed after {duration:.2f}s: HTTP {status}")
				return ActionResult(
					error=f"Google search failed: HTTP {status}",
					include_in_memory=True
				)

			# Wait for page to be ready with proper error handling
			await self._wait_for_page_load(page)

			# Update the cached state after search
			await browser_context._update_state()

			duration = time.time() - start_time
			self.logger.info(f"✅ Google search completed successfully in {duration:.2f}s")

			return ActionResult(
				extracted_content=f'🔍 Searched for "{params.query}" in Google',
				include_in_memory=True
			)
		except asyncio.TimeoutError as e:
			duration = time.time() - start_time
			self.logger.error(f"⏱️  Google search timed out after {duration:.2f}s: {str(e)}")
			return ActionResult(error=f"Search timed out after {duration:.2f}s", include_in_memory=True)
		except Exception as e:
			duration = time.time() - start_time
			self.logger.error(f"❌ Google search failed after {duration:.2f}s: {str(e)}")
			return ActionResult(error=f"Search failed: {str(e)}", include_in_memory=True)

	@BaseTool.action(
		'Navigate to a URL and get page snapshot.',
		param_model=GoToUrlAction
	)
	async def go_to_url(self, params: GoToUrlAction, execution_context: Optional[ActionExecutionContext]):
		"""Navigate to URL and return accessibility snapshot automatically."""
		if not isinstance(params, GoToUrlAction):
			from pydantic import TypeAdapter
			params = TypeAdapter(GoToUrlAction).validate_python(params)

		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		# GUARD: Prevent file:// URLs (local files not supported)
		# BUT allow data: URLs for rendering images
		if (params.url.startswith('file://') or 'file:///' in params.url) and not params.url.startswith('data:'):
			return ActionResult(
				error="Cannot open file:// URLs in browser. If you're trying to view an image, you already have vision capabilities - just analyze the image content that was passed to you in the message. Alternatively, you can use filesystem_read_file to get the image content and create an HTML page with a data URL.",
				include_in_memory=True
			)

		# GUARD: SSRF — reject loopback / RFC1918 / link-local / cloud-metadata
		# hosts before navigating (gated by BROWSER_ALLOW_PRIVATE_URLS). Offload the
		# blocking DNS resolution to a thread so a slow resolver can't freeze the loop.
		ssrf_error = await asyncio.get_running_loop().run_in_executor(
			None, _check_url_ssrf, params.url)
		if ssrf_error:
			self.logger.warning(f"Blocked navigation to {params.url}: {ssrf_error}")
			return ActionResult(error=ssrf_error, include_in_memory=True)

		# GUARD: respect the user-configured allowed_domains allowlist that
		# context.navigate_to() enforces — go_to_url uses page.goto() directly
		# and would otherwise bypass it.
		if not browser_context._is_url_allowed(params.url):
			self.logger.warning(f"Blocked navigation to disallowed domain: {params.url}")
			return ActionResult(
				error=f"Navigation blocked: {params.url} is not in the allowed domains list",
				include_in_memory=True,
			)

		try:
			page = await browser_context.get_current_page()
			self.logger.info(f"Navigating to {params.url}")

			# Navigate with timeout
			response = await page.goto(params.url, timeout=30000, wait_until='domcontentloaded')

			if not response or not response.ok:
				status = response.status if response else 'No response'
				return ActionResult(
					error=f"Navigation failed: HTTP {status}",
					include_in_memory=True
				)

			# Wait for page to be ready with proper error handling
			await self._wait_for_page_load(page)

			# Update the cached state after navigation
			await browser_context._update_state()

			self.logger.info(f"Successfully navigated to {page.url}")

			# FIX (Context Optimization): Return success confirmation only
			# Agent sees full page structure in next prompt via BrowserState.element_tree
			# No need to duplicate page content in tool result (saves 50k+ chars per navigation)
			try:
				page_title = await page.title()
				page_url = str(page.url)

				# Count interactive elements from browser state if available
				element_count = 0
				if browser_context and hasattr(browser_context, 'session'):
					session = await browser_context.get_session()
					if session and hasattr(session, 'cached_state'):
						element_count = len(session.cached_state.selector_map)

				# Return concise confirmation
				result_text = f"✅ Navigated to {page_url}\nTitle: {page_title}"
				if element_count > 0:
					result_text += f"\nInteractive elements: {element_count}"

				self.logger.info(f"Navigated to {page_url} - {element_count} elements")

				return ActionResult(
					extracted_content=result_text,
					include_in_memory=True,
					metadata={
						'url': page_url,
						'title': page_title,
						'method': 'navigation_confirmation'
					}
				)
			except Exception as e:
				self.logger.warning(f"Error getting page metadata: {e}")
				return ActionResult(
					extracted_content=f'✅ Navigated to {page.url}',
					include_in_memory=True
				)

		except Exception as e:
			self.logger.error(f"Navigation to {params.url} failed: {str(e)}")
			return ActionResult(error=f"Navigation failed: {str(e)}", include_in_memory=True)

	@BaseTool.action(
		'Go back to the previous page.',
		param_model=NoParamsAction
	)
	async def go_back_action(self, params: NoParamsAction, execution_context: Optional[ActionExecutionContext]):
		"""Navigate back to the previous page."""
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		try:
			page = await browser_context.get_current_page()
			await page.go_back()
			await self._wait_for_page_load(page)
			return ActionResult(
				extracted_content='🔙 Navigated back',
				include_in_memory=True
			)
		except Exception as e:
			return ActionResult(error=f"Go back failed: {str(e)}", include_in_memory=True)

	@BaseTool.action(
		'Click on an element by its index from the previous observation.',
		param_model=ClickElementAction
	)
	async def click_element(self, params: ClickElementAction, execution_context: Optional[ActionExecutionContext]):
		"""Click an element by its index."""
		if not isinstance(params, ClickElementAction):
			from pydantic import TypeAdapter
			params = TypeAdapter(ClickElementAction).validate_python(params)

		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		try:
			context = browser_context
			session = await context.get_session()
			state = session.cached_state

			if params.index not in state.selector_map:
				return ActionResult(
					error=f'Element with index {params.index} does not exist - retry or use alternative actions',
					include_in_memory=True
				)

			element_node = state.selector_map[params.index]
			initial_pages = len(session.context.pages)

			# Check if element has file uploader
			if hasattr(self, 'is_file_uploader') and await self.is_file_uploader(element_node):
				return ActionResult(
					extracted_content=f'Index {params.index} - has an element which opens file upload dialog. To upload files please use a specific function to upload files',
					include_in_memory=True
				)

			download_path = await context._click_element_node(element_node)
			if download_path:
				msg = f'💾 Downloaded file to {download_path}'
			else:
				msg = f'🖱️ Clicked element with index {params.index}: {element_node.get_all_text_till_next_clickable_element(max_depth=2) if hasattr(element_node, "get_all_text_till_next_clickable_element") else ""}'

			if len(session.context.pages) > initial_pages:
				new_tab_msg = 'New tab opened - switching to it'
				msg += f' - {new_tab_msg}'
				await browser_context.switch_to_tab(-1)

			return ActionResult(extracted_content=msg, include_in_memory=True)
		except Exception as e:
			return ActionResult(error=f"Click failed: {str(e)}", include_in_memory=True)

	@BaseTool.action(
		'Input text into an element by its index.',
		param_model=InputTextAction
	)
	async def input_text(self, params: InputTextAction, execution_context: Optional[ActionExecutionContext]):
		"""Input text into an element."""
		if not isinstance(params, InputTextAction):
			from pydantic import TypeAdapter
			params = TypeAdapter(InputTextAction).validate_python(params)

		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		try:
			context = browser_context
			session = await context.get_session()
			state = session.cached_state

			if params.index not in state.selector_map:
				return ActionResult(
					error=f'Element index {params.index} does not exist - retry or use alternative actions',
					include_in_memory=True
				)

			element_node = state.selector_map[params.index]
			await context._input_text_element_node(element_node, params.text)

			return ActionResult(
				extracted_content=f'⌨️ Input {params.text} into index {params.index}',
				include_in_memory=True
			)
		except Exception as e:
			return ActionResult(error=f"Input text failed: {str(e)}", include_in_memory=True)

	@BaseTool.action(
		'Open a new browser tab with the specified URL.',
		param_model=OpenTabAction
	)
	async def open_tab(self, params: OpenTabAction, execution_context: Optional[ActionExecutionContext]):
		"""Open new tab with URL."""
		if not isinstance(params, OpenTabAction):
			from pydantic import TypeAdapter
			params = TypeAdapter(OpenTabAction).validate_python(params)

		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		# GUARD: SSRF — same protection as go_to_url for the new-tab nav path.
		# Offload the blocking DNS resolution to a thread (see go_to_url).
		ssrf_error = await asyncio.get_running_loop().run_in_executor(
			None, _check_url_ssrf, params.url)
		if ssrf_error:
			self.logger.warning(f"Blocked open_tab to {params.url}: {ssrf_error}")
			return ActionResult(error=ssrf_error, include_in_memory=True)

		try:
			context = browser_context
			self.logger.info(f"Opening new tab with {params.url}")

			# Create new page
			page = await context.session.context.new_page()
			
			# Navigate to URL with timeout
			response = await page.goto(params.url, timeout=30000, wait_until='domcontentloaded')
			
			if not response or not response.ok:
				status = response.status if response else 'No response'
				await page.close()  # Close the failed tab
				return ActionResult(
					error=f"Failed to load page: HTTP {status}",
					include_in_memory=True
				)
			
			# Wait for page to be ready with proper error handling
			await self._wait_for_page_load(page)
			
			# Switch to the new tab
			context.session.current_page = page
			await page.bring_to_front()
			
			# Update the cached state
			await context._update_state()
			
			self.logger.info(f"Successfully opened new tab with {page.url}")
			return ActionResult(
				extracted_content=f'📑 Opened new tab with {page.url}',
				include_in_memory=True
			)
		except Exception as e:
			self.logger.error(f"Open tab failed: {str(e)}")
			return ActionResult(error=f"Open tab failed: {str(e)}", include_in_memory=True)

	@BaseTool.action(
		'Switch to a different browser tab by index.',
		param_model=SwitchTabAction
	)
	async def switch_tab(self, params: SwitchTabAction, execution_context: Optional[ActionExecutionContext]):
		"""Switch to a browser tab by index."""
		if not isinstance(params, SwitchTabAction):
			from pydantic import TypeAdapter
			params = TypeAdapter(SwitchTabAction).validate_python(params)

		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		try:
			context = browser_context
			pages = context.session.context.pages if context.session else []
			if 0 <= params.page_id < len(pages):
				page = pages[params.page_id]
				context.session.current_page = page
				await page.bring_to_front()
				
				# Update the cached state after switching tabs
				await context._update_state()
				
				self.logger.info(f"Switched to tab {params.page_id}: {page.url}")
				msg = f'🔄 Switched to tab {params.page_id}: {page.url}'
			else:
				return ActionResult(
					error=f'Tab index {params.page_id} out of range (0-{len(pages)-1})',
					include_in_memory=True
				)
			return ActionResult(extracted_content=msg, include_in_memory=True)
		except Exception as e:
			self.logger.error(f"Switch tab failed: {str(e)}")
			return ActionResult(error=str(e), include_in_memory=True)

	@BaseTool.action(
		'Scroll the page in the specified direction (up, down, left, right).',
		param_model=ScrollAction
	)
	async def scroll(self, params: ScrollAction, execution_context: Optional[ActionExecutionContext]):
		"""Scroll the page."""
		if not isinstance(params, ScrollAction):
			from pydantic import TypeAdapter
			params = TypeAdapter(ScrollAction).validate_python(params)

		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		try:
			page = await browser_context.get_current_page()
			direction = params.direction
			amount = params.amount or 500  # Default scroll amount

			if direction == "up":
				await page.evaluate(f"window.scrollBy(0, -{amount})")
				msg = f'⬆️ Scrolled up {amount}px'
			elif direction == "down":
				await page.evaluate(f"window.scrollBy(0, {amount})")
				msg = f'⬇️ Scrolled down {amount}px'
			elif direction == "left":
				await page.evaluate(f"window.scrollBy(-{amount}, 0)")
				msg = f'⬅️ Scrolled left {amount}px'
			elif direction == "right":
				await page.evaluate(f"window.scrollBy({amount}, 0)")
				msg = f'➡️ Scrolled right {amount}px'
			else:
				msg = f'❌ Invalid scroll direction: {direction}'

			return ActionResult(extracted_content=msg, include_in_memory=True)
		except Exception as e:
			return ActionResult(error=f"Scroll failed: {str(e)}", include_in_memory=True)

	@BaseTool.action(
		'Send keyboard keys/shortcuts (e.g., "Enter", "Escape", "Control+A").',
		param_model=SendKeysAction
	)
	async def send_keys(self, params: SendKeysAction, execution_context: Optional[ActionExecutionContext]):
		"""Send keyboard keys."""
		if not isinstance(params, SendKeysAction):
			from pydantic import TypeAdapter
			params = TypeAdapter(SendKeysAction).validate_python(params)

		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		try:
			page = await browser_context.get_current_page()
			await page.keyboard.press(params.keys)
			return ActionResult(
				extracted_content=f'⌨️ Sent keys: {params.keys}',
				include_in_memory=True
			)
		except Exception as e:
			return ActionResult(error=f"Send keys failed: {str(e)}", include_in_memory=True)

	@BaseTool.action(
		'Extract the current page content as text.',
		param_model=ExtractPageContentAction
	)
	async def extract_page_content(self, params: ExtractPageContentAction, execution_context: Optional[ActionExecutionContext]):
		"""Extract page content using accessibility snapshot for clean semantic content."""

		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		try:
			page = await browser_context.get_current_page()

			# FIX (Nov 6, 2025): Use accessibility API for clean semantic content
			# Previously returned raw HTML (1-2M chars, 99% noise)
			# Now returns accessibility snapshot (30-80K chars, 100% signal)

			# FIX (Context Optimization v2): Adaptive truncation - balance data vs context
			# Previous 3k limit was too aggressive - agent couldn't complete tasks (see logs: repeated extractions, task loops)
			# New strategy: Provide useful data while preventing context bloat
			# - Small/Medium content (<25k): Keep all - agent needs data to work
			# - Large content (>25k): Truncate to 25k - still substantial, prevents overflow
			# - Compaction at 65% handles overflow if needed
			MAX_EXTRACT_CHARS = 25000  # 8x increase from 3k - reasonable for 128k+ models

			try:
				snapshot = await page.accessibility.snapshot()

				if snapshot:
					# Format snapshot as readable text
					formatted_content = self._format_accessibility_snapshot(snapshot)

					# Add page metadata
					page_title = await page.title()
					page_url = str(page.url)

					# Combine into result
					full_content = f"# {page_title}\n\nURL: {page_url}\n\n{formatted_content}"

					# Adaptive truncation
					if len(full_content) > MAX_EXTRACT_CHARS:
						truncated_content = full_content[:MAX_EXTRACT_CHARS]
						removed_chars = len(full_content) - MAX_EXTRACT_CHARS
						result_text = f"{truncated_content}\n\n[... {removed_chars:,} more chars truncated. Use scroll + extract to see more, or navigate to sub-sections.]"

						self.logger.info(
							f"Extracted page content - {len(full_content):,} chars truncated to {len(result_text):,} chars"
						)
					else:
						result_text = full_content
						self.logger.info(
							f"Extracted page content - {len(result_text):,} chars (accessibility API)"
						)

					return ActionResult(
						extracted_content=result_text,
						include_in_memory=True,
						metadata={
							'url': page_url,
							'title': page_title,
							'method': 'accessibility_snapshot',
							'size': len(result_text),
							'truncated': len(full_content) > MAX_EXTRACT_CHARS
						}
					)
			except Exception as snapshot_error:
				self.logger.warning(f"Accessibility snapshot failed: {snapshot_error}, falling back to HTML extraction")

			# Fallback: If accessibility snapshot fails, use raw HTML with truncation
			content = await page.content()

			# Truncate to reasonable size with guidance
			if len(content) > 50000:
				self.logger.info(f"Large page ({len(content):,} chars), truncating to 50K with guidance")
				content = content[:50000] + "\n\n[Content truncated - page has more content. Use CSS selectors to extract specific sections, or navigate to sub-pages for detailed content.]"

			return ActionResult(
				extracted_content=content,
				include_in_memory=True,
				metadata={
					'method': 'html_fallback',
					'truncated': len(content) > 50000
				}
			)

		except Exception as e:
			self.logger.error(f"Extract failed: {str(e)}")
			return ActionResult(error=f"Extract failed: {str(e)}", include_in_memory=True)

	@BaseTool.action(
		'Refresh the current page.',
		param_model=NoParamsAction
	)
	async def refresh_page(self, params: NoParamsAction, execution_context: Optional[ActionExecutionContext]):
		"""Refresh the page."""

		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		try:
			page = await browser_context.get_current_page()
			self.logger.info(f"Refreshing page: {page.url}")
			
			await page.reload(timeout=30000, wait_until='domcontentloaded')
			
			# Wait for page to be ready with proper error handling
			await self._wait_for_page_load(page)
			
			# Update the cached state after refresh
			await browser_context._update_state()
			
			return ActionResult(
				extracted_content='🔄 Page refreshed',
				include_in_memory=True
			)
		except Exception as e:
			self.logger.error(f"Refresh failed: {str(e)}")
			return ActionResult(error=f"Refresh failed: {str(e)}", include_in_memory=True)

	@BaseTool.action(
		'Scroll down the page.',
		param_model=NoParamsAction
	)
	async def scroll_down(self, params: NoParamsAction, execution_context: Optional[ActionExecutionContext]):
		"""Scroll down."""
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		try:
			page = await browser_context.get_current_page()
			await page.evaluate("window.scrollBy(0, 500)")
			return ActionResult(
				extracted_content='⬇️ Scrolled down',
				include_in_memory=True
			)
		except Exception as e:
			return ActionResult(error=f"Scroll down failed: {str(e)}", include_in_memory=True)

	@BaseTool.action(
		'Scroll up the page.',
		param_model=NoParamsAction
	)
	async def scroll_up(self, params: NoParamsAction, execution_context: Optional[ActionExecutionContext]):
		"""Scroll up."""
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		try:
			page = await browser_context.get_current_page()
			await page.evaluate("window.scrollBy(0, -500)")
			return ActionResult(
				extracted_content='⬆️ Scrolled up',
				include_in_memory=True
			)
		except Exception as e:
			return ActionResult(error=f"Scroll up failed: {str(e)}", include_in_memory=True)

	@BaseTool.action(
		'Scroll to specific text on the page.',
		param_model=SendKeysAction
	)
	async def scroll_to_text(self, params: SendKeysAction, execution_context: Optional[ActionExecutionContext]):
		"""Scroll to text."""
		if not isinstance(params, SendKeysAction):
			from pydantic import TypeAdapter
			params = TypeAdapter(SendKeysAction).validate_python(params)

		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		try:
			page = await browser_context.get_current_page()
			# Use text locator to find and scroll to element
			locator = page.get_by_text(params.keys, exact=False).first
			if await locator.count() > 0:
				await locator.scroll_into_view_if_needed()
				return ActionResult(
					extracted_content=f'📜 Scrolled to text: {params.keys}',
					include_in_memory=True
				)
			else:
				return ActionResult(
					error=f'Text "{params.keys}" not found on page',
					include_in_memory=True
				)
		except Exception as e:
			return ActionResult(error=f"Scroll to text failed: {str(e)}", include_in_memory=True)

	@BaseTool.action(
		'Get available options from a dropdown element by its index.',
		param_model=ClickElementAction
	)
	async def get_dropdown_options(self, params: ClickElementAction, execution_context: Optional[ActionExecutionContext]):
		"""Get dropdown options."""
		if not isinstance(params, ClickElementAction):
			from pydantic import TypeAdapter
			params = TypeAdapter(ClickElementAction).validate_python(params)

		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		try:
			context = browser_context
			session = await context.get_session()
			state = session.cached_state

			if params.index not in state.selector_map:
				return ActionResult(
					error=f'Element index {params.index} does not exist',
					include_in_memory=True
				)

			element_node = state.selector_map[params.index]
			locate_result = await context.get_locate_element(element_node)
			locator = locate_result.locator

			# Get all option elements
			options = await locator.locator('option').all_text_contents()
			return ActionResult(
				extracted_content=f'📋 Dropdown options: {", ".join(options)}',
				include_in_memory=True
			)
		except Exception as e:
			return ActionResult(error=f"Get dropdown options failed: {str(e)}", include_in_memory=True)

	@BaseTool.action(
		'Select a dropdown option by its text value.',
		param_model=InputTextAction
	)
	async def select_dropdown_option(self, params: InputTextAction, execution_context: Optional[ActionExecutionContext]):
		"""Select dropdown option."""
		if not isinstance(params, InputTextAction):
			from pydantic import TypeAdapter
			params = TypeAdapter(InputTextAction).validate_python(params)

		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error="Browser context not available", include_in_memory=True)

		try:
			context = browser_context
			session = await context.get_session()
			state = session.cached_state

			if params.index not in state.selector_map:
				return ActionResult(
					error=f'Element index {params.index} does not exist',
					include_in_memory=True
				)

			element_node = state.selector_map[params.index]
			locate_result = await context.get_locate_element(element_node)
			locator = locate_result.locator

			# Select by label/text
			await locator.select_option(label=params.text)
			return ActionResult(
				extracted_content=f'✅ Selected "{params.text}" from dropdown at index {params.index}',
				include_in_memory=True
			)
		except Exception as e:
			return ActionResult(error=f"Select dropdown option failed: {str(e)}", include_in_memory=True)

	async def is_file_uploader(self, element_node):
		"""Check if element opens file uploader (placeholder)."""
		# This is a placeholder - actual implementation would check element type
		return False

	async def _wait_for_page_load(self, page, timeout: int = 5000):
		"""Helper to wait for page load with proper timeout handling.
		
		Args:
			page: The Playwright page object
			timeout: Timeout in milliseconds for networkidle (default 5s)
		"""
		try:
			# Try to wait for networkidle, but don't fail if it times out
			await page.wait_for_load_state('networkidle', timeout=timeout)
		except Exception as e:
			# If networkidle times out, page might still be usable
			# Just log a debug message and continue
			self.logger.debug(f"Network idle timeout (this is normal for pages with long-polling): {str(e)}")
			# Ensure DOM is at least loaded
			try:
				await page.wait_for_load_state('domcontentloaded', timeout=2000)
			except Exception:
				pass  # Even domcontentloaded might fail on some pages

	async def new_context(self, config: BrowserContextConfig = BrowserContextConfig(), session_id: Optional[str] = None) -> BrowserContext:
		"""Create a browser context"""
		return BrowserContext(config=config, browser=self, session_id=session_id)

	async def get_playwright_browser(self) -> PlaywrightBrowser:
		"""Get a browser context"""
		if self._browser is None:
			return await self._init()

		return self._browser

	async def _init(self):
		"""Initialize the browser session"""
		self._playwright = await async_playwright().start()
		self._browser = await self._setup_browser(self._playwright)

		return self._browser

	async def _setup_cdp(self, playwright: Playwright) -> PlaywrightBrowser:
		"""Sets up and returns a Playwright Browser instance with anti-detection measures."""
		if not self.browser_config.cdp_url:
			raise ValueError('CDP URL is required')
		self.logger.info(f'Connecting to remote browser via CDP {self.browser_config.cdp_url}')
		browser = await playwright.chromium.connect_over_cdp(self.browser_config.cdp_url)
		return browser

	async def _setup_wss(self, playwright: Playwright) -> PlaywrightBrowser:
		"""Sets up and returns a Playwright Browser instance with anti-detection measures."""
		if not self.browser_config.wss_url:
			raise ValueError('WSS URL is required')
		self.logger.info(f'Connecting to remote browser via WSS {self.browser_config.wss_url}')
		browser = await playwright.chromium.connect(self.browser_config.wss_url)
		return browser

	async def _setup_browser_with_instance(self, playwright: Playwright) -> PlaywrightBrowser:
		"""Sets up and returns a Playwright Browser instance with anti-detection measures."""
		if not self.browser_config.chrome_instance_path:
			raise ValueError('Chrome instance path is required')
		import subprocess

		import requests

		try:
			# Check if browser is already running
			response = requests.get('http://localhost:9222/json/version', timeout=2)
			if response.status_code == 200:
				self.logger.info('Reusing existing Chrome instance')
				browser = await playwright.chromium.connect_over_cdp(
					endpoint_url='http://localhost:9222',
					timeout=20000,  # 20 second timeout for connection
				)
				return browser
		except requests.ConnectionError:
			self.logger.debug('No existing Chrome instance found, starting a new one')

		# Start a new Chrome instance
		subprocess.Popen(
			[
				self.browser_config.chrome_instance_path,
				'--remote-debugging-port=9222',
			]
			+ self.browser_config.extra_chromium_args,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)

		# Attempt to connect again after starting a new instance
		for _ in range(10):
			try:
				response = requests.get('http://localhost:9222/json/version', timeout=2)
				if response.status_code == 200:
					break
			except requests.ConnectionError:
				pass
			await asyncio.sleep(1)

		# Attempt to connect again after starting a new instance
		try:
			browser = await playwright.chromium.connect_over_cdp(
				endpoint_url='http://localhost:9222',
				timeout=20000,  # 20 second timeout for connection
			)
			return browser
		except Exception as e:
			self.logger.error(f'Failed to start a new Chrome instance.: {str(e)}')
			raise RuntimeError(
				' To start chrome in Debug mode, you need to close all existing Chrome instances and try again otherwise we can not connect to the instance.'
			)

	async def _setup_standard_browser(self, playwright: Playwright) -> PlaywrightBrowser:
		"""Standard browser setup with proper server environment handling."""
		browser_args = self._configure_server_args(self.server_args)
		
		# If in server environment, double-check headless mode
		if self.browser_config.auto_configure_for_server:
			# Check for server environment indicators
			is_server_env = (
				"SSH_CONNECTION" in os.environ or
				"SSH_CLIENT" in os.environ or
				not os.environ.get("DISPLAY") or
				os.environ.get("XDG_SESSION_TYPE") == "tty"
			)
			
			# Final headless check for server environments
			if is_server_env and not self.browser_config.headless:
				logging.info("Server environment detected, overriding to headless mode")
				self.browser_config.headless = True
		
		try:
			browser = await playwright.chromium.launch(
				headless=self.browser_config.headless,
				slow_mo=self.slow_mo,
				args=browser_args,
				timeout=self.timeout,
				executable_path=self.browser_path,
				env=self.env,
			)
			return browser
		except Exception as e:
			if not self.browser_config.headless:
				# If browser launch fails in headed mode, try again in headless mode
				logging.warning(f"Browser launch failed in headed mode: {str(e)}")
				logging.info("Retrying with headless=True")
				self.browser_config.headless = True
				browser = await playwright.chromium.launch(
					headless=True,
					slow_mo=self.slow_mo,
					args=browser_args,
					timeout=self.timeout,
					executable_path=self.browser_path,
					env=self.env,
				)
				return browser
			else:
				# If already in headless mode, re-raise the exception
				raise

	async def _setup_browser(self, playwright: Playwright) -> PlaywrightBrowser:
		"""Sets up and returns a Playwright Browser instance with anti-detection measures."""
		try:
			if self.browser_config.cdp_url:
				return await self._setup_cdp(playwright)
			if self.browser_config.wss_url:
				return await self._setup_wss(playwright)
			elif self.browser_config.chrome_instance_path:
				return await self._setup_browser_with_instance(playwright)
			else:
				return await self._setup_standard_browser(playwright)
		except Exception as e:
			self.logger.error(f'Failed to initialize Playwright browser: {str(e)}')
			raise

	async def close(self):
		"""Close browser and clean up resources."""
		await self._cleanup()
		
		# Handle Xvfb cleanup
		if self._started_xvfb and self._xvfb_process:
			try:
				# Check if process is still running
				if self._xvfb_process.poll() is None:
					self._xvfb_process.terminate()
					self.logger.info(f"Terminated Xvfb process (PID: {self._xvfb_process.pid})")
			except Exception as e:
				self.logger.warning(f"Error cleaning up Xvfb: {e}")
			
		self.logger.info("Browser closed and resources cleaned up")

	def _format_accessibility_snapshot(self, node, indent=0):
		"""
		Format accessibility tree as readable text.

		Args:
			node: Accessibility tree node from Playwright
			indent: Current indentation level

		Returns:
			List of formatted text lines
		"""
		lines = []
		role = node.get('role', '')
		name = node.get('name', '')

		# Skip empty or decorative elements
		if not name or role in ['none', 'presentation']:
			# Still process children of empty containers
			for child in node.get('children', []):
				lines.extend(self._format_accessibility_snapshot(child, indent))
			return lines

		# Format based on role type
		prefix = '  ' * indent

		if role == 'heading':
			# Format headings with markdown
			level = node.get('level', 1)
			lines.append(f"\n{'#' * level} {name}")
		elif role == 'link':
			# Format links with arrow
			lines.append(f"{prefix}→ {name}")
		elif role == 'button':
			# Format buttons with brackets
			lines.append(f"{prefix}[{name}]")
		elif role == 'listitem':
			# Format list items with bullets
			lines.append(f"{prefix}• {name}")
		elif role in ['paragraph', 'article', 'main', 'text']:
			# Format text content
			lines.append(f"{prefix}{name}")
		elif role == 'code':
			# Format code blocks
			lines.append(f"\n```\n{name}\n```")
		else:
			# Generic content - include if has meaningful text
			if name.strip():
				lines.append(f"{prefix}{name}")

		# Recursively process children
		for child in node.get('children', []):
			lines.extend(self._format_accessibility_snapshot(child, indent + 1))

		return '\n'.join(lines)

	async def __aenter__(self):
		"""Async context manager entry - allows `async with Browser() as browser:`"""
		await self.initialize()
		return self

	async def __aexit__(self, exc_type, exc_val, exc_tb):
		"""Async context manager exit - ensures proper cleanup."""
		await self.close()
		return False  # Don't suppress exceptions

	def __del__(self):
		"""Cleanup resources when object is garbage collected.

		WARNING: This is a last resort cleanup. Always prefer using:
		- `async with Browser() as browser:` (context manager)
		- Explicit `await browser.close()` call

		Async cleanup in __del__ is unreliable and can cause issues.
		This method only handles synchronous cleanup (like Xvfb processes).
		"""
		# Log warning if browser wasn't properly closed
		if hasattr(self, '_browser') and self._browser:
			if hasattr(self, 'logger'):
				self.logger.warning(
					"Browser was not properly closed before garbage collection. "
					"Use 'async with Browser()' or call 'await browser.close()' explicitly."
				)
			else:
				logging.warning("Browser not properly closed before GC")

			# Force nullify Python references (doesn't close actual browser process)
			self._browser = None
			self._playwright = None

		# Synchronous cleanup of Xvfb process (this is safe in __del__)
		if hasattr(self, '_started_xvfb') and self._started_xvfb:
			if hasattr(self, '_xvfb_process') and self._xvfb_process:
				try:
					if self._xvfb_process.poll() is None:
						self._xvfb_process.terminate()
						# Give it a moment to terminate
						try:
							self._xvfb_process.wait(timeout=2)
						except Exception:
							# Force kill if termination times out
							self._xvfb_process.kill()
				except Exception as e:
					if hasattr(self, 'logger'):
						self.logger.debug(f"Error cleaning up Xvfb in __del__: {e}")
					# Swallow exceptions in __del__ to prevent crashes

	async def _cleanup(self):
		"""Async cleanup of browser resources with timeout protection."""
		import asyncio

		async def cleanup_with_timeout():
			try:
				if self._browser:
					await self._browser.close()
					self._browser = None

				if self._playwright:
					await self._playwright.stop()
					self._playwright = None
			except Exception as e:
				self.logger.warning(f"Error in browser async cleanup: {e}")

		try:
			# Give cleanup 5 seconds max before force killing
			await asyncio.wait_for(cleanup_with_timeout(), timeout=5.0)
		except asyncio.TimeoutError:
			self.logger.warning("Browser cleanup timed out after 5 seconds - forcing cleanup")
			# Force cleanup
			self._browser = None
			self._playwright = None
		except Exception as e:
			self.logger.warning(f"Error in browser cleanup: {e}")

