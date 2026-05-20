from __future__ import annotations

import os
from pathlib import Path

from journeysdk import branch, journey, step
from journeysdk.touchpoints.browser import JourneyBrowserPage, open_page
from journeysdk.touchpoints.docker import DockerComposeStack, run_docker


def start_hey_alfie_services() -> DockerComposeStack:
    previous_cwd = Path.cwd()
    try:
        os.chdir("/Users/piotrsliwa/Hey-Alfie/dev")
        return run_docker(
            compose_file="/Users/piotrsliwa/Hey-Alfie/dev/docker-compose.yml",
            project_name="journey-hey-alfie-dev",
            wait_timeout=600,
        )()
    finally:
        os.chdir(previous_cwd)


def sign_in(_stack: DockerComposeStack) -> JourneyBrowserPage:
    page = open_page("http://localhost:3000/", headless=False)
    page.prompt(
        'Sign in as e2etest@heyalfie.com using password "1212" '
        '(or "1111" if not working). Expect no errors.',
        memory="sign-in",
    )
    return page


TOILET_CHAT_TOPIC = "fix a toilet"
ROOF_CHAT_TOPIC = "repair a leaking roof"


def start_chatting_about_toilet(
    _stack: DockerComposeStack,
    saved_page: JourneyBrowserPage,
) -> JourneyBrowserPage:
    page = open_page(saved_page, headless=False)
    page.prompt(
        f"start chatting with Alfie - say you need to {TOILET_CHAT_TOPIC}",
        memory="start-chatting-about-toilet",
    )
    return page


def start_chatting_about_roof_leak(
    _stack: DockerComposeStack,
    saved_page: JourneyBrowserPage,
) -> JourneyBrowserPage:
    page = open_page(saved_page, headless=False)
    page.prompt(
        f"start chatting with Alfie - say you need to {ROOF_CHAT_TOPIC}",
        memory="start-chatting-about-roof-leak",
    )
    return page


def _assert_toilet_chat_visibility(
    page: JourneyBrowserPage,
    *,
    expected_visible: bool,
    memory: str,
) -> None:
    result = page.prompt(
        "Inspect the chat history side panel. Do not create a new chat. "
        "Report whether any previous chat about fixing a toilet is visible there.",
        memory=memory,
        output={
            "toilet_chat_visible": {
                "type": "boolean",
                "description": (
                    "True only when the side panel visibly contains a chat "
                    "about fixing a toilet."
                ),
            },
            "evidence": "Briefly describe what is visible in the side panel.",
        },
    )
    if (
        not isinstance(result, dict)
        or result.get("toilet_chat_visible") is not expected_visible
    ):
        expectation = (
            "toilet chat in side panel"
            if expected_visible
            else "no toilet chat in side panel"
        )
        raise AssertionError(f"Expected {expectation}, got: {result!r}")


def assert_toilet_chat_seen_in_side_panel(
    _stack: DockerComposeStack,
    saved_page: JourneyBrowserPage,
) -> JourneyBrowserPage:
    page = open_page(saved_page, headless=False)
    _assert_toilet_chat_visibility(
        page,
        expected_visible=True,
        memory="assert-toilet-chat-seen",
    )
    return page


def assert_toilet_chat_not_seen_in_side_panel_before_roof_chat(
    _stack: DockerComposeStack,
    saved_page: JourneyBrowserPage,
) -> JourneyBrowserPage:
    page = open_page(saved_page, headless=False)
    _assert_toilet_chat_visibility(
        page,
        expected_visible=False,
        memory="assert-toilet-chat-not-seen-before-roof-chat",
    )
    return page


def assert_toilet_chat_not_seen_in_side_panel_after_roof_chat(
    _stack: DockerComposeStack,
    saved_page: JourneyBrowserPage,
) -> JourneyBrowserPage:
    page = open_page(saved_page, headless=False)
    _assert_toilet_chat_visibility(
        page,
        expected_visible=False,
        memory="assert-toilet-chat-not-seen-after-roof-chat",
    )
    return page


@journey
def browser_resume_journey() -> None:
    stack = step(start_hey_alfie_services)
    page = step(sign_in, stack)

    if branch(start_from=page):
        toilet_page = step(start_chatting_about_toilet, stack, page)
        step(assert_toilet_chat_seen_in_side_panel, stack, toilet_page)
    elif branch(start_from=page):
        restored_page = step(
            assert_toilet_chat_not_seen_in_side_panel_before_roof_chat,
            stack,
            page,
        )
        roof_page = step(start_chatting_about_roof_leak, stack, restored_page)
        step(
            assert_toilet_chat_not_seen_in_side_panel_after_roof_chat,
            stack,
            roof_page,
        )
