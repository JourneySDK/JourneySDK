# 05 Journey Cloud Integrations

Some journeys need resources that should exist outside the local test process: a webhook URL the system under test can reach, or an inbox hosted by Journey Cloud.

That is where Journey Cloud integrations fit.

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

- first get a resource handle
- then trigger the system under test
- then wait until the external effect arrives
- finally assert on the payload

### Execute It Against Journey Cloud

```bash
uv run journey --file docs/cloud_webhook_journey/cloud_webhook_journey.py
```

```console
OK cli plan_start | Plan
OK cli plan_journey | Journey docs/cloud_webhook_journey/cloud_webhook_journey.py:cloud_webhook_journey ...
OK cli plan_metadata | journey_id=cloud_webhook_journey function_ref=... ...
OK cli plan_case | - case_1 branch_env={} labels=['get_webhook_invoice_paid', 'send_invoice_paid_webhook_later', 'receive_webhook_invoice_paid', 'assert_invoice_paid_webhook']
OK cli plan_summary | Summary: 1 journey planned, 1 case planned, 0 failed ...

OK cli execution_section | Execution
OK executor case_start | - case_1 start branches={}
OK executor step_success | step get_webhook_invoice_paid attempt=1 ok duration=...
OK executor step_success | step send_invoice_paid_webhook_later attempt=1 ok duration=...
OK executor step_start | step receive_webhook_invoice_paid attempt=1 start
OK executor step_success | step receive_webhook_invoice_paid attempt=1 ok duration=...
OK executor step_success | step assert_invoice_paid_webhook attempt=1 ok duration=...
OK executor case_complete | - case_1 ok steps=4 duration=...
OK cli execute_summary | Summary: 1 journey executed, 1 case executed, 0 failed ...
```

## Cloud-Hosted Email

Read `docs/cloud_email_journey/cloud_email_journey.py`.

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

- get a cloud-owned handle
- trigger the external side effect
- wait for the result
- assert on the received payload

### Execute It Against Journey Cloud

```bash
uv run journey --file docs/cloud_email_journey/cloud_email_journey.py
```

```console
OK cli plan_start | Plan
OK cli plan_journey | Journey docs/cloud_email_journey/cloud_email_journey.py:cloud_email_journey ...
OK cli plan_metadata | journey_id=cloud_email_journey function_ref=... ...
OK cli plan_case | - case_1 branch_env={} labels=['get_email_inbox', 'send_email', 'receive_email', 'assert_welcome_email'] ...
OK cli plan_summary | Summary: 1 journey planned, 1 case planned, 0 failed ...

OK cli execution_section | Execution
OK executor case_start | - case_1 start branches={}
OK executor step_success | step get_email_inbox attempt=1 ok duration=...
OK executor step_success | step send_email attempt=1 ok duration=...
OK executor step_start | step receive_email attempt=1 start
OK executor step_success | step receive_email attempt=1 ok duration=...
OK executor step_success | step assert_welcome_email attempt=1 ok duration=...
OK executor case_complete | - case_1 ok steps=4 duration=...
OK cli execute_summary | Summary: 1 journey executed, 1 case executed, 0 failed ...
```

## Ownership and Execution Semantics

- Journey Cloud authentication happens while the journey runs.
- The same API key is used consistently across cloud helpers in one run.
- The first API key that claims a cloud resource should own it from then on. Treat webhook paths, inboxes, and similar identifiers as cloud-managed resources, not anonymous shared names.
- The SDK side of the code stays small because Journey Cloud owns the hosted endpoint or inbox details.

Continue with [06 Debugging and Failure Modes](06-debugging-and-failure-modes.md) for the operational side of working with Journey runs.
