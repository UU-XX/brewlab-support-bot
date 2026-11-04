# English Portfolio Recording: 110 Seconds

## Evidence status

Repository media: [six real redacted screenshots](MEDIA.md) from controlled live testing. See [validated results and limitations](LIVE_VALIDATION.md).

The remaining sections are a reusable plan for a future continuous recording, not a description of footage already captured. Automated demonstrations use simulated providers and must be labeled **Offline simulation** if shown.

## Test setup

Use a dedicated support Gmail inbox plus a second inbox as both customer and colleague. Set Gmail's display language to English. Use an English Sheet with the existing `settings` and `log` headers. Mask all mailbox addresses, account avatars, private identifiers and unrelated messages in exported media.

Keep the 120-second quiet window unchanged. Record it in real time, then shorten the waiting segment in the edit with the visible caption **2-minute batching window — time compressed**.

## Exact customer messages

Send these from the same customer address within 30 seconds. Use a unique subject prefix such as **BrewLab Demo 001** so the evidence can be found without showing unrelated mail.

1. **Subject:** BrewLab Demo 001 — Starter kit\n\n**Body:** Hi BrewLab, how much is the Pour-Over Starter Kit?
2. **Subject:** Re: BrewLab Demo 001 — Starter kit\n\n**Body:** One more question: is that price in US dollars?
3. **Subject:** Re: BrewLab Demo 001 — Starter kit\n\n**Body:** And what would shipping to Canada cost, and how long would it take?

Expected content: USD 79, taxes and shipping excluded, Canada shipping from USD 12 and an estimated 5-9 business days. Actual wording is model-generated; do not replace a real draft with a scripted answer for recording.

For human handoff, send a separate batch after the first batch finishes:

**Subject:** BrewLab Demo 002 — Human support\n\n**Body:** I need to talk to a human agent about my order. Please pass this to a colleague.

## Shot list and English captions

| Time | Screen evidence | On-screen caption / narration |
| --- | --- | --- |
| 0-8 s | Gmail and project title | **BrewLab — AI Email Support**. Three customer messages. One thoughtful reply. |
| 8-24 s | Three incoming messages with their timestamps and support labels | The customer sends three questions in quick succession. |
| 24-32 s | Visible waiting indicator or time cut | **2-minute batching window — time compressed**. The bot waits until the customer finishes. |
| 32-53 s | One actual Gmail draft, expanded to show all three answers | One draft answers the product, currency and shipping questions together. |
| 53-72 s | Human request, needs-human label and forwarded originals in the colleague inbox | Human requests are escalated with the original messages attached. |
| 72-88 s | Sheet log filtered to the demo messages | One audit row per source email: intent, handling mode, handoff and outcome. |
| 88-103 s | Sheet mode changed draft → off; a fresh message remains untouched; draft restored | The support team can pause processing from Google Sheets. |
| 103-110 s | Final title card | **Python · Gmail · DeepSeek · Google Sheets**. Review drafts. Keep humans in control. |

For the mode-switch shot, record at least one real poll in off mode and visibly compress that wait too. Merely editing a cell is not proof that processing stopped. A live-mode demonstration is optional and must use only the controlled customer inbox; the README explains its behavior without claiming it was filmed.

## Screenshot alternatives

1. Three incoming customer messages and one expanded consolidated draft.
2. The needs-human label beside the received internal handoff.
3. Three separate Sheet log rows for the consolidated batch.
4. The settings tab in off mode and the new unprocessed test message.

Capture at 1920 x 1080 where possible. Export redacted PNG files and a 1-2 minute MP4. Review every frame around tab switches for addresses, browser profiles and notifications.

## Verification before export

- Match the three source message IDs to three audit rows.
- Confirm exactly one new draft for that batch and zero customer sends in draft mode.
- Poll again and confirm that no duplicate draft or rows appear.
- Confirm the handoff actually arrived with originals attached.
- Confirm off mode leaves a fresh message unprocessed.
- Record the actual date, commit, model and outcome in a private test receipt.
- Set mode to off and stop the worker afterwards.
- Do not claim measured savings, real customers or production deployment from a fictional demo.
