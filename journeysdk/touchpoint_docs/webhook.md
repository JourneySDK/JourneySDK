# Webhook Touchpoint Reference

Use the webhook touchpoint when the app under test sends an HTTP callback that the journey should verify.

## Public API

- `get_webhook_endpoint(path=...)`: step callable that reserves a Journey Cloud webhook endpoint.
- `wait_for_webhook_request(path=..., timeout=..., poll_interval=...)`: step callable that waits for one matching request.
- Webhook helpers require `JOURNEY_CLOUD_API_KEY` and usually `JOURNEY_CLOUD_BASE_URL`.

## Authoring Pattern

```python
from journeysdk import step
from journeysdk.touchpoints.webhook import get_webhook_endpoint, wait_for_webhook_request

endpoint = step(get_webhook_endpoint(path="/invoice-paid"))
step(configure_app_webhook, endpoint.url)
request_payload = step(
    wait_for_webhook_request(path="/invoice-paid", timeout=10, poll_interval=1),
    endpoint,
    retry=6,
    retry_delay=2,
)
step(assert_invoice_paid_webhook, request_payload)
```

Reserve the endpoint before configuring the app. Put retry on the wait step, not on many tiny assertion steps.
