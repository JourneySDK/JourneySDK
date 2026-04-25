from __future__ import annotations

from journeysdk import journey, step
from journeysdk.tools.playwright import open_page


def first():
    page = open_page("https://app.staging.heyalfie.com/", headless=False)
    page.prompt('start chatting with Alfie - say you need to fix a toilet', model='claude-sonnet-4-6')
    # page.wait_for_selector('button:has-text("Log in")').click()
    return page


@journey
def playwright_resume_journey() -> None:
    step(first)
