from __future__ import annotations

import os
from pathlib import Path

from journeysdk import branch, journey, step
from journeysdk.touchpoints.browser import JourneyBrowserPage, open_page
from journeysdk.touchpoints.docker import DockerComposeStack, DockerLogMatcher, run_docker


def start_hey_alfie_services() -> DockerComposeStack:
    previous_cwd = Path.cwd()
    try:
        os.chdir("/Users/piotrsliwa/Hey-Alfie/dev")
        return run_docker(
            compose_file="/Users/piotrsliwa/Hey-Alfie/dev/docker-compose.yml",
            project_name="journey-hey-alfie-dev",
            wait_timeout=600,
            wait_for_logs=[
                DockerLogMatcher(
                    service_name=r"^backend$",
                    message=r"Application startup complete",
                    timeout=600,
                )
            ],
        )
    finally:
        os.chdir(previous_cwd)


def sign_in(_stack: DockerComposeStack) -> JourneyBrowserPage:
    page = open_page("http://localhost:3000/", headless=False)
    page.prompt(
        'Sign in as journeytest@heyalfie.com using password "1212" '
        '(or "1111" if not working). Expect no errors.',
        memory="sign-in",
    )
    return page


def run_toilet_chat_history_check(
    _stack: DockerComposeStack,
    saved_page: JourneyBrowserPage,
) -> bool:
    page = open_page(saved_page, headless=False)
    page.prompt(
        f"start chatting with Alfie - say you need to 'fix a toilet'. Expect there is the new chat added to the 'Active chats' section in the sidebar.",
        memory="start-chatting-about-toilet",
    )
    return True


def run_roof_chat_isolation_check(
    _stack: DockerComposeStack,
    saved_page: JourneyBrowserPage,
) -> bool:
    page = open_page(saved_page, headless=False)
    page.prompt(
        f"Start chatting with Alfie - say you need to 'repair a leaking roof'. Expect there is the new chat added to the 'Active chats' section in the sidebar.",
        memory="start-chatting-about-roof-leak",
    )
    return True


@journey
def browser_resume_journey() -> None:
    stack = step(start_hey_alfie_services)
    page = step(sign_in, stack)

    if branch(start_from=page):
        step(run_toilet_chat_history_check, stack, page)
    elif branch(start_from=page):
        step(run_roof_chat_isolation_check, stack, page)
