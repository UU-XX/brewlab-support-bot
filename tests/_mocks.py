"""Shared fake Gmail, Sheet and model clients plus message fixtures.

No real services are contacted. Record calls and effects for assertions."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from batching import EmailMessage
from config_sheet import Settings
from gmail_client import ThreadMessage
from llm import HistoryTurn, LLMResult
from retry_utils import AmbiguousOutcomeError

# Fake exception names match the SDK because error classification uses class names.
class RateLimitError(Exception):
    pass


class BadRequestError(Exception):
    pass


def make_message(
    message_id: str,
    *,
    sender: str = "alice@example.com",
    thread_id: str = "thread-1",
    subject: str = "Question about pricing",
    body: str = "How much is the Pour-Over Starter Kit?",
    received_at: Optional[float] = None,
) -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        sender=sender,
        subject=subject,
        body=body,
        received_at=time.time() if received_at is None else received_at,
        snippet=body[:60],
        rfc_message_id=f"<{message_id}@mail.example>",
        references="",
    )


class FakeGmail:
    """Fake GmailClient recording all remote effects."""

    def __init__(self, messages: Optional[List[Dict[str, Any]]] = None) -> None:
        self.messages = messages or []
        self.calls: List[tuple] = []
        self.labels: Dict[str, List[str]] = {}
        self.drafts: List[Dict[str, Any]] = []
        self.sent: List[Dict[str, Any]] = []
        self.forwarded: List[Dict[str, Any]] = []
        self.threads: Dict[str, List[ThreadMessage]] = {}
        # Controllable failure injection.
        self.fail_list = False
        self.fail_get = False
        self.fail_send = False
        self.fail_send_times = 0
        self.fail_draft = False
        self.fail_forward = False
        self.fail_labels = False
        self.ambiguous_send = False
        self.ambiguous_draft = False
        self.call_counts: Dict[str, int] = {}

    def _count(self, key: str) -> int:
        self.call_counts[key] = self.call_counts.get(key, 0) + 1
        return self.call_counts[key]

    # --- Reads ---
    def list_new_support_messages(self, max_results: int = 50):
        self._count("list")
        if self.fail_list:
            raise RuntimeError("list failed")
        return [{"id": m["id"]} for m in self.messages]

    def get_message(self, message_id: str, fmt: str = "full"):
        self._count("get")
        if self.fail_get:
            raise RuntimeError("get failed")
        for m in self.messages:
            if m["id"] == message_id:
                return m["raw"]
        raise KeyError(message_id)

    def parse_message(self, raw: Dict[str, Any]):
        return {
            "message_id": raw["id"],
            "thread_id": raw["threadId"],
            "from": raw["from"],
            "sender_email": raw["from"].split("<")[-1].strip("> ").lower(),
            "sender_name": raw["from"].split("<")[0].strip(),
            "subject": raw.get("subject", ""),
            "to": raw.get("to", "support@brewlab.example"),
            "date": raw.get("date", ""),
            "rfc_message_id": raw.get("message_id", ""),
            "references": raw.get("references", ""),
            "in_reply_to": raw.get("in_reply_to", ""),
            "body": raw.get("body", ""),
            "snippet": raw.get("body", "")[:60],
            "label_ids": raw.get("label_ids", []),
            "internal_date": float(raw.get("internal_date", 0)) / 1000.0,
        }

    def get_thread(self, thread_id: str, limit: int = 20):
        self._count("thread")
        return self.threads.get(thread_id, [])[-limit:]

    def get_profile_email(self) -> str:
        return "support@brewlab.example"

    # --- Writes ---
    def add_label(self, message_id: str, label_name: str) -> None:
        self._count("label")
        if self.fail_labels:
            raise RuntimeError("label failed")
        self.calls.append(("label", message_id, label_name))
        self.labels.setdefault(message_id, []).append(label_name)

    def add_labels(self, message_id: str, label_names) -> None:
        """Apply labels atomically, matching a single Gmail modify request.

        A failed label causes the whole call to fail; tests can check that
        bot-processed was not applied after a failed combined modification."""
        self._count("label")
        if self.fail_labels:
            raise RuntimeError("label failed")
        for name in label_names:
            self.calls.append(("label", message_id, name))
            self.labels.setdefault(message_id, []).append(name)

    def create_reply_draft(self, **kwargs) -> str:
        self._count("draft")
        if self.ambiguous_draft:
            raise AmbiguousOutcomeError("draft response lost")
        if self.fail_draft:
            raise RuntimeError("draft failed")
        draft_id = f"draft-{len(self.drafts) + 1}"
        self.drafts.append({"id": draft_id, **kwargs})
        self.calls.append(("draft", kwargs.get("thread_id")))
        return draft_id

    def send_reply(self, **kwargs) -> str:
        self._count("send")
        if self.ambiguous_send:
            raise AmbiguousOutcomeError("send response lost")
        if self.fail_send or self.fail_send_times > 0:
            if self.fail_send_times > 0:
                self.fail_send_times -= 1
            raise RuntimeError("send failed")
        message_id = f"sent-{len(self.sent) + 1}"
        self.sent.append({"id": message_id, **kwargs})
        self.calls.append(("send", kwargs.get("thread_id")))
        return message_id

    def create_message(self, **kwargs) -> str:
        self._count("create_message")
        if self.fail_send:
            raise RuntimeError("send failed")
        message_id = f"msg-{len(self.sent) + 1}"
        self.sent.append({"id": message_id, **kwargs})
        self.calls.append(("create_message", kwargs.get("to")))
        return message_id

    def forward_message(self, message_id: str, to: str) -> None:
        self._count("forward")
        if self.fail_forward:
            raise RuntimeError("forward failed")
        self.forwarded.append({"message_id": message_id, "to": to})
        self.calls.append(("forward", message_id, to))


class FakeSheet:
    """Fake ConfigSheet."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log_rows: List[List[Any]] = []
        self.fail_settings = False
        self.fail_log = False
        self.ambiguous_log = False

    def load_settings(self) -> Settings:
        if self.fail_settings:
            # Match the real adapter's exhausted-retry contract.
            from retry_utils import RetryExhaustedError
            raise RetryExhaustedError(3, RuntimeError("sheet unavailable"))
        return self.settings

    def append_log_row(self, row) -> None:
        if self.ambiguous_log:
            raise AmbiguousOutcomeError("log response lost")
        if self.fail_log:
            raise RuntimeError("log failed")
        self.log_rows.append(list(row))

    def has_log_for_message(self, message_id: str) -> bool:
        """Find an existing message_id when reconciling a pending log append."""
        return any(len(row) > 9 and str(row[9]) == str(message_id) for row in self.log_rows)


class FakeLLM:
    """Fake LLMClient with configurable results and failures."""

    def __init__(self, result: Optional[LLMResult] = None) -> None:
        self.result = result or LLMResult(
            intent="pricing",
            language="en",
            confidence=0.9,
            needs_human=False,
            knowledge_used=["products.md"],
            reply="The Pour-Over Starter Kit is $79. — BrewLab Support",
        )
        self.error: Optional[BaseException] = None
        self.calls: List[Dict[str, Any]] = []
        self.system_prompts: List[str] = []

    def generate(self, *, system_prompt, history, user_content):
        self.calls.append(
            {"system_prompt": system_prompt, "history": history, "user_content": user_content}
        )
        self.system_prompts.append(system_prompt)
        if self.error is not None:
            raise self.error
        return self.result


def default_settings(**overrides) -> Settings:
    settings = Settings(
        mode="draft",
        timezone="America/Los_Angeles",
        active_days="mon,tue,wed,thu,fri,sat,sun",
        active_hours="00:00-23:59",
        tone="friendly",
        confidence_threshold=0.6,
        handoff_keywords=["\u6295\u8bc9", "\u9000\u6b3e", "human", "refund", "complaint", "\u627e\u771f\u4eba"],
        colleague_email="colleague@brewlab.example",
        admin_email="admin@brewlab.example",
        model="gpt-4o-mini",
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings
