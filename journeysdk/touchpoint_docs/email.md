# Email Touchpoint Reference

Use the email touchpoint when a journey needs a hosted inbox, to send mail, or to wait for an expected email.

## Public API

- `get_email_inbox()`: step callable that returns the active Journey Cloud inbox handle.
- `send_email(...)`: step callable that sends an email through Journey Cloud.
- `wait_for_email(...)`: step callable that waits for a matching email.
- Email helpers require `JOURNEY_CLOUD_API_KEY` and usually `JOURNEY_CLOUD_BASE_URL`.

## Authoring Pattern

```python
from journeysdk import step
from journeysdk.touchpoints.email import get_email_inbox, send_email, wait_for_email

inbox = step(get_email_inbox())
step(send_email(subject="Welcome", text_body="Hello from Journey"))
message = step(
    wait_for_email(subject_contains="Welcome", timeout=10, poll_interval=1),
    inbox,
    retry=6,
    retry_delay=2,
)
```

Keep email acquisition and waits in steps. Put retry on the async boundary that waits for the message. Pass the inbox
handle through step results instead of reconstructing it with globals.
