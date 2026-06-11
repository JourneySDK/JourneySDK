# 05 Journey Cloud Touchpoints

Journey Cloud touchpoints let a replayable step verify side effects that leave the browser or local process: hosted
webhook callbacks and hosted email inboxes.

Use them when the service under test needs to talk to something real enough to behave like an external system, but
disposable enough for tests. The step asks Journey Cloud for a handle, passes that handle to the app, triggers the user
action, waits for the side effect, and asserts the payload.

Implemented Journey Cloud touchpoints:

- hosted webhook endpoints
- hosted email inboxes

Future hosted resources such as phone/SMS, payment cards, voice, and messaging are roadmap resources unless the
project already has its own concrete helper.

Set cloud configuration before executing these examples:

```bash
export JOURNEY_CLOUD_API_KEY=<your-api-key>
export JOURNEY_CLOUD_BASE_URL=https://<cloud-base-url>
```

## Cloud-Hosted Webhooks

Read `docs/cloud_webhook_journey/cloud_webhook_journey.py`.

The tutorial keeps the whole integration in one replayable step:

```python
from journeysdk import journey, step
from journeysdk.touchpoints.webhook import get_webhook_endpoint, wait_for_webhook_request


def send_invoice_payment_and_verify_webhook() -> bool:
    endpoint = get_webhook_endpoint(path="/invoice-paid")
    send_invoice_paid_webhook_later(endpoint.url, delay=0.01)
    request_payload = wait_for_webhook_request(
        endpoint,
        timeout=0.5,
        poll_interval=0.01,
    )
    return assert_invoice_paid_webhook(request_payload)


@journey
def cloud_webhook_journey() -> None:
    step(send_invoice_payment_and_verify_webhook, retry=3, retry_delay=0)
```

This is the agent-loop shape: one step owns endpoint acquisition, triggering, waiting, and assertion because those
pieces recover together.

Execute it against Journey Cloud:

```bash
uv run journey verify --file docs/cloud_webhook_journey/cloud_webhook_journey.py
```

```console
Plan
  docs/cloud_webhook_journey/cloud_webhook_journey.py:cloud_webhook_journey ...
    case_1  labels: send_invoice_payment_and_verify_webhook
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1
      send_invoice_payment_and_verify_webhook  executed attempt=1 duration=...
    case_1 done steps=1 duration=...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

In a production billing journey, the same pattern usually lives after app setup:

```python
from journeysdk import branch, journey, step
from journeysdk.touchpoints.browser import open_page
from journeysdk.touchpoints.webhook import get_webhook_endpoint, wait_for_webhook_request


def prepare_configured_billing_app():
    endpoint = get_webhook_endpoint(path="/invoice-paid")
    app = start_billing_app(webhook_url=endpoint.url)
    return ConfiguredBillingApp(app=app, webhook_endpoint=endpoint)


def pay_invoice_and_verify_webhook(context):
    page = open_page(context.app_url)
    page.locator("[data-testid='pay-invoice']").click()
    page.wait_for_url("**/paid")

    request_payload = wait_for_webhook_request(
        context.webhook_endpoint,
        timeout=30,
        poll_interval=0.5,
    )
    assert_invoice_paid_webhook(request_payload)
    return True


@journey
def billing_journey() -> None:
    context = step(prepare_configured_billing_app)

    if branch(replay_from=context):
        step(pay_invoice_and_verify_webhook, context)
```

`ConfiguredBillingApp` should be serializable or implement Journey rehydration if it crosses the branch replay
boundary.

## Cloud-Hosted Email

Read `docs/cloud_email_journey/cloud_email_journey.py`.

The email tutorial also keeps the trigger, wait, and assertion inside one step:

```python
from journeysdk import journey, step
from journeysdk.touchpoints.email import get_email_inbox, send_email, wait_for_email


def send_welcome_email_and_verify_delivery() -> bool:
    inbox = get_email_inbox()
    receipt = send_email(
        inbox,
        subject="Welcome to Journey",
        text_body="Hello from Journey Cloud",
    )
    message = wait_for_email(
        inbox,
        subject_contains="Welcome",
        timeout=60,
        poll_interval=0.5,
    )
    return assert_welcome_email(inbox, receipt, message)


@journey
def cloud_email_journey() -> None:
    step(send_welcome_email_and_verify_delivery)
```

Execute it against Journey Cloud:

```bash
uv run journey verify --file docs/cloud_email_journey/cloud_email_journey.py
```

```console
Plan
  docs/cloud_email_journey/cloud_email_journey.py:cloud_email_journey ...
    case_1  labels: send_welcome_email_and_verify_delivery
  Summary: 1 journey planned, 1 case planned, 0 failed

Execution
    case_1
      send_welcome_email_and_verify_delivery  executed attempt=1 duration=...
    case_1 done steps=1 duration=...
  Summary: 1 journey executed, 1 case executed, 0 failed
```

Split inbox acquisition, send, wait, or assertion into separate steps only when those values are independently useful
as loop targets, retry points, branch replay anchors, or durable inputs to later steps.

## Ownership And Execution Semantics

- Journey Cloud authentication happens while the journey runs.
- The same API key is used consistently across cloud touchpoints in one run.
- The first API key that claims a cloud resource should own it from then on.
- Treat webhook paths, inboxes, and similar identifiers as cloud-managed resources, not anonymous shared names.
- The SDK helper surface stays small because Journey Cloud owns the hosted endpoint or inbox details.

Continue with [06 Debugging and Failure Modes](06-debugging-and-failure-modes.md) for the operational side of working with Journey runs.
