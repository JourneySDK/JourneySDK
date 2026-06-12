# 04 Browser and Local Touchpoints

Journey steps can talk to browsers, local files, Docker Compose apps, hosted webhooks, and any other ordinary Python
integration. This chapter keeps the tutorial examples in one place. For full helper signatures, lifecycle rules, and
limits, print the packaged reference for the touchpoint you are about to use:

```bash
uv run journey touchpoints browser
uv run journey touchpoints docker
uv run journey touchpoints webhook
uv run journey touchpoints http
uv run journey touchpoints all
```

The canonical touchpoint reference source is `journeysdk/touchpoint_docs/*.md`.

## Browser, Webhook, and Local File Branches

Read these files together:

- `docs/simple_journey/simple_journey.py`
- `docs/simple_journey/demo_site.html`

The example opens a local HTML page, branches into a hosted webhook path and a local file path, then uses targeted
execution to run only the branch under development. Each branch is one user-flow step: the click, wait, and assertion
that prove that outcome stay together.

```python
from journeysdk import branch, journey, step


@journey
def simple_journey() -> None:
    homepage = step(demo_homepage_ready)

    if branch(replay_from=homepage):
        step(trigger_endpoint_a_and_verify_webhook)
    elif branch(replay_from=homepage):
        step(store_local_file_and_verify_contents)
```

Run only the file branch:

```bash
uv run journey verify --step store_local_file_and_verify_contents --file docs/simple_journey/simple_journey.py
```

Use `journey touchpoints browser` and `journey touchpoints webhook` for full `open_page(...)`,
recording, replay, `get_webhook_endpoint(...)`, and `wait_for_webhook_request(...)` details.

## Docker Compose Snapshot Tutorial

Read these files together:

- `docs/docker_compose_journey/docker_compose_journey.py`
- `docs/docker_compose_journey/docker-compose.yml`
- `docs/docker_compose_journey/app/Dockerfile`
- `docs/docker_compose_journey/app/server.py`

This example starts a tiny HTTP app plus Postgres, captures a branch-anchor snapshot after `counter_baseline_ready`,
then proves two branches can reuse that baseline:

- branch A increments a database-backed counter from `0` to `1`
- branch B replays from the same anchor and sees the counter restored to `0`

The important authored shape is the branch structure:

```python
from journeysdk import branch, journey, step


@journey
def docker_compose_journey() -> None:
    stack = step(start_docker_stack)
    baseline = step(counter_baseline_ready, stack)

    if branch(replay_from=baseline):
        step(increment_counter_and_assert_branch, stack, baseline)
    elif branch(replay_from=baseline):
        step(read_restored_counter_and_assert_branch, stack, baseline)
```

Run both Docker branches:

```bash
uv run journey verify --file docs/docker_compose_journey/docker_compose_journey.py
```

Target the restore branch while iterating:

```bash
uv run journey loop read_restored_counter_and_assert_branch --file docs/docker_compose_journey/docker_compose_journey.py
```

Use `journey touchpoints docker` for `run_docker`, `DockerLogMatcher`, `DockerHttpCheck`,
`DockerComposeStack.service_url(...)`, lifecycle, snapshot, and rehydration limits.

## Browser Session Resume Tutorial

Read `docs/browser_resume_journey/browser_resume_journey.py`.

The first step signs in and returns a saved browser page. The retryable second step reopens that page and continues the
authenticated flow:

```python
from journeysdk import journey, step


@journey
def browser_resume_journey() -> None:
    pause_seconds = 2.0
    session = step(login_and_capture_session)
    result = step(
        continue_authenticated_dashboard,
        session,
        pause_seconds,
        retry=1,
        retry_delay=0,
    )
    step(assert_protected_action_complete, result)
```

Reset the demo, then run and interrupt it once after the saved session is available:

```bash
uv run python -c "from docs.browser_resume_journey import reset_demo_state; reset_demo_state()"
uv run journey verify --reuse-state --file docs/browser_resume_journey/browser_resume_journey.py
```

Run the same command again to resume from saved state:

```bash
uv run journey verify --reuse-state --file docs/browser_resume_journey/browser_resume_journey.py
```

Use `journey touchpoints browser` for `JourneyBrowserPage`, page rehydration, lifecycle, evidence, and
`open_page(saved_page)` details.

## Browser Discover

Use `journey discover` when a local app is running and you want Journey to draft broad browser coverage from the app UI:

```bash
uv run journey discover http://127.0.0.1:3000 --file journeys/discovered_journey.py
uv run journey verify --file journeys/discovered_journey.py
```

The discoverer opens the start URL, extracts forms, links, buttons, labels, `data-testid`s, finite controls, and route
text, then tries complete deterministic transitions such as fill-select-submit before asking the configured Claude Haiku
model for uncertain next transitions. Select, radio, and checkbox choices are branched up to
`--max-variants-per-control`; discovered stable identifiers can feed generic same-origin JSON, local email, and local
webhook evidence assertions when those endpoints are reachable. Use `JOURNEY_DISCOVER_EMAIL_EVIDENCE_URL` and
`JOURNEY_DISCOVER_WEBHOOK_EVIDENCE_URL` to point discovery at local evidence services that are not on their usual
development ports. It executes restricted Playwright snippets during discovery and writes deterministic `open_page(...)`
step helpers with `branch(...)` where it finds alternate paths. The generated file is a draft: review step boundaries,
fixture data, and assertions before treating it as committed coverage.

## Browser Prompt Tutorial

Read `docs/browser_prompt_journey/browser_prompt_journey.py`.

Use `page.prompt(...)` when a bounded browser action is clearer in natural language than selector code:

```python
def capture_popup_title() -> dict[str, object]:
    page = open_page(f"{ensure_demo_server()}/login")
    return page.prompt(
        'click on a "Sign in" button and get the title of the opened popup',
        memory="sign-in-popup",
        output={
            "popup_title": "The title of the opened popup.",
        },
    )
```

Run the prompt journey:

```bash
uv run journey verify --file docs/browser_prompt_journey/browser_prompt_journey.py
```

Prompt memories are stored as `<memory>.memory.md` files next to the journey's `.journey` directory. Use
`journey touchpoints browser` for prompt memory, model configuration, selector-vs-prompt guidance, and structured
output details.

## What To Notice

- Tutorial journey files show user-flow structure; touchpoint references hold the complete API details.
- `branch(replay_from=...)` lets later cases reuse durable setup from a saved step boundary.
- Browser, webhook, file, and Docker details belong inside coarse user-flow steps, not as one Journey step per click,
  poll, or assertion.
- `journey discover` is a fast way to bootstrap browser coverage, but generated specs still need executable
  `journey verify` evidence before they count as complete.
- `journey loop` and `journey verify --step` are the fastest way to iterate on one branch or late user-flow step.
- Touchpoints remain ordinary Python helpers used from step functions.

Continue with [05 Journey Cloud Integrations](05-journey-cloud-integrations.md) for focused webhook and email examples
hosted by Journey Cloud.
