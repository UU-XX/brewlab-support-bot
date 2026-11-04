"""Regression tests for transport errors, persisted intent and restart recovery.

Use unittest and simulated Gmail, model and Sheets clients; no live services."""

from __future__ import annotations

import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from batching import Batch
from config_sheet import ConfigSheet, Settings
from gmail_client import GmailClient
from llm import HANDOFF_REPLY_EN, HANDOFF_REPLY_ZH, LLMClient, LLMError, LLMResult
from main import Orchestrator
from retry_utils import (
    AmbiguousOutcomeError,
    NonRetryableError,
    RetryExhaustedError,
    call_with_retry,
    classify_write_error,
)
from state_store import (
    EFFECT_DRAFT,
    EFFECT_FORWARD,
    EFFECT_INTENT_LABEL,
    EFFECT_LABEL,
    EFFECT_LOG,
    EFFECT_SEND,
    STATUS_AMBIGUOUS,
    STATUS_PENDING,
    StateStore,
)
from tests._mocks import (
    BadRequestError,
    FakeGmail,
    FakeLLM,
    FakeSheet,
    default_settings,
    make_message,
)
from tests.test_main_flow import WED_10AM, OrchestratorTestBase, build_email_raw


# ---------------------------------------------------------------------------
# 1. Read/write retry classification and transport failures.
# ---------------------------------------------------------------------------


class TransportClassificationTests(unittest.TestCase):
    """Transport timeouts and disconnects become uncertain write outcomes."""

    def setUp(self) -> None:
        self.sentinel_attempts = 0

    def _timeout(self, message="timed out"):
        # Match exception names used by httplib2, urllib3 and the OpenAI-compatible SDK.
        return type("ReadTimeoutError", (OSError,), {})(message)

    def test_read_timeout_is_ambiguous(self):
        self.assertIsInstance(classify_write_error(self._timeout(), "send"), AmbiguousOutcomeError)

    def test_connection_reset_is_ambiguous(self):
        exc = ConnectionResetError(104, "Connection reset by peer")
        self.assertIsInstance(classify_write_error(exc, "draft"), AmbiguousOutcomeError)

    def test_http_500_is_ambiguous_for_write(self):
        class HttpError(Exception):
            status_code = 503

        self.assertIsInstance(classify_write_error(HttpError("boom"), "append"), AmbiguousOutcomeError)

    def test_http_400_is_not_ambiguous(self):
        class HttpError(Exception):
            status_code = 400

        exc = HttpError("bad request")
        self.assertIs(classify_write_error(exc, "send"), exc)

    def test_write_retry_does_not_repeat_transport_failure(self):
        """A write timeout permits only one submission, never an automatic resend."""
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            raise self._timeout()

        with self.assertRaises(AmbiguousOutcomeError):
            call_with_retry(flaky, operation="send message", write=True, sleep=lambda _s: None)
        self.assertEqual(attempts["n"], 1)
    def test_read_retry_still_works(self):
        """The same transport failure on a read allows two backoff retries."""
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise self._timeout()
            return "ok"

        self.assertEqual(
            call_with_retry(flaky, operation="read thread", write=False, sleep=lambda _s: None), "ok"
        )
        self.assertEqual(attempts["n"], 3)

    def test_write_retry_on_explicit_4xx(self):
        """An explicit client rejection can use the bounded retry path."""
        attempts = {"n": 0}

        class HttpError(Exception):
            status_code = 429

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise HttpError("rate limited")
            return "ok"

        self.assertEqual(
            call_with_retry(flaky, operation="append log", write=True, sleep=lambda _s: None), "ok"
        )
        self.assertEqual(attempts["n"], 2)


# ---------------------------------------------------------------------------
# 2. Gmail and Sheets transport classification.
# ---------------------------------------------------------------------------


class RealTransportMappingTests(unittest.TestCase):
    """Inject transport failures through a mock Google API call chain."""

    def _service_with(self, exc):
        from unittest import mock

        service = mock.Mock()
        service.users.return_value.messages.return_value.send.return_value.execute.side_effect = exc
        return service

    def test_gmail_send_timeout_becomes_ambiguous(self):
        from unittest import mock

        service = self._service_with(ConnectionResetError("Connection reset by peer"))
        client = GmailClient(service, sleep=lambda _s: None)
        with self.assertRaises(AmbiguousOutcomeError):
            client.send_reply(thread_id="t1", to="a@b.com", subject="Re: x", body="hi")
        # Submit once without blindly resending.
        self.assertEqual(service.users.return_value.messages.return_value.send.call_count, 1)

    def test_gmail_send_retries_on_explicit_4xx(self):
        from unittest import mock

        service = mock.Mock()
        send = service.users.return_value.messages.return_value.send
        # Raise a real exception object so the adapter reads its HTTP status.
        error = RuntimeError("The server explicitly rejected this send")
        error.resp = mock.Mock(status=400)

        def execute():
            if send.call_count < 3:
                raise error
            return {"id": "sent-1"}

        send.return_value.execute.side_effect = execute
        client = GmailClient(service, sleep=lambda _s: None)
        self.assertEqual(
            client.send_reply(thread_id="t1", to="a@b.com", subject="Re: x", body="hi"), "sent-1"
        )
        self.assertEqual(send.call_count, 3)

    def test_forward_preserves_each_original_and_its_attachment(self):
        """Recover two original messages from forwarded MIME and verify attachment bytes."""
        import base64
        from email import policy
        from email.message import EmailMessage
        from email.parser import BytesParser

        original = EmailMessage()
        original.set_content("See attached photo")
        original.add_attachment(b"\x89PNG\x00\xff", maintype="image", subtype="png", filename="damage.png")
        originals = [b"Subject: first\r\n\r\nPrice?", original.as_bytes()]
        service = mock.Mock()
        messages = service.users.return_value.messages.return_value
        messages.send.return_value.execute.return_value = {"id": "confirmed-forward"}
        messages.get.return_value.execute.side_effect = [
            {"raw": base64.urlsafe_b64encode(data).decode()} for data in originals
        ]
        GmailClient(service, sleep=lambda seconds: None).forward_message(["a", "b"], "team@example.com")
        messages.send.assert_called_once()
        outgoing = base64.urlsafe_b64decode(messages.send.call_args.kwargs["body"]["raw"])
        envelope = BytesParser(policy=policy.default).parsebytes(outgoing)
        self.assertEqual(envelope["To"], "team@example.com")
        self.assertEqual([part.get_payload(decode=True) for part in envelope.iter_attachments()], originals)

    def test_committed_send_with_damaged_receipt_is_not_repeated(self):
        """A decode error after server acceptance must not cause a second submission."""
        import json

        delivered = []
        def commit_then_decode():
            delivered.append("sent")
            return json.loads("{")

        service = mock.Mock()
        service.users.return_value.messages.return_value.send.return_value.execute.side_effect = commit_then_decode
        client = GmailClient(service, sleep=lambda seconds: None)
        with self.assertRaises(AmbiguousOutcomeError):
            client.send_reply(thread_id="t1", to="a@example.com", subject="Re: price", body="USD 79")
        self.assertEqual(delivered, ["sent"])

    def test_null_receipt_after_submission_is_ambiguous(self):
        """Empty send and draft receipts are uncertain, not confirmed success or rejection."""
        for response in (None, {}, {"id": ""}):
            service = mock.Mock()
            client = GmailClient(service, sleep=lambda seconds: None)
            users = service.users.return_value
            for create, operation in ((users.drafts.return_value.create, client.create_reply_draft),
                                      (users.messages.return_value.send, client.send_reply)):
                create.return_value.execute.return_value = response
                with self.assertRaises(AmbiguousOutcomeError):
                    operation(thread_id="t1", to="alice@example.com", subject="Re: price", body="Hello")
                create.assert_called_once()

    def test_sheet_append_timeout_becomes_ambiguous(self):
        from unittest import mock

        service = mock.Mock()
        service.spreadsheets.return_value.values.return_value.append.return_value.execute.side_effect = (
            TimeoutError("timed out")
        )
        sheet = ConfigSheet(service, "sheet-id", sleep=lambda _s: None)
        with self.assertRaises(AmbiguousOutcomeError):
            sheet.append_log_row(["t", "s", "pricing", "draft", "no", "x", 1, "", "th", "m"])
        self.assertEqual(
            service.spreadsheets.return_value.values.return_value.append.call_count, 1
        )

    def test_sheet_pagination_uses_next_range(self):
        """Pagination must advance the range instead of reading it repeatedly."""
        from unittest import mock

        service = mock.Mock()
        values = service.spreadsheets.return_value.values.return_value
        values.get.return_value.execute.side_effect = [
            {"values": [["mode", "draft"]], "nextRange": "settings!A1001:B1500"},
            {"values": [["tone", "friendly"]]},
        ]
        sheet = ConfigSheet(service, "sheet-id", sleep=lambda _s: None)
        self.assertEqual(len(sheet._get_values("settings!A1:B")), 2)
        ranges = [c.kwargs["range"] for c in values.get.call_args_list]
        self.assertEqual(ranges, ["settings!A1:B", "settings!A1001:B1500"])


# ---------------------------------------------------------------------------
# 3. Model constraints and error contracts.
# ---------------------------------------------------------------------------


class SafetyRuleTests(unittest.TestCase):
    def _result(self, **overrides):
        base = dict(
            intent="pricing",
            language="en",
            confidence=0.95,
            needs_human=False,
            knowledge_used=["pricing.md"],
            reply="The kit is $79.",
        )
        base.update(overrides)
        return LLMResult(**base)

    def test_missing_knowledge_replaces_invented_answer(self):
        """Replace unsupported content with the fixed handoff sentence."""
        from llm import apply_safety_rules

        result = apply_safety_rules(
            self._result(knowledge_used=[], reply="I think it is $50.")
        )
        self.assertTrue(result.needs_human)
        self.assertEqual(result.reply, HANDOFF_REPLY_EN)
        self.assertNotIn("$50", result.reply)

    def test_missing_knowledge_chinese(self):
        from llm import apply_safety_rules

        result = apply_safety_rules(
            self._result(language="zh", knowledge_used=[], reply="\u4ef7\u683c\u5927\u6982\u662f 500 \u5143\u3002")
        )
        self.assertEqual(result.reply, HANDOFF_REPLY_ZH)

    def test_handoff_keeps_grounded_explanation(self):
        from llm import apply_safety_rules

        result = apply_safety_rules(self._result(needs_human=True, reply="Refunds need review."))
        self.assertIn("Refunds need review.", result.reply)
        self.assertIn(HANDOFF_REPLY_EN, result.reply)

    def test_chitchat_without_knowledge_allowed(self):
        from llm import apply_safety_rules

        result = apply_safety_rules(
            self._result(intent="chitchat", knowledge_used=[], reply="Thanks so much!")
        )
        self.assertFalse(result.needs_human)
        self.assertEqual(result.reply, "Thanks so much!")


class LLMErrorContractTests(unittest.TestCase):
    def _client(self, side_effect):
        from unittest import mock

        openai_client = mock.Mock()
        openai_client.chat.completions.create.side_effect = side_effect
        return LLMClient(openai_client, "gpt-test", sleep=lambda _s: None)

    def test_auth_error_wrapped_as_llm_error(self):
        client = self._client(BadRequestError("bad key"))
        with self.assertRaises(LLMError) as ctx:
            client.generate(system_prompt="sys", history=[], user_content="q")
        self.assertTrue(ctx.exception.non_retryable)

    def test_non_retryable_error_wrapped(self):
        client = self._client(NonRetryableError("permanent"))
        with self.assertRaises(LLMError):
            client.generate(system_prompt="sys", history=[], user_content="q")

    def test_production_default_sleep_is_real(self):
        """Production defaults must actually wait between retries."""
        self.assertIs(LLMClient(None, "m")._sleep, time.sleep)


# ---------------------------------------------------------------------------
# 4. Orchestration, persisted intent, recovery and finalization.
# ---------------------------------------------------------------------------


class ReworkBase(OrchestratorTestBase):
    def setUp(self) -> None:
        # Reuse shared fake adapters and temporary state for complete workflow tests.
        super().setUp()
        self.now = WED_10AM

    def add_raw(self, raw):
        raw.setdefault("internal_date", int(self.now * 1000))
        self.gmail.messages.append({"id": raw["id"], "raw": raw})

    def make_orchestrator(self, **kwargs):
        """Each restart reloads the same state file and restores persisted batches."""
        orch = OrchestratorTestBase.make_orchestrator(self, **kwargs)
        orch._now = lambda: self.now
        return orch


class PendingIntentTests(ReworkBase):
    def test_pending_written_before_remote_call(self):
        """Pending intent must be on disk before a remote effect."""
        observed = {}
        real_send = self.gmail.send_reply
        self.sheet.settings.mode = "live"
        self.add_raw(build_email_raw("m1"))
        orch = self.make_orchestrator()

        def spy(**kwargs):
            observed["status"] = self.state.effect_status("m1", EFFECT_SEND)
            return real_send(**kwargs)

        self.gmail.send_reply = spy  # type: ignore[assignment]
        self.poll(orch)
        self.assertEqual(observed["status"], STATUS_PENDING)
        self.assertTrue(self.state.is_effect_done("m1", EFFECT_SEND))

    def test_crash_after_send_before_done_not_resent(self):
        """A remote success with unsaved local completion must reconcile rather than resend."""
        self.sheet.settings.mode = "live"
        self.add_raw(build_email_raw("m1"))
        self.state.begin_effect("m1", EFFECT_SEND, thread_id="thread-1")
        # Simulate an existing sent reply in the thread.
        from gmail_client import ThreadMessage

        reply_body = self.llm.result.reply
        self.gmail.threads["thread-1"] = [
            ThreadMessage("m1", "Alice <alice@example.com>", "", "Pricing?", "how much?"),
            ThreadMessage("out-1", "support@brewlab.example", "", "Re: Pricing?", reply_body),
        ]
        orch = self.make_orchestrator()
        self.poll(orch)
        self.assertEqual([item["to"] for item in self.gmail.sent], ["admin@brewlab.example"], "Alert only the administrator; do not resend the customer reply")
        self.assertEqual(self.state.effect_status("m1", EFFECT_SEND), STATUS_AMBIGUOUS)
        self.assertIn("m1", self.state.pending_compensation())

    def test_pending_without_proof_is_paused(self):
        self.sheet.settings.mode = "live"
        self.add_raw(build_email_raw("m1"))
        self.state.begin_effect("m1", EFFECT_SEND, thread_id="thread-1")
        orch = self.make_orchestrator()
        self.poll(orch)
        self.assertEqual([item["to"] for item in self.gmail.sent], ["admin@brewlab.example"])
        self.assertEqual(self.state.effect_status("m1", EFFECT_SEND), STATUS_AMBIGUOUS)
        self.assertIn("m1", self.state.pending_compensation())

    def test_draft_pending_without_proof_paused(self):
        self.add_raw(build_email_raw("m1"))
        self.state.begin_effect("m1", EFFECT_DRAFT, thread_id="thread-1")
        orch = self.make_orchestrator()
        self.poll(orch)
        self.assertEqual(self.gmail.drafts, [])
        self.assertEqual(self.state.effect_status("m1", EFFECT_DRAFT), STATUS_AMBIGUOUS)


class BatchResumeTests(ReworkBase):
    def test_pending_batch_survives_restart(self):
        """Restore a waiting batch without losing messages or repeating model generation."""
        self.sheet.settings.mode = "live"
        self.add_raw(build_email_raw("m1", body="question one"))
        self.state.save_batch(
            "alice@example.com",
            {
                "sender_email": "alice@example.com",
                "reply_key": "m1",
                "created_at": self.now,
                "messages": [
                    {
                        "message_id": "m1",
                        "thread_id": "thread-1",
                        "sender": "Alice <alice@example.com>",
                        "subject": "Pricing?",
                        "body": "question one",
                        "received_at": self.now,
                        "rfc_message_id": "<m1@mail.example>",
                        "references": "",
                    }
                ],
            },
        )
        orchestrator = self.make_orchestrator()
        self.assertIn("alice@example.com", orchestrator.batcher.pending_senders())

    def test_reply_key_stays_stable_after_new_message(self):
        """New mail must not change a frozen batch's effect key."""
        self.add_raw(build_email_raw("m1", body="first question"))
        orch = self.make_orchestrator(quiet_seconds=120)
        self.poll(orch)
        self.assertIn("alice@example.com", orch.batcher.pending_senders())
        reply_key = orch._meta["alice@example.com"]["reply_key"]
        self.assertEqual(reply_key, "m1")
        # Another arrival within the quiet window must not change the stable batch key.
        self.now += 30
        self.add_raw(build_email_raw("m2", body="second question"))
        self.poll(orch)
        self.assertEqual(orch._meta["alice@example.com"]["reply_key"], "m1")
        self.assertEqual(len(orch.batcher.get("alice@example.com").messages), 2)

    def test_internal_date_orders_messages(self):
        """Sort reversed API results by arrival time and retain the actual latest thread."""
        self.sheet.settings.mode = "live"
        older = build_email_raw("m1", thread="thread-old", body="older question")
        newer = build_email_raw("m2", thread="thread-new", body="newer question")
        # Forty seconds separates the messages within this test's sixty-second window.
        older["internal_date"] = int((self.now - 50) * 1000)
        newer["internal_date"] = int((self.now - 10) * 1000)
        # Insert the newer message first to simulate reverse ordering.
        self.add_raw(newer)
        self.add_raw(older)
        orch = self.make_orchestrator(quiet_seconds=60)
        self.poll(orch)
        # Restore the waiting batch from disk after its in-memory instance is discarded.
        orch = self.make_orchestrator(quiet_seconds=60)
        batch = orch.batcher.get("alice@example.com")
        self.assertEqual([m.message_id for m in batch.messages], ["m1", "m2"])
        self.assertEqual(batch.latest_thread_id, "thread-new")

    def test_backlog_with_gap_becomes_two_replies(self):
        """Backlog separated by more than two minutes becomes separate reply batches."""
        self.sheet.settings.mode = "live"
        for key, age in (("new", 130), ("old", 300)):
            raw = build_email_raw(key, thread=key)
            raw["internal_date"] = int((self.now - age) * 1000)
            self.add_raw(raw)
        self.poll(self.make_orchestrator(quiet_seconds=120))
        self.assertEqual([item["thread_id"] for item in self.gmail.sent], ["old", "new"])
        self.poll(self.make_orchestrator(quiet_seconds=120))
        self.assertEqual(len(self.gmail.sent), 2)
        self.assertEqual(len(self.sheet.log_rows), 2)

    def test_older_support_label_after_restart_forms_separate_batch(self):
        """A late-labeled old message must not join a batch five minutes newer."""
        self.sheet.settings.mode = "live"
        for key, age in (("new", 0), ("old", 300)):
            raw = build_email_raw(key, thread=key)
            raw["internal_date"] = int((self.now - age) * 1000)
            self.add_raw(raw)
            self.poll(self.make_orchestrator(quiet_seconds=120))
        self.assertEqual([s["thread_id"] for s in self.gmail.sent], ["old"])
        self.now += 120
        self.poll(self.make_orchestrator(quiet_seconds=120))
        self.assertEqual([s["thread_id"] for s in self.gmail.sent], ["old", "new"])
        self.assertEqual(len(self.sheet.log_rows), 2)

    def test_unread_message_holds_quiet_window_until_recovered(self):
        """Incomplete intake preserves earlier arrivals and reunites them after recovery."""
        from retry_utils import RetryExhaustedError

        first = build_email_raw("m1")
        first["internal_date"] = int(self.now * 1000)
        self.add_raw(first)
        orch = self.make_orchestrator(quiet_seconds=120)
        self.poll(orch)
        second = build_email_raw("m2")
        second["internal_date"] = int((self.now + 100) * 1000)
        self.add_raw(second)
        self.now += 130
        for name in ("list_new_support_messages", "get_message"):
            with mock.patch.object(self.gmail, name, side_effect=RetryExhaustedError(3, TimeoutError())):
                self.poll(orch)
            self.assertEqual(self.gmail.drafts, [])
        self.poll(orch)
        self.assertEqual(self.gmail.drafts, [])
        self.now += 90
        self.poll(orch)
        self.assertEqual(len(self.gmail.drafts), 1)
        self.assertEqual(len(self.sheet.log_rows), 2)


class FinalizeTests(ReworkBase):
    def test_log_commit_then_local_write_failure_does_not_append_twice(self):
        """Recover a remote log append by message ID after a failed local completion write."""
        self.add_raw(build_email_raw("m1"))
        orch = self.make_orchestrator()
        original = self.state.mark_effect_done

        def save_done(mid, effect, **meta):
            if effect == "log":
                raise OSError("Simulated local persistence failure")
            return original(mid, effect, **meta)

        with mock.patch.object(self.state, "mark_effect_done", side_effect=save_done):
            with self.assertRaises(OSError):
                self.poll(orch)
        self.assertEqual(len(self.sheet.log_rows), 1)
        self.poll(self.make_orchestrator())
        self.assertEqual(len(self.sheet.log_rows), 1)
        self.assertTrue(self.state.is_processed("m1"))
        self.assertEqual(len(self.gmail.drafts), 1)

    def test_decision_saved_before_send_respects_new_draft_mode_in_log(self):
        """A restart after decision persistence honors draft mode for both execution and logging."""
        self.sheet.settings.mode = "live"
        self.add_raw(build_email_raw("m1"))
        orch = self.make_orchestrator()
        with mock.patch.object(orch, "_do_send", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.poll(orch)
        self.sheet.settings.mode = "draft"
        self.poll(self.make_orchestrator())
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual(len(self.gmail.drafts), 1)
        self.assertEqual(self.sheet.log_rows[0][3], "draft")

    def test_log_failure_resumes_without_duplicate_log(self):
        """Retain failed finalization and retry without duplicating acknowledged log rows."""
        self.add_raw(build_email_raw("m1"))
        self.sheet.fail_log = True
        orch = self.make_orchestrator()
        self.poll(orch)
        self.assertEqual(self.sheet.log_rows, [])
        # Create the draft only once.
        self.assertEqual(len(self.gmail.drafts), 1)

        self.sheet.fail_log = False
        self.poll(orch)
        self.assertEqual(len(self.sheet.log_rows), 1)
        self.assertEqual(len(self.gmail.drafts), 1, "Recovery must not create another draft")
        self.assertTrue(self.state.is_processed("m1"))

    def test_ambiguous_log_never_rewritten(self):
        """Uncertain log appends require reconciliation rather than automatic repetition."""
        self.add_raw(build_email_raw("m1"))
        self.sheet.ambiguous_log = True
        orch = self.make_orchestrator()
        self.poll(orch)
        calls = {"n": 0}
        original = self.sheet.append_log_row

        def counting(row):
            calls["n"] += 1
            return original(row)

        self.sheet.append_log_row = counting  # type: ignore[assignment]
        self.poll(orch)
        self.assertEqual(calls["n"], 0, "An ambiguous append must not be repeated")
        self.assertIn("m1", self.state.pending_compensation())

    def test_label_failure_retried_next_round(self):
        self.add_raw(build_email_raw("m1"))
        self.gmail.fail_labels = True
        orch = self.make_orchestrator()
        self.poll(orch)
        self.assertNotIn("bot-processed", self.gmail.labels.get("m1", []))
        self.assertNotIn("intent-pricing", self.gmail.labels.get("m1", []))
        self.assertFalse(self.state.is_processed("m1"))
        self.assertIn("label failed", self.sheet.log_rows[0][7])
        self.assertEqual([item["to"] for item in self.gmail.sent], ["admin@brewlab.example"])

        self.gmail.fail_labels = False
        self.poll(orch)
        self.assertIn("intent-pricing", self.gmail.labels["m1"])
        self.assertTrue(self.state.is_processed("m1"))
        self.assertEqual(len(self.sheet.log_rows), 1, "The log must not contain duplicates")

    def test_each_message_gets_own_log_row(self):
        """Three source messages each receive their own labels and log row."""
        for idx in range(1, 4):
            self.add_raw(build_email_raw(f"m{idx}", body=f"question {idx}"))
        orch = self.make_orchestrator(quiet_seconds=0)
        self.poll(orch)
        self.assertEqual(len(self.sheet.log_rows), 3)
        self.assertEqual(len(self.gmail.drafts), 1)
        for idx in range(1, 4):
            self.assertIn("bot-processed", self.gmail.labels[f"m{idx}"])


class FailureHandlingTests(ReworkBase):
    def test_missing_colleague_pauses_before_customer_action(self):
        """Pause without a colleague address; perform one complete handoff after configuration."""
        self.sheet.settings.mode = "live"
        self.sheet.settings.colleague_email = ""
        self.add_raw(build_email_raw("m1", body="\u627e\u771f\u4eba"))
        self.poll(self.make_orchestrator())
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual(self.llm.calls, [])
        self.assertFalse(self.state.is_processed("m1"))
        self.sheet.settings.colleague_email = "desk@example.com"
        self.poll(self.make_orchestrator())
        self.assertEqual(len(self.gmail.forwarded), 1)
        self.assertIn("needs-human", self.gmail.labels["m1"])
        self.assertEqual(len(self.sheet.log_rows), 1)

    def test_attachment_extends_quiet_window_and_hands_off(self):
        """An attachment-only message still waits for silence before the complete batch is handed off."""
        first = build_email_raw("m1")
        first["internal_date"] = int(self.now * 1000)
        self.add_raw(first)
        orch = self.make_orchestrator(quiet_seconds=120)
        self.poll(orch)
        attachment = build_email_raw("m2", body="", thread="attachment")
        attachment["internal_date"] = int((self.now + 90) * 1000)
        self.add_raw(attachment)
        self.now += 130
        self.poll(orch)
        self.assertEqual(self.gmail.drafts, [])
        self.now += 90
        self.poll(orch)
        self.assertEqual(len(self.gmail.drafts), 1)
        self.assertEqual(self.gmail.drafts[0]["thread_id"], "attachment")
        self.assertEqual(self.llm.calls, [])
        self.assertEqual([row[2] for row in self.sheet.log_rows], ["handoff", "handoff"])

    def test_forward_failure_records_compensation_and_alerts(self):
        """Forwarding failure remains visible and does not stop other finalization."""
        self.gmail.fail_forward = True
        self.add_raw(build_email_raw("m1", body="I want a refund, this is a complaint"))
        orch = self.make_orchestrator()
        result = self.poll(orch)[0]
        self.assertTrue(result.handoff)
        self.assertTrue(result.error)
        comp = self.state.pending_compensation()
        self.assertTrue(any("forward" in effects for effects in comp.values()))
        alert = [s for s in self.gmail.sent if s.get("to") == "admin@brewlab.example"]
        self.assertEqual(len(alert), 1, "A forwarding failure must raise an alert")
        # Per-message finalization continues.
        self.assertIn("bot-processed", self.gmail.labels["m1"])
        self.assertEqual(len(self.sheet.log_rows), 1)

    def test_llm_non_retryable_goes_fallback(self):
        """Authentication failure still uses fallback handling rather than bypassing it."""
        self.llm.error = LLMError("bad key", non_retryable=True)
        self.add_raw(build_email_raw("m1", body="Hello, price?"))
        orch = self.make_orchestrator()
        self.poll(orch)
        self.assertEqual(len(self.gmail.drafts), 1)
        self.assertIn("We have received your email", self.gmail.drafts[0]["body"])
        alerts = [s for s in self.gmail.sent if s.get("to") == "admin@brewlab.example"]
        self.assertEqual(len(alerts), 1)

    def test_send_failure_creates_fallback_and_alert(self):
        self.sheet.settings.mode = "live"
        # Fail only the customer send; a separate test covers a total Gmail outage.
        self.gmail.send_reply = mock.Mock(side_effect=RuntimeError("Customer send failed"))
        self.add_raw(build_email_raw("m1"))
        orch = self.make_orchestrator()
        self.poll(orch)
        self.assertEqual(len(self.gmail.drafts), 1, "A failed send must create a fallback draft")
        self.assertEqual(self.sheet.log_rows[0][5], self.gmail.drafts[0]["body"])
        self.assertEqual([s.get("to") for s in self.gmail.sent], ["admin@brewlab.example"])

    def test_restart_after_fallback_draft_completes_alert(self):
        """A crash between fallback draft and alert must resume only the missing alert."""
        self.llm.error = LLMError("Model outage")
        self.add_raw(build_email_raw("m1"))
        orch = self.make_orchestrator()
        with mock.patch.object(orch, "_send_admin_alert", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.poll(orch)
        self.assertEqual(len(self.gmail.drafts), 1)
        self.poll(self.make_orchestrator())
        self.assertEqual(len(self.gmail.drafts), 1)
        self.assertEqual([s["to"] for s in self.gmail.sent], ["admin@brewlab.example"])
        self.assertEqual(self.sheet.log_rows[0][5], self.gmail.drafts[0]["body"])

    def test_read_failure_alerts_with_no_customer_address(self):
        """An intake failure alerts the administrator without drafting to an unknown recipient."""
        from retry_utils import RetryExhaustedError

        orch = self.make_orchestrator()
        error = RetryExhaustedError(3, RuntimeError("Intake failed"))
        with mock.patch.object(self.gmail, "list_new_support_messages", side_effect=error):
            self.poll(orch)
            self.poll(orch)
        self.add_raw(build_email_raw("m1"))
        with mock.patch.object(self.gmail, "get_message", side_effect=error):
            self.poll(orch)
        self.assertEqual([s["to"] for s in self.gmail.sent], ["admin@brewlab.example"] * 2)
        self.assertEqual(self.gmail.drafts, [])
        self.poll(self.make_orchestrator())
        self.assertTrue(self.state.is_processed("m1"))
        self.assertNotIn("read", self.state.pending_compensation().get("m1", {}))

    def test_model_from_sheet_applied(self):
        """The Sheet model setting controls the actual model adapter."""
        self.sheet.settings.model = "gpt-4.1-mini"
        self.add_raw(build_email_raw("m1"))
        orch = self.make_orchestrator()
        self.poll(orch)
        self.assertEqual(self.llm.model, "gpt-4.1-mini")

    def test_forward_failure_on_llm_failure_still_alerts(self):
        self.llm.error = LLMError("boom")
        self.gmail.fail_forward = True
        self.add_raw(build_email_raw("m1", body="\u6295\u8bc9\uff0c\u6211\u8981\u9000\u6b3e"))
        orch = self.make_orchestrator()
        self.poll(orch)
        alerts = [s for s in self.gmail.sent if s.get("to") == "admin@brewlab.example"]
        self.assertEqual(len(alerts), 1)
        self.assertTrue(self.state.pending_compensation())


class IntergrationHelpersTests(unittest.TestCase):
    def test_settings_default_model_exists(self):
        self.assertTrue(Settings().model)

    def test_batch_latest_is_max_received_at(self):
        m1 = make_message("a", received_at=100)
        m2 = make_message("b", received_at=200)
        batch = Batch(sender_email="x@y.com", messages=[m2, m1])
        self.assertEqual(batch.latest.message_id, "b", "Choose the latest arrival even when a batch is not sorted")


if __name__ == "__main__":
    unittest.main()
