# Connect a Test Mailbox

Use two inboxes you control: a support mailbox and a customer/colleague mailbox. Keep the support mailbox free of real customer data while recording.

## 1. Create Google OAuth credentials

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create or select a project.
2. Enable **Gmail API** and **Google Sheets API**.
3. Configure **Google Auth Platform** branding, audience and data access. For a personal test, keep the app in testing and add the support mailbox as a test user.
4. Create an OAuth client of type **Desktop app**.
5. Download the client JSON to this project as `credentials.json`. Do not commit it.
6. Authorize the support mailbox locally using the helper below. The bot requests Gmail modification and spreadsheet access.

Follow Google's [Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python) if console labels differ.

## 2. Create a DeepSeek key

1. Sign in to the [DeepSeek API platform](https://platform.deepseek.com/).
2. Create an API key and ensure the account has API access and sufficient balance.
3. Copy `.env.example` to `.env` and set `DEEPSEEK_API_KEY` there, never in a screenshot or repository.
4. Keep `LLM_PROVIDER=deepseek` and `LLM_MODEL=deepseek-flash`.

The adapter uses the official [JSON output mode](https://api-docs.deepseek.com/guides/json_mode/) and validates every response locally. The model name is configurable; check [current model availability](https://api-docs.deepseek.com/quick_start/pricing/) before changing it.

## 3. Authorize and create a dedicated Sheet

Activate your virtual environment, then run:

```bash
python scripts/setup_google.py --create-sheet --support-email YOUR_SUPPORT_ADDRESS --colleague-email YOUR_SECOND_ADDRESS
```

The browser asks you to authorize the support mailbox. The helper verifies the selected account before creating a new spreadsheet. It writes `token.json` and prints the Sheet URL and ID. It does not overwrite an existing Sheet and refuses to create another while its local setup receipt exists. The new Sheet starts in **off** mode.

Set `SPREADSHEET_ID` and `SUPPORT_EMAIL` in your local `.env`. The colleague address is also used as the demo administrator inbox. All addresses stay in local configuration and the private Sheet.

## 4. Run the first draft test

1. Create a Gmail label named `support`.
2. Set the Sheet's operating days, hours and time zone to include the test window.
3. Set `mode` to `draft`.
4. Run `python main.py`.
5. Send the three English messages from [the recording guide](DEMO.md) and apply `support` to all three.
6. Wait at least 120 seconds after the last arrival, plus up to one 60-second poll.
7. Confirm one consolidated draft and three log rows.

Draft mode still sends internal handoff and administrator emails. Use only the authorized test inbox for those actions. Set mode back to **off** and stop the worker when the demonstration ends.

## Private files

Never publish `.env`, OAuth JSON files, `state/`, raw recordings, unmasked screenshots, private Sheet links or mailbox content. The repository excludes them by default. Only publish reviewed, redacted exports.
