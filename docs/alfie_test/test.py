from __future__ import annotations

from journeysdk import journey, step
from journeysdk.tools.playwright import open_page


def first():
    page = open_page("https://app.staging.heyalfie.com/", headless=False)
    page.wait_for_selector('button:has-text("Log in")').click()


@journey
def playwright_resume_journey() -> None:
    step(first)
