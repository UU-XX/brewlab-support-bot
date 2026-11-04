"""Persistent journal for batches, remote effects and recovery work.

Use one JSON file with atomic replacement and a process-local lock. Record
pending before remote submission, then done, ambiguous or failed. Pending and
ambiguous outcomes require reconciliation. Keep source messages, waiting batches
and per-message finalization queues across polls and restarts. One worker must
own each mailbox and state file."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Remote effect names.
EFFECT_DRAFT = "draft"
EFFECT_SEND = "send"
EFFECT_FORWARD = "forward"
EFFECT_LOG = "log"
EFFECT_LABEL = "label"
EFFECT_INTENT_LABEL = "intent_label"
EFFECT_ALERT = "alert"

# Effect states.
STATUS_DONE = "done"
STATUS_PENDING = "pending"  # Persisted intent with an unknown remote outcome.
STATUS_AMBIGUOUS = "ambiguous"  # Attempted action with an uncertain outcome requiring human reconciliation.
STATUS_FAILED = "failed"

_DEFAULTS = {
    "processed_messages": {},
    "side_effects": {},
    "pending_compensation": {},
    "batches": {},
    "batches_to_reply": {},
}


class StateStore:
    """A JSON state store protected by a process-local lock."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"version": 2}
        for key, default in _DEFAULTS.items():
            self._data[key] = json.loads(json.dumps(default))
        self.load()

    # ---------- Disk persistence ----------

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                raw = self.path.read_text(encoding="utf-8")
                data = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                # Never silently reset corrupt state; doing so could resend processed mail.
                raise RuntimeError(f"Corrupt state file; manual recovery required: {self.path} ({exc})") from exc
            # Existing state must have the complete structure; preserve deduplication history.
            required = set(_DEFAULTS) - ({"batches_to_reply"} if isinstance(data, dict) and data.get("version") == 1 else set())
            if not isinstance(data, dict) or data.get("version") not in (1, 2) or any(not isinstance(data.get(k), dict) for k in required):
                raise RuntimeError("Corrupt state structure; manual recovery required")
            for key, default in _DEFAULTS.items():
                self._data[key] = data.get(key, json.loads(json.dumps(default)))

    def save(self) -> None:
        """Write atomically to avoid partially written state."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=self.path.name, suffix=".tmp", dir=str(self.path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, ensure_ascii=False, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, self.path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)

    # ---------- Processed messages ----------

    def is_processed(self, message_id: str) -> bool:
        with self._lock:
            return message_id in self._data["processed_messages"]

    def mark_processed(self, message_id: str, **meta: Any) -> None:
        with self._lock:
            entry = {"at": time.time()}
            entry.update(meta)
            self._data["processed_messages"][message_id] = entry
            self.save()

    def done_message_ids(self) -> List[str]:
        with self._lock:
            return list(self._data["processed_messages"].keys())

    # ---------- Remote effects ----------

    def effect_entry(self, message_id: str, effect: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data["side_effects"].get(message_id, {}).get(effect, {}))

    def effect_status(self, message_id: str, effect: str) -> str:
        return str(self.effect_entry(message_id, effect).get("status", ""))

    def is_effect_done(self, message_id: str, effect: str) -> bool:
        return self.effect_status(message_id, effect) == STATUS_DONE

    def is_effect_ambiguous(self, message_id: str, effect: str) -> bool:
        return self.effect_status(message_id, effect) == STATUS_AMBIGUOUS

    def is_effect_pending(self, message_id: str, effect: str) -> bool:
        return self.effect_status(message_id, effect) == STATUS_PENDING

    def is_effect_unconfirmed(self, message_id: str, effect: str) -> bool:
        """Pending and ambiguous effects may have happened and must not be blindly repeated."""
        return self.effect_status(message_id, effect) in (STATUS_PENDING, STATUS_AMBIGUOUS)

    def record_effect(
        self, message_id: str, effect: str, status: str, **meta: Any
    ) -> None:
        with self._lock:
            per_message = self._data["side_effects"].setdefault(message_id, {})
            entry = {"status": status, "at": time.time()}
            entry.update(meta)
            per_message[effect] = entry
            self.save()

    def begin_effect(self, message_id: str, effect: str, **meta: Any) -> None:
        """Persist pending intent before calling the remote service."""
        self.record_effect(message_id, effect, STATUS_PENDING, **meta)

    def mark_effect_done(self, message_id: str, effect: str, **meta: Any) -> None:
        with self._lock:
            self.record_effect(message_id, effect, STATUS_DONE, **meta)
            # Successful completion clears the matching recovery item.
            comp = self._data["pending_compensation"].get(message_id)
            if comp and effect in comp:
                comp.pop(effect, None)
                if not comp:
                    self._data["pending_compensation"].pop(message_id, None)
            self.save()

    def mark_effect_ambiguous(self, message_id: str, effect: str, **meta: Any) -> None:
        """Record an uncertain outcome for reconciliation and prohibit automatic retry."""
        with self._lock:
            self.record_effect(message_id, effect, STATUS_AMBIGUOUS, **meta)
            self._data["pending_compensation"].setdefault(message_id, {})[effect] = {
                "at": time.time(),
                "reason": meta.get("error", "ambiguous outcome"),
            }
            self.save()

    def mark_effect_failed(self, message_id: str, effect: str, **meta: Any) -> None:
        self.record_effect(message_id, effect, STATUS_FAILED, **meta)

    # ---------- Recovery items ----------

    def pending_compensation(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data["pending_compensation"])

    def add_compensation(self, message_id: str, effect: str, reason: str) -> None:
        with self._lock:
            self._data["pending_compensation"].setdefault(message_id, {})[effect] = {
                "at": time.time(),
                "reason": reason,
            }
            self.save()

    def clear_compensation(self, message_id: str, effect: Optional[str] = None) -> None:
        with self._lock:
            if effect is None:
                self._data["pending_compensation"].pop(message_id, None)
            else:
                # A recovered read clears only its own item, retaining other unresolved effects.
                entries = self._data["pending_compensation"].get(message_id, {})
                entries.pop(effect, None)
                if not entries:
                    self._data["pending_compensation"].pop(message_id, None)
            self.save()

    # ---------- Batches retained across polls and restarts ----------

    def save_batch(self, sender_email: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._data["batches"][sender_email] = payload
            self.save()

    def load_batches(self) -> Dict[str, Any]:
        with self._lock:
            return {k: dict(v) for k, v in self._data["batches"].items()}

    def drop_batch(self, sender_email: str) -> None:
        with self._lock:
            if sender_email in self._data["batches"]:
                del self._data["batches"][sender_email]
                self.save()

    # ---------- Reply finalization queues ----------

    def freeze_batch(self, sender: str, batch_id: str, payload: Dict[str, Any]) -> None:
        """Move a waiting batch into finalization in one write, avoiding duplicate sources."""
        with self._lock:
            self._data["batches_to_reply"][batch_id] = payload
            self._data["batches"].pop(sender, None)
            self.save()

    def save_batch_to_reply(self, batch_id: str, payload: Dict[str, Any]) -> None:
        """Persist one frozen batch."""
        with self._lock:
            self._data["batches_to_reply"][batch_id] = payload
            self.save()

    def load_batches_to_reply(self) -> Dict[str, Any]:
        """Return a copy of a frozen batch."""
        with self._lock:
            return {k: dict(v) for k, v in self._data["batches_to_reply"].items()}

    def pop_batch_to_reply(self, batch_id: str) -> None:
        with self._lock:
            self._data["batches_to_reply"].pop(batch_id, None)
            self.save()

    def partition_sender(self, sender: str, ready: dict, waiting: dict) -> None:
        """Repartition a sender's messages, persisting frozen and waiting groups atomically."""
        with self._lock:
            self._data["batches_to_reply"].update(ready)
            self._data["batches"][sender] = waiting
            self.save()

    def all_effects(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data["side_effects"])
