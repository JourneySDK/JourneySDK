from __future__ import annotations

from journeysdk import journey, step
from journeysdk.tools.playwright import capture_page_state, open_page, PlaywrightPageState


def first():
    with open_page(PlaywrightPageState.from_url('https://app.staging.heyalfie.com/'), headless=False) as page:
        page.wait_for_selector('button:has-text("Log in")').click()
        session = capture_page_state(page)


@journey
def playwright_resume_journey() -> None:
    step(first)