# Journey Prompt Memory

version: 2
component: browser
instruction: Sign in as journeytest@heyalfie.com using password "1212" (or "1111" if not working). Expect no errors.
instruction_sha256: 396c48189cc09db61204ed7bb155f7b84b148f96b40c7c69d40f76c506b75daf
observation_signature: {"title":"Hey Alfie \u2014 Find Trusted Tradespeople","url":"http://localhost:3000/"}
observation_signature_sha256: 5e58857408d0bfed065fecf9ba02ca556ad2c7b4b374854340e663c77a77eada
run_count: 3
updated_at: 2026-05-25T08:43:37.619242Z

## Replay code
```python
# Wait for the page to fully load
page.wait_for_load_state('networkidle', timeout=timeout_ms)

# Click on the "Log in" button
page.locator('button:has-text("Log in")').click(timeout=timeout_ms)
page.wait_for_load_state('networkidle', timeout=timeout_ms)

# Fill in the email field
page.locator('input#identifier-field').fill('journeytest@heyalfie.com', timeout=timeout_ms)

# Fill in the password field with fallback password
page.locator('input#password-field').fill('1111', timeout=timeout_ms)

# Click the Continue button
page.locator('button.cl-formButtonPrimary').click(timeout=timeout_ms)

# Wait for the page to load after sign-in
page.wait_for_load_state('networkidle', timeout=timeout_ms)
```

## Success check code
```python
# Verify user is authenticated and on dashboard
assert 'Hey Alfie' in page.title(), f"Title check failed: {page.title()}"
assert 'localhost:3000' in page.url, f"URL check failed: {page.url}"

# Verify dashboard content is visible
page.wait_for_selector('text=Start a new chat', timeout=timeout_ms)
page.wait_for_selector('text=Home', timeout=timeout_ms)
page.wait_for_selector('text=Explore all services', timeout=timeout_ms)
page.wait_for_selector('text=RECENT CHATS', timeout=timeout_ms)

# Verify no error messages are displayed
error_selectors = ['text=error', 'text=Error', 'text=failed', 'text=Failed']
for selector in error_selectors:
    count = page.locator(selector).count()
    assert count == 0, f"Error message found: {selector}"
```

## Notes
- Primary password "1212" failed; fallback password "1111" succeeded
- Use `input#identifier-field` for email and `input#password-field` for password
- Use `button.cl-formButtonPrimary` for the Continue/submit button
- Dashboard loads with "Hey Journey," greeting, navigation menu, RECENT CHATS section, and service cards
- All success criteria verified: authenticated user, dashboard elements present, no errors

## Final output
```text
Sign-in successful. User authenticated as journeytest@heyalfie.com. Dashboard loaded with no errors. Page displays 'Hey Journey,' greeting and all expected dashboard elements including navigation menu, recent chats section, service cards, and to-do list.
```
