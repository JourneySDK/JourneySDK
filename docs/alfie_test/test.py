from __future__ import annotations

from journeysdk import journey, step
from journeysdk.tools.playwright import JourneyPlaywrightPage, open_page


def first() -> JourneyPlaywrightPage:
    page = open_page("https://app.staging.heyalfie.com/", headless=False)
    page.prompt('Sign in as e2etest@heyalfie.com using password "1212" (or "1111" if not working). Expect no errors.', memory='sign-in')
    return page


def start_chatting(saved_page: JourneyPlaywrightPage) -> JourneyPlaywrightPage:
    page = open_page(saved_page, headless=False)
    page.prompt('start chatting with Alfie - say you need to fix a toilet')
    return page


@journey
def playwright_resume_journey() -> None:
    page = step(first)
    # step(start_chatting, page)
