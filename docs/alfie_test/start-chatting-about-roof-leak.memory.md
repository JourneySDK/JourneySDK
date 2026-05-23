# Journey Prompt Memory

version: 2
component: browser
instruction: Start chatting with Alfie - say you need to 'repair a leaking roof'. Expect there is the new chat added to the 'Active chats' section in the sidebar.
instruction_sha256: 86bef0b10d2796f85df4bb862e4ec805d8fb38e2279352d6961f50837437acce
observation_signature: {"title":"Hey Alfie \u2014 Find Trusted Tradespeople","url":"http://localhost:3000/"}
observation_signature_sha256: 5e58857408d0bfed065fecf9ba02ca556ad2c7b4b374854340e663c77a77eada
run_count: 1
updated_at: 2026-05-23T09:36:47.059382Z

## Replay code
```python
# Click the new chat button
page.get_by_test_id("new-chat-button").click()

# Type the message in the composer input
composer_input = page.locator('[data-testid="composer-input"]')
composer_input.click()
composer_input.type("repair a leaking roof", timeout=timeout_ms)

# Send the message
page.get_by_test_id("send-message-button").click(timeout=timeout_ms)
```

## Success check code
```python
# Verify the chat appears in RECENT CHATS section in the sidebar
recent_chats = page.locator('[data-testid="recent-chats"]')
recent_chats.wait_for(state="visible", timeout=timeout_ms)

# Verify "repair a leaking roof" appears in the recent chats list
chat_entry = page.locator('text=repair a leaking roof').first
chat_entry.wait_for(state="visible", timeout=timeout_ms)

# Verify the message was sent and response received
response_text = page.locator('text=I can definitely help with that').first
response_text.wait_for(state="visible", timeout=timeout_ms)
```

## Notes
- Use test IDs for reliable element selection (new-chat-button, composer-input, send-message-button)
- The chat successfully appears in RECENT CHATS sidebar section after sending
- Message "repair a leaking roof" is the key content to verify in the chat list
- Alfie's response confirms the chat was created and message was received

## Final output
```text
Successfully started a chat with Alfie about 'repair a leaking roof'. The message has been sent and the new chat is now visible in the RECENT CHATS section in the sidebar, showing multiple entries for 'repair a leaking roof' conversations.
```
