"""Gmail channel adapter for messages, threads, drafts, replies and handoff.

Alternative channels implement this adapter contract. Thread replies require
a threadId, matching subject and In-Reply-To / References headers. Uncertain
remote writes are surfaced for reconciliation rather than blindly repeated."""

from __future__ import annotations

import base64
import logging
from html import unescape
import re
import time
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from email.message import EmailMessage as MailEnvelope
from email.utils import make_msgid, parseaddr
from typing import Any, Callable, Dict, List, Optional, Sequence

from retry_utils import AmbiguousOutcomeError, NonRetryableError, call_with_retry


class OutsideActiveWindow(NonRetryableError):
    """The operating window has closed; preserve work for the next window."""

logger = logging.getLogger(__name__)

SUPPORT_LABEL = "support"
PROCESSED_LABEL = "bot-processed"
INTENT_LABELS = {
    "pricing": "intent-pricing",
    "aftersales": "intent-aftersales",
    "chitchat": "intent-chitchat",
    "handoff": "intent-handoff",
}
HANDOFF_LABEL = "needs-human"
MAX_HISTORY_MESSAGES = 20


@dataclass
class ThreadMessage:
    """A historical message used as conversation context."""

    message_id: str
    from_header: str
    to_header: str
    subject: str
    body: str
    date: str = ""
    label_ids: List[str] = field(default_factory=list)

    @property
    def is_from_us(self) -> bool:
        return self.from_header.lower().startswith(("brewlab", "me", "support@brewlab"))


def decode_body(payload: Dict[str, Any], load_data: Optional[Callable[[str], str]] = None) -> str:
    """Recursively extract the body, preferring text/plain."""
    if payload is None:
        return ""
    mime_type = payload.get("mimeType", "").lower()
    body = payload.get("body", {}) or {}
    header = MailEnvelope()
    for item in payload.get("headers", []):
        name = item.get("name", "").lower()
        if name in ("content-type", "content-disposition") and name not in header:
            header[name] = item["value"]
    # Attachments and embedded originals must not hide the customer's own body.
    if payload.get("filename") or header.get_filename() or header.get_content_disposition() == "attachment" or mime_type.startswith("message/"):
        return ""
    charset = header.get_content_charset() or "utf-8"
    if mime_type in ("text/plain", "text/html"):
        data = body.get("data")
        if not data and body.get("attachmentId") and load_data is not None:
            data = load_data(body["attachmentId"])
        text = _b64url_decode(data, charset) if data else ""
        return _strip_html(text) if mime_type == "text/html" else text
    parts = payload.get("parts", []) or []
    if mime_type == "multipart/alternative":
        parts = sorted(parts, key=lambda part: part.get("mimeType") != "text/plain")
    sections = []
    for part in parts:
        text = decode_body(part, load_data)
        if text.strip():
            if mime_type == "multipart/alternative":
                return text
            sections.append(text)
    return "\n".join(sections)


def _b64url_decode(data: str, charset: str = "utf-8") -> str:
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode(charset)
    except (ValueError, TypeError, LookupError):
        return ""


def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    # Strip HTML tags before decoding entities so customer-written angle brackets survive.
    return unescape(re.sub(r"<[^>]+>", "", text)).strip()


class GmailClient:
    """Wrap Gmail messages, threads, labels and drafts endpoints."""

    def __init__(self, service: Any, user_id: str = "me", sleep: Any = None) -> None:
        self.service = service
        self.user_id = user_id
        # Production waits between retries; tests explicitly inject a no-op sleeper.
        self._sleep = time.sleep if sleep is None else sleep
        self._label_cache: Dict[str, str] = {}
        self.before_write = lambda: None

    # ---------- Internal API calls ----------

    def _execute(self, operation: str, fn: Any, write: bool = False) -> Any:
        """Retry reads; distinguish rejected writes from uncertain outcomes."""
        def attempt():
            # Check operating hours before every write, including attempts after backoff.
            if write:
                self.before_write()
            return fn()
        return call_with_retry(attempt, operation=operation, write=write, sleep=self._sleep)

    def _users(self) -> Any:
        return self.service.users()

    def _submit(self, operation: str, request: Any) -> str:
        """Validate the receipt inside the write guard; an unknown remote ID forbids resubmission."""
        def execute():
            result = request().execute()
            remote_id = result.get("id") if isinstance(result, dict) else None
            if not isinstance(remote_id, str) or not remote_id.strip():
                raise AmbiguousOutcomeError(f"{operation} did not return a verifiable resource ID")
            return remote_id
        return self._execute(operation, execute, write=True)

    # ---------- Labels ----------

    def ensure_label(self, name: str) -> str:
        """Ensure a label exists and return its ID."""
        if name in self._label_cache:
            return self._label_cache[name]
        existing = self._execute(
            "Gmail list labels", lambda: self._users().labels().list(userId=self.user_id).execute()
        )
        for label in existing.get("labels", []) or []:
            if label.get("name") == name:
                self._label_cache[name] = label["id"]
                return label["id"]
        created = self._execute(
            "Gmail create label",
            lambda: self._users()
            .labels()
            .create(userId=self.user_id, body={"name": name, "labelListVisibility": "labelShow"})
            .execute(),
            write=True,
        )
        self._label_cache[name] = created["id"]
        return created["id"]

    def add_label(self, message_id: str, label_name: str) -> None:
        label_id = self.ensure_label(label_name)
        self._execute(
            f"Gmail apply label {label_name}",
            lambda: self._users()
            .messages()
            .modify(
                userId=self.user_id,
                id=message_id,
                body={"addLabelIds": [label_id]},
            )
            .execute(),
            write=True,
        )

    def add_labels(self, message_id: str, label_names: Sequence[str]) -> None:
        label_ids = [self.ensure_label(name) for name in label_names]
        if not label_ids:
            return
        self._execute(
            "Gmail apply labels",
            lambda: self._users()
            .messages()
            .modify(userId=self.user_id, id=message_id, body={"addLabelIds": label_ids})
            .execute(),
            write=True,
        )

    def get_message_label_names(self, message_id: str) -> List[str]:
        """Return current label names for idempotency checks."""
        msg = self._execute(
            "Gmail get message",
            lambda: self._users()
            .messages()
            .get(userId=self.user_id, id=message_id, format="minimal")
            .execute(),
        )
        ids = set(msg.get("labelIds", []) or [])
        if not ids:
            return []
        listed = self._execute(
            "Gmail list labels", lambda: self._users().labels().list(userId=self.user_id).execute()
        )
        return [
            label.get("name", "")
            for label in listed.get("labels", []) or []
            if label.get("id") in ids
        ]

    # ---------- Reading messages ----------

    def list_new_support_messages(self, max_results: Optional[int] = None) -> List[Dict[str, Any]]:
        """List all pages of support messages without bot-processed."""
        query = f"label:{SUPPORT_LABEL} -label:{PROCESSED_LABEL} -in:sent -in:drafts"
        messages: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        while max_results is None or len(messages) < max_results:
            page_size = 500 if max_results is None else min(500, max_results - len(messages))
            params: Dict[str, Any] = {
                "userId": self.user_id,
                "q": query,
                "maxResults": page_size,
            }
            if page_token:
                params["pageToken"] = page_token
            page = self._execute(
                "Gmail list messages",
                lambda p=params: self._users().messages().list(**p).execute(),
            )
            messages.extend(page.get("messages", []) or [])
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return messages if max_results is None else messages[:max_results]

    def get_message(self, message_id: str, fmt: str = "full") -> Dict[str, Any]:
        return self._execute(
            "Gmail get full message",
            lambda: self._users()
            .messages()
            .get(userId=self.user_id, id=message_id, format=fmt)
            .execute(),
        )

    def _message_body(self, raw: Dict[str, Any]) -> str:
        """Use complete bodies for inbox and history reads; fetch external bodies by message ID."""
        def load_data(part_id: str) -> str:
            def fetch() -> str:
                part = self._users().messages().attachments().get(
                    userId=self.user_id, messageId=raw["id"], id=part_id
                ).execute()
                data = part.get("data") if isinstance(part, dict) else None
                if not isinstance(data, str):
                    raise ValueError("The external body response has no data field")
                return data
            return self._execute("Gmail get full body", fetch)
        return decode_body(raw.get("payload", {}), load_data).strip()

    def get_thread(self, thread_id: str, limit: int = MAX_HISTORY_MESSAGES) -> List[ThreadMessage]:
        """Return the latest limit messages from a thread."""
        thread = self._execute(
            "Gmail get thread",
            lambda: self._users()
            .threads()
            .get(userId=self.user_id, id=thread_id, format="full")
            .execute(),
        )
        # Exclude unsent drafts before selecting the latest twenty conversation messages.
        raw_messages = [msg for msg in thread.get("messages", []) or [] if "DRAFT" not in msg.get("labelIds", [])]
        # Sort by internalDate instead of relying on server order.
        raw_messages = sorted(raw_messages, key=_internal_date_millis)
        # Keep the latest limit messages in chronological order.
        recent = raw_messages[-limit:]
        history: List[ThreadMessage] = []
        for raw in recent:
            headers = _headers(raw)
            history.append(
                ThreadMessage(
                    message_id=raw.get("id", ""),
                    from_header=headers.get("from", ""),
                    to_header=headers.get("to", ""),
                    subject=headers.get("subject", ""),
                    body=self._message_body(raw),
                    date=headers.get("date", ""),
                    label_ids=list(raw.get("labelIds", []) or []),
                )
            )
        return history

    def parse_message(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Flatten a Gmail message resource into the application model."""
        headers = _headers(raw)
        sender_name, sender_addr = parseaddr(headers.get("from", ""))
        return {
            "message_id": raw.get("id", ""),
            "thread_id": raw.get("threadId", ""),
            "from": headers.get("from", ""),
            "sender_email": sender_addr.lower(),
            "sender_name": sender_name,
            "subject": headers.get("subject", ""),
            "to": headers.get("to", ""),
            "date": headers.get("date", ""),
            "rfc_message_id": headers.get("message-id", ""),
            "references": headers.get("references", ""),
            "in_reply_to": headers.get("in-reply-to", ""),
            "body": self._message_body(raw),
            "snippet": raw.get("snippet", ""),
            "label_ids": list(raw.get("labelIds", []) or []),
            # Use actual arrival time, not poll time, for sorting and the quiet window.
            "internal_date": _internal_date_millis(raw) / 1000.0,
        }

    def get_profile_email(self) -> str:
        profile = self._execute(
            "Gmail get profile",
            lambda: self._users().getProfile(userId=self.user_id).execute(),
        )
        return profile.get("emailAddress", "")

    # ---------- Writing messages ----------

    def _build_raw_message(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        from_email: str = "",
        in_reply_to: str = "",
        references: str = "",
    ) -> str:
        msg = MIMEText(body, "plain", "utf-8")
        msg["To"] = to
        msg["Subject"] = subject
        if from_email:
            msg["From"] = from_email
        msg["Message-ID"] = make_msgid(domain="brewlab.example")
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        refs = " ".join(x for x in [references, in_reply_to] if x).strip()
        if refs:
            msg["References"] = refs
        return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    def create_reply_draft(
        self,
        thread_id: str,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str = "",
        references: str = "",
        from_email: str = "",
    ) -> str:
        """Create a reply draft in its thread and return the draft ID."""
        raw = self._build_raw_message(
            to=to,
            subject=subject,
            body=body,
            from_email=from_email,
            in_reply_to=in_reply_to,
            references=references,
        )
        return self._submit(
            "Gmail create draft",
            lambda: self._users()
            .drafts()
            .create(
                userId=self.user_id,
                body={"message": {"raw": raw, "threadId": thread_id}},
            ),
        )

    def create_message(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        thread_id: str = "",
        in_reply_to: str = "",
        references: str = "",
        from_email: str = "",
    ) -> str:
        """Send a message with an optional thread ID and return its message ID."""
        raw = self._build_raw_message(
            to=to,
            subject=subject,
            body=body,
            from_email=from_email,
            in_reply_to=in_reply_to,
            references=references,
        )
        payload: Dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        return self._submit(
            "Gmail send message",
            lambda: self._users().messages().send(userId=self.user_id, body=payload),
        )

    def send_reply(
        self,
        thread_id: str,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str = "",
        references: str = "",
        from_email: str = "",
    ) -> str:
        return self.create_message(
            to=to,
            subject=subject,
            body=body,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=references,
            from_email=from_email,
        )

    def forward_message(self, message_id: str | Sequence[str], to: str) -> None:
        """Attach every original message as an EML, preserving headers, body and attachments."""
        if not to:
            raise ValueError("The handoff recipient is empty")
        ids = [message_id] if isinstance(message_id, str) else list(message_id)
        envelope = MailEnvelope()
        envelope["To"] = to
        envelope["Subject"] = f"[BrewLab] Support handoff ({len(ids)})"
        envelope.set_content("Open the attached original emails to review the customer’s request and attachments.")
        for index, mid in enumerate(ids, 1):
            encoded = self.get_message(mid, fmt="raw")["raw"]
            data = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            if not data:
                raise ValueError("The original message content is missing")
            envelope.add_attachment(data, maintype="application", subtype="octet-stream", filename=f"original-{index}.eml")
        payload = {"raw": base64.urlsafe_b64encode(envelope.as_bytes()).decode("ascii")}
        self._submit("Gmail forward originals", lambda: self._users().messages().send(userId=self.user_id, body=payload))

    def find_reply_drafts(self, thread_id: str, body_contains: str = "") -> List[str]:
        """Find an existing draft before retrying an uncertain creation."""
        drafts = self._execute(
            "Gmail list drafts",
            lambda: self._users()
            .drafts()
            .list(userId=self.user_id, maxResults=100)
            .execute(),
        )
        matched: List[str] = []
        for draft in drafts.get("drafts", []) or []:
            msg = draft.get("message", {}) or {}
            if msg.get("threadId") != thread_id:
                continue
            if body_contains:
                try:
                    detail = self.get_message(msg["id"], fmt="full")
                    if body_contains[:40] not in self.parse_message(detail)["body"]:
                        continue
                except Exception:  # pragma: no cover - treat a failed lookup as not found
                    continue
            matched.append(draft.get("id", ""))
        return matched

    def is_message_sent(self, message_id: str) -> bool:
        """Check for an existing mailbox message after an uncertain send."""
        try:
            self.get_message(message_id, fmt="minimal")
            return True
        except Exception:
            return False


def _headers(raw: Dict[str, Any]) -> Dict[str, str]:
    """Normalize payload headers to lowercase keys."""
    payload = raw.get("payload", {}) or {}
    result: Dict[str, str] = {}
    for header in payload.get("headers", []) or []:
        name = header.get("name", "").lower()
        if name:
            result[name] = header.get("value", "")
    return result


def _internal_date_millis(raw: Dict[str, Any]) -> int:
    """Read internalDate in milliseconds; missing or invalid values sort first."""
    try:
        return int(raw.get("internalDate") or 0)
    except (TypeError, ValueError):
        return 0


def reply_subject(original_subject: str) -> str:
    """Retain an existing Re: prefix or add one."""
    subject = (original_subject or "").strip() or "(no subject)"
    if re.match(r"^(re|fw|fwd)\s*:", subject, flags=re.I):
        return subject
    return f"Re: {subject}"
