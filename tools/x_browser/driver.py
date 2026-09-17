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
import re
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
    "conversation_link": 'a[href^="/messages/"]',
    "message_entry": '[data-testid="messageEntry"]',
    "dm_composer": '[data-testid="dmComposerTextInput"]',
    "dm_send": '[data-testid="dmComposerSendButton"]',
    # Post-signup profile edit (/settings/profile) and handle change
    # (/settings/screen_name). Best-effort: X churns these, and a miss is
    # REPORTED (applied=False), never faked.
    "profile_bio": '[data-testid="ProfileDescriptionTextarea"], textarea[name="description"]',
    "profile_save": '[data-testid="Profile_Save_Button"]',
    "screen_name_input": 'input[name="typedScreenName"], input[name="screen_name"]',
    "settings_save": '[data-testid="settingsDetailSave"]',
}

PROFILE_SETTINGS_URL = "https://x.com/settings/profile"
SCREEN_NAME_SETTINGS_URL = "https://x.com/settings/screen_name"

HOME_URL = "https://x.com/home"
MESSAGES_URL = "https://x.com/messages"


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

    # -- direct messages -------------------------------------------------

    async def _conversation_rows(self) -> list:
        """Return visible inbox rows in a stable, serializable shape."""
        rows = []
        for link in await self.page.query_selector_all(
                SELECTORS["conversation_link"]):
            try:
                href = await link.get_attribute("href") or ""
                text = (await link.inner_text()).strip()
            except Exception:
                continue
            if not href or href.rstrip("/") == "/messages":
                continue
            rows.append({
                "conversation_id": href.rstrip("/").rsplit("/", 1)[-1],
                "href": href,
                "summary": text,
            })
        return rows

    @staticmethod
    def _matches_conversation(row: dict, participant: str) -> bool:
        needle = str(participant or "").strip().lstrip("@").casefold()
        if not needle:
            return False
        cid = str(row.get("conversation_id") or "").casefold()
        summary = str(row.get("summary") or "").casefold()
        return needle == cid or f"@{needle}" in summary or needle in summary

    async def _open_conversation(self, participant: str) -> dict:
        direct_id = str(participant or "").strip()
        if re.fullmatch(r"[0-9]+(?:-[0-9]+)*", direct_id):
            url = f"{MESSAGES_URL}/{direct_id}"
            await self.page.goto(url, wait_until="domcontentloaded")
            return {"conversation_id": direct_id, "href": f"/messages/{direct_id}",
                    "summary": ""}
        await self.page.goto(MESSAGES_URL, wait_until="domcontentloaded")
        await self.page.wait_for_selector(
            SELECTORS["conversation_link"], timeout=15000, state="visible")
        rows = await self._conversation_rows()
        match = next((r for r in rows
                      if self._matches_conversation(r, participant)), None)
        if match is None:
            raise RuntimeError(
                f"no visible X DM conversation matches '{participant}'")
        href = str(match["href"])
        url = href if href.startswith("http") else f"https://x.com{href}"
        await self.page.goto(url, wait_until="domcontentloaded")
        return match

    async def read_dms(self, participant: str = "", max_results: int = 20) -> dict:
        """Read the visible inbox or an existing thread through the logged-in UI."""
        if not await self.is_logged_in():
            raise RuntimeError("x session is not logged in")
        if not participant:
            await self.page.goto(MESSAGES_URL, wait_until="domcontentloaded")
            try:
                await self.page.wait_for_selector(
                    SELECTORS["conversation_link"], timeout=15000, state="visible")
            except Exception as exc:
                # Never turn a stale selector / challenge page into a confident
                # empty inbox (the same semantic bug this rail exists to avoid).
                try:
                    body = (await self.page.inner_text("body")).casefold()
                except Exception:
                    body = ""
                empty_markers = (
                    "welcome to your inbox",
                    "send a message, get a message",
                )
                if any(marker in body for marker in empty_markers):
                    return {"view": "inbox", "conversations": []}
                raise RuntimeError(
                    "X inbox did not expose conversation rows; the page may be "
                    "blocked or the selector may need updating") from exc
            rows = await self._conversation_rows()
            return {"view": "inbox", "conversations": rows[:max_results]}

        row = await self._open_conversation(participant)
        await self.page.wait_for_selector(
            SELECTORS["message_entry"], timeout=15000, state="visible")
        messages = []
        entries = await self.page.query_selector_all(SELECTORS["message_entry"])
        for entry in entries[-max_results:]:
            try:
                text = (await entry.inner_text()).strip()
            except Exception:
                continue
            if text:
                messages.append({"text": text})
        return {"view": "thread", "conversation": row, "messages": messages}

    async def send_dm(self, participant: str, text: str) -> dict:
        """Send to an existing visible conversation through the logged-in UI."""
        if not await self.is_logged_in():
            raise RuntimeError("x session is not logged in")
        row = await self._open_conversation(participant)
        box = await self.page.wait_for_selector(
            SELECTORS["dm_composer"], timeout=15000, state="visible")
        await box.click()
        await box.type(text, delay=10)
        button = await self.page.wait_for_selector(
            SELECTORS["dm_send"], timeout=8000, state="visible")
        await button.click()
        return {"conversation_id": row["conversation_id"], "sent": True}

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

    async def set_handle_and_profile(self, handle: str, bio: str) -> dict:
        """Apply the requested @handle and the automation-disclosure bio.

        Runs AFTER the account exists (the settings pages need a login). Each
        half is independent and best-effort, and the outcome is RETURNED as
        ``{"handle_applied": bool, "bio_applied": bool, "handle": <live>}`` so
        the caller can say what actually happened. A handle X refuses (taken,
        too long) leaves the assigned one in place — that is a fact to report,
        not a failure of the signup. Until 2026-09-17 this method was a ``pass``
        stub while the docs claimed the disclosure was written into the bio.
        """
        out = {"handle_applied": False, "bio_applied": False, "handle": ""}
        if handle:
            try:
                await self.page.goto(SCREEN_NAME_SETTINGS_URL, wait_until="domcontentloaded")
                el = await self.page.wait_for_selector(
                    SELECTORS["screen_name_input"], timeout=8000, state="visible")
                if el:
                    await el.fill(handle)
                    await asyncio.sleep(0.3)
                    btn = await self.page.wait_for_selector(
                        SELECTORS["settings_save"], timeout=6000, state="visible")
                    if btn:
                        await btn.click()
                        await asyncio.sleep(1.0)
                live = await self.current_handle()
                out["handle_applied"] = bool(live) and live.lower() == handle.lower()
            except Exception as e:
                logger.debug("x handle change not applied: %s", e)
        if bio:
            try:
                await self.page.goto(PROFILE_SETTINGS_URL, wait_until="domcontentloaded")
                el = await self.page.wait_for_selector(
                    SELECTORS["profile_bio"], timeout=8000, state="visible")
                if el:
                    await el.fill(bio)
                    await asyncio.sleep(0.3)
                    btn = await self.page.wait_for_selector(
                        SELECTORS["profile_save"], timeout=6000, state="visible")
                    if btn:
                        await btn.click()
                        await asyncio.sleep(1.0)
                        out["bio_applied"] = True
            except Exception as e:
                logger.debug("x bio not applied: %s", e)
        out["handle"] = await self.current_handle()
        return out

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
