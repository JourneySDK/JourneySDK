# Journey Prompt Memory

version: 2
component: browser
instruction: start chatting with Alfie - say you need to 'fix a toilet'. Expect there is the new chat added to the 'Active chats' section in the sidebar.
instruction_sha256: d3714791e82dfe98de421a616af5d482bcee117f6556bbced98003af9f46ae92
observation_signature: {"title":"Hey Alfie \u2014 Find Trusted Tradespeople","url":"http://localhost:3000/"}
observation_signature_sha256: 5e58857408d0bfed065fecf9ba02ca556ad2c7b4b374854340e663c77a77eada
run_count: 1
updated_at: 2026-05-23T09:35:26.068363Z

## Replay code
```python
# Click on the composer input field and type the message
input_field = page.locator('[data-testid="composer-input"]')
input_field.click(timeout=timeout_ms)
input_field.type("fix a toilet")

# Click the send button to send the message
send_button = page.locator('[data-testid="send-message-button"]')
send_button.click(timeout=timeout_ms)

# Wait for the chat to appear in the sidebar
page.wait_for_selector('[data-testid="recent-chats"] >> text=fix a toilet', timeout=timeout_ms)
```

## Success check code
```python
# Verify the chat message was sent
assert page.locator('text=fix a toilet').is_visible(timeout=timeout_ms)

# Verify the chat appears in RECENT CHATS section in the sidebar
recent_chats_section = page.locator('[data-testid="recent-chats"]')
assert recent_chats_section.is_visible(timeout=timeout_ms)
assert recent_chats_section.locator('text=fix a toilet').is_visible(timeout=timeout_ms)

# Verify Alfie's response is visible
assert page.locator('text=I can help you sort out your toilet').is_visible(timeout=timeout_ms)
```

## Notes
- The input field uses `data-testid="composer-input"` (not the placeholder-based selector which timed out)
- Send button uses `data-testid="send-message-button"`
- Chat appears in sidebar under "RECENT CHATS" section after sending
- Alfie responds with "I can help you sort out your toilet! Let's find out a bit more about what's going on."

## Final output
```text
Successfully started a chat with Alfie by saying 'fix a toilet'. The new chat has been added to the 'RECENT CHATS' section in the sidebar, confirming the chat was created and is now visible in the active chats list.
```
