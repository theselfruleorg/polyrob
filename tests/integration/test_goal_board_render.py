"""Work › Goals, rendered in a real browser against a real console.

A seeded ``goals.db`` (every live status, a dependency, a sub-goal, an open
decision, finished goals and another tenant's goal) behind a ``local``-posture
console (uvicorn, owner login). The board must draw every lane, the
dependency and the decision; the detail must open from a card and say what it
waits on; a phone width must stack the lanes. Screenshots land in
``GOAL_BOARD_SCREENSHOTS`` when it is set (the report attaches them).

Skips when no Chromium can launch. A Chromium that is installed but not the
build this Playwright expects can be named with ``PLAYWRIGHT_CHROMIUM_EXECUTABLE``.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.unit.webview.goal_board_seed import seed  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
OWNER_USER = "owner"
OWNER_PASSWORD = "board-check-password"
OWNER_ID = "owner-1"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def console(tmp_path):
    from argon2 import PasswordHasher
    data = tmp_path / "data"
    data.mkdir()
    ids = seed(str(data / "goals.db"), owner=OWNER_ID)
    port = _free_port()
    env = {**os.environ,
           "POLYROB_POSTURE": "local", "POLYROB_DATA_DIR": str(data),
           "POLYROB_HOME": str(tmp_path / "home"), "DATA_ROOT": str(tmp_path / "sessions"),
           "JWT_SECRET_KEY": "goal-board-render-check-secret-" * 2,
           "POLYROB_OWNER_USERNAME": OWNER_USER,
           "POLYROB_OWNER_PASSWORD_HASH": PasswordHasher().hash(OWNER_PASSWORD),
           # GOALS_ENABLED is a posture member: a direct env line is ignored,
           # the override bundle is the one way in. The console starts no
           # dispatcher, so nothing on the seeded board runs.
           "POLYROB_OVERRIDES": "GOALS_ENABLED=true", "PYTHONPATH": str(REPO),
           # The temp folder sits under /private/var on macOS, which the posture
           # guard reads as a system data dir; the owner login still fronts it.
           "WEBVIEW_ALLOW_LOCAL_POSTURE": "1",
           # A writable console needs a bound owner and the shared registry.
           "POLYROB_OWNER_USER_ID": OWNER_ID, "SESSION_REGISTRY_BACKEND": "sqlite"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "webview.server:app", "--host", "127.0.0.1",
         "--port", str(port)], cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail("console exited: " + proc.stdout.read().decode()[-3000:])
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.3)
        else:
            pytest.fail("console did not start in 60 s")
        yield f"http://127.0.0.1:{port}", ids
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _browser(p):
    exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
    try:
        return p.chromium.launch(executable_path=exe)
    except Exception as exc:  # no browser on this machine
        pytest.skip(f"no Chromium could launch: {exc}")


def _sign_in(page, base):
    page.goto(f"{base}/owner-login")
    page.fill("input[name=username]", OWNER_USER)
    page.fill("input[name=password]", OWNER_PASSWORD)
    page.click("form[action='/owner-login'] [type=submit]")
    page.wait_for_load_state("networkidle")


def _shot(page, name):
    out = os.environ.get("GOAL_BOARD_SCREENSHOTS")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / name), full_page=True)


def test_the_board_renders_every_lane_and_the_detail(console):
    base, ids = console
    with sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        _sign_in(page, base)
        page.goto(f"{base}/work#goals")
        page.wait_for_selector("#work-goals-board details.lane[data-lane=running] [data-goal-id]")

        lanes = page.eval_on_selector_all("#work-goals-board details.lane",
                                          "ls => ls.map(l => l.dataset.lane)")
        assert lanes == ["ask", "triage", "waiting", "ready", "running", "blocked", "paused"]
        assert "switched off" not in page.inner_text("#work-goals-notice")
        board = page.inner_text("#work-goals-board")
        assert "Which page should the site link to?" in board
        assert "Waits for: Write the weekly digest" in board
        assert "Waiting for your decision: Which page should the site link to?" in board
        assert "Another tenant's goal" not in board
        # the sub-goal sits inside its parent's card
        assert page.query_selector(
            f"[data-goal-id='{ids['digest']}'] .card-group [data-goal-id='{ids['sub']}']")
        # finished goals are folded away
        assert not page.eval_on_selector("[data-lane=finished]", "d => d.open")
        _shot(page, "goal-board-desktop.png")

        page.click(f"[data-goal-id='{ids['recap']}'] button.card-open")
        page.wait_for_selector("#goal-detail[open] .detail-section")
        detail = page.inner_text("#goal-detail")
        assert "Post the weekly recap" in detail
        assert "Write the weekly digest" in detail          # what it waits for
        assert f"goal={ids['recap']}" in page.evaluate("location.href")
        _shot(page, "goal-board-detail.png")
        page.click("#goal-detail-close")

        # the filter persists in the URL and narrows the board
        page.fill("#work-goals-bar input[name=q]", "landing")
        assert "q=landing" in page.evaluate("location.href")   # replaceState: page.url lags
        assert page.eval_on_selector_all("#work-goals-board [data-goal-id]", "n => n.length") == 1

        # the goal form sends the goals a new goal waits for; it lands in Waiting
        page.fill("#work-goals-bar input[name=q]", "")
        page.click("[data-pane=goals] details.section > summary")
        form = "form[data-create=goals]"
        page.fill(f"{form} [name=title]", "Email the weekly recap")
        page.fill(f"{form} [name=depends_on]", ids["recap"])
        page.click(f"{form} button[type=submit]")
        page.wait_for_selector("#work-goals-board details.lane[data-lane=waiting] "
                               ":text('Email the weekly recap')")
        assert "Waits for: Post the weekly recap" in page.inner_text(
            "#work-goals-board details.lane[data-lane=waiting]")

        phone = browser.new_page(viewport={"width": 390, "height": 844})
        _sign_in(phone, base)
        phone.goto(f"{base}/work#goals")
        phone.wait_for_selector("#work-goals-board [data-goal-id]")
        width = phone.evaluate("document.documentElement.scrollWidth")
        assert width <= 390, f"the phone page scrolls sideways ({width}px)"
        _shot(phone, "goal-board-phone.png")
        browser.close()
        assert not errors, errors
