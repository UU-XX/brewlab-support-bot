"""Polling entry point coordinating Gmail, the model, Sheets and local state.

Each cycle reads validated settings, respects off mode and operating hours,
groups messages by actual arrival time, and waits for a two-minute quiet window.
Ready batches use recent history and knowledge to produce a draft or live reply.
Handoffs forward to a colleague, including in draft mode. Persist pending remote
actions before submission, then finish labels and logs for each original message.
Recover acknowledged work without duplicating replies. Uncertain writes require
reconciliation; known failures attempt a fallback draft and administrator alert."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from email.utils import parseaddr
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from batching import (
    DEFAULT_QUIET_SECONDS,
    Batch,
    EmailMessage,
    MessageBatcher,
    merge_messages,
)
from config_sheet import (
    LOG_HEADERS,
    ConfigError,
    ConfigSheet,
    Settings,
    build_log_row,
    is_within_active_window,
    match_handoff_keyword,
)
from gmail_client import (
    HANDOFF_LABEL,
    INTENT_LABELS,
    PROCESSED_LABEL,
    GmailClient,
    OutsideActiveWindow,
    ThreadMessage,
    reply_subject,
)
from knowledge_loader import KnowledgeBase, load_knowledge
from llm import (
    HANDOFF_REPLY_EN,
    HANDOFF_REPLY_ZH,
    HistoryTurn,
    LLMClient,
    LLMError,
    LLMResult,
    build_system_prompt,
    detect_language,
    fallback_reply,
)
from retry_utils import (
    AmbiguousOutcomeError,
    RetryExhaustedError,
)
from state_store import (
    EFFECT_ALERT,
    EFFECT_DRAFT,
    EFFECT_FORWARD,
    EFFECT_INTENT_LABEL,
    EFFECT_LABEL,
    EFFECT_LOG,
    EFFECT_SEND,
    STATUS_AMBIGUOUS,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    StateStore,
)

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 60


@dataclass
class ProcessResult:
    """A batch or message outcome used by the workflow and test assertions."""

    sender_email: str
    thread_id: str
    intent: str = ""
    mode: str = ""
    handoff: bool = False
    reply_summary: str = ""
    actions: List[str] = field(default_factory=list)
    error: str = ""
    skipped: str = ""


def to_datetime(ts: float, tz_name: str) -> datetime:
    """Convert a timestamp to the configured zone, falling back to local time."""
    try:
        return datetime.fromtimestamp(ts, ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001 - is_within_active_window reports invalid zones
        return datetime.fromtimestamp(ts)


def new_batch_id(messages: Sequence[EmailMessage]) -> str:
    """Derive a restart-stable batch ID from source message IDs."""
    raw = "|".join(sorted(m.message_id for m in messages))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def batch_to_payload(batch: Batch, reply_key: str = "") -> Dict[str, Any]:
    """Serialize a batch for StateStore."""
    return {
        "sender_email": batch.sender_email,
        "reply_key": reply_key or batch.latest.message_id,
        "messages": [
            {
                "message_id": m.message_id,
                "thread_id": m.thread_id,
                "sender": m.sender,
                "subject": m.subject,
                "body": m.body,
                "received_at": m.received_at,
                "snippet": m.snippet,
                "rfc_message_id": m.rfc_message_id,
                "references": m.references,
            }
            for m in batch.messages
        ],
    }


def payload_to_batch(payload: Dict[str, Any]) -> Batch:
    """Restore a batch from persistent state."""
    messages = [
        EmailMessage(
            message_id=m["message_id"],
            thread_id=m["thread_id"],
            sender=m["sender"],
            subject=m.get("subject", ""),
            body=m.get("body", ""),
            received_at=float(m.get("received_at", 0)),
            snippet=m.get("snippet", ""),
            rfc_message_id=m.get("rfc_message_id", ""),
            references=m.get("references", ""),
        )
        for m in payload.get("messages", [])
    ]
    messages.sort(key=lambda m: m.received_at)
    return Batch(sender_email=payload.get("sender_email", ""), messages=messages)


def reply_key_for(batch: Batch, payload: Optional[Dict[str, Any]] = None) -> str:
    """Keep the reply effect key fixed after batch creation."""
    if payload and payload.get("reply_key"):
        return str(payload["reply_key"])
    return new_batch_id(batch.messages)


class Orchestrator:
    """Assemble the adapters into a testable processing workflow."""

    def __init__(
        self,
        *,
        gmail: Any,
        sheet: Any,
        llm: Any,
        state: StateStore,
        knowledge: Optional[KnowledgeBase] = None,
        settings: Optional[Settings] = None,
        quiet_seconds: float = DEFAULT_QUIET_SECONDS,
        sleep: Any = time.sleep,
        now_fn: Any = None,
        self_email: str = "support@brewlab.example",
    ) -> None:
        self.gmail = gmail
        self.sheet = sheet
        self.llm = llm
        self.state = state
        self.knowledge = knowledge or load_knowledge()
        self._settings = settings
        self.batcher = MessageBatcher(quiet_seconds=quiet_seconds)
        self._sleep = time.sleep if sleep is None else sleep
        self._now = now_fn or time.time
        self.gmail.before_write = self._ensure_active
        self.self_email = self_email
        # Batch metadata maps batch IDs to creation time and a stable reply key.
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._settings_loaded_at: float = 0.0
        # Track unfinished work so the finalization queue is retained.
        self._resume_later = False
        # Restore persisted batches and finalization queues on startup.
        self._restore_state()

    # ---------- Configuration ----------

    def load_settings(self) -> Settings:
        # Adapters own the retry budget; this layer handles only final failure.
        settings = self.sheet.load_settings()
        self._settings = settings
        self._settings_loaded_at = time.time()
        # Apply the Sheet model setting to subsequent model requests.
        if getattr(self.llm, "model", None) != settings.model:
            try:
                self.llm.model = settings.model
            except AttributeError:  # pragma: no cover - fake clients need not expose a writable model
                pass
        return settings

    # ---------- Recovery ----------

    def _restore_state(self) -> None:
        """Restore pending batches and finalization queues.

        Preserve messages still waiting for silence. Reconcile frozen batches
        with unfinished effects, then complete per-message logs and labels."""
        for sender, payload in self.state.load_batches().items():
            batch = payload_to_batch(payload)
            if not batch.messages:
                self.state.drop_batch(sender)
                continue
            self.batcher._pending[sender] = batch  # noqa: SLF001 - explicit recovery boundary
            self._meta[sender] = {
                "batch_id": new_batch_id(batch.messages),
                "created_at": float(payload.get("created_at", time.time())),
                "reply_key": reply_key_for(batch, payload),
                "mode": payload.get("mode", ""),  # Remember the intake mode for recovery logs.
                "frozen": bool(payload.get("frozen", False)),
            }
        logger.info(
            "State restored: %d pending batches, %d finalization queues",
            len(self.state.load_batches()),
            len(self.state.load_batches_to_reply()),
        )

    # ---------- Polling ----------

    def poll_once(self, now: Optional[float] = None) -> List[ProcessResult]:
        """Run one cycle of intake, recovery and ready-batch processing."""
        current = self._now() if now is None else now
        try:
            settings = self.load_settings()
        except (ConfigError, RetryExhaustedError, AmbiguousOutcomeError) as exc:
            logger.error("Configuration unavailable; pausing this cycle: %s", exc)
            return []

        if settings.mode == "off":
            logger.info("Mode is off; skipping Gmail reads and writes")
            return []
        if not settings.colleague_email.strip():
            # Keep incoming messages pending until a handoff recipient is configured.
            logger.error("Missing colleague_email; processing resumes after settings are completed")
            return []

        try:
            if not is_within_active_window(settings, to_datetime(current, settings.timezone)):
                logger.info("Outside operating hours; skipping this cycle")
                return []
        except ConfigError as exc:
            logger.error("Invalid operating window; pausing this cycle: %s", exc)
            return []

        try:
            return self._poll_active(current, settings)
        except OutsideActiveWindow:
            logger.info("Operating window closed; preserving unfinished work for the next window")
            return []

    def _ensure_active(self) -> None:
        """Recheck the current clock after a potentially slow operation."""
        if self._settings.mode == "off" or not is_within_active_window(self._settings, datetime.fromtimestamp(self._now(), ZoneInfo(self._settings.timezone))):
            raise OutsideActiveWindow("Paused outside operating hours")

    def _write_gmail(self, key: str, effect: str, action: Any, *args, **kwargs):
        """Check hours before mailbox writes; a blocked call can resume later."""
        try:
            self._ensure_active()
            return action(*args, **kwargs)
        except OutsideActiveWindow:
            self.state.mark_effect_failed(key, effect, error="Outside operating hours; this request has not been submitted")
            raise

    def _poll_active(self, current: float, settings: Settings) -> List[ProcessResult]:
        """Preserve queues when intake or execution crosses the closing time."""
        complete = self._ingest_new_messages(current)
        results: List[ProcessResult] = []

        # First finish per-message logs and labels left from previous cycles.
        for batch_id, payload in self.state.load_batches_to_reply().items():
            self._ensure_active()
            result = self._finish_pending_reply(batch_id, payload, settings)
            if result is None:
                continue
            if result is not None:
                results.append(result)

        # Then process batches whose quiet windows have elapsed.
        if not complete:
            # Unknown incoming mail prevents proving silence; only frozen finalization may continue.
            return results
        for batch in self.batcher.ready_batches(current):
            self._ensure_active()
            sender = batch.sender_email
            meta = self._meta.get(sender, {})
            payload = dict(self.state.load_batches().get(sender, {}))
            key = meta.get("reply_key") or reply_key_for(batch, payload)

            # Drop already processed batches without creating another reply.
            if all(self.state.is_processed(m.message_id) for m in batch.messages):
                self.batcher.pop(sender)
                self._meta.pop(sender, None)
                self.state.drop_batch(sender)
                continue

            frozen = self.batcher.pop(sender)
            if frozen is None:
                continue
            # Freeze persistent source IDs and the reply key, then move to finalization.
            batch_id = new_batch_id(frozen.messages)
            self.state.freeze_batch(
                sender, batch_id,
                {
                    **batch_to_payload(frozen, key),
                    "summary": merge_messages(frozen.messages),
                    "actions_result": None,
                    "errors": [],
                    "frozen_at": current,
                    # Persist the actual execution mode rather than using future Sheet settings.
                    "mode": settings.mode,
                },
            )
            self.state.drop_batch(sender)
            self._meta.pop(sender, None)
            item = self.state.load_batches_to_reply()[batch_id]
            result = self._finish_pending_reply(batch_id, item, settings)
            if result is not None:
                results.append(result)
        return results

    def _ingest_new_messages(self, now: float) -> bool:
        """Read new messages by arrival time, excluding already processed or queued mail."""
        try:
            refs = self.gmail.list_new_support_messages()
        except (RetryExhaustedError, AmbiguousOutcomeError) as exc:
            # A Gmail outage may prevent both reading mail and creating drafts or alerts.
            # Persist unresolved work for reconciliation after service recovers.
            logger.error("Gmail intake failed; skipping intake: %s", exc)
            # Use one alert key per continuous outage; a recovered intake resets the incident.
            key = "__gmail_list__"
            issue = self.state.pending_compensation().get(key, {}).get("list", {})
            if not issue:
                self.state.add_compensation(key, "list", f"Message listing failed: {exc}")
                issue = self.state.pending_compensation()[key]["list"]
            self._send_admin_alert(None, key + str(issue["at"]), self._settings, str(exc), None)
            return False
        if "__gmail_list__" in self.state.pending_compensation():
            self.state.clear_compensation("__gmail_list__")
        incoming = []
        complete = True
        for ref in refs or []:
            message_id = ref.get("id", "")
            if not message_id or self.state.is_processed(message_id) or self._is_in_pending(message_id):
                continue
            try:
                raw = self.gmail.get_message(message_id)
                parsed = self.gmail.parse_message(raw)
                self.state.clear_compensation(message_id, "read")
            except (RetryExhaustedError, AmbiguousOutcomeError) as exc:
                self._compensate_unreadable(message_id, str(exc))
                complete = False
                continue
            # Retain sender, thread and arrival time even for empty bodies; hand off the whole batch.
            ts = now
            msg = EmailMessage(
                message_id=parsed["message_id"],
                thread_id=parsed["thread_id"],
                sender=parsed["from"],
                subject=parsed["subject"],
                body=parsed["body"],
                received_at=float(parsed.get("internal_date") or ts),
                snippet=parsed["snippet"],
                rfc_message_id=parsed["rfc_message_id"],
                references=parsed["references"],
            )
            incoming.append(msg)
        # Wait for complete intake before grouping backlog so unknown arrivals cannot split a batch.
        if not complete:
            return False
        by_sender: Dict[str, List[EmailMessage]] = {}
        for message in incoming:
            by_sender.setdefault(message.sender_email, []).append(message)
        for sender, additions in by_sender.items():
            previous = self.batcher.get(sender)
            combined = {m.message_id: m for m in (previous.messages if previous else [])}
            combined.update({m.message_id: m for m in additions})
            groups: List[List[EmailMessage]] = []
            for message in sorted(combined.values(), key=lambda m: (m.received_at, m.message_id)):
                if not groups or (self.batcher.quiet_seconds > 0 and
                                  message.received_at - groups[-1][-1].received_at > self.batcher.quiet_seconds):
                    groups.append([])
                groups[-1].append(message)

            old_key = self._meta.get(sender, {}).get("reply_key")
            ready = {}
            for group in groups:
                batch = Batch(sender, group)
                key = old_key if any(m.message_id == old_key for m in group) else group[0].message_id
                payload = batch_to_payload(batch, key)
                if group is not groups[-1]:
                    ready[new_batch_id(group)] = {**payload, "summary": merge_messages(group),
                                                 "actions_result": None, "errors": [], "frozen_at": now,
                                                 "mode": self._settings.mode}
            # Persist ready and still-waiting groups atomically before updating memory.
            self.state.partition_sender(sender, ready, payload)
            self.batcher.pop(sender)
            self.batcher.add_many(batch.messages)
            self._meta[sender] = {"reply_key": key, "created_at": now, "mode": ""}
        return True

    def _is_in_pending(self, message_id: str) -> bool:
        """Check whether a message is already waiting or being finalized.

        A finalization entry may still lack bot-processed after a label error.
        Re-batching it could suppress replies or mix it with new customer mail."""
        for sender in self.batcher.pending_senders():
            batch = self.batcher.get(sender)
            if batch and any(m.message_id == message_id for m in batch.messages):
                return True
        for payload in self.state.load_batches_to_reply().values():
            if any(m.get("message_id") == message_id for m in payload.get("messages", [])):
                return True
        return False

    # ---------- Batch execution ----------

    def process_batch(self, batch: Any, settings: Settings) -> ProcessResult:
        """Process a batch directly through the same finalization workflow."""
        batch_id = new_batch_id(batch.messages)
        reply_key = self.state.load_batches_to_reply().get(batch_id, {}).get(
            "reply_key"
        ) or batch.latest.message_id
        self.state.save_batch_to_reply(
            batch_id,
            {**batch_to_payload(batch, reply_key), "summary": merge_messages(batch.messages),
             "actions_result": None, "errors": [], "mode": settings.mode},
        )
        result = self._finish_pending_reply(batch_id, self.state.load_batches_to_reply()[batch_id], settings)
        return result or ProcessResult(sender_email=batch.sender_email, thread_id=batch.latest_thread_id)

    def _finish_pending_reply(
        self, batch_id: str, payload: Dict[str, Any], settings: Settings
    ) -> Optional[ProcessResult]:
        """Execute or recover a reply and per-message finalization.

        Persist decisions and pending effects before remote calls. Pending or
        ambiguous effects may already have happened and require reconciliation.
        Retain handoff decisions and summaries across a crash after submission."""
        batch = payload_to_batch(payload)
        if not batch.messages:
            self.state.pop_batch_to_reply(batch_id)
            return None
        key = str(payload.get("reply_key") or new_batch_id(batch.messages))
        result_data = payload.get("actions_result") or {}
        # A reply explicitly blocked by operating hours can resume in the next window.
        if any(self.state.effect_entry(key, effect).get("error", "").startswith("Outside operating hours") for effect in (EFFECT_SEND, EFFECT_DRAFT)):
            payload["decided"] = False
        # Never upgrade an unsent draft to a send on recovery; a switch to draft applies immediately.
        if not result_data:
            if payload.get("mode") == "draft" and settings.mode == "live":
                settings = replace(settings, mode="draft")
            payload["mode"] = settings.mode
        result = ProcessResult(
            sender_email=batch.sender_email,
            thread_id=batch.latest_thread_id,
            intent=result_data.get("intent", ""),
            mode=settings.mode,
            handoff=bool(result_data.get("handoff", False)),
            reply_summary=result_data.get("reply_summary", ""),
            error="; ".join(payload.get("errors", []) or []),
        )
        for action in result_data.get("actions", []) or []:
            result.actions.append(action)

        if not result_data:
            # On first entry, generate a reply and perform its send, draft or fallback effect.
            result_data = self._execute_reply(batch, key, settings, result, payload)
            payload["actions_result"] = result_data
            payload["decided"] = bool(result_data.get("decided"))
            self.state.save_batch_to_reply(batch_id, payload)

        # A saved actions_result does not prove the reply completed.
        # Reconcile remote effects when the decision is missing or a reply is pending.
        # Do not finalize and discard the queue before the reply outcome is accounted for.
        if not result_data.get("fallback_error") and (not payload.get("decided") or self._reply_effect_unconfirmed(key)):
            reconciled = self._reconcile_pending_reply(batch, key, payload, result)
            if reconciled is not None:
                # Unknown outcomes require human reconciliation without a duplicate send.
                result_data = reconciled
                payload["actions_result"] = reconciled
            else:
                # Only execute again when there is no effect or non-execution is confirmed.
                retry_data = self._execute_reply(batch, key, settings, result, payload)
                result_data = retry_data
                payload["actions_result"] = retry_data
            payload["decided"] = True
            self.state.save_batch_to_reply(batch_id, payload)
            result.intent = result_data.get("intent", result.intent)
            result.handoff = bool(result_data.get("handoff", result.handoff))
            result.reply_summary = result_data.get("reply_summary", result.reply_summary)
            result.actions = list(result_data.get("actions", []) or [])
            for err in result_data.get("errors", []) or []:
                if err and err not in result.error:
                    result.error = f"{result.error}; {err}" if result.error else err

        # Persist fallback decisions before effects so completed drafts and alerts are skipped on restart.
        if result_data.get("fallback_error"):
            result.intent = result_data["intent"]
            result.handoff = result_data["handoff"]
            result.error = result_data["fallback_error"]
            body = result_data["reply_body"]
            self._create_fallback_draft(batch, key, settings, body, result)
            result.reply_summary = _summarize(body) if self.state.is_effect_done(key, EFFECT_DRAFT) else ""
            self._send_admin_alert(batch, key, settings, result.error, result)

        # Forward human handoffs to the colleague, including in draft mode.
        if result_data.get("handoff"):
            if settings.colleague_email:
                self._do_forward(batch, key, settings, result)
            else:
                self.state.add_compensation(key, EFFECT_FORWARD, "colleague_email is not configured; human handoff is incomplete")
                result.error = result.error or "Missing colleague email; awaiting human action"
                payload["errors"] = [result.error]
                self.state.save_batch_to_reply(batch_id, payload)
                return result

        # Do not repeat unknown remote effects; notify the administrator to reconcile them.
        if any(self.state.is_effect_unconfirmed(key, effect) for effect in (EFFECT_SEND, EFFECT_DRAFT, EFFECT_FORWARD)):
            self._send_admin_alert(batch, key, settings, result.error or "Remote outcome is uncertain; inspect the mailbox", result)

        # Finalize intent labels, needs-human, logs and bot-processed independently for each source.
        started = float(payload.get("frozen_at", time.time()))
        elapsed_ms = int((time.time() - started) * 1000)
        # Use the persisted handling mode because Sheet settings may have changed.
        log_mode = str(payload.get("mode") or settings.mode)
        all_done = True
        for msg in batch.messages:
            if not self._finalize_message(msg, result, log_mode, elapsed_ms):
                all_done = False

        if all_done:
            self.state.pop_batch_to_reply(batch_id)
            return result
        payload["errors"] = [result.error] if result.error else []
        self.state.save_batch_to_reply(batch_id, payload)
        # Keep unfinished batches queued for the next cycle while returning their current outcomes.
        return result

    def _execute_reply(
        self,
        batch: Batch,
        key: str,
        settings: Settings,
        result: ProcessResult,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Generate and deliver a reply, with fallback handling for known failures.

        Persist the decision first, write the pending intent, call the remote
        service, then mark completion. After a crash, reconcile pending effects
        rather than automatically submitting the same reply again."""
        latest = batch.latest

        # A decision may exist before any remote call; record the mode used for this execution.
        if not any(self.state.is_effect_done(key, effect) or self.state.is_effect_unconfirmed(key, effect) for effect in (EFFECT_SEND, EFFECT_DRAFT)):
            if payload.get("mode") == "draft" and settings.mode == "live":
                settings = replace(settings, mode="draft")
            payload["mode"] = settings.mode
            self.state.save_batch_to_reply(new_batch_id(batch.messages), payload)

        # Pending, ambiguous or completed reply effects prevent creating another reply effect.
        reply_states = {
            effect: self.state.effect_status(key, effect)
            for effect in (EFFECT_SEND, EFFECT_DRAFT)
        }
        if any(state in (STATUS_DONE, STATUS_AMBIGUOUS) for state in reply_states.values()):
            return self._recovered_result(batch, payload, reply_states)
        if any(state == STATUS_PENDING for state in reply_states.values()):
            # After a crash around submission, reconcile the remote outcome before continuing.
            recovered = self._reconcile_pending_reply(batch, key, payload, result)
            if recovered is not None:
                return recovered
            # Only confirmed non-execution allows a fresh generation and submission.

        history: List[HistoryTurn] = []
        try:
            history = self._load_history(batch.latest_thread_id, {msg.message_id for msg in batch.messages})
            user_content = merge_messages(batch.messages)
            keyword = match_handoff_keyword(user_content, settings.keyword_list())
            if any(not msg.body.strip() for msg in batch.messages):
                language = detect_language(" ".join(msg.subject + " " + msg.body for msg in batch.messages))
                reply = HANDOFF_REPLY_ZH if language == "zh" else HANDOFF_REPLY_EN
                llm_result = LLMResult(intent="handoff", language=language, confidence=0.0,
                                       needs_human=True, knowledge_used=[], reply=reply)
            else:
                llm_result = self._generate(settings, history, user_content)
        except (RetryExhaustedError, AmbiguousOutcomeError) as exc:
            # Missing thread history is a failure, not permission to generate without context.
            logger.error("Thread history remains unavailable; using fallback handling: %s", exc)
            self.state.add_compensation(key, "history", f"Thread history read failed: {exc}")
            return self._handle_failure(batch, key, settings, f"Thread history read failed: {exc}", result)
        except LLMError as exc:
            logger.error("Reply generation failed; using fallback handling: %s", exc)
            return self._handle_failure(batch, key, settings, str(exc), result)

        intent = llm_result.intent
        handoff = bool(llm_result.needs_human) or bool(keyword)
        if llm_result.is_low_confidence(settings.confidence_threshold):
            handoff = True
        if keyword:
            # Complaint, refund and human-agent keywords take precedence as handoff intent.
            intent = "handoff"
        if handoff:
            llm_result.needs_human = True
            llm_result.reply = _ensure_handoff_text(llm_result.reply, llm_result.language)

        result.intent = intent
        result.handoff = handoff
        result.reply_summary = _summarize(llm_result.reply)

        # Persist the decision so handoff and log summaries survive a crash after submission.
        decision = {
            "intent": intent,
            "handoff": handoff,
            "reply_summary": result.reply_summary,
            "language": llm_result.language,
            "reply_body": llm_result.reply,
            "actions": [],
            "errors": [],
            # The persisted decision controls recovery and reply reconciliation.
            "decided": True,
        }
        payload["actions_result"] = decision
        self.state.save_batch_to_reply(
            str(payload.get("batch_id") or new_batch_id(batch.messages)), payload
        )

        actions: List[str] = []
        if settings.mode == "live":
            ok = self._do_send(batch, key, llm_result, settings, result)
            if ok:
                actions.append("send")
            elif self.state.effect_status(key, EFFECT_SEND) == STATUS_FAILED:
                return self._handle_failure(batch, key, settings, result.error, result)
        elif settings.mode == "draft":
            ok = self._do_draft(batch, key, llm_result, settings, result)
            if ok:
                actions.append("draft")
            elif self.state.effect_status(key, EFFECT_DRAFT) == STATUS_FAILED:
                return self._handle_failure(batch, key, settings, result.error, result)

        decision["actions"] = actions
        decision["errors"] = [result.error] if result.error else []
        return decision

    def _recovered_result(
        self, batch: Batch, payload: Dict[str, Any], states: Dict[str, str]
    ) -> Dict[str, Any]:
        """Restore the original decision when reply effects already exist.

        Preserve intent, handoff and summary rather than replacing them with
        empty values that could suppress forwarding or erase audit details."""
        decision = dict(payload.get("actions_result") or {})
        done_effects = [name for name, state in states.items() if state == STATUS_DONE]
        ambiguous = [name for name, state in states.items() if state == STATUS_AMBIGUOUS]
        if done_effects and "resumed" in decision and not decision.get("intent"):
            # Legacy resumed-only state has no decision; fall back conservatively to human handoff.
            decision = {
                "intent": "handoff",
                "handoff": True,
                "reply_summary": "",
            }
        if done_effects:
            decision.setdefault("actions", [])
            for effect in done_effects:
                if effect not in decision["actions"]:
                    decision["actions"].append(effect)
        decision["resumed"] = True
        decision.setdefault("intent", "handoff")
        decision.setdefault("handoff", decision["intent"] == "handoff")
        decision.setdefault("reply_summary", "")
        if ambiguous:
            decision["errors"] = list(decision.get("errors") or []) + [
                f"{name} outcome is uncertain; paused for human reconciliation" for name in ambiguous
            ]
        return decision

    def _reconcile_pending_reply(
        self,
        batch: Batch,
        key: str,
        payload: Dict[str, Any],
        result: ProcessResult,
    ) -> Optional[Dict[str, Any]]:
        """Reconcile pending effects: confirmed execution becomes done; unknown becomes ambiguous.

        Return None only when continuing with one submission is safe."""
        decision = dict(payload.get("actions_result") or {})
        # Prefer the persisted intended reply; use a batch summary only for legacy state.
        reply_body = str(
            decision.get("reply_body")
            or payload.get("summary")
            or getattr(result, "reply_body", "")
            or ""
        )
        for effect in (EFFECT_SEND, EFFECT_DRAFT):
            if not self.state.is_effect_pending(key, effect):
                continue
            found = False
            if effect == EFFECT_SEND:
                found = self._verify_sent(batch.latest, reply_body)
            else:
                found = bool(self._find_drafts(batch.latest_thread_id, reply_body))
            if found:
                self.state.mark_effect_done(key, effect, verified=True)
                self._mirror_effect(batch, key, effect, verified=True)
                logger.info("%s confirmed complete; restored to done without repeating it", effect)
                continue
            # Do not resend an unconfirmed operation; require human reconciliation.
            self.state.mark_effect_ambiguous(key, effect, error="pending without proof")
            self.state.add_compensation(key, effect, "Uncertain outcome; reconcile manually before retrying")
            decision.setdefault("errors", []).append(f"{effect} outcome is uncertain; awaiting human reconciliation")
            decision.setdefault("intent", "handoff")
            decision.setdefault("handoff", True)
            decision.setdefault("reply_summary", _summarize(reply_body))
            return decision
        # Clear pending state only after non-execution is established.
        for effect in (EFFECT_SEND, EFFECT_DRAFT):
            if self.state.effect_status(key, effect) == STATUS_PENDING:
                self.state.mark_effect_failed(key, effect, error="verified not executed")
        return None

    def _load_history(self, thread_id: str, source_ids: set[str]) -> List[HistoryTurn]:
        """Read twenty recent messages, excluding current input by message ID."""
        try:
            thread = self.gmail.get_thread(thread_id, limit=20)
        except (RetryExhaustedError, AmbiguousOutcomeError) as exc:
            # A failed thread read cannot be treated as valid context; propagate it.
            logger.error("Thread history read failed: %s", exc)
            raise
        turns: List[HistoryTurn] = []
        for item in thread:
            if isinstance(item, ThreadMessage):
                from_header, body = item.from_header or "", item.body or ""
                mid = item.message_id
            else:  # Support dictionary-shaped history records.
                from_header, body = str(item.get("from_header", "")), item.get("body", "")
                mid = item.get("message_id") or item.get("id")
            if mid in source_ids or not body.strip():
                continue
            is_from_us = parseaddr(from_header)[1].casefold() == self.self_email.casefold()
            turns.append(HistoryTurn(role="assistant" if is_from_us else "user", content=body))
        return turns[-20:]

    def _generate(
        self, settings: Settings, history: Sequence[HistoryTurn], user_content: str
    ) -> LLMResult:
        system_prompt = build_system_prompt(self.knowledge.render(), settings.tone)
        return self.llm.generate(
            system_prompt=system_prompt,
            history=history,
            user_content=user_content,
        )

    # ---------- Reply effects ----------

    def _do_send(self, batch, key, llm_result, settings, result) -> bool:
        """Reply in the latest thread, persisting pending before submission."""
        latest = batch.latest
        if self.state.is_effect_done(key, EFFECT_SEND):
            if "send" not in result.actions:
                result.actions.append("send")
            return True
        if self.state.is_effect_unconfirmed(key, EFFECT_SEND):
            return self._pause_on_unconfirmed(batch, key, EFFECT_SEND, llm_result.reply, result)

        self.state.begin_effect(key, EFFECT_SEND, thread_id=batch.latest_thread_id)
        try:
            sent_id = self._write_gmail(key, EFFECT_SEND, self.gmail.send_reply,
                thread_id=batch.latest_thread_id,
                to=batch.sender_email,
                subject=reply_subject(latest.subject),
                body=llm_result.reply,
                in_reply_to=latest.rfc_message_id,
                references=latest.references,
                from_email=self.self_email,
            )
        except OutsideActiveWindow:
            raise
        except AmbiguousOutcomeError as exc:
            self.state.mark_effect_ambiguous(key, EFFECT_SEND, error=str(exc))
            result.error = f"send ambiguous: {exc}"
            return False
        except Exception as exc:  # noqa: BLE001 - the adapter already owns two retries
            self.state.mark_effect_failed(key, EFFECT_SEND, error=str(exc))
            result.error = f"send failed: {exc}"
            return False
        self.state.mark_effect_done(key, EFFECT_SEND, sent_id=sent_id)
        self._mirror_effect(batch, key, EFFECT_SEND, sent_id=sent_id)
        return True

    def _do_draft(self, batch, key, llm_result, settings, result) -> bool:
        """Create a thread draft without sending to the customer; persist pending first."""
        latest = batch.latest
        if self.state.is_effect_done(key, EFFECT_DRAFT):
            # Support legacy draft state keyed by the original message ID.
            if not self.state.is_effect_done(latest.message_id, EFFECT_DRAFT):
                self.state.mark_effect_done(latest.message_id, EFFECT_DRAFT, inherited=True)
            return True
        if self.state.is_effect_unconfirmed(key, EFFECT_DRAFT):
            return self._pause_on_unconfirmed(batch, key, EFFECT_DRAFT, llm_result.reply, result)

        self.state.begin_effect(key, EFFECT_DRAFT, thread_id=batch.latest_thread_id)
        try:
            draft_id = self._write_gmail(key, EFFECT_DRAFT, self.gmail.create_reply_draft,
                thread_id=batch.latest_thread_id,
                to=batch.sender_email,
                subject=reply_subject(latest.subject),
                body=llm_result.reply,
                in_reply_to=latest.rfc_message_id,
                references=latest.references,
                from_email=self.self_email,
            )
        except OutsideActiveWindow:
            raise
        except AmbiguousOutcomeError as exc:
            self.state.mark_effect_ambiguous(key, EFFECT_DRAFT, error=str(exc))
            result.error = f"draft ambiguous: {exc}"
            return False
        except Exception as exc:  # noqa: BLE001
            self.state.mark_effect_failed(key, EFFECT_DRAFT, error=str(exc))
            result.error = f"draft failed: {exc}"
            return False
        self.state.mark_effect_done(key, EFFECT_DRAFT, draft_id=draft_id)
        self._mirror_effect(batch, key, EFFECT_DRAFT, draft_id=draft_id)
        return True

    def _pause_on_unconfirmed(
        self, batch: Batch, key: str, effect: str, reply: str, result: ProcessResult
    ) -> bool:
        """Reconcile pending or ambiguous effects; never blindly repeat an unknown write."""
        if self.state.is_effect_pending(key, effect):
            found = False
            if effect == EFFECT_SEND:
                found = self._verify_sent(batch.latest, reply)
            else:
                found = bool(self._find_drafts(batch.latest_thread_id, reply))
            if found:
                self.state.mark_effect_done(key, effect, verified=True)
                self._mirror_effect(batch, key, effect, verified=True)
                if effect not in result.actions:
                    result.actions.append(effect)
                return True
        self.state.mark_effect_ambiguous(key, effect, error="unconfirmed")
        self.state.add_compensation(key, effect, "Uncertain outcome; reconcile manually before retrying")
        result.error = result.error or f"{effect} unconfirmed, paused for manual review"
        return False

    def _do_forward(self, batch, key, settings, result) -> None:
        """Forward to a colleague and retain failures without blocking other finalization.

        Completed, pending and ambiguous effects must not be blindly repeated."""
        latest = batch.latest
        if self.state.is_effect_done(key, EFFECT_FORWARD):
            return
        if self.state.is_effect_ambiguous(key, EFFECT_FORWARD):
            result.skipped = "forward-ambiguous"
            return
        if self.state.is_effect_pending(key, EFFECT_FORWARD):
            # Reconcile a previous forwarding attempt before deciding it completed.
            if self._verify_forwarded(latest, settings.colleague_email):
                self.state.mark_effect_done(key, EFFECT_FORWARD, verified=True)
                if "forward" not in result.actions:
                    result.actions.append("forward")
                return
            self.state.mark_effect_ambiguous(key, EFFECT_FORWARD, error="forward unconfirmed")
            self.state.add_compensation(key, EFFECT_FORWARD, "Forwarding outcome is uncertain; human reconciliation required")
            result.skipped = "forward-pending"
            return
        self.state.begin_effect(key, EFFECT_FORWARD, to=settings.colleague_email)
        try:
            if len(batch.messages) == 1:
                self._write_gmail(key, EFFECT_FORWARD, self.gmail.forward_message, latest.message_id, settings.colleague_email)
            else:
                # Forward every original in a cross-thread batch so no complaint content is omitted.
                self._write_gmail(key, EFFECT_FORWARD, self.gmail.forward_message, [msg.message_id for msg in batch.messages], settings.colleague_email)
        except OutsideActiveWindow:
            raise
        except AmbiguousOutcomeError as exc:
            self.state.mark_effect_ambiguous(key, EFFECT_FORWARD, error=str(exc))
            result.error = result.error or f"forward ambiguous: {exc}"
            return
        except Exception as exc:  # noqa: BLE001
            # A required handoff failure must remain visible and queued for recovery.
            self.state.mark_effect_failed(key, EFFECT_FORWARD, error=str(exc))
            self.state.add_compensation(key, EFFECT_FORWARD, f"Forwarding to colleague failed: {exc}")
            result.error = result.error or f"forward failed: {exc}"
            self._send_admin_alert(
                batch,
                key,
                settings,
                f"Forwarding to colleague failed: {exc}",
                result,
                error_prefix="forward",
            )
            return
        self.state.mark_effect_done(key, EFFECT_FORWARD, to=settings.colleague_email)
        result.actions.append("forward")

    def _verify_forwarded(self, latest: EmailMessage, to: str) -> bool:
        """Do not infer this forwarding outcome from historical forwarded content."""
        return False

    def _mirror_effect(self, batch: Batch, key: str, effect: str, **meta) -> None:
        """Copy reply effect state to each source message for diagnosis."""
        for msg in batch.messages:
            if msg.message_id != key:
                self.state.mark_effect_done(msg.message_id, effect, inherited=key, **meta)

    def _find_drafts(self, thread_id: str, reply: str) -> List[str]:
        """An old draft with matching text does not prove this request succeeded."""
        return []

    def _verify_sent(self, latest: EmailMessage, reply: str) -> bool:
        """Without a unique remote operation ID, leave send confirmation to a person."""
        return False

    # ---------- Independent per-message finalization ----------

    def _finalize_message(
        self, msg: EmailMessage, result: ProcessResult, log_mode: str, elapsed_ms: int
    ) -> bool:
        """Complete labels, bot-processed and the audit log independently.

        Each step records its own pending, done, ambiguous or failed state.
        Reconcile pending Sheet appends by message ID instead of appending twice.
        Keep the queue until all required steps have completed."""
        mid = msg.message_id
        ok = True
        intent_label = INTENT_LABELS.get(result.intent, INTENT_LABELS["handoff"])

        # Apply intent and needs-human labels in one idempotent modify request.
        if not self.state.is_effect_done(mid, EFFECT_INTENT_LABEL):
            labels = [intent_label] + ([HANDOFF_LABEL] if result.handoff else [])
            if self.state.is_effect_ambiguous(mid, EFFECT_INTENT_LABEL):
                ok = False  # An uncertain labeling outcome remains subject to reconciliation.
            else:
                self.state.begin_effect(mid, EFFECT_INTENT_LABEL, labels=labels)
                try:
                    if hasattr(self.gmail, "add_labels"):
                        self._write_gmail(mid, EFFECT_INTENT_LABEL, self.gmail.add_labels, mid, labels)
                    else:
                        for name in labels:
                            self._write_gmail(mid, EFFECT_INTENT_LABEL, self.gmail.add_label, mid, name)
                    self.state.mark_effect_done(mid, EFFECT_INTENT_LABEL, labels=labels)
                except OutsideActiveWindow:
                    raise
                except AmbiguousOutcomeError as exc:
                    self.state.mark_effect_ambiguous(mid, EFFECT_INTENT_LABEL, error=str(exc))
                    result.error = result.error or f"label ambiguous: {exc}"
                    ok = False
                except Exception as exc:  # noqa: BLE001
                    self.state.mark_effect_failed(mid, EFFECT_INTENT_LABEL, error=str(exc))
                    result.error = result.error or f"label failed: {exc}"
                    ok = False  # A labeling failure must not be marked complete.

        # Track bot-processed separately from logging and intent labeling.
        if not self.state.is_effect_done(mid, EFFECT_LABEL):
            if self.state.is_effect_ambiguous(mid, EFFECT_LABEL):
                ok = False
            else:
                self.state.begin_effect(mid, EFFECT_LABEL)
                try:
                    self._write_gmail(mid, EFFECT_LABEL, self.gmail.add_label, mid, PROCESSED_LABEL)
                    self.state.mark_effect_done(mid, EFFECT_LABEL)
                except OutsideActiveWindow:
                    raise
                except AmbiguousOutcomeError as exc:
                    self.state.mark_effect_ambiguous(mid, EFFECT_LABEL, error=str(exc))
                    result.error = result.error or f"processed-label ambiguous: {exc}"
                    ok = False
                except Exception as exc:  # noqa: BLE001
                    self.state.mark_effect_failed(mid, EFFECT_LABEL, error=str(exc))
                    result.error = result.error or f"processed-label failed: {exc}"
                    ok = False

        if not ok:
            # If a reply already exists, label failures should alert without creating another reply.
            self._send_admin_alert(None, "labels-" + mid, self._settings, result.error or "Labels require reconciliation", result)

        # Write the log after observing labeling results; skip done and reconcile pending.
        if not self.state.is_effect_done(mid, EFFECT_LOG):
            if self.state.is_effect_ambiguous(mid, EFFECT_LOG):
                ok = False
            elif self.state.is_effect_pending(mid, EFFECT_LOG):
                if self._verify_log_row(mid):
                    self.state.mark_effect_done(mid, EFFECT_LOG, verified=True)
                else:
                    self.state.mark_effect_ambiguous(mid, EFFECT_LOG, error="log pending unverified")
                    self.state.add_compensation(mid, EFFECT_LOG, "The log row may already exist; reconcile by message_id")
                    result.error = result.error or "log pending, paused for manual review"
                    ok = False
            else:
                self.state.begin_effect(mid, EFFECT_LOG)
                row = build_log_row(
                    timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
                    sender=msg.sender_email or msg.sender,
                    intent=result.intent,
                    mode=log_mode,
                    handoff=result.handoff,
                    reply_summary=result.reply_summary,
                    elapsed_ms=elapsed_ms,
                    error=result.error,
                    thread_id=msg.thread_id,
                    message_id=mid,
                )
                try:
                    self.sheet.append_log_row(row)
                except AmbiguousOutcomeError as exc:
                    self.state.mark_effect_ambiguous(mid, EFFECT_LOG, error=str(exc))
                    self.state.add_compensation(mid, EFFECT_LOG, "Log outcome is uncertain; human reconciliation required")
                    result.error = result.error or f"log ambiguous: {exc}"
                    ok = False
                except Exception as exc:  # noqa: BLE001 - the adapter already owns two retries
                    # Known rejection may be retried next cycle without duplicating acknowledged rows.
                    self.state.mark_effect_failed(mid, EFFECT_LOG, error=str(exc))
                    result.error = result.error or f"log failed: {exc}"
                    ok = False
                else:
                    # A local failure after a remote append must stay pending to prevent duplicate rows.
                    self.state.mark_effect_done(mid, EFFECT_LOG)

        if ok:
            self.state.mark_processed(
                mid,
                thread_id=msg.thread_id,
                intent=result.intent,
                mode=log_mode,
                handoff=result.handoff,
            )
        else:
            # Keep the batch in the finalization queue for the next poll.
            self._resume_later = True
        return ok

    def _verify_log_row(self, message_id: str) -> bool:
        """Check message_id before recovering a pending log append."""
        checker = getattr(self.sheet, "has_log_for_message", None)
        if not callable(checker):
            return False  # A failed lookup requires a person, not another blind append.
        try:
            return bool(checker(message_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Log reconciliation failed: %s", exc)
            return False

    def _reply_effect_unconfirmed(self, key: str) -> bool:
        """Check for a possibly executed but unconfirmed reply effect."""
        return any(
            self.state.is_effect_unconfirmed(key, effect)
            for effect in (EFFECT_SEND, EFFECT_DRAFT)
        )

    # ---------- Fallback handling ----------

    def _handle_failure(
        self,
        batch: Batch,
        key: str,
        settings: Settings,
        error: str,
        result: ProcessResult,
    ) -> Dict[str, Any]:
        """After a model or delivery failure, attempt a fallback draft and administrator alert."""
        latest = batch.latest
        # An empty body can still use the subject or earlier batch messages for language detection.
        language = detect_language(latest.body.strip() or latest.subject.strip() or " ".join(
            msg.body + " " + msg.subject for msg in batch.messages
        ))
        body = fallback_reply(language)
        result.handoff = True
        result.intent = "handoff"
        result.reply_summary = _summarize(body)
        actions: List[str] = []
        if not result.error:
            result.error = error
        elif error and error not in result.error:
            result.error = f"{result.error}; {error}"

        # Persist fallback intent before creating the draft, alert and forwarding effects.
        return {
            "decided": True,
            "fallback_error": result.error,
            "reply_body": body,
            "intent": "handoff",
            "handoff": True,
            "reply_summary": result.reply_summary or "",
            "actions": actions,
            "errors": [result.error] if result.error else [],
        }

    def _create_fallback_draft(
        self, batch: Batch, key: str, settings: Settings, body: str, result: ProcessResult
    ) -> None:
        latest = batch.latest
        if self.state.is_effect_done(key, EFFECT_DRAFT):
            return
        if self.state.is_effect_unconfirmed(key, EFFECT_DRAFT):
            drafts = self._find_drafts(batch.latest_thread_id, body)
            if drafts:
                self.state.mark_effect_done(key, EFFECT_DRAFT, draft_id=drafts[0], fallback=True)
                return
            self.state.mark_effect_ambiguous(key, EFFECT_DRAFT, error="fallback draft unconfirmed")
            self.state.add_compensation(key, EFFECT_DRAFT, "Fallback draft outcome is uncertain; human reconciliation required")
            return
        self.state.begin_effect(key, EFFECT_DRAFT, fallback=True, thread_id=batch.latest_thread_id)
        try:
            draft_id = self._write_gmail(key, EFFECT_DRAFT, self.gmail.create_reply_draft,
                thread_id=batch.latest_thread_id,
                to=batch.sender_email,
                subject=reply_subject(latest.subject),
                body=body,
                in_reply_to=latest.rfc_message_id,
                references=latest.references,
                from_email=self.self_email,
            )
        except OutsideActiveWindow:
            raise
        except AmbiguousOutcomeError as exc:
            self.state.mark_effect_ambiguous(key, EFFECT_DRAFT, error=f"fallback ambiguous: {exc}")
            self.state.add_compensation(key, "fallback_draft", "Fallback draft outcome is uncertain; human reconciliation required")
            return
        except Exception as exc:  # noqa: BLE001
            self.state.mark_effect_failed(key, EFFECT_DRAFT, error=str(exc))
            # A total Gmail outage can block drafts and alerts; persist recovery work locally.
            self.state.add_compensation(key, "fallback_draft", f"Gmail unavailable; fallback draft failed: {exc}")
            result.error = f"{result.error}; fallback draft failed: {exc}"
            return
        self.state.mark_effect_done(key, EFFECT_DRAFT, draft_id=draft_id, fallback=True)
        self._mirror_effect(batch, key, EFFECT_DRAFT, draft_id=draft_id, fallback=True)
        result.actions.append("fallback-draft")

    def _send_admin_alert(
        self,
        batch: Optional[Batch],
        key: str,
        settings: Settings,
        error: str,
        result: Optional[ProcessResult],
        error_prefix: str = "alert",
    ) -> None:
        """Alert the administrator; persist unresolved work if Gmail is unavailable."""
        if self.state.is_effect_done(key, EFFECT_ALERT):
            return
        if not settings.admin_email:
            self.state.add_compensation(key, EFFECT_ALERT, "admin_email is not configured; no alert was sent")
            return
        if self.state.is_effect_ambiguous(key, EFFECT_ALERT):
            return
        if self.state.is_effect_pending(key, EFFECT_ALERT):
            # A crash after alert submission requires reconciliation, not blind resending.
            self.state.mark_effect_ambiguous(key, EFFECT_ALERT, error="alert unconfirmed")
            self.state.add_compensation(key, EFFECT_ALERT, "Alert delivery outcome is uncertain; human reconciliation required")
            return
        source = batch.sender_email if batch else key
        thread = batch.latest_thread_id if batch else ""
        subject = f"[BrewLab bot] Processing failure alert - {source}"
        body = (
            "Automatic email support could not process a message.\n\n"
            f"Source: {source}\nThread: {thread}\n"
            f"Error: {error}\nPlease follow up manually."
        )
        self.state.begin_effect(key, EFFECT_ALERT, to=settings.admin_email)
        try:
            self._write_gmail(key, EFFECT_ALERT, self.gmail.create_message, to=settings.admin_email, subject=subject, body=body)
        except OutsideActiveWindow:
            raise
        except AmbiguousOutcomeError as exc:
            self.state.mark_effect_ambiguous(key, EFFECT_ALERT, error=str(exc))
            self.state.add_compensation(key, EFFECT_ALERT, "Alert delivery outcome is uncertain")
            return
        except Exception as exc:  # noqa: BLE001
            self.state.mark_effect_failed(key, EFFECT_ALERT, error=str(exc))
            self.state.add_compensation(key, EFFECT_ALERT, "Gmail unavailable; alert failed: %s" % exc)
            return
        self.state.mark_effect_done(key, EFFECT_ALERT, to=settings.admin_email)
        if result is not None:
            result.actions.append(error_prefix if error_prefix != "alert" else "alert")

    def _compensate_unreadable(self, message_id: str, error: str) -> None:
        """Record a known unreadable message for recovery; no reply can be addressed yet."""
        self.state.add_compensation(message_id, "read", f"Message read failed; manual intake required: {error}")
        self._send_admin_alert(None, "read-" + message_id, self._settings, error, None)


# ---------- Helpers ----------


def _ensure_handoff_text(reply: str, language: str) -> str:
    text = (reply or "").strip()
    handoff = HANDOFF_REPLY_ZH if language == "zh" else HANDOFF_REPLY_EN
    if not text or _looks_like_handoff(text):
        return text or handoff
    return f"{text}\n\n{handoff}"


def _looks_like_handoff(reply: str) -> bool:
    lowered = reply.lower()
    return any(
        marker in lowered
        for marker in ("colleague", "human", "\u8f6c\u7ed9\u540c\u4e8b", "\u8f6c\u4eba\u5de5", "\u540c\u4e8b\u8ddf\u8fdb", "passed this")
    )


def _summarize(text: str, limit: int = 120) -> str:
    return " ".join((text or "").split())[:limit]


# ---------- Runtime assembly ----------


def build_runtime(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Assemble real adapters from environment settings."""
    from pathlib import Path

    from dotenv import load_dotenv
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from openai import OpenAI

    load_dotenv()
    env = dict(os.environ) if env is None else env
    provider = env.get("LLM_PROVIDER", "deepseek").strip().lower()
    if provider not in {"openai", "deepseek"}:
        raise RuntimeError("LLM_PROVIDER must be deepseek or openai")
    key_name = "DEEPSEEK_API_KEY" if provider == "deepseek" else "OPENAI_API_KEY"
    if not env.get(key_name, "").strip():
        raise RuntimeError(f"Missing {key_name}; set it in your local .env file")
    default_model = "deepseek-flash" if provider == "deepseek" else "gpt-4o-mini"
    model = env.get("LLM_MODEL") or env.get("OPENAI_MODEL") or default_model

    base = Path(__file__).resolve().parent
    credentials_path = base / env.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_path = base / env.get("GMAIL_TOKEN_FILE", "token.json")
    scopes = [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/spreadsheets",
    ]

    if not credentials_path.exists():
        raise RuntimeError(
            f"Missing OAuth credentials at {credentials_path}; follow README setup and never commit this file"
        )

    # Cache the first local OAuth authorization in the ignored token.json file.
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)  # type: ignore[arg-type]
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    gmail_service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    spreadsheet_id = env.get("SPREADSHEET_ID", "")
    if not spreadsheet_id:
        raise RuntimeError("Missing SPREADSHEET_ID")

    # Disable SDK retries so they do not multiply the application retry budget.
    openai_client = OpenAI(
        api_key=env[key_name],
        base_url="https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com/v1",
        max_retries=0,
        timeout=60,
    )

    state = StateStore(base / env.get("STATE_FILE", "state/bot_state.json"))
    gmail = GmailClient(gmail_service)
    sheet = ConfigSheet(sheets_service, spreadsheet_id)
    # Use real exponential backoff in production.
    knowledge = load_knowledge()
    llm = LLMClient(
        openai_client,
        model,
        provider=provider,
        sleep=time.sleep,
        # Pass loaded source names so production rejects fabricated references.
        known_sources=knowledge.files,
    )
    return {
        "gmail": gmail,
        "sheet": sheet,
        "llm": llm,
        "state": state,
        "knowledge": knowledge,
        "self_email": env.get("SUPPORT_EMAIL", "support@brewlab.example"),
    }


def run_loop(poll_interval: int = DEFAULT_POLL_INTERVAL, max_cycles: Optional[int] = None) -> None:
    """Run the polling loop, once every 60 seconds by default."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    runtime = build_runtime()
    orchestrator = Orchestrator(**runtime)
    cycles = 0
    while True:
        try:
            orchestrator.poll_once()
        except Exception:  # noqa: BLE001 - an unexpected cycle failure must not terminate the worker
            logger.exception("Unhandled cycle error; continuing with the next poll")
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        try:
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            logger.info("Interrupted; shutting down")
            break


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    from dotenv import load_dotenv

    # Read the environment file before resolving command-line defaults.
    load_dotenv()

    parser = argparse.ArgumentParser(description="BrewLab AI email support bot")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit")
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=int(os.environ.get("POLL_INTERVAL", DEFAULT_POLL_INTERVAL)),
        help="Polling interval in seconds (default: 60)",
    )
    args = parser.parse_args(argv)
    run_loop(poll_interval=args.poll_interval, max_cycles=1 if args.once else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
