"""
UAT conftest — env-gate so browser tests never run in the regular pytest pass.

Activate with:  RUN_UAT=1 uv run pytest tests/uat --no-cov -q
Default run (`uv run pytest`) skips every test in this package.
"""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

# ---------------------------------------------------------------------------
# Env-gate: skip everything unless RUN_UAT=1
# ---------------------------------------------------------------------------

_RUN_UAT = os.environ.get("RUN_UAT", "").strip() == "1"


def pytest_collection_modifyitems(config, items):
    """Auto-skip all UAT tests when RUN_UAT != 1."""
    if _RUN_UAT:
        return
    skip_marker = pytest.mark.skip(reason="UAT skipped — set RUN_UAT=1 to run")
    for item in items:
        if "tests/uat" in str(item.fspath):
            item.add_marker(skip_marker)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:8501"
SCREENSHOT_DIR = "docs/uat/screenshots"

# Typical wait after any Streamlit interaction (rerun settles in ~2-3 s)
RERUN_WAIT_MS = 3000

# Max initial load timeout (generous — Streamlit cold start)
LOAD_TIMEOUT_MS = 30_000


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_instance():
    """One Chromium browser for the entire UAT session."""
    if not _RUN_UAT:
        pytest.skip("RUN_UAT not set")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page_1920(browser_instance: Browser) -> Page:
    """1920x1080 page — cleaned up after each test."""
    ctx: BrowserContext = browser_instance.new_context(
        viewport={"width": 1920, "height": 1080}
    )
    page = ctx.new_page()
    yield page
    ctx.close()


@pytest.fixture
def page_1366(browser_instance: Browser) -> Page:
    """1366x768 page — cleaned up after each test."""
    ctx: BrowserContext = browser_instance.new_context(
        viewport={"width": 1366, "height": 768}
    )
    page = ctx.new_page()
    yield page
    ctx.close()


@pytest.fixture
def page_390(browser_instance: Browser) -> Page:
    """390x844 page (iPhone-size) — cleaned up after each test."""
    ctx: BrowserContext = browser_instance.new_context(
        viewport={"width": 390, "height": 844}
    )
    page = ctx.new_page()
    yield page
    ctx.close()


# ---------------------------------------------------------------------------
# Shared helpers (available to test modules via import)
# ---------------------------------------------------------------------------


def wait_idle(page: Page, ms: int = RERUN_WAIT_MS) -> None:
    """Wait for Streamlit to settle after an interaction."""
    page.wait_for_timeout(ms)


def pick_dossier(page: Page, index: int) -> str:
    """Open the dossier selectbox and choose the option at 1-based *index*.

    index=1 → first real dossier (sentinel at 0).
    Returns the inner_text of the selected option.
    """
    boxes = page.locator('[data-testid="stSelectbox"]')
    box = boxes.first
    box.click()
    wait_idle(page, 800)
    options = page.locator('li[role="option"]')
    options.nth(index).click()
    wait_idle(page, RERUN_WAIT_MS)
    return options.nth(index).inner_text() if options.count() > index else ""


def assert_no_traceback(page: Page) -> None:
    """Fail if a Python traceback or stException widget is visible."""
    body = page.inner_text("body")
    assert "Traceback" not in body, "Raw Python Traceback found on page"
    # Streamlit wraps unhandled exceptions in a div with data-testid=stException
    exc_els = page.locator('[data-testid="stException"]')
    assert exc_els.count() == 0, "stException element found on page"


def load_app(page: Page, path: str = "") -> None:
    """Navigate to the app, wait for Streamlit to finish loading."""
    page.goto(f"{BASE_URL}{path}", wait_until="domcontentloaded")
    # Wait for at least one stMarkdownContainer or stDataFrame to appear,
    # which means the Python script completed its first run.
    page.wait_for_selector(
        '[data-testid="stMarkdownContainer"], [data-testid="stDataFrame"]',
        timeout=LOAD_TIMEOUT_MS,
    )
    wait_idle(page, 1500)
