from __future__ import annotations

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright


def connect_via_cdp(cdp_url: str) -> tuple[object, Browser, BrowserContext, Page]:
    """
    Connect to an already-running local browser with remote debugging enabled.
    Returns (playwright, browser, context, page). Caller must close playwright.
    """
    p = sync_playwright().start()
    browser = p.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.pages[0] if context.pages else context.new_page()
    return p, browser, context, page

