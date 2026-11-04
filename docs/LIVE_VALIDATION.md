# Live Integration Validation

Test date: November 4, 2026. Provider: DeepSeek, model `deepseek-flash`.
All messages concerned the fictional BrewLab store and used controlled test inboxes.

## Message consolidation

Three English questions arrived within 77 seconds. The real 120-second quiet window elapsed before processing. The bot created one Gmail draft answering the product price, currency, Canadian shipping charge and delivery estimate. It wrote three separate Google Sheets audit rows and sent no customer reply. A second processing pass left the draft and row counts unchanged.

## Human handoff and mode switching

A new English message requested a human agent. With the Sheet set to `off`, a processing pass produced no draft, forwarding message or log row. After switching to `draft` and allowing the quiet window to expire, the bot created one English acknowledgement draft, applied `intent-handoff`, `needs-human` and `bot-processed`, and wrote one audit row with handoff enabled.

The support mailbox contains one sent internal handoff message. Its original-message attachment was compared byte-for-byte with the received email. A second pass did not duplicate the draft, handoff or log row. Recipient-side receipt has not been independently checked.

## Boundaries

- Gmail and Sheets outcomes above were checked through their APIs, using the real application orchestrator and persistent state.
- The initial three messages were classified as spam by Gmail and were moved to the inbox for this controlled test. An earlier empty test message was left untouched and excluded from the three-message demonstration.
- No live-mode customer reply was sent during this session; live mode is covered by offline tests.
- The Sheet was returned to `off` at the end of testing.
- Six redacted real screenshots and an English narrated screenshot walkthrough are available in [the media gallery](MEDIA.md). The video is not continuous screen capture and does not show an interactive mode-switch sequence.
- Raw evidence, mailbox identifiers, OAuth files and API keys remain private.
