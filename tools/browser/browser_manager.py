"""
BrowserManager component for managing browser lifecycle and context pooling.

This module provides centralized browser management following the BaseComponent
pattern, handling context pooling, allocation, cleanup, and resource management.
"""

import asyncio
import os
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

from core.base_component import BaseComponent
from core.config import BotConfig, DEFAULT_MAX_BROWSER_CONTEXTS
from tools.browser.browser import Browser
from tools.browser.context import BrowserContext, BrowserContextConfig

#: A released context holds its pool slot until its close ends: a close that hangs
#: is given up after this many seconds, so the slot is freed in every case.
CONTEXT_CLOSE_TIMEOUT_SEC = 30.0


def _session_key(agent_id: str) -> str:
    """The session of an ``agentname_sessionid`` id, for the per-session limit. The part
    before ``_`` is the agent NAME, so every session of ``agent`` shared one allowance."""
    return agent_id.split('_', 1)[-1]


@dataclass
class BrowserManagerConfig:
    """Configuration for BrowserManager."""
    max_contexts: int = DEFAULT_MAX_BROWSER_CONTEXTS
    max_contexts_per_session: int = 2  # Per-session limit
    context_timeout: float = 30.0
    headless: bool = True
    stale_context_timeout: float = 300.0  # 5 minutes
    enable_pooling: bool = True
    wait_queue_timeout: float = 60.0  # Default wait timeout
    # Remote browser endpoints are required for custody processes.  These are
    # intentionally environment-driven so endpoint credentials never enter
    # the BotConfig object or ordinary session metadata.
    cdp_url: Optional[str] = None
    wss_url: Optional[str] = None
    # Backstop-only: the primary allocation path already resolves waiters
    # synchronously from _release_context_internal the instant a context
    # frees up. This loop just recovers any edge case that skips that path,
    # so it does not need sub-second cadence.
    wait_queue_backstop_interval: float = 15.0
    security_flags: Dict[str, Any] = field(default_factory=lambda: {
        'bypass_csp': False,
        'ignore_https_errors': False,
        'java_script_enabled': True,
    })


class BrowserManager(BaseComponent):
    """
    Manages browser instances and context pooling for the application.

    Responsibilities:
    - Browser lifecycle management
    - Context pooling and allocation
    - Stale context cleanup
    - Resource cleanup on shutdown
    """

    def __init__(self, config: Optional[BotConfig] = None):
        """
        Initialize BrowserManager with configuration.

        Args:
            config: Bot configuration instance
        """
        config = config or BotConfig()
        super().__init__(config=config, name="browser_manager")

        # Browser management configuration
        self.browser_config = BrowserManagerConfig(
            max_contexts=getattr(self.config, 'max_browser_contexts', DEFAULT_MAX_BROWSER_CONTEXTS),
            max_contexts_per_session=getattr(
                self.config, 'max_contexts_per_session',
                BrowserManagerConfig.max_contexts_per_session,
            ),
            headless=getattr(self.config, 'browser_headless', True),
            context_timeout=getattr(self.config, 'browser_context_timeout', 30.0),
            stale_context_timeout=getattr(self.config, 'browser_stale_timeout', 300.0),
            enable_pooling=getattr(self.config, 'browser_enable_pooling', True),
            wait_queue_backstop_interval=getattr(
                self.config, 'browser_wait_queue_backstop_interval',
                BrowserManagerConfig.wait_queue_backstop_interval,
            ),
            cdp_url=os.getenv('BROWSER_CDP_URL') or None,
            wss_url=os.getenv('BROWSER_WSS_URL') or None,
        )

        # Browser instance
        self.browser: Optional[Browser] = None

        # Context management. 098 TS-06: no pool — a released context is
        # always closed (a "reset" context kept the previous tenant's cookies).
        self.contexts_in_use: Dict[str, BrowserContext] = {}
        #: 098 TS-05: session -> the in-flight creation (a reserved slot that
        #: counts against max_contexts; a second caller awaits the same future).
        self._creating: Dict[str, asyncio.Future] = {}
        #: Contexts detached from the pool and not yet closed: a close of a dead browser
        #: takes seconds, and the context still holds its memory, so it holds its slot too.
        self._closing = 0
        self.context_allocation_times: Dict[str, float] = {}

        # Per-session tracking
        self.session_contexts: Dict[str, List[str]] = {}  # session -> [context_ids]
        self.session_configs: Dict[str, BrowserContextConfig] = {}  # session -> config

        # Wait queue for context allocation (session_id -> Future)
        self._wait_queue: Dict[str, asyncio.Future] = {}
        self._queue_order: List[str] = []  # Maintain FIFO order

        # Synchronization
        self._pool_lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._queue_processor_task: Optional[asyncio.Task] = None

        self.logger.info(f"BrowserManager initialized with max_contexts={self.browser_config.max_contexts}")

    async def _validate_dependencies(self) -> None:
        """Validate dependencies - BrowserManager has no external dependencies."""
        pass

    async def _initialize(self) -> None:
        """Implementation-specific initialization."""
        try:
            # Pass configuration flags to Browser
            from tools.browser.browser import BrowserConfig
            browser_config = BrowserConfig(
                headless=self.browser_config.headless,
                disable_security=self.browser_config.security_flags.get('bypass_csp', False),
                cdp_url=self.browser_config.cdp_url,
                wss_url=self.browser_config.wss_url,
            )
            # `config=` is the BotConfig slot BaseTool reads; the BrowserConfig
            # goes in `browser_config=`. Until 2026-09-17 the BrowserConfig was
            # passed as `config=`, so Browser built a DEFAULT BrowserConfig and
            # the remote endpoint (and disable_security) never reached it — a
            # custody deployment refused local Chromium while an isolated
            # browser service sat idle on 127.0.0.1:9222.
            self.browser = Browser(
                headless=self.browser_config.headless,
                config=self.config,
                browser_config=browser_config,
            )
            # Report the endpoint from what was BUILT, not from our own field.
            endpoint = self.browser.browser_config.endpoint_kind()
            self.logger.info(
                "Browser initialized successfully "
                f"(headless={self.browser_config.headless}, endpoint={endpoint})"
            )

        except Exception as e:
            self.logger.error(f"Failed to initialize browser: {e}")
            raise

    async def _ensure_background_tasks_started(self) -> None:
        """Start the pool's background tasks on first real use.

        These loops (stale-context cleanup, wait-queue backstop) have nothing
        to do until a context has been requested at least once, but
        BrowserManager itself is initialized eagerly at server startup
        regardless of whether any session ever uses the browser tool. Starting
        them lazily means a server/session that never touches the browser tool
        pays zero background-task cost. Idempotent and race-safe under
        concurrent callers (double-checked under ``_pool_lock``).
        """
        if self._queue_processor_task is not None:
            return
        async with self._pool_lock:
            if self._queue_processor_task is not None:
                return
            if self.browser_config.enable_pooling:
                self._cleanup_task = asyncio.create_task(self._cleanup_stale_contexts())
            self._queue_processor_task = asyncio.create_task(self._process_wait_queue())

    async def get_context(
        self,
        session_id: str,
        reuse: bool = True,
        allowed_domains: Optional[List[str]] = None,
        config: Optional[BrowserContextConfig] = None,
        wait: bool = False,
        timeout: Optional[float] = None
    ) -> Optional[BrowserContext]:
        """
        Get a browser context for a session.

        Args:
            session_id: Session identifier requesting the context
            reuse: Whether to reuse contexts from the pool
            allowed_domains: List of allowed domains for this session
            config: Custom configuration for this session's contexts
            wait: Whether to wait in queue if no context available
            timeout: Maximum time to wait (defaults to wait_queue_timeout)

        Returns:
            BrowserContext instance or None if unavailable
        """
        await self._ensure_background_tasks_started()

        # Set when the allocation path decides this caller must wait in the
        # queue. The actual await happens AFTER the pool lock is released —
        # `_pool_lock` is a non-reentrant asyncio.Lock, so blocking on the
        # waiter future while holding it would deadlock (release_context /
        # _notify_wait_queue can never run to hand off a context).
        wait_future: Optional[asyncio.Future] = None

        creating: Optional[asyncio.Future] = None
        reserved = False
        async with self._pool_lock:
            # Check if session already has a context
            if session_id in self.contexts_in_use:
                # Touch the clock: BROWSER_STALE_TIMEOUT is an IDLE timeout. Until
                # 2026-09-18 this was allocation AGE, so the reaper closed the
                # context of any session whose run outlived 5 min (32 reaps/24 h);
                # the session's next new_page then hit a chrome segfault and the
                # step hung to its 600 s timeout.
                self.context_allocation_times[session_id] = time.time()
                self.logger.debug(f"Returning existing context for session {session_id}")
                return self.contexts_in_use[session_id]
            if session_id in self._creating:  # 098 TS-05: one creation per session
                creating = self._creating[session_id]
            else:
                # Store session config if provided
                if config:
                    self.session_configs[session_id] = config
                elif session_id not in self.session_configs:
                    # Create default config with allowed domains
                    self.session_configs[session_id] = BrowserContextConfig(
                        allowed_domains=allowed_domains or []
                    )
                base_session = _session_key(session_id)
                held = len(self.session_contexts.get(base_session, ())) + sum(
                    1 for s in self._creating if _session_key(s) == base_session)
                if held >= self.browser_config.max_contexts_per_session:
                    if not wait:
                        self.logger.warning(
                            f"Per-session context limit reached ({self.browser_config.max_contexts_per_session}) "
                            f"for session {base_session}. Use wait=True to queue."
                        )
                        return None
                    wait_future = self._enqueue_waiter(session_id)
                elif self._slots_taken() >= self.browser_config.max_contexts:
                    if not wait:
                        self.logger.warning(
                            f"Context limit reached ({self.browser_config.max_contexts}) "
                            f"for session {session_id}. Use wait=True to queue."
                        )
                        return None
                    wait_future = self._enqueue_waiter(session_id)
                else:  # reserve the slot under the lock; create outside it
                    creating = asyncio.get_running_loop().create_future()
                    self._creating[session_id] = creating
                    reserved = True

        # Released the pool lock. If we were queued, wait for a context here.
        if wait_future is not None:
            return await self._wait_for_context(
                session_id,
                wait_future,
                timeout or self.browser_config.wait_queue_timeout,
            )
        if not reserved:  # another caller is creating this session's context
            return await asyncio.shield(creating)
        return await self._finish_create(session_id, creating)

    def _slots_taken(self) -> int:
        """In-use contexts plus reserved creations (both count against the cap)."""
        return len(self.contexts_in_use) + len(self._creating) + self._closing

    async def _finish_create(self, session_id: str, creating: asyncio.Future):
        """Create the reserved context OUTSIDE the lock (Playwright can take
        seconds), then register it; a failure frees the slot for the queue."""
        context = None
        try:
            context = await self._create_context(session_id)
        finally:
            async with self._pool_lock:
                self._creating.pop(session_id, None)
                if context is not None:
                    self._register(session_id, context)
                if not creating.done():
                    creating.set_result(context)
                if context is None:
                    await self._notify_wait_queue()
        return context

    def _register(self, session_id: str, context: BrowserContext) -> None:
        """IN_USE (caller holds the pool lock)."""
        self.contexts_in_use[session_id] = context
        self.context_allocation_times[session_id] = time.time()
        base = self.session_contexts.setdefault(_session_key(session_id), [])
        if session_id not in base:
            base.append(session_id)

    async def _create_context(self, session_id: str) -> Optional[BrowserContext]:
        """
        Create a new browser context.

        Args:
            session_id: Session identifier for the context

        Returns:
            New BrowserContext or None if creation fails
        """
        if not self.browser:
            self.logger.error(f"No browser available for session {session_id}")
            return None

        try:
            # Use session-specific config if available
            config = self.session_configs.get(session_id)
            context = await asyncio.wait_for(
                self.browser.new_context(config=config, session_id=session_id),
                timeout=self.browser_config.context_timeout
            )
            self.logger.info(f"Created new browser context for session {session_id}")
            return context

        except asyncio.TimeoutError:
            self.logger.error(
                f"Timeout creating context for {session_id} "
                f"after {self.browser_config.context_timeout}s"
            )
            return None
        except Exception as e:
            self.logger.error(f"Failed to create context for {session_id}: {e}")
            return None

    def _detach_context(self, session_id: str) -> Optional[BrowserContext]:
        """Take the session's context out of the pool (caller holds the lock). The slot is
        free from here; the caller closes the context OUTSIDE the lock, because a close of
        a dead browser takes seconds and every session waits on this lock."""
        if session_id not in self.contexts_in_use:
            self.logger.debug(f"No context to release for session {session_id} (already released or never allocated)")
            return None

        context = self.contexts_in_use.pop(session_id)
        self.context_allocation_times.pop(session_id, None)
        self._closing += 1  # released by _close_detached

        # Remove from session tracking
        base_session = _session_key(session_id)
        if base_session in self.session_contexts:
            if session_id in self.session_contexts[base_session]:
                self.session_contexts[base_session].remove(session_id)
            # Clean up empty session entries
            if not self.session_contexts[base_session]:
                del self.session_contexts[base_session]
        return context

    async def _release_context_internal(self, session_id: str, close: bool = False) -> None:
        """
        Release that closes UNDER the pool lock: for the shutdown only, where no other
        session is served. Every other release uses release_context().

        Args:
            session_id: Session identifier releasing the context
            close: Whether to close the context instead of pooling
        """
        context = self._detach_context(session_id)
        if context is None:
            return
        await self._close_detached(context, session_id)  # 098 TS-06: always closed, never pooled

        # Process wait queue after releasing (no lock needed - caller holds it)
        await self._notify_wait_queue()

    async def release_context(self, session_id: str, close: bool = False) -> None:
        """
        Release a browser context and close it, outside the pool lock.
        This method is idempotent - calling it multiple times is safe.

        Args:
            session_id: Session identifier releasing the context
            close: Kept for callers; a context is always closed, never pooled
        """
        async with self._pool_lock:
            context = self._detach_context(session_id)
        if context is None:
            return
        await self._close_detached(context, session_id)
        async with self._pool_lock:
            await self._notify_wait_queue()

    def forget_session_config(self, session_id: str) -> None:
        """Drop the cached per-session BrowserContextConfig, if any.

        Call this only when a session is permanently ending (full session
        teardown) -- while a session is alive, the cached config is
        deliberately reused across repeated get_context()/release_context()
        cycles so allowed_domains stay consistent for the whole conversation
        (see get_context's `elif session_id not in self.session_configs`
        fallback). session_configs otherwise grows one entry per session_id
        that ever requested a browser context, for the life of the process.
        """
        self.session_configs.pop(session_id, None)

    async def _cleanup_stale_contexts(self) -> None:
        """
        Periodically clean up stale contexts that have been in use too long.
        """
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                await self._reap_stale_contexts_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in stale context cleanup: {e}")

    async def _reap_stale_contexts_once(self) -> None:
        """One reaper pass: close contexts IDLE (not merely old) past the timeout."""
        async with self._pool_lock:
            current_time = time.time()
            stale_sessions = [
                session_id
                for session_id, last_used in self.context_allocation_times.items()
                if current_time - last_used > self.browser_config.stale_context_timeout
            ]
            detached = []
            for session_id in stale_sessions:
                self.logger.warning(
                    f"Cleaning up stale context for session {session_id} "
                    f"(idle {current_time - self.context_allocation_times.get(session_id, current_time):.0f}s)"
                )
                context = self._detach_context(session_id)
                if context is not None:
                    detached.append((context, session_id))
        if detached:
            # Shielded: these left the pool, so a cancel here would lose the rest for ever.
            await asyncio.shield(asyncio.gather(
                *(self._close_detached(context, session_id) for context, session_id in detached)))
            async with self._pool_lock:
                await self._notify_wait_queue()

    def _enqueue_waiter(self, session_id: str) -> asyncio.Future:
        """
        Register a session in the wait queue and return its future.

        MUST be called while holding ``self._pool_lock`` (it mutates the queue
        structures). It does NOT acquire the lock itself, so it is safe to call
        from inside ``get_context``'s locked region. The caller then releases
        the lock and awaits the returned future via ``_wait_for_context``.
        """
        future = asyncio.get_event_loop().create_future()
        self._wait_queue[session_id] = future
        self._queue_order.append(session_id)
        self.logger.info(
            f"Session {session_id} queued for context (position {len(self._queue_order)})"
        )
        return future

    async def _wait_for_context(
        self,
        session_id: str,
        future: asyncio.Future,
        timeout: float
    ) -> Optional[BrowserContext]:
        """
        Wait for a previously-enqueued context request to be fulfilled.

        Args:
            session_id: Session identifier (already registered via _enqueue_waiter)
            future: The future returned by _enqueue_waiter
            timeout: Maximum time to wait

        Returns:
            BrowserContext or None if timeout
        """
        delivered = False
        try:
            context = await asyncio.wait_for(future, timeout=timeout)
            delivered = True
            if context:
                self.logger.info(f"Session {session_id} acquired context from queue")
            return context or None
        except asyncio.TimeoutError:
            self.logger.warning(f"Session {session_id} timed out waiting for context")
            return None
        finally:
            # 098 TS-05: every exit — timeout, error, CANCEL — leaves the queue;
            # a context handed over to a waiter that is gone is released.
            self._wait_queue.pop(session_id, None)
            if session_id in self._queue_order:
                self._queue_order.remove(session_id)
            if (not delivered and future.done() and not future.cancelled()
                    and future.exception() is None and future.result() is not None):
                asyncio.ensure_future(self.release_context(session_id, close=True))

    async def _notify_wait_queue(self) -> None:
        """
        Process the wait queue and allocate contexts to waiting sessions.
        """
        # This is called within _pool_lock
        if not self._queue_order:
            return

        # Process waiting sessions in FIFO order
        while self._queue_order:
            if self._slots_taken() >= self.browser_config.max_contexts:
                break

            # Get the first waiter
            session_id = self._queue_order[0]
            if session_id not in self._wait_queue:
                # Already timed out or cancelled
                self._queue_order.pop(0)
                continue

            future = self._wait_queue[session_id]
            if future.done():  # the waiter is gone (cancelled): skip it
                self._queue_order.pop(0)
                del self._wait_queue[session_id]
                continue

            context = await self._create_context(session_id)
            if not context:
                break  # could not allocate, stop trying
            self._queue_order.pop(0)
            del self._wait_queue[session_id]
            if future.done():  # cancelled while the context was created
                await self._close_quietly(context, session_id)
                continue
            self._register(session_id, context)
            future.set_result(context)
            self.logger.debug(f"Allocated context to waiting session {session_id}")

    async def _close_detached(self, context: BrowserContext, session_id: str) -> None:
        """Close a context _detach_context took out of the pool; its slot frees after."""
        try:
            await self._close_quietly(context, session_id)
        finally:
            self._closing -= 1

    async def _close_quietly(self, context: BrowserContext, session_id: str) -> None:
        try:
            await asyncio.wait_for(context.close(), timeout=CONTEXT_CLOSE_TIMEOUT_SEC)
            self.logger.debug(f"Closed context for session {session_id}")
        except asyncio.TimeoutError:
            self.logger.error(f"Context for {session_id} did not close within "
                              f"{CONTEXT_CLOSE_TIMEOUT_SEC:g}s; its pool slot is freed")
        except Exception as e:
            self.logger.error(f"Error closing context for {session_id}: {e}")

    async def _process_wait_queue(self) -> None:
        """
        Background task to process the wait queue.

        This is a backstop only -- the primary path already resolves waiters
        synchronously from _release_context_internal the moment a context
        frees up (see _notify_wait_queue). This loop exists purely to recover
        from any path that frees capacity without going through that release
        call, so it does not need sub-second cadence.
        """
        while True:
            try:
                await asyncio.sleep(self.browser_config.wait_queue_backstop_interval)

                async with self._pool_lock:
                    await self._notify_wait_queue()

            except asyncio.CancelledError:
                # Cancel any waiting futures on shutdown
                async with self._pool_lock:
                    for session_id, future in self._wait_queue.items():
                        if not future.done():
                            future.cancel()
                    self._wait_queue.clear()
                    self._queue_order.clear()
                break
            except Exception as e:
                self.logger.error(f"Error processing wait queue: {e}")

    async def _cleanup(self) -> None:
        """Implementation-specific cleanup with retries and timeouts."""
        self.logger.info("Starting BrowserManager cleanup")

        # Cancel background tasks with shorter timeout for faster shutdown
        for task in [self._cleanup_task, self._queue_processor_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)  # Reduced from 5.0
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    self.logger.debug("Task cancellation timed out, continuing cleanup")
                except Exception as e:
                    self.logger.warning(f"Error cancelling task: {e}")
        # 098 TS-05: a later first use restarts them (the handles gated it).
        self._cleanup_task = self._queue_processor_task = None

        async with self._pool_lock:
            # Close all contexts in use with shorter timeout per context
            # Use internal release to avoid nested lock acquisition
            for session_id in list(self.contexts_in_use.keys()):
                try:
                    await asyncio.wait_for(
                        self._release_context_internal(session_id, close=True),
                        timeout=5.0  # Reduced from 10.0
                    )
                except asyncio.TimeoutError:
                    self.logger.warning(f"Timeout releasing context for {session_id}, forcing cleanup")
                    # Force remove from tracking
                    self.contexts_in_use.pop(session_id, None)
                    self.context_allocation_times.pop(session_id, None)
                except Exception as e:
                    self.logger.error(f"Error releasing context for {session_id}: {e}")
                    # Force remove from tracking even on error
                    self.contexts_in_use.pop(session_id, None)
                    self.context_allocation_times.pop(session_id, None)

            self.session_contexts.clear()
            for fut in list(self._wait_queue.values()) + list(self._creating.values()):
                if not fut.done():
                    fut.cancel()
            self._wait_queue.clear()
            self._queue_order.clear()
            self._creating.clear()

            # Close browser with retries
            if self.browser:
                for retry in range(3):
                    try:
                        await asyncio.wait_for(self.browser.close(), timeout=15.0)
                        self.logger.info("Browser closed successfully")
                        break
                    except asyncio.TimeoutError:
                        self.logger.warning(f"Timeout closing browser (retry {retry + 1}/3)")
                        if retry == 2:
                            self.logger.error("Failed to close browser after 3 attempts, forcing cleanup")
                    except Exception as e:
                        self.logger.error(f"Error closing browser: {e}")
                        break
                self.browser = None

            # Clean up orphaned browser processes
            await self._cleanup_orphaned_processes()

        self.logger.info("BrowserManager cleanup completed")

    async def _cleanup_orphaned_processes(self) -> None:
        """Kill orphaned browser processes to prevent memory leaks.

        This method is called during cleanup to ensure all browser processes
        are properly terminated even if Playwright doesn't clean them up.
        """
        try:
            import psutil
            import os

            current_pid = os.getpid()
            killed_count = 0

            for proc in psutil.process_iter(['pid', 'name', 'ppid']):
                try:
                    proc_info = proc.info
                    # Kill orphaned Playwright/Chrome processes
                    if (proc_info['ppid'] == current_pid and
                        proc_info['name'] in ['chrome', 'chromium', 'headless_shell', 'node']):
                        self.logger.debug(
                            f"Killing orphaned browser process: {proc_info['name']} "
                            f"(PID: {proc_info['pid']})"
                        )
                        proc.terminate()
                        proc.wait(timeout=3.0)
                        killed_count += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                    pass

            if killed_count > 0:
                self.logger.info(f"Cleaned up {killed_count} orphaned browser processes")

        except ImportError:
            self.logger.debug("psutil not available - skipping process cleanup")
        except Exception as e:
            self.logger.warning(f"Error during process cleanup: {e}")

    async def cleanup(self) -> None:
        """Public cleanup: the guarded BaseComponent path, so ``_initialized``
        resets and ``get_browser()`` re-initializes afterwards (098 TS-05). A
        never-initialized manager still closes whatever it holds."""
        if self._initialized:
            await super().cleanup()
        else:
            await self._cleanup()

    def get_status(self) -> Dict[str, Any]:
        """
        Get current status of the browser manager.

        Returns:
            Status dictionary with metrics
        """
        return {
            'browser_active': self.browser is not None,
            'contexts_in_use': len(self.contexts_in_use),
            'contexts_pooled': 0,
            'contexts_creating': len(self._creating),
            'max_contexts': self.browser_config.max_contexts,
            'max_contexts_per_session': self.browser_config.max_contexts_per_session,
            'sessions': list(self.contexts_in_use.keys()),
            'session_context_counts': {
                session: len(contexts)
                for session, contexts in self.session_contexts.items()
            },
            'wait_queue_depth': len(self._wait_queue),
            'wait_queue_sessions': list(self._wait_queue.keys()),
            'total_contexts_allocated': self._slots_taken(),
        }

    async def get_browser(self) -> Optional['Browser']:
        """Get the browser instance, initializing if needed."""
        if not self.is_initialized:
            await self.initialize()
        return self.browser
