"""Batch messages from the same sender after a 120-second quiet window.

Normalize sender addresses, retain all source threads, and reply in the latest
thread. New arrivals must never be added to an already frozen batch."""

from __future__ import annotations

import re
from email.utils import parseaddr
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

DEFAULT_QUIET_SECONDS = 120


def normalize_sender(raw: str) -> str:
    """Extract and normalize the address in a display-name mailbox string."""
    if not raw:
        return ""
    # The standard parser distinguishes quoted display names from the actual address.
    email = parseaddr(raw)[1]
    return email.strip().lower()


@dataclass
class EmailMessage:
    """A lightweight message model shared by the workflow and tests."""

    message_id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    received_at: float
    snippet: str = ""
    rfc_message_id: str = ""
    references: str = ""

    @property
    def sender_email(self) -> str:
        return normalize_sender(self.sender)


@dataclass
class Batch:
    """A pending group of messages from one sender."""

    sender_email: str
    messages: List[EmailMessage] = field(default_factory=list)

    @property
    def latest(self) -> EmailMessage:
        return max(self.messages, key=lambda message: (message.received_at, message.message_id))

    @property
    def latest_thread_id(self) -> str:
        return self.latest.thread_id

    @property
    def thread_ids(self) -> List[str]:
        """Return unique thread IDs in their original order."""
        seen: List[str] = []
        for msg in self.messages:
            if msg.thread_id not in seen:
                seen.append(msg.thread_id)
        return seen

    @property
    def quiet_since(self) -> float:
        """Start the quiet window at the most recent message arrival."""
        return max(msg.received_at for msg in self.messages)

    def source_summary(self) -> str:
        """Describe batch sources for prompts and logs."""
        return ", ".join(self.thread_ids)


class MessageBatcher:
    """An in-memory batcher; StateStore handles persistence."""

    def __init__(self, quiet_seconds: float = DEFAULT_QUIET_SECONDS) -> None:
        self.quiet_seconds = quiet_seconds
        self._pending: Dict[str, Batch] = {}

    def add(self, msg: EmailMessage) -> None:
        key = msg.sender_email
        batch = self._pending.setdefault(key, Batch(sender_email=key))
        if all(old.message_id != msg.message_id for old in batch.messages):
            batch.messages.append(msg)
            batch.messages.sort(key=lambda item: (item.received_at, item.message_id))

    def add_many(self, messages: Iterable[EmailMessage]) -> None:
        for msg in messages:
            self.add(msg)

    def pending_senders(self) -> List[str]:
        return list(self._pending.keys())

    def get(self, sender_email: str) -> Optional[Batch]:
        return self._pending.get(sender_email)

    def is_ready(self, sender_email: str, now: Optional[float] = None) -> bool:
        """Check whether the quiet window has elapsed."""
        batch = self._pending.get(sender_email)
        if not batch or not batch.messages:
            return False
        current = time.time() if now is None else now
        return (current - batch.quiet_since) >= self.quiet_seconds

    def ready_batches(self, now: Optional[float] = None) -> List[Batch]:
        """Return ready batches ordered by latest arrival time."""
        batches = [
            b for key, b in self._pending.items() if self.is_ready(key, now)
        ]
        batches.sort(key=lambda b: b.quiet_since)
        return batches

    def pop(self, sender_email: str) -> Optional[Batch]:
        return self._pending.pop(sender_email, None)

    def clear(self) -> None:
        self._pending.clear()


def merge_messages(messages: Sequence[EmailMessage]) -> str:
    """Join chronological messages with their source thread and subject."""
    chunks: List[str] = []
    for idx, msg in enumerate(messages, start=1):
        chunks.append(
            f"[Message {idx}] thread={msg.thread_id} subject={msg.subject!r}\n{msg.body.strip()}"
        )
    return "\n\n".join(chunks)
