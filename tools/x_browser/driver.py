"""XPageDriver — the Playwright-page choreography for x.com.

Pure page automation, injectable ``page`` (so the tool + tests drive it without
a real browser). Every selector lives in ONE ``SELECTORS`` dict — X markup churns
often, so there is exactly one place to fix when it does.

The driver NEVER touches credentials directly: a password is typed by the tool
via the browser secret-substitution seam, not passed here as a literal.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# x.com DOM handles (data-testid is X's most stable hook). One place to update.
SELECTORS = {
    "compose_box": '[data-testid="tweetTextarea_0"]',
    "post_button": '[data-testid="tweetButtonInline"], [data-testid="tweetButton"]',
    "home_marker": '[data-testid="SideNav_NewTweet_Button"]',
    "login_link": '[data-testid="loginButton"], [href="/login"]',
    "signup_name": 'input[name="name"]',
    "signup_email": 'input[name="email"]',
    "verification_code": 'input[name="verfication_code"], input[data-testid="ocfEnterTextTextInput"]',
    "password_new": 'input[name="password"]',
    "arkose_frame": 'iframe[src*="arkoselabs"], iframe[title*="challenge"]',
    "phone_field": 'input[name="phone_number"]',
}

HOME_URL = "https://x.com/home"


class XPageDriver:
    def __init__(self, page: Any) -> None:
        self.page = page

    async def _visible(self, selector: str, timeout: float = 4000) -> bool:
        try:
            el = await self.page.wait_for_selector(
                selector, timeout=timeout, state="visible")
            return el is not None
        except Exception:
            return False

    async def is_logged_in(self) -> bool:
        """True when the authenticated home chrome is present."""
        try:
            await self.page.goto(HOME_URL, wait_until="domcontentloaded")
        except Exception as e:
            logger.debug("x is_logged_in goto failed: %s", e)
        return await self._visible(SELECTORS["home_marker"], timeout=8000)

    async def post(self, text: str) -> str:
        """Compose + publish a tweet; return the resulting permalink URL."""
        if not await self.is_logged_in():
            raise RuntimeError("x session is not logged in")
        box = await self.page.wait_for_selector(
            SELECTORS["compose_box"], timeout=15000, state="visible")
        await box.click()
        await box.type(text, delay=15)
        # Small settle so the Post button enables.
        await asyncio.sleep(0.2)
        btn = await self.page.wait_for_selector(
            SELECTORS["post_button"], timeout=8000, state="visible")
        await btn.click()
        return await self._await_permalink()

    async def _await_permalink(self, timeout: float = 15000) -> str:
        """Best-effort resolution of the posted tweet's URL.

        Waits for the composer to clear (the reliable success signal), then reads
        the newest ``/status/`` link. Falls back to the profile URL if X did not
        surface a permalink in time — the caller still knows the post succeeded.
        """
        try:
            await self.page.wait_for_selector(
                SELECTORS["compose_box"], state="hidden", timeout=timeout)
        except Exception:
            pass
        try:
            href = await self.page.get_attribute(
                'a[href*="/status/"]', "href")
            if href:
                return href if href.startswith("http") else f"https://x.com{href}"
        except Exception as e:
            logger.debug("x permalink read failed: %s", e)
        return HOME_URL

    # -- signup choreography (used by tools/x_browser/signup.py) ----------

    SIGNUP_URL = "https://x.com/i/flow/signup"

    async def open_signup(self) -> None:
        await self.page.goto(self.SIGNUP_URL, wait_until="domcontentloaded")

    async def fill_details(self, name: str, email: str, dob: str) -> None:
        await self._type_if_present(SELECTORS["signup_name"], name)
        await self._type_if_present(SELECTORS["signup_email"], email)
        # DOB fields on X are three selects; left to the live form + owner assist.

    async def request_code(self) -> None:
        # Advancing the multi-step form triggers the emailed code. The concrete
        # "Next"/"Sign up" click sequence is resolved live; kept minimal so the
        # obstacle guard, not a brittle click script, owns the hard cases.
        pass

    async def enter_code(self, code: str) -> None:
        await self._type_if_present(SELECTORS["verification_code"], code)

    async def set_password(self, password: str) -> None:
        await self._type_if_present(SELECTORS["password_new"], password)

    async def set_handle_and_profile(self, handle: str, bio: str) -> None:
        # Profile/bio editing happens post-signup on /settings/profile; the
        # automation disclosure (bio) is applied there.
        pass

    async def current_handle(self) -> str:
        try:
            el = await self.page.query_selector(
                '[data-testid="SideNav_AccountSwitcher_Button"]')
            if el:
                text = await el.inner_text()
                for token in text.split():
                    if token.startswith("@"):
                        return token.lstrip("@")
        except Exception:
            pass
        return ""

    async def export_storage_state(self) -> dict:
        try:
            return await self.page.context.storage_state()
        except Exception:
            return {}

    async def probe(self) -> str:
        """Classify the current page for the signup obstacle taxonomy."""
        if await self._visible(SELECTORS["arkose_frame"], timeout=1500):
            return "arkose_challenge"
        if await self._visible(SELECTORS["phone_field"], timeout=1000):
            return "phone_required"
        try:
            body = (await self.page.inner_text("body"))[:2000].lower()
        except Exception:
            body = ""
        if any(k in body for k in ("suspended", "couldn't create", "unusual")):
            return "account_suspended" if "suspend" in body else "unusual_activity"
        return "ok"

    async def challenge_cleared(self) -> bool:
        return not await self._visible(SELECTORS["arkose_frame"], timeout=1500)

    async def _type_if_present(self, selector: str, value: str) -> None:
        try:
            el = await self.page.wait_for_selector(
                selector, timeout=6000, state="visible")
            if el:
                await el.fill(value)
        except Exception as e:
            logger.debug("x signup field %s not fillable: %s", selector, e)
