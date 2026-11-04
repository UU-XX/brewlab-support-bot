# BrewLab Test Emails: 15 Scenarios

All customer content is fictional. Automated tests use simulated providers; live recording uses separately authorized test accounts. Intent values are `pricing`, `aftersales`, `chitchat` and `handoff`. A handoff flag may accompany an after-sales or pricing intent.

| # | Scenario | Customer input or setup | Expected intent | Expected behavior |
| --- | --- | --- | --- | --- |
| 1 | Product pricing | "How much is the Pour-Over Starter Kit? Do you ship to Canada?" | pricing | Quote USD 79 and Canadian delivery estimates from the knowledge base; create a draft. |
| 2 | Price comparison | "Your Burr Grinder Mini is $129, but I saw it for $99 elsewhere. Can you match it?" | pricing | Explain manual verification of an authorized seller's offer; do not promise a match. |
| 3 | Tracking delay | "Tracking for order BL-1042 has not updated for six business days." | aftersales | Hand off for investigation; do not invent a shipment status. |
| 4 | Return request | "I want to return my Gooseneck Kettle. It arrived ten days ago." | aftersales | Explain eligibility and request human review; do not promise a refund amount. |
| 5 | Three-message burst | Same sender within 30 seconds: "How much is the kit?" / "Is that in USD?" / "What does shipping to Canada cost?" | pricing | Wait 120 seconds after the last arrival; create one draft in the latest thread, with three source log rows. |
| 6 | Missing knowledge | "Do you offer barista training in Berlin?" | handoff, or another intent with handoff=true | Replace unsupported content with the fixed English handoff reply and forward to a colleague. |
| 7 | Complaint | "This is my third delayed order. I want to file a complaint." | handoff | Match complaint; add needs-human and forward originals. |
| 8 | Human requested | "Please let me talk to a human agent." | handoff | Match human; create an acknowledgement draft and forward to the configured colleague. |
| 9 | Multilingual robustness | A predominantly Chinese question containing English product names; encoded fixtures are in the charset tests. | pricing | Preserve the customer's language and decode complete bodies correctly. This is a regression check, not part of the English recording. |
| 10 | English product guidance | "Which grinder settings should I use for pour-over?" | aftersales | Answer in English using FAQ settings 18-22; no handoff is needed. |
| 11 | Thanks | "Your packaging is lovely. Thank you!" | chitchat | Prepare a brief friendly reply; citations are optional for casual conversation. |
| 12 | Mode switching | Use a new synthetic inquiry in draft, live and off modes. | pricing in active modes | draft creates a draft; live sends one reply; off performs no Gmail processing. |
| 13 | Outside hours | Submit a question on Saturday at 03:00 with weekday-only settings. | Not classified yet | Keep it unprocessed until the active window; no labels or log rows. |
| 14 | Model outage | Inject repeated model API failures for a pricing question. | handoff fallback | After two eligible retries, attempt a fallback draft and administrator alert. |
| 15 | Gmail outage | Inject read failures, explicit rejections and uncertain write failures separately. | Depends on the failure stage | Retry eligible reads; preserve recovery work. Never blindly repeat an uncertain send. |

## Additional regression coverage

- Restart recovery without duplicate drafts, sends or audit rows.
- Unknown remote outcomes retained for manual reconciliation.
- Low-confidence decisions routed to a colleague.
- Invalid settings pause processing.
- Late support labeling does not merge unrelated backlog.
- Empty or externally stored MIME bodies do not bypass handoff detection.
- DeepSeek JSON mode uses the same local validation and retry budget as schema mode.
