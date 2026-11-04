"""Authorize a test mailbox and optionally create a dedicated, initially paused Sheet."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_sheet import LOG_HEADERS

SCOPES = ["https://www.googleapis.com/auth/gmail.modify",
          "https://www.googleapis.com/auth/spreadsheets"]


def main() -> int:
    """Verify the authorized mailbox before creating any demonstration resources."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--support-email", required=True)
    parser.add_argument("--colleague-email")
    parser.add_argument("--create-sheet", action="store_true")
    args = parser.parse_args()
    receipt_path = ROOT / "state" / "google_setup.json"
    if args.create_sheet and receipt_path.exists():
        parser.error("A setup receipt already exists in state/google_setup.json; reuse its Sheet.")
    if args.create_sheet and not args.colleague_email:
        parser.error("--colleague-email is required when creating a demo Sheet")
    client_path, token_path = ROOT / "credentials.json", ROOT / "token.json"
    if not client_path.is_file():
        parser.error("Save your Google Desktop OAuth client as credentials.json first")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES) if token_path.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES).run_local_server(port=0)
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = gmail.users().getProfile(userId="me").execute()
    if profile["emailAddress"].lower() != args.support_email.lower():
        parser.error("The authorized mailbox does not match --support-email; no Sheet was created")
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print("Support mailbox authorized. Local token saved; do not publish it.")
    if not args.create_sheet:
        return 0

    # Persist intent before creation so an uncertain response cannot silently create duplicates.
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with receipt_path.open("x", encoding="utf-8") as stream:
        json.dump({"status": "creation_pending"}, stream)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    spreadsheet = sheets.spreadsheets().create(body={
        "properties": {"title": "BrewLab - Support Demo"},
        "sheets": [{"properties": {"title": "settings"}}, {"properties": {"title": "log"}}],
    }).execute()
    sheet_id = spreadsheet["spreadsheetId"]
    receipt = {"status": "created", "spreadsheet_id": sheet_id,
               "url": spreadsheet["spreadsheetUrl"]}
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    settings = [
        ["mode", "off"], ["timezone", "America/Los_Angeles"],
        ["active_days", "mon,tue,wed,thu,fri"], ["active_hours", "09:00-18:00"],
        ["tone", "friendly, concise, professional"], ["confidence_threshold", "0.6"],
        ["handoff_keywords", "human,refund,complaint,lawyer,manager"],
        ["colleague_email", args.colleague_email], ["admin_email", args.colleague_email],
        ["model", "deepseek-flash"],
    ]
    sheets.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body={
        "valueInputOption": "RAW", "data": [
            {"range": "settings!A1:B11", "values": settings},
            {"range": "log!A1:J1", "values": [LOG_HEADERS]},
        ],
    }).execute()
    receipt["status"] = "ready"
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print("Demo Sheet created in off mode. Add its ID to your private .env:")
    print(f"SPREADSHEET_ID={sheet_id}")
    print(receipt["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
