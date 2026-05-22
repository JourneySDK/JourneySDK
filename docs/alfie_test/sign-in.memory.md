# Journey Prompt Memory

version: 2
component: browser
instruction: Sign in as e2etest@heyalfie.com using password "1212" (or "1111" if not working). Expect no errors.
instruction_sha256: dd79da60adb655c1d377b005e6be794c59d6af376e0a8976647a5c8878cb65a7
observation_signature: {"title":"Hey Alfie \u2014 Find Trusted Tradespeople","url":"http://localhost:3000/"}
observation_signature_sha256: 5e58857408d0bfed065fecf9ba02ca556ad2c7b4b374854340e663c77a77eada
run_count: 3
updated_at: 2026-05-22T21:48:05.648272Z

## Replay code
```python
# Navigate to sign-in page
page.goto("http://localhost:3000/sign-in", timeout=timeout_ms)
page.wait_for_load_state("networkidle", timeout=timeout_ms)

# Enter email
email_field = page.locator("input[name='identifier']")
email_field.fill("e2etest@heyalfie.com", timeout=timeout_ms)
page.locator("button.cl-formButtonPrimary").click(timeout=timeout_ms)
page.wait_for_load_state("networkidle", timeout=timeout_ms)

# Enter password
password_field = page.locator("input[name='password']")
password_field.fill("1111", timeout=timeout_ms)
page.wait_for_timeout(500)

# Click Continue
page.locator("button.cl-formButtonPrimary").click(timeout=timeout_ms)
page.wait_for_load_state("networkidle", timeout=timeout_ms)
page.wait_for_timeout(4000)
```

## Success check code
```python
# Assert greeting is visible
page.locator("text=Hey Test,").wait_for(state="visible", timeout=timeout_ms)

# Assert no error messages
assert not page.get_by_test_id("form-feedback-error").is_visible(), "Error message visible after sign-in"

# Assert recent chats are present in sidebar
page.locator("text=repair a leaking roof").first.wait_for(state="visible", timeout=timeout_ms)
page.locator("text=fix a toilet").wait_for(state="visible", timeout=timeout_ms)
```

## Notes
- Password "1212" is incorrect; use "1111".
- The sign-in flow is two-step: first submit email, then submit password, both using `button.cl-formButtonPrimary`.
- After successful sign-in the page stays at `http://localhost:3000/` and shows "Hey Test," greeting with recent chats in the sidebar.
- If already on the home page and signed in, the greeting check alone is sufficient.

## Final output
```text
Sign-in successful. The authenticated home screen displays the 'Hey Test,' greeting, recent chats (repair a leaking roof, fix a toilet, repair a leaking roof, repair a leaking roof) in the sidebar, and no error messages. Sign-in completed using password '1111'.
```
