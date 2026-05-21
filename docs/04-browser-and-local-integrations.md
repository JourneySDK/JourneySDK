# 04 Browser and Local Touchpoints

Journey does not care whether a step talks to a browser, a file on disk, a Docker Compose app, or a hosted webhook
touchpoint. If it is ordinary Python, it can live inside the same authored journey.

This is the practical meaning of touchpoints: the journey can cross from one system to another without changing shape.
One step may drive a browser page, another may wait for a webhook, and another may inspect local state. Journey keeps
explicit replay boundaries durable while your Python chooses which touchpoint to use.

This chapter shows both sides of that idea:

- one journey that mixes browser work, a Journey Cloud webhook, and a downloaded file
- one journey that snapshots a local Docker Compose app behind a step anchor
- one journey that captures a browser session so a later run can reopen it

## One Journey Can Mix Browser, Webhook, and Local File Work

Read these files together:

- `docs/simple_journey/simple_journey.py`
- `docs/simple_journey/demo_site.html`

The browser helper is still just a normal Python function:

```python
from journeysdk.touchpoints.browser import open_page


def assert_demo_homepage() -> bool:
    page = open_page(_DEMO_PAGE_URL)
    title = page.title()
    buttons = page.get_by_role("button")
    ...
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
from journeysdk.touchpoints.webhook import get_webhook_endpoint, wait_for_webhook_request


@journey
def simple_journey() -> None:
    after_setup = step(assert_demo_homepage)

    if branch(start_from=after_setup):
        endpoint = step(get_webhook_endpoint(path="/endpoint-a"))
        step(click_trigger_endpoint_a, endpoint.url)
        request_payload = step(wait_for_webhook_request(path="/endpoint-a"), endpoint)
        step(assert_endpoint_a_webhook, request_payload)
    elif branch(start_from=after_setup):
        step(click_store_local_file)
        file_info = step(local_file_is_written)
        step(assert_local_file_contents, file_info)
```

`open_page()` launches the browser, Journey closes it at the step-exit boundary, and Chromium is downloaded
automatically the first time it is needed in the current environment. That first browser launch needs network access
and can take a moment.

By default, every browser context opened by `open_page()` records a Playwright trace and video for debugging. Journey
stores those files under `.journey/recordings/` next to the journey source file, using flat filenames that begin with
the execution sequence number, case, step, attempt, context, and run id. Open a trace with
`playwright show-trace .journey/recordings/<recording>.trace.zip` to inspect the action timeline and DOM snapshots.
Use `--no-browser-recording` when a run should avoid writing browser traces and videos, for example when page content is
sensitive.

### Execute Only the File Branch

```bash
uv run journey --file docs/simple_journey/simple_journey.py --step assert_local_file_contents
```

```console
Plan
  docs/simple_journey/simple_journey.py:simple_journey ...
    case_1  labels: assert_demo_homepage, get_webhook_endpoint_a, click_trigger_endpoint_a, receive_webhook_endpoint_a, assert_endpoint_a_webhook; branches: {bg_1=branch_1}
    case_2  labels: assert_demo_homepage, click_store_local_file, local_file_is_written, assert_local_file_contents; branches: {bg_1=branch_2}
  Summary: 1 journey planned, 2 cases planned, 0 failed

Execution
    case_2  branches={bg_1=branch_2}
      assert_demo_homepage  ok attempt=1 duration=...
      click_store_local_file  ok attempt=1 duration=...
      local_file_is_written  ok attempt=1 duration=...
      assert_local_file_contents  ok attempt=1 duration=...
    case_2 done steps=4 duration=... stopped_at=assert_local_file_contents replay_anchor=assert_demo_homepage
  Summary: 1 journey executed, 1 case executed, 0 failed
```

That targeted run is a good example of why Journey is useful during development. You can focus on the file branch
without acquiring a webhook endpoint, while still executing the selected case from its first step boundary.

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
implements Journey's rehydration protocol. Docker snapshots are created only when the stack is in the replay closure
for an explicit boundary, such as the branch anchor below:

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
  docs/docker_compose_journey/docker_compose_journey.py:docker_compose_journey ...
    case_1  labels: run_docker, assert_stack_ready, capture_baseline_state, increment_counter, assert_increment_branch; branches: {bg_1=branch_1}
    case_2  labels: run_docker, assert_stack_ready, capture_baseline_state, read_counter_state, assert_restored_counter_branch; branches: {bg_1=branch_2}
  Summary: 1 journey planned, 2 cases planned, 0 failed

Execution
    case_1  branches={bg_1=branch_1}
      run_docker  ok attempt=1 duration=...
      assert_stack_ready  ok attempt=1 duration=...
      capture_baseline_state  ok attempt=1 duration=...
      increment_counter  ok attempt=1 duration=...
      assert_increment_branch  ok attempt=1 duration=...
    case_1 done steps=5 duration=...
    case_2  branches={bg_1=branch_2}
      read_counter_state  ok attempt=1 duration=...
      assert_restored_counter_branch  ok attempt=1 duration=...
    case_2 done steps=5 duration=...
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

Docker snapshotting is strict about the state it owns. It rolls back
Docker-managed volume contents only, so services should keep durable,
replay-relevant state in managed volumes and tolerate container restarts. Bind
mounts are allowed as external host state and are not copied or restored;
external volumes, read-only volume mounts, unsupported mount types, and
multi-container services are rejected. Keep Docker-backed journey steps procedure-sized: tiny do/assert fragments add
more boundaries, and only explicit `branch(start_from=...)` or positive `retry=...` boundaries should need Docker
snapshot storage.

## Capture and Resume a Browser Session

Read `docs/browser_resume_journey/browser_resume_journey.py`.

The first helper logs in once and serializes the browser state:

```python
def login_and_capture_session() -> JourneyBrowserPage:
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
    session: JourneyBrowserPage,
    pause_seconds: float,
) -> dict[str, str]:
    page = open_page(session)
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

### Reset the Demo

```bash
uv run python -c "from docs.browser_resume_journey import reset_demo_state; reset_demo_state()"
```

### First Run: Interrupt After the Session Is Saved

```bash
uv run journey --file docs/browser_resume_journey/browser_resume_journey.py
```

Press `Ctrl-C` once when the tutorial note tells you to. The command stops after the active step completes; press it a
second time only if you want to force an immediate dirty-step interruption.

Expected pretty stdout:

```console
Plan
  docs/browser_resume_journey/browser_resume_journey.py:browser_resume_journey ...
    case_1  labels: login_and_capture_session, continue_authenticated_dashboard, assert_protected_action_complete
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1
      login_and_capture_session  ok attempt=1 duration=...
      continue_authenticated_dashboard  start attempt=1
Ctrl-C received. Finishing the active step so Journey can save progress. Press Ctrl-C again to stop now.
      continue_authenticated_dashboard  ok attempt=1 duration=...
Interrupted: Journey execution was interrupted before it finished.
Hint: Run the same command again to resume from saved progress.
```

Additional pretty stdout:

```console
Signed in and returned JourneyBrowserPage for http://127.0.0.1:.../dashboard. ...
continue_authenticated_dashboard() reopened the saved dashboard at http://127.0.0.1:.../dashboard. ...
Press Ctrl-C once during the next 2.0 seconds to stop gracefully after this step reaches post-exit. ...
```

### Second Run: Reopen the Same Saved Session

```bash
uv run journey --file docs/browser_resume_journey/browser_resume_journey.py
```

Expected pretty stdout:

```console
Plan
  docs/browser_resume_journey/browser_resume_journey.py:browser_resume_journey ...
    case_1  labels: login_and_capture_session, continue_authenticated_dashboard, assert_protected_action_complete
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1 resume
      continue_authenticated_dashboard  start attempt=2
      continue_authenticated_dashboard  ok attempt=2 duration=...
      assert_protected_action_complete  ok attempt=1 duration=...
    case_1 done steps=3 duration=...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

Additional pretty stdout:

```console
The protected action completed. Positive retry made continue_authenticated_dashboard() an explicit replay boundary, so
interrupted runs restart that procedure with the same saved JourneyBrowserPage instead of logging in again.
```

If you press Ctrl-C a second time, Journey stops the dirty browser step now. Browser cleanup is best-effort and the
step restarts on the next run with the saved `JourneyBrowserPage` input:

```console
      continue_authenticated_dashboard  start attempt=1
Ctrl-C received. Finishing the active step so Journey can save progress. Press Ctrl-C again to stop now.
Ctrl-C received again. Stopping now; this step will restart from the nearest replay boundary on resume.
Warning: continue_authenticated_dashboard interrupted after ... (KeyboardInterrupt)
Interrupted: Journey execution was interrupted before it finished.
Hint: Run the same command again to resume from saved progress.
```

## Prompt a Live Page with an LLM

Read `docs/browser_prompt_journey/browser_prompt_journey.py`.

  SDK already includes Playwright and LangChain. Set your provider credentials with the normal provider ...
environment variables such as `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`. Browser prompts default to
`anthropic:claude-sonnet-4-6`; override that with LangChain's `provider:model` syntax such as
`model="anthropic:claude-sonnet-4-5"`, or set `JOURNEY_BROWSER_PROMPT_MODEL`.

The helper can stay small. Without `output=...`, `page.prompt(...)` returns a plain string. With `output=...`, Journey
uses the model provider's structured-output feature and returns a dictionary with those fields:

`JourneyBrowserPage` extends Playwright's sync `Page`, so ordinary Playwright page methods and locators work on the
returned object. See the [Playwright Page API](https://playwright.dev/python/docs/api/class-page) for reference.

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

The next step can assert against the structured output:

```python
def assert_prompt_result(result: dict[str, object]) -> bool:
    assert result["popup_title"]
    return True
```

Expectation wording in the instruction, such as `Expect ...`, is treated as required success criteria. If the requested
browser task cannot complete or the current page does not satisfy those criteria, `page.prompt(...)` raises
`RuntimeError` instead of returning successful prompt output.

By default, each `page.prompt(...)` inside a journey step gets a generated callsite memory file. The
`memory="sign-in-popup"` argument gives this prompt an explicit memory file instead. After a successful run, Journey
writes `docs/browser_prompt_journey/.journey/sign-in-popup.memory.md`. Later runs with the same prompt memory replay the
successful fast path first, and fall back to the model with the remembered path as context if replay no longer matches
the page. Prompt memory stores compact code and checks only; it does not store screenshots, rendered HTML, or full model
prompts.

Explicit memory names must be literal strings, and explicit or generated names must be unique within one compiled
journey. That keeps planning deterministic and makes memory files easy to review in version control. Pass `memory=None`
to disable memory for one prompt. Use `--no-memory` when you want to run without reading or updating prompt memory, or
`--no-memory-update` when existing memory should still be read but not rewritten.

## What To Notice

- Browser logic stays inside normal Python functions. Journey does not wrap browser automation in a separate DSL.
- The same journey can branch into a webhook case and a local file case.
- `JourneyBrowserPage` is just another step value. Returning it lets Journey save it only when an explicit replay
  boundary needs to close, rehydrate, and pass it into later steps.
- `JourneyBrowserPage.prompt(...)` works on a live page handle and returns either plain text or explicit structured
  output instead of mutating the saved-page semantics of the original handle.
- Steps that open a page but return other data should close the page explicitly, as shown in `continue_authenticated_dashboard()`.
- Targeted execution is especially useful for UI work because you can rerun only the branch you are debugging.

Continue with [05 Journey Cloud Integrations](05-journey-cloud-integrations.md) for focused webhook and email examples hosted by Journey Cloud.
