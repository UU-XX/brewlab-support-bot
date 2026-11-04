"""Core workflow tests with unittest and simulated provider adapters."""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from config_sheet import Settings, is_within_active_window, match_handoff_keyword
from llm import HANDOFF_REPLY_EN, HANDOFF_REPLY_ZH, LLMError, LLMResult
from main import Orchestrator
from retry_utils import AmbiguousOutcomeError, RetryExhaustedError, call_with_retry
from state_store import (
    EFFECT_DRAFT,
    EFFECT_LOG,
    EFFECT_SEND,
    STATUS_AMBIGUOUS,
    StateStore,
)
from tests._mocks import (
    FakeGmail,
    FakeLLM,
    FakeSheet,
    default_settings,
    make_message,
)

TZ = ZoneInfo("America/Los_Angeles")
# Wednesday at 10:00 in Los Angeles is inside operating hours.
WED_10AM = datetime(2026, 3, 4, 10, 0, tzinfo=TZ).timestamp()


def build_email_raw(message_id: str, *, sender="Alice <alice@example.com>", thread="thread-1",
                    subject="Pricing?", body="How much is the Pour-Over Starter Kit?",
                    message_id_header="<m1@mail.example>") -> dict:
    return {
        "id": message_id,
        "threadId": thread,
        "from": sender,
        "subject": subject,
        "body": body,
        "message_id": message_id_header,
        "references": "",
    }


class OrchestratorTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = StateStore(Path(self.tmp.name) / "state.json")
        self.gmail = FakeGmail()
        self.sheet = FakeSheet(default_settings())
        self.llm = FakeLLM()
        self.now = WED_10AM

    def make_orchestrator(self, **kwargs) -> Orchestrator:
        # Reload state from disk on each construction to model a real process restart.
        state = StateStore(Path(self.tmp.name) / "state.json")
        self.state = state
        return Orchestrator(
            gmail=self.gmail,
            sheet=self.sheet,
            llm=self.llm,
            state=state,
            quiet_seconds=kwargs.pop("quiet_seconds", 0),
            sleep=lambda _s: None,
            now_fn=lambda: self.now,
            settings=self.sheet.settings,
            **kwargs,
        )

    def add_raw_email(self, raw: dict) -> None:
        self.gmail.messages.append({"id": raw["id"], "raw": raw})

    def poll(self, orch: Orchestrator):
        return orch.poll_once(now=self.now)


class NormalFlowTests(OrchestratorTestBase):
    def test_pricing_normal_draft_mode_creates_draft_only(self):
        """A normal draft inquiry creates a draft, labels and logs without a customer send."""
        self.add_raw_email(build_email_raw("m1"))
        orch = self.make_orchestrator()
        results = self.poll(orch)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].intent, "pricing")
        self.assertFalse(results[0].handoff)
        self.assertEqual(len(self.gmail.drafts), 1)
        self.assertEqual(self.gmail.sent, [], "Draft mode must not send a customer reply")
        labels = self.gmail.labels["m1"]
        self.assertIn("intent-pricing", labels)
        self.assertIn("bot-processed", labels)
        self.assertEqual(len(self.sheet.log_rows), 1)
        self.assertEqual(len(self.sheet.log_rows[0]), 10)
        self.assertTrue(self.state.is_processed("m1"))

    def test_live_mode_sends_reply_in_thread(self):
        """Live mode sends a reply with the original thread ID."""
        self.sheet.settings.mode = "live"
        self.add_raw_email(build_email_raw("m1"))
        orch = self.make_orchestrator()
        self.poll(orch)

        self.assertEqual(len(self.gmail.sent), 1)
        self.assertEqual(self.gmail.sent[0]["thread_id"], "thread-1")
        self.assertEqual(self.gmail.drafts, [])

    def test_off_mode_does_nothing(self):
        """Off mode performs no Gmail reads, writes or log appends."""
        self.sheet.settings.mode = "off"
        self.add_raw_email(build_email_raw("m1"))
        orch = self.make_orchestrator()
        results = self.poll(orch)

        self.assertEqual(results, [])
        self.assertNotIn("list", self.gmail.call_counts)
        self.assertEqual(self.sheet.log_rows, [])

    def test_outside_active_window_skips(self):
        """Outside operating hours, do not process, label or log messages."""
        self.sheet.settings.active_hours = "09:00-11:00"
        self.now = datetime(2026, 3, 4, 3, 0, tzinfo=TZ).timestamp()  # 03:00
        self.add_raw_email(build_email_raw("m1"))
        orch = self.make_orchestrator()
        results = self.poll(orch)

        self.assertEqual(results, [])
        self.assertEqual(self.sheet.log_rows, [])
        self.assertNotIn("m1", self.gmail.labels)

    def test_weekend_outside_active_days(self):
        """Skip weekends excluded by active_days."""
        self.sheet.settings.active_days = "mon,tue,wed,thu,fri"
        self.now = datetime(2026, 3, 7, 10, 0, tzinfo=TZ).timestamp()  # Saturday.
        self.add_raw_email(build_email_raw("m1"))
        orch = self.make_orchestrator()
        self.assertEqual(self.poll(orch), [])
        self.assertEqual(self.sheet.log_rows, [])


class BatchingTests(OrchestratorTestBase):
    def test_three_messages_merged_into_one_reply(self):
        """Three messages from one sender produce one reply after the quiet window."""
        self.sheet.settings.mode = "live"
        for idx in range(1, 4):
            self.add_raw_email(
                build_email_raw(f"m{idx}", body=f"question {idx}",
                                message_id_header=f"<m{idx}@mail.example>")
            )
        orch = self.make_orchestrator(quiet_seconds=120)
        # The first poll only collects messages while waiting for silence.
        self.poll(orch)
        self.assertEqual(self.gmail.sent, [])

        # Poll again after the quiet window expires.
        self.now += 130
        self.poll(orch)
        self.assertEqual(len(self.gmail.sent), 1, "A merged batch must send only one reply")
        # The prompt contains all three source messages.
        content = self.llm.calls[0]["user_content"]
        self.assertIn("Message 1", content)
        self.assertIn("Message 3", content)
        self.assertIn("question 3", content)

    def test_cross_thread_merge_replies_latest_thread(self):
        """Cross-thread batching replies only in the latest thread while retaining both sources."""
        self.sheet.settings.mode = "live"
        m1 = make_message("m1", thread_id="thread-old", body="part one")
        m2 = make_message("m2", thread_id="thread-new", body="part two")
        orch = self.make_orchestrator(quiet_seconds=0)
        from batching import Batch

        batch = Batch(sender_email="alice@example.com", messages=[m1, m2])
        orch.process_batch(batch, self.sheet.settings)

        self.assertEqual(len(self.gmail.sent), 1)
        self.assertEqual(self.gmail.sent[0]["thread_id"], "thread-new")
        content = self.llm.calls[0]["user_content"]
        self.assertIn("thread-old", content)
        self.assertIn("thread-new", content)


class HandoffTests(OrchestratorTestBase):
    def test_keyword_handoff_forwards_colleague_in_draft_mode(self):
        """A complaint in draft mode still forwards to a colleague and adds needs-human."""
        self.add_raw_email(
            build_email_raw("m1", body="This is a complaint, I want a refund now!")
        )
        orch = self.make_orchestrator()
        results = self.poll(orch)

        self.assertTrue(results[0].handoff)
        self.assertEqual(len(self.gmail.forwarded), 1)
        self.assertEqual(self.gmail.forwarded[0]["to"], "colleague@brewlab.example")
        self.assertIn("needs-human", self.gmail.labels["m1"])
        self.assertEqual(self.gmail.sent, [], "Draft mode must still avoid a customer send")

    def test_low_confidence_triggers_handoff(self):
        """Low model confidence triggers human handoff."""
        self.llm.result = LLMResult(
            intent="pricing", language="en", confidence=0.2, needs_human=False,
            knowledge_used=["pricing.md"], reply="It's around 100 dollars.",
        )
        self.add_raw_email(build_email_raw("m1"))
        orch = self.make_orchestrator()
        results = self.poll(orch)

        self.assertTrue(results[0].handoff)
        self.assertEqual(len(self.gmail.forwarded), 1)
        # The reply must include the handoff acknowledgement.
        self.assertIn("colleague", self.gmail.drafts[0]["body"].lower())

    def test_unknown_question_never_fabricates(self):
        """An unsupported question uses the fixed handoff sentence without inventing an answer."""
        self.llm.result = LLMResult(
            intent="handoff", language="en", confidence=0.95, needs_human=True,
            knowledge_used=[], reply=HANDOFF_REPLY_EN,
        )
        self.add_raw_email(build_email_raw("m1", body="Do you run barista classes in Berlin?"))
        orch = self.make_orchestrator()
        results = self.poll(orch)

        self.assertTrue(results[0].handoff)
        self.assertIn("colleague", self.gmail.drafts[0]["body"])
        self.assertEqual(len(self.gmail.forwarded), 1)

    def test_handoff_chinese_text(self):
        """Preserve the localized Chinese handoff sentence."""
        self.llm.result = LLMResult(
            intent="handoff", language="zh", confidence=0.9, needs_human=True,
            knowledge_used=[], reply="",
        )
        self.add_raw_email(build_email_raw("m1", body="\u6211\u8981\u627e\u771f\u4eba\u5ba2\u670d"))
        orch = self.make_orchestrator()
        self.poll(orch)
        self.assertIn(HANDOFF_REPLY_ZH, self.gmail.drafts[0]["body"])


class FailureTests(OrchestratorTestBase):
    def test_llm_failure_creates_fallback_draft_and_alert(self):
        """A persistent model failure creates a fallback draft and administrator alert."""
        self.llm.error = LLMError("boom")
        self.add_raw_email(build_email_raw("m1", body="How much is the grinder?"))
        orch = self.make_orchestrator()
        results = self.poll(orch)

        self.assertEqual(len(self.gmail.drafts), 1)
        self.assertIn("We have received your email", self.gmail.drafts[0]["body"])
        alert = [s for s in self.gmail.sent if s.get("to") == "admin@brewlab.example"]
        self.assertEqual(len(alert), 1, "An administrator alert must be sent")
        # A fallback draft is not a direct customer reply.
        to_customer = [s for s in self.gmail.sent if s.get("to") == "alice@example.com"]
        self.assertEqual(to_customer, [])
        self.assertTrue(results[0].error)

    def test_llm_failure_english_vs_chinese_fallback(self):
        """Fallback language follows the customer."""
        self.llm.error = LLMError("boom")
        self.add_raw_email(build_email_raw("m1", body="\u4f60\u597d\uff0c\u8bf7\u95ee\u5496\u5561\u673a\u591a\u5c11\u94b1\uff1f"))
        orch = self.make_orchestrator()
        self.poll(orch)
        self.assertIn("\u6211\u4eec\u5df2\u6536\u5230\u60a8\u7684\u90ae\u4ef6", self.gmail.drafts[0]["body"])

    def test_gmail_persistent_failure_records_compensation(self):
        """A persistent Gmail failure records recovery work without duplicate sends."""
        self.gmail.fail_draft = True
        self.gmail.fail_send = True
        self.llm.error = LLMError("boom")
        self.add_raw_email(build_email_raw("m1"))
        orch = self.make_orchestrator()
        results = self.poll(orch)

        self.assertTrue(self.state.pending_compensation(), "Unresolved work must be recorded locally")
        self.assertIn("m1", self.state.pending_compensation())
        self.assertTrue(results[0].error)
        self.assertEqual(self.gmail.drafts, [])
        # Another poll must not duplicate the send.
        self.poll(orch)
        self.assertEqual(self.gmail.drafts, [])

    def test_send_retries_twice_then_succeeds(self):
        """Two failed attempts are followed by success on the third attempt."""
        self.sheet.settings.mode = "live"
        self.add_raw_email(build_email_raw("m1"))
        orch = self.make_orchestrator()

        real_send = self.gmail.send_reply
        attempts = {"n": 0}

        def flaky_send(**kwargs):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise RuntimeError("transient")
            return real_send(**kwargs)

        # Use the real GmailClient send_reply retry path.
        def retrying_send(**kwargs):
            return call_with_retry(
                lambda: flaky_send(**kwargs), operation="send", sleep=lambda _s: None
            )

        self.gmail.send_reply = retrying_send  # type: ignore[assignment]
        self.poll(orch)

        self.assertEqual(attempts["n"], 3, "One initial attempt plus two retries")
        self.assertEqual(len(self.gmail.sent), 1)

    def test_ambiguous_send_not_retried(self):
        """An uncertain send becomes ambiguous and must not be repeated."""
        self.sheet.settings.mode = "live"
        self.gmail.ambiguous_send = True
        self.add_raw_email(build_email_raw("m1"))
        orch = self.make_orchestrator()
        self.poll(orch)

        self.assertEqual([item["to"] for item in self.gmail.sent], ["admin@brewlab.example"])
        # A second poll must not send again.
        self.poll(orch)
        self.assertEqual([item["to"] for item in self.gmail.sent], ["admin@brewlab.example"])

    def test_sheet_settings_failure_pauses(self):
        """Unavailable Sheet settings pause processing without sending mail."""
        self.sheet.fail_settings = True
        self.add_raw_email(build_email_raw("m1"))
        orch = self.make_orchestrator()
        results = self.poll(orch)
        self.assertEqual(results, [])
        self.assertEqual(self.gmail.drafts, [])
        self.assertEqual(self.gmail.sent, [])

    def test_log_failure_does_not_block_labels(self):
        """A log failure does not prevent intent and processing label attempts."""
        self.sheet.fail_log = True
        self.add_raw_email(build_email_raw("m1"))
        orch = self.make_orchestrator()
        self.poll(orch)
        self.assertIn("bot-processed", self.gmail.labels["m1"])


class IdempotencyTests(OrchestratorTestBase):
    def test_reprocessed_message_does_not_duplicate_side_effects(self):
        """Persistent deduplication prevents repeated drafts, sends and logs after restart."""
        self.add_raw_email(build_email_raw("m1"))
        orch = self.make_orchestrator()
        self.poll(orch)
        self.assertEqual(len(self.gmail.drafts), 1)
        self.assertEqual(len(self.sheet.log_rows), 1)

        # Construct a new orchestrator and reload the same state file.
        reloaded_state = StateStore(Path(self.tmp.name) / "state.json")
        orch2 = Orchestrator(
            gmail=self.gmail, sheet=self.sheet, llm=self.llm, state=reloaded_state,
            quiet_seconds=0, sleep=lambda _s: None, now_fn=lambda: self.now,
        )
        self.poll(orch2)

        self.assertEqual(len(self.gmail.drafts), 1, "Do not create a duplicate draft")
        self.assertEqual(len(self.sheet.log_rows), 1, "Do not append a duplicate log row")

    def test_partial_effects_resume_only_missing(self):
        """After restart, complete the missing log without recreating the draft."""
        self.state.mark_effect_done("m1", EFFECT_DRAFT, draft_id="draft-x")
        self.add_raw_email(build_email_raw("m1"))
        orch = self.make_orchestrator()
        self.poll(orch)

        self.assertEqual(self.gmail.drafts, [], "The draft already exists and must not be recreated")
        self.assertEqual(len(self.sheet.log_rows), 1, "The missing log row must be appended")
        self.assertIn("bot-processed", self.gmail.labels["m1"])

    def test_state_file_roundtrip(self):
        """Round-trip persistent state."""
        self.state.mark_effect_done("m9", EFFECT_SEND, sent_id="s1")
        self.state.mark_processed("m9", intent="pricing")
        reloaded = StateStore(Path(self.tmp.name) / "state.json")
        self.assertTrue(reloaded.is_processed("m9"))
        self.assertTrue(reloaded.is_effect_done("m9", EFFECT_SEND))


class RetryUtilityTests(unittest.TestCase):
    def test_retry_succeeds_after_two_failures(self):
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("transient")
            return "ok"

        result = call_with_retry(flaky, operation="t", sleep=lambda _s: None)
        self.assertEqual(result, "ok")
        self.assertEqual(attempts["n"], 3)

    def test_retry_exhausted_after_two_retries(self):
        def always_fail():
            raise RuntimeError("boom")

        with self.assertRaises(RetryExhaustedError) as ctx:
            call_with_retry(always_fail, operation="t", sleep=lambda _s: None)
        self.assertEqual(ctx.exception.attempts, 3)

    def test_ambiguous_not_retried(self):
        attempts = {"n": 0}

        def ambiguous():
            attempts["n"] += 1
            raise AmbiguousOutcomeError("lost response")

        with self.assertRaises(AmbiguousOutcomeError):
            call_with_retry(ambiguous, operation="t", sleep=lambda _s: None)
        self.assertEqual(attempts["n"], 1, "An uncertain outcome must not be retried")

    def test_backoff_delays_increase(self):
        delays = []

        def fail():
            raise RuntimeError("x")

        with self.assertRaises(RetryExhaustedError):
            call_with_retry(fail, operation="t", sleep=delays.append, backoff_base=1.0)
        self.assertEqual(len(delays), 2)
        self.assertGreater(delays[1], delays[0], "Exponential backoff delays must increase")


class ConfigRuleTests(unittest.TestCase):
    def test_keyword_match(self):
        self.assertEqual(
            match_handoff_keyword("I want a REFUND", ["refund"]), "refund"
        )
        self.assertIsNone(match_handoff_keyword("hello", ["refund"]))

    def test_active_window_boundaries(self):
        settings = default_settings(active_hours="09:00-18:00", active_days="wed")
        inside = datetime(2026, 3, 4, 9, 0, tzinfo=TZ)
        outside = datetime(2026, 3, 4, 18, 0, tzinfo=TZ)
        self.assertTrue(is_within_active_window(settings, inside))
        self.assertFalse(is_within_active_window(settings, outside))


if __name__ == "__main__":
    unittest.main()
