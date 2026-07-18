"""
Playwright browser on steroids.
"""

import asyncio
import base64
import gc
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, TypedDict
from datetime import datetime

from playwright.async_api import TimeoutError
from playwright.async_api import Browser as PlaywrightBrowser
from playwright.async_api import (
	BrowserContext as PlaywrightBrowserContext,
)
from playwright.async_api import (
	ElementHandle,
	FrameLocator,
	Page,
)

from tools.browser.views import (
	BrowserError,
	BrowserState,
	TabInfo,
	URLNotAllowedError,
)
from tools.dom.service import DomService
from tools.dom.views import DOMElementNode, SelectorMap
from utils.time_utils import time_execution_sync

if TYPE_CHECKING:
	from tools.browser.browser import Browser

logger = logging.getLogger(__name__)


class BrowserContextWindowSize(TypedDict):
	width: int
	height: int


@dataclass
class BrowserContextConfig:
	"""
	Configuration for the BrowserContext.

	Default values:
	    cookies_file: None
	        Path to cookies file for persistence

	        disable_security: False
	                Disable browser security features (safe default - opt-in relaxations only)

	    minimum_wait_page_load_time: 0.5
	        Minimum time to wait before getting page state for LLM input

	        wait_for_network_idle_page_load_time: 1.0
	                Time to wait for network requests to finish before getting page state.
	                Lower values may result in incomplete page loads.

	    maximum_wait_page_load_time: 5.0
	        Maximum time to wait for page load before proceeding anyway

	    wait_between_actions: 1.0
	        Time to wait between multiple per step actions

	    browser_window_size: {
	            'width': 1280,
	            'height': 1100,
	        }
	        Default browser window size

	    no_viewport: False
	        Disable viewport

	    save_recording_path: None
	        Path to save video recordings

	    save_downloads_path: None
	        Path to save downloads to

	    trace_path: None
	        Path to save trace files. It will auto name the file with the TRACE_PATH/{context_id}.zip

	    locale: None
	        Specify user locale, for example en-GB, de-DE, etc. Locale will affect navigator.language value, Accept-Language request header value as well as number and date formatting rules. If not provided, defaults to the system default locale.

	    user_agent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/85.0.4183.102 Safari/537.36'
	        custom user agent to use.

	    highlight_elements: True
	        Highlight elements in the DOM on the screen

	    viewport_expansion: 500
	        Viewport expansion in pixels. This amount will increase the number of elements which are included in the state what the LLM will see. If set to -1, all elements will be included (this leads to high token usage). If set to 0, only the elements which are visible in the viewport will be included.

	    allowed_domains: None
	        List of allowed domains that can be accessed. If None, all domains are allowed.
	        Example: ['example.com', 'api.example.com']

	    include_dynamic_attributes: bool = True
	        Include dynamic attributes in the CSS selector. If you want to reuse the css_selectors, it might be better to set this to False.
	"""

	cookies_file: str | None = None
	minimum_wait_page_load_time: float = 0.5
	wait_for_network_idle_page_load_time: float = 1
	maximum_wait_page_load_time: float = 5
	wait_between_actions: float = 1

	disable_security: bool = False

	browser_window_size: BrowserContextWindowSize = field(default_factory=lambda: {'width': 1280, 'height': 1100})
	no_viewport: Optional[bool] = None

	save_recording_path: str | None = None
	save_downloads_path: str | None = None
	trace_path: str | None = None
	locale: str | None = None
	user_agent: str = (
		'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36  (KHTML, like Gecko) Chrome/85.0.4183.102 Safari/537.36'
	)

	highlight_elements: bool = True
	viewport_expansion: int = 500
	allowed_domains: list[str] | None = None
	include_dynamic_attributes: bool = True

	_force_keep_context_alive: bool = False


@dataclass
class BrowserSession:
	context: PlaywrightBrowserContext
	current_page: Page
	cached_state: BrowserState
	session_id: Optional[str] = None


class BrowserContext:
	def __init__(
		self,
		browser: 'Browser',
		config: BrowserContextConfig = BrowserContextConfig(),
		session_id: Optional[str] = None
	):
		# Set up logger with session ID
		logger_name = "task.context"
		if session_id:
			short_id = session_id[:8]
			logger_name = f"{logger_name}[{short_id}]"
		self.logger = logging.getLogger(logger_name)
		
		self.browser = browser
		self.config = config
		self.session_id = session_id  # Store the session ID

		# Initialize these as None - they'll be set up when needed
		self.session: BrowserSession | None = None

	async def __aenter__(self):
		"""Async context manager entry"""
		await self._initialize_session()
		return self

	async def __aexit__(self, exc_type, exc_val, exc_tb):
		"""Async context manager exit - enhanced for proper WebSocket cleanup"""
		self.logger.debug(f"BrowserContext.__aexit__ called with exc_type={exc_type}")
		
		# First close all pages explicitly to ensure WebSocket connections close properly
		try:
			if self.session and self.session.context and self.session.context.pages:
				for page in self.session.context.pages:
					try:
						await page.close(timeout=3000)
					except Exception as e:
						self.logger.debug(f"Error closing page during __aexit__: {e}")
		except Exception as e:
			self.logger.warning(f"Error closing pages during __aexit__: {e}")
			
		# Then close the whole browser context
		try:
			await asyncio.wait_for(self.close(), timeout=15.0)
		except asyncio.TimeoutError:
			self.logger.warning("Context close timed out in __aexit__, resources may not be fully cleaned up")
		except Exception as e:
			self.logger.warning(f"Error during __aexit__ context close: {e}")
			
		# Make sure any orphaned WebSocket connections are closed
		try:
			# Wait a moment for any connections to finish closing
			await asyncio.sleep(0.5)
		except Exception:
			pass

	async def close(self):
		"""Close the browser instance and properly clean up all resources"""
		self.logger.debug('Closing browser context')

		try:
			# check if already closed
			if self.session is None:
				return

			# Save cookies before closing
			try:
				await self.save_cookies()
			except Exception as e:
				self.logger.debug(f'Failed to save cookies during closure: {e}')

			# Save trace if configured
			if self.config.trace_path:
				try:
					# WS-3: clean the session id before it becomes a filename.
					_stem = self.session_id or uuid.uuid4()
					try:
						from agents.task.path import pm as _pm
						_stem = _pm().clean_session_id(str(_stem))
					except Exception:
						_stem = str(_stem)
					trace_path = os.path.join(self.config.trace_path, f'{_stem}.zip')
					await self.session.context.tracing.stop(path=trace_path)
					self.logger.debug(f'Saved trace to {trace_path}')
				except Exception as e:
					self.logger.debug(f'Failed to stop tracing: {e}')

			# Close all pages first to ensure clean disconnection of websockets
			try:
				if self.session.context and self.session.context.pages:
					for page in self.session.context.pages:
						try:
							await page.close(timeout=5000)  # Higher timeout for page closing
						except Exception as page_error:
							self.logger.debug(f'Error closing page: {page_error}')
			except Exception as pages_error:
				self.logger.debug(f'Error closing pages: {pages_error}')

			# Close the context more forcefully if needed
			if not self.config._force_keep_context_alive:
				try:
					# Set a reasonable timeout for context closure
					await asyncio.wait_for(self.session.context.close(), timeout=10.0)
					self.logger.debug('Browser context closed successfully')
				except asyncio.TimeoutError:
					self.logger.warning('Context close timed out, attempting force close')
					try:
						# Force close using lower-level APIs if available
						if hasattr(self.session.context, '_impl_obj'):
							await self.session.context._impl_obj.close()
					except Exception as force_close_error:
						self.logger.warning(f'Force close failed: {force_close_error}')
				except Exception as e:
					self.logger.warning(f'Failed to close context gracefully: {e}')
					# Try to force close anyway
					try:
						if hasattr(self.session.context, '_impl_obj'):
							await self.session.context._impl_obj.close()
					except Exception:
						pass
		except Exception as e:
			self.logger.warning(f'Exception during browser context closure: {e}')
		finally:
			# Always ensure session is set to None
			self.session = None
			
			# Force garbage collection to release resources
			try:
				import gc
				gc.collect()
			except Exception:
				pass
				
	async def __aenter__(self):
		"""Async context manager entry - allows `async with BrowserContext() as ctx:`"""
		await self._initialize_session()
		return self

	async def __aexit__(self, exc_type, exc_val, exc_tb):
		"""Async context manager exit - ensures proper cleanup."""
		await self.close()
		return False  # Don't suppress exceptions

	def __del__(self):
		"""Cleanup when object is destroyed through garbage collection.

		WARNING: This is a last resort cleanup. Always prefer using:
		- `async with BrowserContext() as context:` (context manager)
		- Explicit `await context.close()` call

		Async operations in __del__ are problematic and unreliable.
		This method just logs a warning and nullifies references.
		"""
		if self.session is not None:
			self.logger.warning(
				'BrowserContext was not properly closed before being garbage collected. '
				'Use "async with BrowserContext()" or call "await context.close()" explicitly.'
			)

			# Just nullify references - don't attempt async operations in __del__
			# This prevents deadlocks and event loop issues
			self.session = None

			# Explicit garbage collection to help clean up
			try:
				import gc
				gc.collect()
			except Exception:
				pass  # Swallow exceptions in __del__

	async def _initialize_session(self):
		"""Initialize the browser session with improved WebSocket handling"""
		self.logger.debug('Initializing browser context')

		try:
			playwright_browser = await self.browser.get_playwright_browser()
			
			# Configure better connection parameters
			context_options = {}
			
			# Add connection timeout settings to prevent hanging connections
			context_options["viewport"] = self.config.browser_window_size
			context_options["no_viewport"] = False
			context_options["user_agent"] = self.config.user_agent
			context_options["java_script_enabled"] = True
			context_options["bypass_csp"] = self.config.disable_security
			context_options["ignore_https_errors"] = self.config.disable_security
			context_options["record_video_dir"] = self.config.save_recording_path
			context_options["record_video_size"] = self.config.browser_window_size
			context_options["locale"] = self.config.locale
			
			# Create context with improved options
			context = await self._create_context(playwright_browser, context_options)
			
			# Set timeouts after context creation
			# These methods are supported on the context object
			context.set_default_timeout(60000)  # 60 seconds default timeout
			context.set_default_navigation_timeout(60000)  # 60 seconds for navigation
			
			# Set up listeners and connection handlers
			self._add_new_page_listener(context)
			
			# Configure proper error handling for WebSocket connections
			self._add_error_listeners(context)

			# Check if there's an existing page we can use
			existing_pages = context.pages
			if existing_pages:
				page = existing_pages[-1]  # Use the last existing page
				self.logger.debug('Reusing existing page')
			else:
				try:
					page = await context.new_page()
					self.logger.debug('Created new page')
				except Exception as e:
					self.logger.error(f"Error creating new page: {e}")
					# Attempt to recover
					self.logger.info("Attempting recovery by reusing context pages")
					pages = context.pages
					if not pages:
						self.logger.error("No pages available for recovery")
						raise
					page = pages[0]  # Use the first page
					
			# Create a controller with the initialized services
			initial_state = self._get_initial_state(page)

			self.session = BrowserSession(
				context=context,
				current_page=page,
				cached_state=initial_state,
				session_id=self.session_id  # Pass session ID to BrowserSession
			)

			# Set the page title to include the full session ID
			if self.session_id:
				try:
					await page.evaluate(f'document.title = "SESSION {self.session_id}"')
				except Exception as e:
					self.logger.debug(f"Failed to set page title: {e}")

			return self.session
		except Exception as e:
			self.logger.error(f"Failed to initialize browser session: {e}")
			raise

	def _add_error_listeners(self, context: PlaywrightBrowserContext):
		"""Add error handlers to properly manage WebSocket connections"""
		async def on_error(error):
			self.logger.warning(f"Browser context error: {error}")
		
		# Add error listener
		context.on("error", on_error)
		
		# Add close listener
		async def on_close():
			self.logger.debug("Browser context closed")
			
		context.on("close", on_close)
		
	async def _ssrf_route_guard(self, route):
		"""Abort a top-level navigation whose (possibly redirected) URL resolves to an
		internal / metadata address. Runs per request, so redirect hops are re-checked.

		Only ``document`` requests are validated (the SSRF-to-metadata vector); all other
		resource types continue immediately to keep interception overhead low. Fail-open
		on the guard's own error (the initial-URL guard in browser.py still applies) so a
		handler bug can't wedge all browsing.
		"""
		try:
			request = route.request
			if request.resource_type == 'document':
				from tools.browser.browser import _check_url_ssrf
				err = await asyncio.get_running_loop().run_in_executor(
					None, _check_url_ssrf, request.url)
				if err:
					self.logger.warning(f"SSRF: aborting navigation to {request.url}: {err}")
					await route.abort('blockedbyclient')
					return
			await route.continue_()
		except Exception as e:
			self.logger.debug(f"SSRF route guard error (continuing): {e}")
			try:
				await route.continue_()
			except Exception:
				pass

	async def _create_context(self, browser: PlaywrightBrowser, context_options: dict = None):
		"""Creates a new browser context with anti-detection measures and loads cookies if available."""
		if context_options is None:
			context_options = {}

		if self.browser.config.cdp_url and len(browser.contexts) > 0:
			context = browser.contexts[0]
		elif self.browser.config.chrome_instance_path and len(browser.contexts) > 0:
			# Connect to existing Chrome instance instead of creating new one
			context = browser.contexts[0]
		else:
			# Original code for creating new context
			context = await browser.new_context(**context_options)

		# SSRF: validate EVERY top-level navigation at request time — including redirect
		# hops — so a public URL that 302-redirects to a link-local / cloud-metadata
		# address is aborted. page.goto only checks the initial URL; with route
		# interception active, Playwright re-fires this handler for each redirect target.
		# Gated by BROWSER_ALLOW_PRIVATE_URLS (same switch as the initial-URL guard).
		if os.getenv('BROWSER_ALLOW_PRIVATE_URLS', 'false').strip().lower() not in ('1', 'true', 'yes', 'on'):
			await context.route("**/*", self._ssrf_route_guard)

		# Load cookies if they exist
		if self.config.cookies_file and os.path.exists(self.config.cookies_file):
			with open(self.config.cookies_file, 'r') as f:
				cookies = json.load(f)
				self.logger.info(f'Loaded {len(cookies)} cookies from {self.config.cookies_file}')
				await context.add_cookies(cookies)

		# Expose anti-detection scripts
		await context.add_init_script(
			"""
            // Webdriver property
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });

            // Languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US']
            });

            // Plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });

            // Chrome runtime
            window.chrome = { runtime: {} };

            // Permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
            (function () {
                const originalAttachShadow = Element.prototype.attachShadow;
                Element.prototype.attachShadow = function attachShadow(options) {
                    return originalAttachShadow.call(this, { ...options, mode: "open" });
                };
            })();
            """
		)

		return context

	def _add_new_page_listener(self, context: PlaywrightBrowserContext):
		async def on_page(page: Page):
			if self.browser.config.cdp_url:
				await page.reload()  # Reload the page to avoid timeout errors
			await page.wait_for_load_state()
			self.logger.debug(f'New page opened: {page.url}')
			if self.session is not None:
				self.session.current_page = page

		context.on('page', on_page)

	async def get_session(self) -> BrowserSession:
		"""Lazy initialization of the browser and related components"""
		if self.session is None:
			return await self._initialize_session()
		return self.session

	async def get_current_page(self) -> Page:
		"""Get the current page"""
		session = await self.get_session()
		return session.current_page

	async def _wait_for_stable_network(self):
		page = await self.get_current_page()

		pending_requests = set()
		last_activity = asyncio.get_event_loop().time()

		# Define relevant resource types and content types
		RELEVANT_RESOURCE_TYPES = {
			'document',
			'stylesheet',
			'image',
			'font',
			'script',
			'iframe',
		}

		RELEVANT_CONTENT_TYPES = {
			'text/html',
			'text/css',
			'application/javascript',
			'image/',
			'font/',
			'application/json',
		}

		# Additional patterns to filter out
		IGNORED_URL_PATTERNS = {
			# Analytics and tracking
			'analytics',
			'tracking',
			'telemetry',
			'beacon',
			'metrics',
			# Ad-related
			'doubleclick',
			'adsystem',
			'adserver',
			'advertising',
			# Social media widgets
			'facebook.com/plugins',
			'platform.twitter',
			'linkedin.com/embed',
			# Live chat and support
			'livechat',
			'zendesk',
			'intercom',
			'crisp.chat',
			'hotjar',
			# Push notifications
			'push-notifications',
			'onesignal',
			'pushwoosh',
			# Background sync/heartbeat
			'heartbeat',
			'ping',
			'alive',
			# WebRTC and streaming
			'webrtc',
			'rtmp://',
			'wss://',
			# Common CDNs for dynamic content
			'cloudfront.net',
			'fastly.net',
		}

		async def on_request(request):
			# Filter by resource type
			if request.resource_type not in RELEVANT_RESOURCE_TYPES:
				return

			# Filter out streaming, websocket, and other real-time requests
			if request.resource_type in {
				'websocket',
				'media',
				'eventsource',
				'manifest',
				'other',
			}:
				return

			# Filter out by URL patterns
			url = request.url.lower()
			if any(pattern in url for pattern in IGNORED_URL_PATTERNS):
				return

			# Filter out data URLs and blob URLs
			if url.startswith(('data:', 'blob:')):
				return

			# Filter out requests with certain headers
			headers = request.headers
			if headers.get('purpose') == 'prefetch' or headers.get('sec-fetch-dest') in [
				'video',
				'audio',
			]:
				return

			nonlocal last_activity
			pending_requests.add(request)
			last_activity = asyncio.get_event_loop().time()
			# logger.debug(f'Request started: {request.url} ({request.resource_type})')

		async def on_response(response):
			request = response.request
			if request not in pending_requests:
				return

			# Filter by content type if available
			content_type = response.headers.get('content-type', '').lower()

			# Skip if content type indicates streaming or real-time data
			if any(
				t in content_type
				for t in [
					'streaming',
					'video',
					'audio',
					'webm',
					'mp4',
					'event-stream',
					'websocket',
					'protobuf',
				]
			):
				pending_requests.remove(request)
				return

			# Only process relevant content types
			if not any(ct in content_type for ct in RELEVANT_CONTENT_TYPES):
				pending_requests.remove(request)
				return

			# Skip if response is too large (likely not essential for page load)
			content_length = response.headers.get('content-length')
			if content_length and int(content_length) > 5 * 1024 * 1024:  # 5MB
				pending_requests.remove(request)
				return

			nonlocal last_activity
			pending_requests.remove(request)
			last_activity = asyncio.get_event_loop().time()
			# logger.debug(f'Request resolved: {request.url} ({content_type})')

		# Attach event listeners
		page.on('request', on_request)
		page.on('response', on_response)

		try:
			# Wait for idle time
			start_time = asyncio.get_event_loop().time()
			while True:
				await asyncio.sleep(0.1)
				now = asyncio.get_event_loop().time()
				if len(pending_requests) == 0 and (now - last_activity) >= self.config.wait_for_network_idle_page_load_time:
					break
				if now - start_time > self.config.maximum_wait_page_load_time:
					self.logger.debug(
						f'Network timeout after {self.config.maximum_wait_page_load_time}s with {len(pending_requests)} '
						f'pending requests: {[r.url for r in pending_requests]}'
					)
					break

		finally:
			# Clean up event listeners
			page.remove_listener('request', on_request)
			page.remove_listener('response', on_response)

		self.logger.debug(f'Network stabilized for {self.config.wait_for_network_idle_page_load_time} seconds')

	async def _wait_for_page_and_frames_load(self, timeout_overwrite: float | None = None):
		"""
		Ensures page is fully loaded before continuing.
		Waits for either network to be idle or minimum WAIT_TIME, whichever is longer.
		Also checks if the loaded URL is allowed.
		"""
		# Start timing
		start_time = time.time()

		# Wait for page load
		try:
			await self._wait_for_stable_network()

			# Check if the loaded URL is allowed
			page = await self.get_current_page()
			await self._check_and_handle_navigation(page)
		except URLNotAllowedError as e:
			raise e
		except Exception:
			self.logger.warning('Page load failed, continuing...')
			pass

		# Calculate remaining time to meet minimum WAIT_TIME
		elapsed = time.time() - start_time
		remaining = max((timeout_overwrite or self.config.minimum_wait_page_load_time) - elapsed, 0)

		self.logger.debug(f'--Page loaded in {elapsed:.2f} seconds, waiting for additional {remaining:.2f} seconds')

		# Sleep remaining time if needed
		if remaining > 0:
			await asyncio.sleep(remaining)

	def _is_url_allowed(self, url: str) -> bool:
		"""Check if a URL is allowed based on the whitelist configuration."""
		if not self.config.allowed_domains:
			return True

		try:
			from urllib.parse import urlparse

			parsed_url = urlparse(url)
			domain = parsed_url.netloc.lower()

			# Remove port number if present
			if ':' in domain:
				domain = domain.split(':')[0]

			# Check if domain matches any allowed domain pattern
			return any(
				domain == allowed_domain.lower() or domain.endswith('.' + allowed_domain.lower())
				for allowed_domain in self.config.allowed_domains
			)
		except Exception as e:
			self.logger.error(f'Error checking URL allowlist: {str(e)}')
			return False

	async def _check_and_handle_navigation(self, page: Page) -> None:
		"""Check if current page URL is allowed and handle if not."""
		if not self._is_url_allowed(page.url):
			self.logger.warning(f'Navigation to non-allowed URL detected: {page.url}')
			try:
				await self.go_back()
			except Exception as e:
				self.logger.error(f'Failed to go back after detecting non-allowed URL: {str(e)}')
			raise URLNotAllowedError(f'Navigation to non-allowed URL: {page.url}')

	async def navigate_to(self, url: str):
		"""Navigate to a URL"""
		if not self._is_url_allowed(url):
			raise BrowserError(f'Navigation to non-allowed URL: {url}')

		page = await self.get_current_page()
		await page.goto(url)
		await page.wait_for_load_state()

	async def refresh_page(self):
		"""Refresh the current page"""
		page = await self.get_current_page()
		await page.reload()
		await page.wait_for_load_state()

	async def go_back(self):
		"""Navigate back in history"""
		page = await self.get_current_page()
		try:
			# 10 ms timeout
			await page.go_back(timeout=10, wait_until='domcontentloaded')
			# await self._wait_for_page_and_frames_load(timeout_overwrite=1.0)
		except Exception as e:
			# Continue even if its not fully loaded, because we wait later for the page to load
			self.logger.debug(f'During go_back: {e}')

	async def go_forward(self):
		"""Navigate forward in history"""
		page = await self.get_current_page()
		try:
			await page.go_forward(timeout=10, wait_until='domcontentloaded')
		except Exception as e:
			# Continue even if its not fully loaded, because we wait later for the page to load
			self.logger.debug(f'During go_forward: {e}')
			pass

	async def close_current_tab(self):
		"""Close the current tab"""
		session = await self.get_session()
		page = session.current_page
		await page.close()

		# Switch to the first available tab if any exist
		if session.context.pages:
			await self.switch_to_tab(0)

		# otherwise the browser will be closed

	async def get_page_html(self) -> str:
		"""Get the current page HTML content"""
		page = await self.get_current_page()
		return await page.content()

	async def execute_javascript(self, script: str):
		"""Execute JavaScript code on the page"""
		page = await self.get_current_page()
		return await page.evaluate(script)

	# Removed decorator - time_execution_async doesn't exist yet
	async def get_state(self, capture_screenshot: bool = True) -> BrowserState:
		"""Get the current state of the browser.
		
		Args:
			capture_screenshot: Whether to capture a screenshot. Set to False for non-vision
			                   tasks to save ~100-200ms per step. Default True for backward compat.
		
		Returns:
			BrowserState with current page information
		"""
		try:
			await self._wait_for_page_and_frames_load()
			session = await self.get_session()
			
			try:
				updated_state = await self._update_state(capture_screenshot=capture_screenshot)
				session.cached_state = updated_state
			except Exception as e:
				logger.error(f"Error updating state: {str(e)}")
				# If we have a cached state, use it instead of failing
				if hasattr(session, 'cached_state') and session.cached_state:
					logger.debug(f"Using cached state instead due to error")
					# Mark this state as cached so agent can handle it differently
					if not hasattr(session.cached_state, '_is_cached'):
						session.cached_state._is_cached = True
					return session.cached_state
				else:
					# Create minimal valid state
					page = await self.get_current_page()
					minimal_root_node = DOMElementNode(
						tag_name="html",
						is_visible=True,
						parent=None,
						xpath="",
						attributes={},
						children=[]
					)
					session.cached_state = BrowserState(
						element_tree=minimal_root_node,
						selector_map={},
						url=page.url if page else "",
						title=await page.title() if page else "",
						tabs=await self.get_tabs_info() if page else [],
						screenshot=None
					)
					# Mark as cached
					session.cached_state._is_cached = True

			# Save cookies if a file is specified
			if self.config.cookies_file:
				asyncio.create_task(self.save_cookies())

			return session.cached_state
		except Exception as e:
			logger.error(f"Critical error in get_state: {str(e)}")
			# Create minimal valid state as last resort
			minimal_root_node = DOMElementNode(
				tag_name="html",
				is_visible=True,
				parent=None,
				xpath="",
				attributes={},
				children=[]
			)
			return BrowserState(
				element_tree=minimal_root_node,
				selector_map={},
				url="",
				title="Error Page",
				tabs=[],
				screenshot=None
			)

	async def _update_state(self, focus_element: int = -1, capture_screenshot: bool = True) -> BrowserState:
		"""Update and return state.
		
		Args:
			focus_element: Element index to focus on (default -1 = none)
			capture_screenshot: Whether to capture screenshot. Set False for non-vision tasks
			                   to save ~100-200ms per step.
		
		Returns:
			Updated BrowserState
		"""
		session = await self.get_session()

		# Check if current page is still valid, if not switch to another available page
		try:
			page = await self.get_current_page()
			# Test if page is still accessible
			await page.evaluate('1')
		except Exception as e:
			logger.debug(f'Current page is no longer accessible: {str(e)}')
			# Get all available pages
			pages = session.context.pages
			if pages:
				session.current_page = pages[-1]
				page = session.current_page
				logger.debug(f'Switched to page: {await page.title()}')
			else:
				raise BrowserError('Browser closed: no valid pages available')

		try:
			await self.remove_highlights()
			dom_service = DomService(page)
			content = await dom_service.get_clickable_elements(
				focus_element=focus_element,
				viewport_expansion=self.config.viewport_expansion,
				highlight_elements=self.config.highlight_elements,
			)

			# FIX (Dec 2025): Only capture screenshot when vision is needed
			# This saves ~100-200ms per step for non-vision tasks
			screenshot_b64 = None
			if capture_screenshot:
				screenshot_b64 = await self.take_screenshot()
			else:
				logger.debug("Skipping screenshot capture (vision disabled)")
			
			pixels_above, pixels_below = await self.get_scroll_info(page)

			self.current_state = BrowserState(
				element_tree=content.element_tree,
				selector_map=content.selector_map,
				url=page.url,
				title=await page.title(),
				tabs=await self.get_tabs_info(),
				screenshot=screenshot_b64,
				pixels_above=pixels_above,
				pixels_below=pixels_below,
			)

			return self.current_state
		except Exception as e:
			logger.error(f'Failed to update state: {str(e)}')
			# Return last known good state if available
			if hasattr(self, 'current_state'):
				return self.current_state
			raise

	# region - Browser Actions

	async def take_screenshot(self, full_page: bool = False) -> str:
		"""
		Returns a base64 encoded screenshot of the current page.
		Includes error handling and retries for more reliable screenshot capture.
		"""
		try:
			page = await self.get_current_page()
			
			# Try a more reliable screenshot approach
			try:
				# First attempt with standard approach
				screenshot = await page.screenshot(
					full_page=full_page,
					animations='disabled',
					timeout=10000  # Longer timeout for screenshot capture
				)
				
				# Verify the screenshot has content
				if not screenshot or len(screenshot) < 100:
					logger.warning("Screenshot appears to be empty or too small, retrying with alternative method")
					# Try alternative approach - capture viewport only
					screenshot = await page.screenshot(
						full_page=False,
						animations='disabled',
						timeout=10000
					)
					
			except Exception as screenshot_error:
				logger.error(f"Error taking screenshot with standard method: {screenshot_error}")
				# Fallback method
				try:
					screenshot = await page.screenshot(
						full_page=False,
						animations='disabled',
						timeout=10000
					)
				except Exception as fallback_error:
					logger.error(f"Fallback screenshot method also failed: {fallback_error}")
					# Return empty string if all screenshot methods fail
					return ""
			
			screenshot_b64 = base64.b64encode(screenshot).decode('utf-8')
			
			# Log the screenshot size to help with debugging
			logger.debug(f"Screenshot captured successfully - size: {len(screenshot_b64)} bytes")
			
			# If we have a session ID, save the screenshot to the session directory
			session = await self.get_session()
			if hasattr(session, 'session_id') and session.session_id:
				try:
					# Try to use PathManager for consistent path handling
					try:
						from agents.task.path import pm
						path_manager = pm()
						
						# Get screenshots directory for this session
						screenshots_dir = path_manager.get_subdir(session.session_id, "screenshots")
						
						# Ensure the directory exists
						screenshots_dir = str(screenshots_dir)
						os.makedirs(screenshots_dir, exist_ok=True)
					
					except (ImportError, ValueError, Exception) as e:
						logger.warning(f"PathManager not available: {str(e)}")
						
						# Fallback to direct path construction. WS-3: anchor to the RESOLVED session
						# tree (never a relative "data" under the cwd) and clean the session id the
						# same way pm() would, so a stray id can't escape the tree.
						from core.runtime_paths import resolve_session_data_root
						try:
							from agents.task.path import pm as _pm
							_sid = _pm().clean_session_id(session.session_id)
						except Exception:
							_sid = str(session.session_id)
						_auto_root = os.path.join(str(resolve_session_data_root()), "auto")
						screenshots_dir = os.path.join(_auto_root, _sid, "screenshots")
						
						# Check if screenshots_dir has duplicated session ID to avoid nesting
						pattern = os.path.join(_auto_root, _sid)
						if screenshots_dir.count(pattern) > 1:
							# Fix path by removing the duplication
							parts = screenshots_dir.split(pattern)
							screenshots_dir = pattern.join([parts[0], parts[-1]])
							logger.warning(f"Fixed duplicate session paths: {screenshots_dir}")
					
					# Ensure the directory exists
					os.makedirs(screenshots_dir, exist_ok=True)
					
					# Get current page URL and encode it for the filename
					current_url = page.url
					# Truncate and encode URL for filename
					url_for_filename = ""
					if current_url:
						# Limit URL length and remove unsafe characters
						import urllib.parse
						import re
						# Keep just the domain and first part of path
						url_parts = urllib.parse.urlparse(current_url)
						shorter_url = f"{url_parts.netloc}{url_parts.path[:30]}"
						# Clean URL for safe filename
						url_for_filename = re.sub(r'[^\w\-_]', '_', shorter_url)[:50]
						# Add leading underscore for parsing
						url_for_filename = f"_{url_for_filename}"
					
					# Generate filename with timestamp
					timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
					fullpage_suffix = "_fullpage" if full_page else ""
					filename = f"screenshot_{timestamp}{fullpage_suffix}{url_for_filename}.png"
					
					# Create file path 
					if 'path_manager' in locals():
						file_path = path_manager.create_file_path(
							session.session_id, 
							"screenshots",
							filename
						)
					else:
						file_path = os.path.join(screenshots_dir, filename)
					
					# Save raw screenshot bytes
					with open(file_path, "wb") as f:
						f.write(screenshot)
						
					logger.debug(f"Saved screenshot directly to {file_path}")
				except Exception as save_error:
					logger.error(f"Failed to save screenshot to disk: {save_error}")
			
			return screenshot_b64
		except Exception as e:
			logger.error(f"Failed to take screenshot: {e}")
			return ""  # Return empty string on failure

	async def take_and_save_fullpage_screenshot(self) -> Optional[str]:
		"""
		Takes a fullpage screenshot and saves it directly to the session directory.
		Returns the path to the saved file, or None if saving failed.
		"""
		try:
			page = await self.get_current_page()
			session = await self.get_session()
			
			if not hasattr(session, 'session_id') or not session.session_id:
				logger.warning("Cannot save fullpage screenshot: no session ID available")
				return None
				
			# Try to use SessionManager for consistent path handling
			try:
				from agents.task.path import pm
				path_manager = pm()
				
				# Get screenshots directory for this session
				screenshots_dir = path_manager.get_subdir(session.session_id, "screenshots")
				
				# Ensure the directory exists
				screenshots_dir = str(screenshots_dir)
				os.makedirs(screenshots_dir, exist_ok=True)
			
			except (ImportError, ValueError, Exception) as e:
				logger.warning(f"PathManager not available for fullpage screenshot: {str(e)}")
				
				# Fallback to direct path construction. WS-3: anchor to the RESOLVED session
				# tree (never a relative "data" under the cwd) + clean the session id.
				from core.runtime_paths import resolve_session_data_root
				try:
					from agents.task.path import pm as _pm
					_sid = _pm().clean_session_id(session.session_id)
				except Exception:
					_sid = str(session.session_id)
				_auto_root = os.path.join(str(resolve_session_data_root()), "auto")
				screenshots_dir = os.path.join(_auto_root, _sid, "screenshots")
				
				# Check if screenshots_dir has duplicated session ID to avoid nesting
				pattern = os.path.join(_auto_root, _sid)
				if screenshots_dir.count(pattern) > 1:
					# Fix path by removing the duplication
					parts = screenshots_dir.split(pattern)
					screenshots_dir = pattern.join([parts[0], parts[-1]])
					logger.warning(f"Fixed duplicate session paths in fullpage: {screenshots_dir}")
			
			# Ensure the directory exists
			os.makedirs(screenshots_dir, exist_ok=True)
			
			# Generate filename with timestamp
			timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
			filename = f"screenshot_{timestamp}_fullpage.png"
			
			# Create file path
			if 'path_manager' in locals():
				file_path = path_manager.create_file_path(
					session.session_id,
					"screenshots",
					filename
				)
			else:
				file_path = os.path.join(screenshots_dir, filename)
			
			# Take fullpage screenshot
			try:
				# Use a longer timeout for fullpage screenshots
				screenshot = await page.screenshot(
					full_page=True,
					animations='disabled',
					timeout=15000
				)
				
				# Save screenshot directly to file
				with open(file_path, "wb") as f:
					f.write(screenshot)
					
				logger.debug(f"Saved fullpage screenshot to {file_path}")
				return file_path
			except Exception as screenshot_error:
				logger.error(f"Error taking fullpage screenshot: {screenshot_error}")
				return None
		except Exception as e:
			logger.error(f"Failed to take and save fullpage screenshot: {e}")
			return None

	async def remove_highlights(self):
		"""
		Removes all highlight overlays and labels created by the highlightElement function.
		Handles cases where the page might be closed or inaccessible.
		"""
		try:
			page = await self.get_current_page()
			await page.evaluate(
				"""
                try {
                    // Remove the highlight container and all its contents
                    const container = document.getElementById('playwright-highlight-container');
                    if (container) {
                        container.remove();
                    }

                    // Remove highlight attributes from elements
                    const highlightedElements = document.querySelectorAll('[browser-user-highlight-id^="playwright-highlight-"]');
                    highlightedElements.forEach(el => {
                        el.removeAttribute('browser-user-highlight-id');
                    });
                } catch (e) {
                    console.error('Failed to remove highlights:', e);
                }
                """
			)
		except Exception as e:
			logger.debug(f'Failed to remove highlights (this is usually ok): {str(e)}')
			# Don't raise the error since this is not critical functionality
			pass

	# endregion

	# region - User Actions

	@classmethod
	def _convert_simple_xpath_to_css_selector(cls, xpath: str) -> str:
		"""Converts simple XPath expressions to CSS selectors."""
		if not xpath:
			return ''

		# Remove leading slash if present
		xpath = xpath.lstrip('/')

		# Split into parts
		parts = xpath.split('/')
		css_parts = []

		for part in parts:
			if not part:
				continue

			# Handle index notation [n]
			if '[' in part:
				base_part = part[: part.find('[')]
				index_part = part[part.find('[') :]

				# Handle multiple indices
				indices = [i.strip('[]') for i in index_part.split(']')[:-1]]

				for idx in indices:
					try:
						# Handle numeric indices
						if idx.isdigit():
							index = int(idx) - 1
							base_part += f':nth-of-type({index + 1})'
						# Handle last() function
						elif idx == 'last()':
							base_part += ':last-of-type'
						# Handle position() functions
						elif 'position()' in idx:
							if '>1' in idx:
								base_part += ':nth-of-type(n+2)'
					except ValueError:
						continue

				css_parts.append(base_part)
			else:
				css_parts.append(part)

		base_selector = ' > '.join(css_parts)
		return base_selector

	@classmethod
	def _enhanced_css_selector_for_element(cls, element: DOMElementNode, include_dynamic_attributes: bool = True) -> str:
		"""
		Creates a CSS selector for a DOM element, handling various edge cases and special characters.

		Args:
		        element: The DOM element to create a selector for

		Returns:
		        A valid CSS selector string
		"""
		try:
			# Get base selector from XPath
			css_selector = cls._convert_simple_xpath_to_css_selector(element.xpath)

			# Handle class attributes
			if 'class' in element.attributes and element.attributes['class'] and include_dynamic_attributes:
				# Define a regex pattern for valid class names in CSS
				valid_class_name_pattern = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_-]*$')

				# Iterate through the class attribute values
				classes = element.attributes['class'].split()
				for class_name in classes:
					# Skip empty class names
					if not class_name.strip():
						continue

					# Check if the class name is valid
					if valid_class_name_pattern.match(class_name):
						# Append the valid class name to the CSS selector
						css_selector += f'.{class_name}'
					else:
						# Skip invalid class names
						continue

			# Expanded set of safe attributes that are stable and useful for selection
			SAFE_ATTRIBUTES = {
				# Data attributes (if they're stable in your application)
				'id',
				# Standard HTML attributes
				'name',
				'type',
				'placeholder',
				# Accessibility attributes
				'aria-label',
				'aria-labelledby',
				'aria-describedby',
				'role',
				# Common form attributes
				'for',
				'autocomplete',
				'required',
				'readonly',
				# Media attributes
				'alt',
				'title',
				'src',
				# Custom stable attributes (add any application-specific ones)
				'href',
				'target',
			}

			if include_dynamic_attributes:
				dynamic_attributes = {
					'data-id',
					'data-qa',
					'data-cy',
					'data-testid',
				}
				SAFE_ATTRIBUTES.update(dynamic_attributes)

			# Handle other attributes
			for attribute, value in element.attributes.items():
				if attribute == 'class':
					continue

				# Skip invalid attribute names
				if not attribute.strip():
					continue

				if attribute not in SAFE_ATTRIBUTES:
					continue

				# Escape special characters in attribute names
				safe_attribute = attribute.replace(':', r'\:')

				# Handle different value cases
				if value == '':
					css_selector += f'[{safe_attribute}]'
				elif any(char in value for char in '"\'<>`\n\r\t'):
					# Use contains for values with special characters
					# Regex-substitute *any* whitespace with a single space, then strip.
					collapsed_value = re.sub(r'\s+', ' ', value).strip()
					# Escape embedded double-quotes.
					safe_value = collapsed_value.replace('"', '\\"')
					css_selector += f'[{safe_attribute}*="{safe_value}"]'
				else:
					css_selector += f'[{safe_attribute}="{value}"]'

			return css_selector

		except Exception:
			# Fallback to a more basic selector if something goes wrong
			tag_name = element.tag_name or '*'
			return f"{tag_name}[highlight_index='{element.highlight_index}']"

	async def get_locate_element(self, element: DOMElementNode) -> Optional[ElementHandle]:
		current_frame = await self.get_current_page()

		# Start with the target element and collect all parents
		parents: list[DOMElementNode] = []
		current = element
		while current.parent is not None:
			parent = current.parent
			parents.append(parent)
			current = parent

		# Reverse the parents list to process from top to bottom
		parents.reverse()

		# Process all iframe parents in sequence
		iframes = [item for item in parents if item.tag_name == 'iframe']
		for parent in iframes:
			css_selector = self._enhanced_css_selector_for_element(
				parent,
				include_dynamic_attributes=self.config.include_dynamic_attributes,
			)
			current_frame = current_frame.frame_locator(css_selector)

		css_selector = self._enhanced_css_selector_for_element(
			element, include_dynamic_attributes=self.config.include_dynamic_attributes
		)

		try:
			if isinstance(current_frame, FrameLocator):
				element_handle = await current_frame.locator(css_selector).element_handle()
				return element_handle
			else:
				# Try to scroll into view if hidden
				element_handle = await current_frame.query_selector(css_selector)
				if element_handle:
					await element_handle.scroll_into_view_if_needed()
					return element_handle
				return None
		except Exception as e:
			logger.error(f'Failed to locate element: {str(e)}')
			return None

	async def _input_text_element_node(self, element_node: DOMElementNode, text: str):
		"""
		Input text into an element with proper error handling and state management.
		Handles different types of input fields and ensures proper element state before input.
		"""
		try:
			# Highlight before typing
			if element_node.highlight_index is not None:
				await self._update_state(focus_element=element_node.highlight_index)

			page = await self.get_current_page()
			element_handle = await self.get_locate_element(element_node)

			if element_handle is None:
				raise BrowserError(f'Element: {repr(element_node)} not found')

			# Enhanced element readiness check with multiple attempts
			max_attempts = 3
			for attempt in range(max_attempts):
				try:
					# Wait for element to be stable and visible
					await element_handle.wait_for_element_state('stable', timeout=3000)
					await element_handle.wait_for_element_state('visible', timeout=3000)
					await element_handle.scroll_into_view_if_needed(timeout=3000)
					
					# Additional wait for potential dynamic enabling
					await page.wait_for_timeout(200)
					
					# Check if element is enabled
					is_enabled = await element_handle.is_enabled()
					if not is_enabled:
						# Wait a bit more for element to become enabled
						await page.wait_for_timeout(500)
						is_enabled = await element_handle.is_enabled()
					
					if is_enabled:
						break
					elif attempt == max_attempts - 1:
						logger.warning(f'Element not enabled after {max_attempts} attempts, continuing anyway')
				except Exception as e:
					if attempt == max_attempts - 1:
						logger.warning(f'Element readiness check failed: {str(e)}, continuing anyway')
					else:
						await page.wait_for_timeout(500)  # Wait before retry

			# Get element properties to determine input method
			is_contenteditable = await element_handle.get_property('isContentEditable')

			# Multiple strategies for text input with progressively more aggressive approaches
			strategies = [
				# Strategy 1: Standard approach
				lambda: self._input_strategy_standard(element_handle, text, is_contenteditable),
				# Strategy 2: Click first, then input
				lambda: self._input_strategy_click_first(element_handle, text, is_contenteditable),
				# Strategy 3: Force focus and clear before input
				lambda: self._input_strategy_force_focus(element_handle, text, is_contenteditable),
				# Strategy 4: JavaScript-based input as last resort
				lambda: self._input_strategy_javascript(element_handle, text, page)
			]

			last_error = None
			for i, strategy in enumerate(strategies):
				try:
					await strategy()
					logger.debug(f'Text input successful using strategy {i + 1}')
					return
				except Exception as e:
					last_error = e
					logger.debug(f'Input strategy {i + 1} failed: {str(e)}')
					if i < len(strategies) - 1:
						# Wait before trying next strategy
						await page.wait_for_timeout(200)

			# If all strategies failed, raise the last error
			raise last_error

		except Exception as e:
			logger.debug(f'Failed to input text into element: {repr(element_node)}. Error: {str(e)}')
			raise BrowserError(f'Failed to input text into index {element_node.highlight_index}')

	async def _input_strategy_standard(self, element_handle, text, is_contenteditable):
		"""Standard input strategy"""
		if await is_contenteditable.json_value():
			await element_handle.evaluate('el => el.textContent = ""')
			await element_handle.type(text, delay=5)
		else:
			await element_handle.fill(text)

	async def _input_strategy_click_first(self, element_handle, text, is_contenteditable):
		"""Click element first, then input text"""
		await element_handle.click(timeout=5000)
		if await is_contenteditable.json_value():
			await element_handle.evaluate('el => el.textContent = ""')
			await element_handle.type(text, delay=5)
		else:
			await element_handle.fill(text)

	async def _input_strategy_force_focus(self, element_handle, text, is_contenteditable):
		"""Force focus and clear before input"""
		await element_handle.focus()
		await element_handle.evaluate('el => { el.select(); el.value = ""; }')
		await element_handle.type(text, delay=5)

	async def _input_strategy_javascript(self, element_handle, text, page):
		"""JavaScript-based input as last resort"""
		await element_handle.evaluate(f'''
			el => {{
				el.focus();
				el.value = "{text}";
				el.dispatchEvent(new Event('input', {{ bubbles: true }}));
				el.dispatchEvent(new Event('change', {{ bubbles: true }}));
			}}
		''')
		# Small delay to let any change handlers run
		await page.wait_for_timeout(100)

	async def _click_element_node(self, element_node: DOMElementNode) -> Optional[str]:
		"""
		Optimized method to click an element using xpath.
		"""
		page = await self.get_current_page()

		try:
			# Highlight before clicking
			if element_node.highlight_index is not None:
				await self._update_state(focus_element=element_node.highlight_index)

			element_handle = await self.get_locate_element(element_node)

			if element_handle is None:
				raise Exception(f'Element: {repr(element_node)} not found')

			async def perform_click(click_func):
				"""Performs the actual click, handling both download
				and navigation scenarios."""
				if self.config.save_downloads_path:
					try:
						# Try short-timeout expect_download to detect a file download has been been triggered
						async with page.expect_download(timeout=5000) as download_info:
							await click_func()
						download = await download_info.value
						# Determine file path
						suggested_filename = download.suggested_filename
						unique_filename = await self._get_unique_filename(self.config.save_downloads_path, suggested_filename)
						download_path = os.path.join(self.config.save_downloads_path, unique_filename)
						await download.save_as(download_path)
						logger.debug(f'Download triggered. Saved file to: {download_path}')
						return download_path
					except TimeoutError:
						# If no download is triggered, treat as normal click
						logger.debug('No download triggered within timeout. Checking navigation...')
						await page.wait_for_load_state()
						await self._check_and_handle_navigation(page)
				else:
					# Standard click logic if no download is expected
					await click_func()
					await page.wait_for_load_state()
					await self._check_and_handle_navigation(page)

			try:
				return await perform_click(lambda: element_handle.click(timeout=1500))
			except URLNotAllowedError as e:
				raise e
			except Exception:
				try:
					return await perform_click(lambda: page.evaluate('(el) => el.click()', element_handle))
				except URLNotAllowedError as e:
					raise e
				except Exception as e:
					raise Exception(f'Failed to click element: {str(e)}')

		except URLNotAllowedError as e:
			raise e
		except Exception as e:
			raise Exception(f'Failed to click element: {repr(element_node)}. Error: {str(e)}')

	async def get_tabs_info(self) -> list[TabInfo]:
		"""Get information about all tabs"""
		session = await self.get_session()

		tabs_info = []
		for page_id, page in enumerate(session.context.pages):
			tab_info = TabInfo(page_id=page_id, url=page.url, title=await page.title())
			tabs_info.append(tab_info)

		return tabs_info

	async def switch_to_tab(self, page_id: int) -> None:
		"""Switch to a specific tab by its page_id

		@You can also use negative indices to switch to tabs from the end (Pure pythonic way)
		"""
		session = await self.get_session()
		pages = session.context.pages
		tabs_count = len(pages)

		if tabs_count == 0:
			raise BrowserError('No tabs available to switch to')

		# Handle negative indices properly
		if page_id < 0:
			# Convert negative index to positive index
			positive_index = tabs_count + page_id
			if positive_index < 0:
				# Even after conversion, index is still negative (out of range)
				tabs_info = await self.get_tabs_info()
				tabs_str = "\n".join([f"Tab {tab.page_id}: {tab.title} ({tab.url})" for tab in tabs_info])
				raise BrowserError(f'No tab found with page_id: {page_id} (negative index out of range). Available tabs:\n{tabs_str}')
			page_id = positive_index

		if page_id >= tabs_count:
			# Index is out of range, show available tabs in error message
			tabs_info = await self.get_tabs_info()
			tabs_str = "\n".join([f"Tab {tab.page_id}: {tab.title} ({tab.url})" for tab in tabs_info])
			raise BrowserError(f'No tab found with page_id: {page_id} (out of range). Available tabs:\n{tabs_str}')

		page = pages[page_id]

		# Check if the tab's URL is allowed before switching
		if not self._is_url_allowed(page.url):
			raise BrowserError(f'Cannot switch to tab with non-allowed URL: {page.url}')

		session.current_page = page

		await page.bring_to_front()
		await page.wait_for_load_state()

	async def create_new_tab(self, url: str | None = None) -> None:
		"""Create a new tab and optionally navigate to a URL"""
		if url and not self._is_url_allowed(url):
			raise BrowserError(f'Cannot create new tab with non-allowed URL: {url}')

		session = await self.get_session()
		new_page = await session.context.new_page()
		session.current_page = new_page

		await new_page.wait_for_load_state()

		page = await self.get_current_page()

		if url:
			await page.goto(url)
			await self._wait_for_page_and_frames_load(timeout_overwrite=1)

	# endregion

	# region - Helper methods for easier access to the DOM
	async def get_selector_map(self) -> SelectorMap:
		session = await self.get_session()
		return session.cached_state.selector_map

	async def get_element_by_index(self, index: int) -> ElementHandle | None:
		selector_map = await self.get_selector_map()
		element_handle = await self.get_locate_element(selector_map[index])
		return element_handle

	async def get_dom_element_by_index(self, index: int) -> DOMElementNode:
		selector_map = await self.get_selector_map()
		return selector_map[index]

	async def save_cookies(self):
		"""Save current cookies to file"""
		if self.session and self.session.context and self.config.cookies_file:
			try:
				cookies = await self.session.context.cookies()
				logger.debug(f'Saving {len(cookies)} cookies to {self.config.cookies_file}')

				# Check if the path is a directory and create it if necessary
				dirname = os.path.dirname(self.config.cookies_file)
				if dirname:
					os.makedirs(dirname, exist_ok=True)

				with open(self.config.cookies_file, 'w') as f:
					json.dump(cookies, f)
			except Exception as e:
				logger.warning(f'Failed to save cookies: {str(e)}')

	async def is_file_uploader(self, element_node: DOMElementNode, max_depth: int = 3, current_depth: int = 0) -> bool:
		"""Check if element or its children are file uploaders"""
		if current_depth > max_depth:
			return False

		# Check current element
		is_uploader = False

		if not isinstance(element_node, DOMElementNode):
			return False

		# Check for file input attributes
		if element_node.tag_name == 'input':
			is_uploader = element_node.attributes.get('type') == 'file' or element_node.attributes.get('accept') is not None

		if is_uploader:
			return True

		# Recursively check children
		if element_node.children and current_depth < max_depth:
			for child in element_node.children:
				if isinstance(child, DOMElementNode):
					if await self.is_file_uploader(child, max_depth, current_depth + 1):
						return True

		return False

	async def get_scroll_info(self, page: Page) -> tuple[int, int]:
		"""Get scroll position information for the current page."""
		scroll_y = await page.evaluate('window.scrollY')
		viewport_height = await page.evaluate('window.innerHeight')
		total_height = await page.evaluate('document.documentElement.scrollHeight')
		pixels_above = scroll_y
		pixels_below = total_height - (scroll_y + viewport_height)
		return pixels_above, pixels_below

	async def reset_context(self):
		"""Reset the browser session
		Call this when you don't want to kill the context but just kill the state
		"""
		# close all tabs and clear cached state
		session = await self.get_session()

		pages = session.context.pages
		for page in pages:
			await page.close()

		session.cached_state = self._get_initial_state()
		session.current_page = await session.context.new_page()

	def _get_initial_state(self, page: Optional[Page] = None) -> BrowserState:
		"""Get the initial state of the browser
		
		Note: This creates a minimal state for a new browser session.
		The state should be updated via get_state() after any navigation.
		"""
		# Log if we're creating an initial state with about:blank
		url = page.url if page else ''
		if url == 'about:blank' or url == '':
			self.logger.debug("Creating initial state with blank page - state will be updated after first navigation")
		
		return BrowserState(
			element_tree=DOMElementNode(
				tag_name='root',
				is_visible=True,
				parent=None,
				xpath='',
				attributes={},
				children=[],
			),
			selector_map={},
			url=url,
			title='',
			screenshot=None,
			tabs=[],
		)

	async def _get_unique_filename(self, directory, filename):
		"""Generate a unique filename by appending (1), (2), etc., if a file already exists."""
		base, ext = os.path.splitext(filename)
		counter = 1
		new_filename = filename
		while os.path.exists(os.path.join(directory, new_filename)):
			new_filename = f'{base} ({counter}){ext}'
			counter += 1
		return new_filename
