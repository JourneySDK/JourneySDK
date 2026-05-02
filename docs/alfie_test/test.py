from __future__ import annotations

from journeysdk import journey, step
from journeysdk.tools.playwright import open_page


def first():
    page = open_page("https://app.staging.heyalfie.com/", headless=False)
    result = page.prompt('Sign in as e2etest@heyalfie.com using password "1111". Expect no errors.', memory='sign-in')
    # should never reach here
    page.prompt('start chatting with Alfie - say you need to fix a toilet')
    return page


@journey
def playwright_resume_journey() -> None:
    step(first)
