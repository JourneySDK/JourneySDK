"""Tutorial journey showing agentic prompting against a live Playwright page."""

from __future__ import annotations

from journeysdk import journey, step
from journeysdk.tools.playwright import (
    JourneyPlaywrightPromptResult,
    open_page,
)

from docs.playwright_resume_journey._auth_demo import ensure_demo_server


def capture_popup_title() -> JourneyPlaywrightPromptResult:
    """Use a multimodal LLM to open the sign-in popup and summarize it."""

    page = open_page(f"{ensure_demo_server()}/login")
    try:
        return page.prompt(
            'click on a "Sign in" button and get the title of the opened popup',
            model="anthropic/claude-sonnet-4-5",
        )
    finally:
        page.__exit__(None, None, None)


def assert_prompt_result(result: JourneyPlaywrightPromptResult) -> bool:
    """Confirm that the prompt returned a non-empty summary."""

    if not result.text.strip():
        raise AssertionError("Expected page.prompt(...) to return a non-empty result.")
    if not result.pages:
        raise AssertionError("Expected page.prompt(...) to report at least one page.")
    return True


@journey
def playwright_prompt_journey() -> None:
    prompt_result = step(capture_popup_title)
    step(assert_prompt_result, prompt_result)
