# Webhook Touchpoint Reference

Use the webhook touchpoint when the app under test sends an HTTP callback that the journey should verify.

## Public API

- `get_webhook_endpoint(path=...)`: reserves a Journey Cloud webhook endpoint during step execution.
- `wait_for_webhook_request(endpoint, timeout=..., poll_interval=...)`: waits for one matching request during step
  execution.
- Webhook helpers require `JOURNEY_CLOUD_API_KEY` and usually `JOURNEY_CLOUD_BASE_URL`.

## Authoring Pattern

```python
from journeysdk import step
from journeysdk.touchpoints.webhook import get_webhook_endpoint, wait_for_webhook_request

def pay_invoice_and_verify_webhook():
    endpoint = get_webhook_endpoint(path="/invoice-paid")
    configure_app_webhook(endpoint.url)
    pay_invoice_through_the_ui()
    request_payload = wait_for_webhook_request(
        endpoint,
        timeout=10,
        poll_interval=1,
    )
    assert_invoice_paid_webhook(request_payload)
    return True


step(pay_invoice_and_verify_webhook, retry=6, retry_delay=2)
```

Reserve the endpoint inside the same step that configures the app, triggers the callback, waits for the payload, and
asserts it unless the endpoint handle is itself a useful replay anchor.
