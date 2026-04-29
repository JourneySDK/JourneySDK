# 04 Browser and Local Integrations

Journey does not care whether a step talks to a browser, a file on disk, or a webhook listener. If it is ordinary Python, it can live inside the same authored journey.

This chapter shows both sides of that idea:

- one journey that mixes Playwright, a local webhook, and a downloaded file
- one journey that snapshots a local Docker Compose app behind a step anchor
- one journey that captures a browser session so a later run can reopen it

## One Journey Can Mix Browser, Webhook, and Local File Work

Read these files together:

- `docs/simple_journey/simple_journey.py`
- `docs/simple_journey/demo_site.html`

The browser helper is still just a normal Python function:

```python
def assert_demo_homepage() -> bool:
    from playwright.sync_api import sync_playwright
    from journeysdk.tools.playwright import ensure_browser_installed

    ensure_browser_installed()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(_DEMO_PAGE_URL, wait_until="load")
        title = page.title()
        buttons = page.get_by_role("button")
        ...
        browser.close()
    return True
```

The local file step is equally ordinary:

```python
def local_file_is_written() -> dict[str, str]:
    if not _STORED_FILE.exists():
        raise FileNotFoundError(f"Local demo file '{_STORED_FILE}' was not downloaded.")
    return {
        "path": str(_STORED_FILE),
        "content": _STORED_FILE.read_text(encoding="utf-8"),
    }
```

And the journey that ties them together still reads like sequential Python:

```python
from journeysdk import branch, journey, step


@journey
def simple_journey() -> None:
    receive_endpoint_a = host_webhook_endpoint(port=8765, path="/endpoint-a")

    after_setup = step(assert_demo_homepage)

    if branch(start_from=after_setup):
        step(click_trigger_endpoint_a)
        request_payload = step(receive_endpoint_a)
        step(assert_endpoint_a_webhook, request_payload)
    elif branch(start_from=after_setup):
        step(click_store_local_file)
        file_info = step(local_file_is_written)
        step(assert_local_file_contents, file_info)
```

`ensure_browser_installed()` and `open_page()` automatically download Chromium the first time they need it in the
current environment. That first browser launch needs network access and can take a moment.

### Execute Only the File Branch

```bash
uv run journey --file docs/simple_journey/simple_journey.py --step assert_local_file_contents
```

```console
Plan
Journey docs/simple_journey/simple_journey.py:simple_journey
journey_id=simple_journey function_ref=...
- case_1 branch_env={'bg_1': 'branch_1'} labels=['assert_demo_homepage', 'click_trigger_endpoint_a', 'receive_webhook_endpoint_a', 'assert_endpoint_a_webhook']
- case_2 branch_env={'bg_1': 'branch_2'} labels=['assert_demo_homepage', 'click_store_local_file', 'local_file_is_written', 'assert_local_file_contents']
Summary: 1 journey planned, 2 cases planned, 0 failed

Execution
[journey] time=... level=INFO component=executor event=execution_log message="- case_2 start branches={bg_1=branch_2}"
[journey] time=... level=INFO component=executor event=execution_log message="  step assert_demo_homepage attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="  step click_store_local_file attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="  step local_file_is_written attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="  step assert_local_file_contents attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="- case_2 ok steps=4 duration=... stopped_at=assert_local_file_contents replay_anchor=assert_demo_homepage"
Summary: 1 journey executed, 1 case executed, 0 failed
```

That targeted run is a good example of why Journey is useful during development. You can focus on the file branch
without running the webhook branch, while still executing the selected case from its first step boundary.

## Snapshot a Local Docker Compose App

Read these files together:

- `docs/docker_compose_journey/docker_compose_journey.py`
- `docs/docker_compose_journey/docker-compose.yml`
- `docs/docker_compose_journey/app/Dockerfile`
- `docs/docker_compose_journey/app/server.py`

This example is intentionally branched. It boots a tiny HTTP app plus Postgres, captures a branch-anchor snapshot after
`capture_baseline_state`, then uses two later branches to show what restore means in practice:

- branch A increments a database-backed counter from `0` to `1`
- branch B replays from the same step-anchor snapshot and sees the counter back at `0`

The Docker helper is still just a normal Journey step factory:

```python
stack = step(
    run_docker(
        compose_file=_COMPOSE_FILE,
        project_name="journey-docker-docs",
    )
)
```

The shared setup reads like plain Python, even though the app is running locally in Docker:

```python
stack = step(
    run_docker(
        compose_file=_COMPOSE_FILE,
        project_name="journey-docker-docs",
    )
)
step(assert_stack_ready, stack)
```

The interesting state lives on the `stack` value, and `DockerComposeStack`
implements Journey's rehydration protocol:

```python
baseline = step(capture_baseline_state, stack)
```

The interesting part is the branch structure. One shared step captures the baseline counter before either
branch mutates anything, then the branches diverge:

```python
if branch(start_from=baseline):
    incremented = step(increment_counter, stack)
    step(assert_increment_branch, baseline, incremented)
elif branch(start_from=baseline):
    restored = step(read_counter_state, stack)
    step(assert_restored_counter_branch, baseline, restored)
```

### Execute Both Docker Branches

```bash
uv run journey --file docs/docker_compose_journey/docker_compose_journey.py
```

```console
Plan
Journey docs/docker_compose_journey/docker_compose_journey.py:docker_compose_journey
journey_id=docker_compose_journey function_ref=...
- case_1 branch_env={'bg_1': 'branch_1'} labels=['run_docker', 'assert_stack_ready', 'capture_baseline_state', 'increment_counter', 'assert_increment_branch']
- case_2 branch_env={'bg_1': 'branch_2'} labels=['run_docker', 'assert_stack_ready', 'capture_baseline_state', 'read_counter_state', 'assert_restored_counter_branch']
Summary: 1 journey planned, 2 cases planned, 0 failed

Execution
[journey] time=... level=INFO component=executor event=execution_log message="- case_1 start branches={bg_1=branch_1}"
[journey] time=... level=INFO component=executor event=execution_log message="  step run_docker attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="  step assert_stack_ready attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="  step capture_baseline_state attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="  step increment_counter attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="  step assert_increment_branch attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="- case_1 ok steps=5 duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="- case_2 start branches={bg_1=branch_2}"
[journey] time=... level=INFO component=executor event=execution_log message="  step read_counter_state attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="  step assert_restored_counter_branch attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="- case_2 ok steps=5 duration=..."
Summary: 1 journey executed, 2 cases executed, 0 failed
```

That second case is the whole point. Branch A already changed the counter to `1`, but branch B still reads `0`
because Journey restored the `capture_baseline_state` step-anchor snapshot before `read_counter_state` ran.

### Target the Restore Branch While Iterating

```bash
uv run journey --file docs/docker_compose_journey/docker_compose_journey.py --step assert_restored_counter_branch
```

That targeted run reports `replay_anchor=capture_baseline_state`, so you can focus on the restore branch without
changing the branch behavior. The targeted run reports the replay anchor as metadata; it does not skip directly to the
anchor unless existing state or retry behavior causes replay.

Docker snapshotting is strict on purpose. In v1 it aims for exact rollback of
container filesystems plus Docker-managed volume contents, so it rejects bind
mounts, external volumes, read-only mounts, and multi-container services.

## Capture and Resume a Browser Session

Read `docs/playwright_resume_journey/playwright_resume_journey.py`.

The first helper logs in once and serializes the browser state:

```python
def login_and_capture_session() -> JourneyPlaywrightPage:
    login_url = f"{ensure_demo_server()}/login"
    page = open_page(login_url)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/dashboard")
    page.wait_for_function(
        "() => document.getElementById('auth-state').textContent === 'authenticated'"
    )
    return page
```

The second helper reopens that saved state and keeps working from there:

```python
def continue_authenticated_dashboard(
    session: JourneyPlaywrightPage,
    pause_seconds: float,
) -> dict[str, str]:
    page = open_page(session)
    try:
        auth_state = page.locator("#auth-state").text_content()
        ...
        time.sleep(pause_seconds)
        page.get_by_role("button", name="Complete protected action").click()
        ...
        return {
            "auth_state": auth_state,
            "status": status_text or "",
        }
    finally:
        page.__exit__(None, None, None)
```

The journey is still small:

```python
from journeysdk import journey, step


@journey
def playwright_resume_journey() -> None:
    pause_seconds = 2.0
    session = step(login_and_capture_session)
    result = step(continue_authenticated_dashboard, session, pause_seconds)
    step(assert_protected_action_complete, result)
```

### Reset the Demo

```bash
uv run python -c "from docs.playwright_resume_journey import reset_demo_state; reset_demo_state(state_path='/tmp/journey-playwright-resume-tutorial.state')"
```

### First Run: Interrupt After the Session Is Saved

```bash
uv run journey --file docs/playwright_resume_journey/playwright_resume_journey.py --state /tmp/journey-playwright-resume-tutorial.state
```

Press `Ctrl-C` once when the tutorial note tells you to. The command stops after the active step completes; press it a
second time only if you want to force an immediate dirty-step interruption.

Expected stdout:

```console
Plan
Journey docs/playwright_resume_journey/playwright_resume_journey.py:playwright_resume_journey
journey_id=playwright_resume_journey function_ref=...
- case_1 branch_env={} labels=['login_and_capture_session', 'continue_authenticated_dashboard', 'assert_protected_action_complete']
Summary: 1 journey planned, 1 case planned, 0 failed

Execution
[journey] time=... level=INFO component=executor event=execution_log message="- case_1 start branches={}"
[journey] time=... level=INFO component=executor event=execution_log message="  step login_and_capture_session attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="  step continue_authenticated_dashboard attempt=1 start"
[journey] time=... level=WARNING component=cli event=graceful_interrupt_requested message="interrupt requested; waiting for the active step to reach post-exit" ...
[journey] time=... level=INFO component=executor event=execution_log message="  step continue_authenticated_dashboard attempt=1 ok duration=..."
Interrupted.
What happened: Journey execution was interrupted before it finished.
Try this: Run the same command again with --state
```

Expected stderr:

```console
[journey] time=... level=INFO component=tutorial event=tutorial_note message="Signed in and returned JourneyPlaywrightPage for http://127.0.0.1:.../dashboard. ..."
[journey] time=... level=INFO component=tutorial event=tutorial_note message="continue_authenticated_dashboard() reopened the saved dashboard at http://127.0.0.1:.../dashboard. ..."
[journey] time=... level=INFO component=tutorial event=tutorial_note message="Press Ctrl-C once during the next 2.0 seconds to stop gracefully after this step reaches post-exit. ..."
```

### Second Run: Reopen the Same Saved Session

```bash
uv run journey --file docs/playwright_resume_journey/playwright_resume_journey.py --state /tmp/journey-playwright-resume-tutorial.state
```

Expected stdout:

```console
Plan
Journey docs/playwright_resume_journey/playwright_resume_journey.py:playwright_resume_journey
journey_id=playwright_resume_journey function_ref=...
- case_1 branch_env={} labels=['login_and_capture_session', 'continue_authenticated_dashboard', 'assert_protected_action_complete']
Summary: 1 journey planned, 1 case planned, 0 failed

Execution
- case_1 resume branches={}
[journey] time=... level=INFO component=executor event=execution_log message="  step assert_protected_action_complete attempt=1 ok duration=..."
[journey] time=... level=INFO component=executor event=execution_log message="- case_1 ok steps=3 duration=..."
Summary: 1 journey executed, 1 case executed, 0 failed
```

Expected stderr:

```console
[journey] time=... level=INFO component=tutorial event=tutorial_note message="The protected action completed. If this run resumed from saved state, continue_authenticated_dashboard() restarted with the same saved JourneyPlaywrightPage instead of logging in again."
```

## Prompt a Live Page with an LLM

Read `docs/playwright_prompt_journey/playwright_prompt_journey.py`.

Journey SDK already includes Playwright and LiteLLM. Set your provider credentials with the normal provider
environment variables such as `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`. Pick a multimodal model explicitly with
`model=...`, or set `JOURNEY_PLAYWRIGHT_PROMPT_MODEL`.

The helper can stay small:

```python
def capture_popup_title() -> JourneyPlaywrightPromptResult:
    page = open_page(f"{ensure_demo_server()}/login")
    try:
        return page.prompt(
            'click on a "Sign in" button and get the title of the opened popup',
            model="anthropic/claude-sonnet-4-5",
        )
    finally:
        page.__exit__(None, None, None)
```

The next step can assert against the structured result:

```python
def assert_prompt_result(result: JourneyPlaywrightPromptResult) -> bool:
    assert result.text
    assert result.pages
    return True
```

`result.text` is the model's final user-facing answer. `result.pages` reports the original page plus any popup or tab
the prompt loop discovered, and `result.steps` records the bounded action history without storing hidden reasoning.

## What To Notice

- Browser logic stays inside normal Python functions. Journey does not wrap Playwright in a separate DSL.
- The same journey can branch into a webhook case and a local file case.
- `JourneyPlaywrightPage` is just another step value. Returning it lets Journey save it at a step boundary, close it,
  rehydrate it, and pass it into later steps.
- `JourneyPlaywrightPage.prompt(...)` works on a live page handle and returns a structured result instead of mutating the saved-page semantics of the original handle.
- Steps that open a page but return other data should close the page explicitly, as shown in `continue_authenticated_dashboard()`.
- Targeted execution is especially useful for UI work because you can rerun only the branch you are debugging.

Continue with [05 Journey Cloud Integrations](05-journey-cloud-integrations.md) when the external resource should be hosted by Journey Cloud instead of the local test process.
