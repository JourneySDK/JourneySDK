# 04 Browser and Local Integrations

Journey does not care whether a step talks to a browser, a file on disk, or a webhook listener. If it is ordinary Python, it can live inside the same authored journey.

This chapter shows both sides of that idea:

- one journey that mixes Playwright, a local webhook, and a downloaded file
- one journey that snapshots a local Docker Compose app behind a checkpoint
- one journey that captures a browser session so a later run can reopen it

## One Journey Can Mix Browser, Webhook, and Local File Work

Read these files together:

- `docs/simple_journey/simple_journey.py`
- `docs/simple_journey/demo_site.html`

The browser helper is still just a normal Python function:

```python
def assert_demo_homepage() -> bool:
    from playwright.sync_api import sync_playwright

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
from journey import branch, checkpoint, journey, step


@journey
def simple_journey() -> None:
    receive_endpoint_a = host_webhook_endpoint(port=8765, path="/endpoint-a")

    step(assert_demo_homepage)

    after_setup = checkpoint()
    if branch(start_from=after_setup):
        step(click_trigger_endpoint_a)
        request_payload = step(receive_endpoint_a)
        step(assert_endpoint_a_webhook, request_payload)
    elif branch(start_from=after_setup):
        step(click_store_local_file)
        file_info = step(local_file_is_written)
        step(assert_local_file_contents, file_info)
```

### One-Time Playwright Setup

```bash
uv run --with playwright python -m playwright install chromium
```

### Plan the Browser Journey

```bash
uv run journey plan --file docs/simple_journey/simple_journey.py
```

```console
Journey docs/simple_journey/simple_journey.py:simple_journey
journey_id=simple_journey function_ref=...
- case_1 branch_env={'bg_1': 'branch_1'} labels=['assert_demo_homepage', 'click_trigger_endpoint_a', 'receive_webhook_endpoint_a', 'assert_endpoint_a_webhook']
- case_2 branch_env={'bg_1': 'branch_2'} labels=['assert_demo_homepage', 'click_store_local_file', 'local_file_is_written', 'assert_local_file_contents']

Summary: 1 journey planned, 2 cases planned, 0 failed
```

### Execute Only the File Branch

```bash
uv run --with playwright journey execute --file docs/simple_journey/simple_journey.py --step assert_local_file_contents
```

```console
Journey docs/simple_journey/simple_journey.py:simple_journey
journey_id=simple_journey function_ref=...
- case_2 start branches={bg_1=branch_2}
  step click_store_local_file attempt=1 ok duration=...
  step local_file_is_written attempt=1 ok duration=...
  step assert_local_file_contents attempt=1 ok duration=...
- case_2 ok steps=3 duration=... stopped_at=assert_local_file_contents replay_anchor=cp_1
Summary: 1 journey executed, 1 case executed, 0 failed
```

That targeted run is a good example of why Journey is useful during development. You can focus on the file branch without rerunning the webhook branch.

## Snapshot a Local Docker Compose App

Read these files together:

- `docs/docker_compose_journey/docker_compose_journey.py`
- `docs/docker_compose_journey/docker-compose.yml`

This example is intentionally branched. It shows one shared Docker setup, one hooked checkpoint, and two later
branches that both start from the same `after_boot` anchor.

The Docker helper is still just a normal Journey step factory:

```python
stack = step(
    run_docker(
        compose_file=_COMPOSE_FILE,
        project_name="journey-docker-docs",
    )
)
```

The shared setup still reads like plain Python:

```python
stack = step(
    run_docker(
        compose_file=_COMPOSE_FILE,
        project_name="journey-docker-docs",
    )
)
step(assert_stack_running, stack)
```

The checkpoint stays explicit about what gets stored and restored:

```python
after_boot = checkpoint(
    stack,
    store=store_docker,
    restore=restore_docker,
    snapshot_name="after_boot",
)
```

And the branch structure mirrors Journey's other checkpoint-started examples:

```python
summary = step(capture_stack_summary, stack)
if branch(start_from=after_boot):
    step(assert_running_branch, summary)
elif branch(start_from=after_boot):
    step(assert_boot_logs_branch, summary)
```

### Plan the Docker Journey

```bash
uv run journey plan --file docs/docker_compose_journey/docker_compose_journey.py
```

```console
Journey docs/docker_compose_journey/docker_compose_journey.py:docker_compose_journey
journey_id=docker_compose_journey function_ref=...
- case_1 branch_env={'bg_1': 'branch_1'} labels=['run_docker', 'assert_stack_running', 'capture_stack_summary', 'assert_running_branch']
- case_2 branch_env={'bg_1': 'branch_2'} labels=['run_docker', 'assert_stack_running', 'capture_stack_summary', 'assert_boot_logs_branch']

Summary: 1 journey planned, 2 cases planned, 0 failed
```

### Target the Second Docker Branch

```bash
uv run journey execute --file docs/docker_compose_journey/docker_compose_journey.py --step assert_boot_logs_branch
```

```console
Journey docs/docker_compose_journey/docker_compose_journey.py:docker_compose_journey
journey_id=docker_compose_journey function_ref=...
- case_2 start branches={bg_1=branch_2}
  step run_docker attempt=1 ok duration=...
  step assert_stack_running attempt=1 ok duration=...
  step capture_stack_summary attempt=1 ok duration=...
  step assert_boot_logs_branch attempt=1 ok duration=...
- case_2 ok steps=4 duration=... stopped_at=assert_boot_logs_branch replay_anchor=cp_1
Summary: 1 journey executed, 1 case executed, 0 failed
```

That `replay_anchor=cp_1` is the important bit. It shows that the branch is associated with the saved Docker
checkpoint. During a full two-case run, Journey restores the `after_boot` snapshot before it continues into the later
branch.

`store_docker(...)` and `restore_docker(...)` are strict on purpose. In v1 they aim for exact rollback of container
filesystems plus Docker-managed volume contents, so they reject bind mounts, external volumes, read-only mounts, and
multi-container services.

## Capture and Resume a Browser Session

Read `docs/playwright_resume_journey/playwright_resume_journey.py`.

The first helper logs in once and serializes the browser state:

```python
def login_and_capture_session() -> PlaywrightPageState:
    login_url = f"{ensure_demo_server()}/login"
    with open_page(PlaywrightPageState.from_url(login_url)) as page:
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url("**/dashboard")
        page.wait_for_function(
            "() => document.getElementById('auth-state').textContent === 'authenticated'"
        )
        session = capture_page_state(page)
    return session
```

The second helper reopens that saved state and keeps working from there:

```python
def continue_authenticated_dashboard(
    session: PlaywrightPageState,
    pause_seconds: float,
) -> dict[str, str]:
    with open_page(session) as page:
        auth_state = page.locator("#auth-state").text_content()
        ...
        time.sleep(pause_seconds)
        page.get_by_role("button", name="Complete protected action").click()
        ...
    return {
        "auth_state": auth_state,
        "status": status_text or "",
    }
```

The journey is still small:

```python
from journey import journey, step


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
uv run --with playwright journey execute --file docs/playwright_resume_journey/playwright_resume_journey.py --state /tmp/journey-playwright-resume-tutorial.state
```

Press `Ctrl-C` when the tutorial note tells you to.

Expected stdout:

```console
Journey docs/playwright_resume_journey/playwright_resume_journey.py:playwright_resume_journey
journey_id=playwright_resume_journey function_ref=...
- case_1 start branches={}
  step login_and_capture_session attempt=1 ok duration=...
  step continue_authenticated_dashboard attempt=1 start
  step continue_authenticated_dashboard attempt=1 interrupted duration=...
Interrupted.
```

Expected stderr:

```console
[tutorial] Signed in and captured PlaywrightPageState for http://127.0.0.1:.../dashboard. The next step can reopen this authenticated dashboard from saved state without logging in again.
[tutorial] continue_authenticated_dashboard() reopened the saved dashboard at http://127.0.0.1:.../dashboard. journey resumes at the step boundary, so this step restarts from the top on resume with the same saved PlaywrightPageState.
[tutorial] Press Ctrl-C during the next 2.0 seconds to interrupt after the authenticated browser state has already been saved.
```

### Second Run: Reopen the Same Saved Session

```bash
uv run --with playwright journey execute --file docs/playwright_resume_journey/playwright_resume_journey.py --state /tmp/journey-playwright-resume-tutorial.state
```

Expected stdout:

```console
Journey docs/playwright_resume_journey/playwright_resume_journey.py:playwright_resume_journey
journey_id=playwright_resume_journey function_ref=...
- case_1 resume branches={}
  step continue_authenticated_dashboard attempt=2 start
  step continue_authenticated_dashboard attempt=2 ok duration=...
  step assert_protected_action_complete attempt=1 ok duration=...
- case_1 ok steps=3 duration=...
Summary: 1 journey executed, 1 case executed, 0 failed
```

Expected stderr:

```console
[tutorial] The protected action completed. If this run resumed from saved state, continue_authenticated_dashboard() restarted with the same saved PlaywrightPageState instead of logging in again.
```

## What To Notice

- Browser logic stays inside normal Python functions. Journey does not wrap Playwright in a separate DSL.
- The same journey can branch into a webhook case and a local file case.
- `PlaywrightPageState` is just another step value. That is why Journey can save it, resume it, and pass it into later steps.
- Targeted execution is especially useful for UI work because you can rerun only the branch you are debugging.

Continue with [05 Journey Cloud Integrations](05-journey-cloud-integrations.md) when the external resource should be hosted by Journey Cloud instead of the local test process.
