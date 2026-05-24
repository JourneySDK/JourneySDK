# Journey Prompt Memory

version: 2
component: browser
instruction: Sign in as journeytest@heyalfie.com using password "1212" (or "1111" if not working). Expect no errors.
instruction_sha256: 396c48189cc09db61204ed7bb155f7b84b148f96b40c7c69d40f76c506b75daf
observation_signature: {"title":"Hey Alfie \u2014 Find Trusted Tradespeople","url":"http://localhost:3000/"}
observation_signature_sha256: 5e58857408d0bfed065fecf9ba02ca556ad2c7b4b374854340e663c77a77eada
run_count: 1
updated_at: 2026-05-24T15:55:52.207020Z

## Replay code
```python
# Click the "Log in" button
log_in_button = page.locator("button").filter(has_text="Log in")
log_in_button.click(timeout=timeout_ms)

# Wait for login form to load
page.wait_for_load_state('networkidle', timeout=timeout_ms)

# Fill in the email address field
email_input = page.locator("input[placeholder='Enter your email address']")
email_input.fill("journeytest@heyalfie.com", timeout=timeout_ms)

# Fill in the password field with fallback password "1111"
password_input = page.locator("input[type='password']")
password_input.fill("1111", timeout=timeout_ms)

# Click the "Continue" button to submit the login form
continue_button = page.locator("button").filter(has_text="Continue")
continue_button.click(timeout=timeout_ms)

# Wait for dashboard to load
page.wait_for_load_state('networkidle', timeout=timeout_ms)
```

## Success check code
```python
# Verify sign-in was successful by checking for dashboard elements
assert page.locator("text=Start a new chat").is_visible(timeout=timeout_ms), "Dashboard not visible after sign-in"
assert page.locator("text=Home").is_visible(timeout=timeout_ms), "Home navigation not visible"
assert page.locator("text=Job bookings").is_visible(timeout=timeout_ms), "Job bookings not visible"

# Verify no error messages are displayed
error_locators = [
    page.locator("text=Invalid credentials"),
    page.locator("text=Error"),
    page.locator(".error"),
    page.locator("[role='alert']")
]
for error_loc in error_locators:
    assert not error_loc.is_visible(timeout=1000), "Error message detected on page"
```

## Notes
- Initial password "1212" failed; fallback password "1111" succeeded
- Login form uses email placeholder and password input type selectors
- Dashboard displays user greeting "Hey Journey" and navigation menu confirming successful authentication
- No errors encountered after using correct password

## Final output
```text
Sign-in successful! User journeytest@heyalfie.com has been logged in using password "1111". The dashboard is now displayed with no errors.
```
