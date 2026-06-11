# 05 Journey Cloud Touchpoints

Some journeys need resources that should exist outside the local test process: a webhook URL the system under test can
reach, or an inbox hosted by Journey Cloud.

That is where Journey Cloud touchpoints fit. A touchpoint is a system, service, or channel that participates in the
tested journey; these hosted touchpoints let one Journey spec drive the app and verify side effects outside the
browser.

Use a cloud touchpoint when the service under test needs to communicate with something real enough to behave like an
external system, but disposable enough for tests. Instead of building a temporary webhook server or borrowing a real
mailbox, the journey asks Journey Cloud for a handle, passes that handle to the service under test, then waits for the
effect at a later step.

The small examples in this chapter are tutorial-sized so each touchpoint helper is visible. They are not the recommended
production shape for agent inner loops. In production journeys, prefer coarse user-flow steps: acquire/configure the
cloud touchpoint, drive the user action, wait for the side effect, and assert the payload inside one step when those
pieces recover together.

Journey Cloud touchpoints available in the SDK today:

- hosted webhook endpoints
- hosted email inboxes

The same model is intended to extend to future hosted resources, such as phone/SMS, payment cards, voice, and messaging,
but this chapter documents only the implemented email and webhook touchpoints.

Two rules matter before anything else:

- cloud resources are acquired while journey steps execute
- execution uses `JOURNEY_CLOUD_API_KEY` and `JOURNEY_CLOUD_BASE_URL`

Set those variables before you execute the cloud examples:

```bash
export JOURNEY_CLOUD_API_KEY=<your-api-key>
export JOURNEY_CLOUD_BASE_URL=https://<cloud-base-url>
```

## Cloud-Hosted Webhooks

Read `docs/cloud_webhook_journey/cloud_webhook_journey.py`.

This first example splits acquisition, triggering, waiting, and assertion into separate steps only to teach the helper
APIs.

```python
from journeysdk import journey, step


@journey
def cloud_webhook_journey() -> None:
    endpoint = step(get_webhook_endpoint(path="/invoice-paid"))
    step(send_invoice_paid_webhook_later, endpoint.url)
    request_payload = step(
        wait_for_webhook_request(
            path="/invoice-paid",
            timeout=0.05,
            poll_interval=0.01,
        ),
        endpoint,
        retry=3,
        retry_delay=0,
    )
    step(assert_invoice_paid_webhook, request_payload)
```

The shape is familiar:

- first get a touchpoint handle, such as a webhook endpoint
- then give the handle to the system under test or trigger work that uses it
- then wait until the external effect reaches that touchpoint
- finally assert on the payload the touchpoint observed

### Execute It Against Journey Cloud

```bash
uv run journey --file docs/cloud_webhook_journey/cloud_webhook_journey.py
```

```console
Plan
  docs/cloud_webhook_journey/cloud_webhook_journey.py:cloud_webhook_journey ...
    case_1  labels: get_webhook_invoice_paid, send_invoice_paid_webhook_later, receive_webhook_invoice_paid, assert_invoice_paid_webhook
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1
      get_webhook_invoice_paid  executed attempt=1 duration=...
      send_invoice_paid_webhook_later  executed attempt=1 duration=...
      receive_webhook_invoice_paid  start executed attempt=1
      receive_webhook_invoice_paid  executed attempt=1 duration=...
      assert_invoice_paid_webhook  executed attempt=1 duration=...
    case_1 done steps=4 duration=...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

### Coarse Production Shape

For an agent loop, avoid turning every touchpoint helper into its own replay boundary. If the webhook endpoint,
application configuration, browser action, webhook wait, and payload assertion recover together, keep them in one
branch-level verification step:

```python
from journeysdk import branch, journey, step
from journeysdk.touchpoints.browser import open_page
from journeysdk.touchpoints.webhook import get_webhook_endpoint, wait_for_webhook_request


def prepare_configured_billing_app():
    endpoint = get_webhook_endpoint(path="/invoice-paid")()
    app = start_billing_app(webhook_url=endpoint.url)
    return ConfiguredBillingApp(app=app, webhook_endpoint=endpoint)


def pay_invoice_and_verify_webhook(context):
    page = open_page(context.app_url)
    page.locator("[data-testid='pay-invoice']").click()
    page.wait_for_url("**/paid")

    request_payload = wait_for_webhook_request(
        path="/invoice-paid",
        timeout=30,
        poll_interval=0.5,
    )(context.webhook_endpoint)
    assert_invoice_paid_webhook(request_payload)


@journey
def billing_journey() -> None:
    context = step(prepare_configured_billing_app)

    if branch(start_from=context):
        step(pay_invoice_and_verify_webhook, context)
```

`ConfiguredBillingApp` should be serializable or implement Journey rehydration if it crosses the branch replay
boundary. The important boundary decision is that `wait_for_webhook_request(...)` and
`assert_invoice_paid_webhook(...)` are helpers inside the verification step, not standalone steps, unless an agent would
target or branch from them independently.

## Cloud-Hosted Email

Read `docs/cloud_email_journey/cloud_email_journey.py`.

As with the webhook tutorial, this example exposes each helper as a separate step for readability. In a production
agent loop, `wait_for_email(...)` and the email assertions can live inside the same coarse step that triggers the email
when the intermediate message is not independently targetable.

```python
from journeysdk import journey, step


@journey
def cloud_email_journey() -> None:
    inbox = step(get_email_inbox())
    receipt = step(
        send_email(
            subject="Welcome to Journey",
            text_body="Hello from Journey Cloud",
        )
    )
    message = step(
        wait_for_email(
            subject_contains="Welcome",
            timeout=0.05,
            poll_interval=0.01,
        ),
        inbox,
    )
    step(assert_welcome_email, inbox, receipt, message)
```

This flow uses the same pattern as the webhook example:

- get a cloud-owned touchpoint handle
- trigger the external side effect
- wait for the result at that touchpoint
- assert on the received payload

### Execute It Against Journey Cloud

```bash
uv run journey --file docs/cloud_email_journey/cloud_email_journey.py
```

```console
Plan
  docs/cloud_email_journey/cloud_email_journey.py:cloud_email_journey ...
    case_1  labels: get_email_inbox, send_email, receive_email, assert_welcome_email
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1
      get_email_inbox  executed attempt=1 duration=...
      send_email  executed attempt=1 duration=...
      receive_email  start executed attempt=1
      receive_email  executed attempt=1 duration=...
      assert_welcome_email  executed attempt=1 duration=...
    case_1 done steps=4 duration=...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

## Ownership and Execution Semantics

- Journey Cloud authentication happens while the journey runs.
- The same API key is used consistently across cloud touchpoints in one run.
- The first API key that claims a cloud resource should own it from then on. Treat webhook paths, inboxes, and similar identifiers as cloud-managed resources, not anonymous shared names.
- The SDK side of the code stays small because Journey Cloud owns the hosted endpoint or inbox details.

Continue with [06 Debugging and Failure Modes](06-debugging-and-failure-modes.md) for the operational side of working with Journey runs.
