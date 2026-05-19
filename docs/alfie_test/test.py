from __future__ import annotations

import os
from pathlib import Path

from journeysdk import journey, step
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


def start_chatting(
    _stack: DockerComposeStack,
    saved_page: JourneyBrowserPage,
) -> JourneyBrowserPage:
    page = open_page(saved_page, headless=False)
    page.prompt("start chatting with Alfie - say you need to fix a toilet")
    return page


@journey
def browser_resume_journey() -> None:
    stack = step(start_hey_alfie_services)
    page = step(sign_in, stack)
    step(start_chatting, stack, page)
