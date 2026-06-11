# Email Touchpoint Reference

Use the email touchpoint when a journey needs a hosted inbox, to send mail, or to wait for an expected email.

## Public API

- `get_email_inbox()`: returns the active Journey Cloud inbox handle during step execution.
- `send_email(...)`: sends an email through Journey Cloud during step execution.
- `wait_for_email(...)`: waits for a matching email during step execution.
- Email helpers require `JOURNEY_CLOUD_API_KEY` and usually `JOURNEY_CLOUD_BASE_URL`.

## Authoring Pattern

```python
from journeysdk import step
from journeysdk.touchpoints.email import get_email_inbox, send_email, wait_for_email

def send_welcome_email_and_verify_delivery():
    inbox = get_email_inbox()
    receipt = send_email(
        inbox,
        subject="Welcome",
        text_body="Hello from Journey",
    )
    message = wait_for_email(
        inbox,
        subject_contains="Welcome",
        timeout=10,
        poll_interval=1,
    )
    assert receipt["subject"] == "Welcome"
    assert message["subject"] == "Welcome"
    return True


step(send_welcome_email_and_verify_delivery, retry=6, retry_delay=2)
```

Call email helpers inside the step that triggers and verifies the email. Split inbox acquisition, send, wait, or
assertion into separate steps only when those values are independently useful as loop targets or replay anchors.
