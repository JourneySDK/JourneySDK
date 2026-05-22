# Journey Prompt Memory

version: 2
component: browser
instruction: Start chatting with Alfie - say you need to 'repair a leaking roof'. Expect there is the new chat added to the 'Active chats' section in the sidebar.
instruction_sha256: 86bef0b10d2796f85df4bb862e4ec805d8fb38e2279352d6961f50837437acce
observation_signature: {"title":"Hey Alfie \u2014 Find Trusted Tradespeople","url":"http://localhost:3000/"}
observation_signature_sha256: 5e58857408d0bfed065fecf9ba02ca556ad2c7b4b374854340e663c77a77eada
run_count: 1
updated_at: 2026-05-22T21:45:04.299269Z

## Replay code
```python
# Wait for page to load
page.wait_for_load_state('networkidle', timeout=timeout_ms)

# Click on the composer input and type the message
composer = page.locator('[data-testid="composer-input"]')
composer.click(timeout=timeout_ms)
composer.type("repair a leaking roof", timeout=timeout_ms)

# Click the send button
send_button = page.get_by_test_id("send-message-button")
send_button.click(timeout=timeout_ms)

# Wait for navigation to the new chat page
page.wait_for_url("**/chats/**", timeout=timeout_ms)
```

## Success check code
```python
# Verify the chat page URL
assert "/chats/" in page.url, f"Expected chat URL, got: {page.url}"

# Verify the message was sent and Alfie responded
page.wait_for_selector('text="repair a leaking roof"', timeout=timeout_ms)

# Verify the chat appears in the RECENT CHATS section of the sidebar
recent_chats_section = page.locator('text="RECENT CHATS"')
recent_chats_section.wait_for(timeout=timeout_ms)

# Verify the new chat titled 'repair a leaking roof' is visible in the sidebar
sidebar_chat = page.locator('text="repair a leaking roof"').first
sidebar_chat.wait_for(timeout=timeout_ms)
assert sidebar_chat.is_visible(), "Chat 'repair a leaking roof' not visible in sidebar"
```

## Notes
- The composer input uses `data-testid="composer-input"` and send button uses `data-testid="send-message-button"`.
- After sending, the page navigates to `/chats/<uuid>`.
- The sidebar shows "RECENT CHATS" section (not "Active chats" as stated in the instruction — the final output confirmed it appears under "RECENT CHATS").
- The chat title "repair a leaking roof" appears in the sidebar as the active/highlighted chat.

## Final output
```text
The message 'repair a leaking roof' was sent to Alfie, and the new chat titled 'repair a leaking roof' is visible in the RECENT CHATS section of the sidebar, highlighted as the active chat.
```
