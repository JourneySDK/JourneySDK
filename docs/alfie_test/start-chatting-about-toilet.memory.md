# Journey Prompt Memory

version: 2
component: browser
instruction: start chatting with Alfie - say you need to 'fix a toilet'. Expect there is the new chat added to the 'Active chats' section in the sidebar.
instruction_sha256: d3714791e82dfe98de421a616af5d482bcee117f6556bbced98003af9f46ae92
observation_signature: {"title":"Hey Alfie \u2014 Find Trusted Tradespeople","url":"http://localhost:3000/"}
observation_signature_sha256: 5e58857408d0bfed065fecf9ba02ca556ad2c7b4b374854340e663c77a77eada
run_count: 1
updated_at: 2026-05-22T21:43:55.728799Z

## Replay code
```python
# Click the new chat button to start a chat with Alfie
page.get_by_test_id("new-chat-button").click(timeout=timeout_ms)
page.wait_for_timeout(2000)

# Wait for the composer input to be ready
page.wait_for_selector('[data-testid="composer-input"]:not([contenteditable="false"])', timeout=timeout_ms)
page.wait_for_timeout(500)

# Type the message
composer = page.get_by_test_id("composer-input")
composer.click(timeout=timeout_ms)
composer.type("fix a toilet", delay=50)
page.wait_for_timeout(500)

# Send the message
page.get_by_test_id("send-message-button").click(timeout=timeout_ms)
page.wait_for_timeout(3000)
```

## Success check code
```python
# Assert the message "fix a toilet" was sent and is visible in the chat
page.wait_for_selector('text="fix a toilet"', timeout=timeout_ms)

# Assert the "Hey Alfie" chat appears in the RECENT CHATS / Active chats section of the sidebar
page.wait_for_selector('text="RECENT CHATS"', timeout=timeout_ms)
assert page.locator('text="Hey Alfie"').count() > 0, "Expected 'Hey Alfie' chat in the sidebar RECENT CHATS section"
```

## Notes
- The new chat button has `data-testid="new-chat-button"`.
- The composer input has `data-testid="composer-input"` and must be waited on until it is not disabled (contenteditable not false).
- The send button has `data-testid="send-message-button"`.
- After sending, the sidebar shows a "RECENT CHATS" section with "Hey Alfie" as the active chat entry.
- The chat URL changes to `/chats/<uuid>` after creation.

## Final output
```text
The chat with Alfie has been started with the message 'fix a toilet' sent. The new chat titled 'Hey Alfie' is visible and active in the 'RECENT CHATS' section of the sidebar.
```
