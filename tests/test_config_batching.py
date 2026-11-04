"""Configuration, operating-window, keyword, logging and batching tests."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from batching import (
    Batch,
    EmailMessage,
    MessageBatcher,
    merge_messages,
    normalize_sender,
)
from config_sheet import (
    LOG_HEADERS,
    ConfigError,
    ConfigSheet,
    build_log_row,
    build_settings,
    is_within_active_window,
    match_handoff_keyword,
    parse_active_days,
    parse_active_hours,
    row_to_dict,
)
from state_store import StateStore

TZ = ZoneInfo("America/Los_Angeles")


class SettingsParsingTests(unittest.TestCase):
    def test_row_to_dict(self):
        rows = [["mode", "live"], ["timezone", "UTC"], ["", "ignored"], ["tone"]]
        parsed = row_to_dict(rows)
        self.assertEqual(parsed["mode"], "live")
        self.assertEqual(parsed["timezone"], "UTC")
        self.assertEqual(parsed["tone"], "")
        self.assertNotIn("", parsed)

    def test_build_settings_defaults(self):
        settings = build_settings({})
        self.assertEqual(settings.mode, "draft", "The default mode must be draft")
        self.assertEqual(settings.timezone, "America/Los_Angeles")

    def test_invalid_mode_raises(self):
        with self.assertRaises(ConfigError):
            build_settings({"mode": "auto"})

    def test_invalid_timezone_raises(self):
        with self.assertRaises(ConfigError):
            build_settings({"timezone": "Mars/Olympus"})

    def test_invalid_threshold_raises(self):
        with self.assertRaises(ConfigError):
            build_settings({"confidence_threshold": "abc"})
        with self.assertRaises(ConfigError):
            build_settings({"confidence_threshold": "2"})

    def test_active_days_parsing(self):
        self.assertEqual(parse_active_days("mon,wed,fri"), {0, 2, 4})
        self.assertEqual(parse_active_days("\u5468\u4e00,\u5468\u4e09"), {0, 2})
        with self.assertRaises(ConfigError):
            parse_active_days("")

    def test_active_hours_parsing(self):
        self.assertEqual(parse_active_hours("09:00-18:30"), (540, 1110))
        with self.assertRaises(ConfigError):
            parse_active_hours("18:00-09:00")
        with self.assertRaises(ConfigError):
            parse_active_hours("9am-6pm")

    def test_keyword_list_from_string(self):
        settings = build_settings({"handoff_keywords": "Refund, \u6295\u8bc9 , HUMAN"})
        self.assertEqual(settings.keyword_list(), ["refund", "\u6295\u8bc9", "human"])

    def test_keyword_match_case_insensitive(self):
        self.assertEqual(match_handoff_keyword("I need a REFUND", ["refund"]), "refund")
        self.assertIsNone(match_handoff_keyword("hello", ["refund"]))

    def test_within_window(self):
        settings = build_settings(
            {"active_days": "wed", "active_hours": "09:00-17:00", "timezone": "America/Los_Angeles"}
        )
        self.assertTrue(
            is_within_active_window(settings, datetime(2026, 3, 4, 12, 0, tzinfo=TZ))
        )
        self.assertFalse(
            is_within_active_window(settings, datetime(2026, 3, 4, 8, 59, tzinfo=TZ))
        )
        self.assertFalse(
            is_within_active_window(settings, datetime(2026, 3, 4, 17, 0, tzinfo=TZ))
        )


class LogRowTests(unittest.TestCase):
    def test_headers_are_ten_fields(self):
        self.assertEqual(len(LOG_HEADERS), 10)

    def test_build_log_row_order(self):
        row = build_log_row(
            timestamp="2026-03-04T10:00:00-08:00",
            sender="a@b.com",
            intent="pricing",
            mode="draft",
            handoff=True,
            reply_summary="summary",
            elapsed_ms=123,
            error="",
            thread_id="t1",
            message_id="m1",
        )
        self.assertEqual(len(row), 10)
        self.assertEqual(row[4], "yes")
        self.assertEqual(row[2], "pricing")
        self.assertEqual(row[6], 123)


class ConfigSheetServiceTests(unittest.TestCase):
    def _fake_sheets_service(self, rows):
        service = mock.Mock()
        values = service.spreadsheets.return_value.values.return_value
        values.get.return_value.execute.return_value = {"values": rows}
        values.append.return_value.execute.return_value = {"updates": {"updatedRows": 1}}
        return service

    def test_load_settings_from_service(self):
        service = self._fake_sheets_service([["mode", "live"], ["timezone", "UTC"]])
        sheet = ConfigSheet(service, "sheet-id")
        settings = sheet.load_settings()
        self.assertEqual(settings.mode, "live")
        self.assertEqual(settings.timezone, "UTC")

    def test_empty_settings_raises(self):
        service = self._fake_sheets_service([])
        sheet = ConfigSheet(service, "sheet-id")
        with self.assertRaises(ConfigError):
            sheet.load_settings()

    def test_append_log_row(self):
        service = self._fake_sheets_service([])
        sheet = ConfigSheet(service, "sheet-id")
        sheet.append_log_row(["t", "s", "i", "draft", "no", "sum", 10, "", "th", "mid"])
        service.spreadsheets.return_value.values.return_value.append.assert_called_once()

    def test_append_log_row_wrong_length(self):
        service = self._fake_sheets_service([])
        sheet = ConfigSheet(service, "sheet-id")
        with self.assertRaises(ValueError):
            sheet.append_log_row(["too", "few"])

    def test_load_settings_retries_on_transient_error(self):
        service = mock.Mock()
        values = service.spreadsheets.return_value.values.return_value
        values.get.return_value.execute.side_effect = [
            RuntimeError("transient"),
            {"values": [["mode", "draft"]]},
        ]
        sheet = ConfigSheet(service, "sheet-id", sleep=lambda _s: None)
        settings = sheet.load_settings()
        self.assertEqual(settings.mode, "draft")
        self.assertEqual(values.get.return_value.execute.call_count, 2)


class BatchingTests(unittest.TestCase):
    def test_normalize_sender(self):
        self.assertEqual(normalize_sender("Alice <Alice@Example.COM>"), "alice@example.com")
        self.assertEqual(normalize_sender("bob@example.com"), "bob@example.com")

    def test_quiet_window_not_ready(self):
        batcher = MessageBatcher(quiet_seconds=120)
        msg = EmailMessage("m1", "t1", "a@b.com", "s", "b", received_at=1000.0)
        batcher.add(msg)
        self.assertFalse(batcher.is_ready("a@b.com", now=1050.0))
        self.assertTrue(batcher.is_ready("a@b.com", now=1120.0))

    def test_new_message_resets_quiet_window(self):
        batcher = MessageBatcher(quiet_seconds=120)
        batcher.add(EmailMessage("m1", "t1", "a@b.com", "s", "b1", received_at=1000.0))
        self.assertTrue(batcher.is_ready("a@b.com", now=1120.0))
        batcher.add(EmailMessage("m2", "t1", "a@b.com", "s", "b2", received_at=1150.0))
        self.assertFalse(batcher.is_ready("a@b.com", now=1170.0), "A new message must reset the quiet window")
        self.assertTrue(batcher.is_ready("a@b.com", now=1280.0))

    def test_ready_batches_sorted(self):
        batcher = MessageBatcher(quiet_seconds=10)
        batcher.add(EmailMessage("m1", "t1", "b@b.com", "s", "b", received_at=100.0))
        batcher.add(EmailMessage("m2", "t2", "a@b.com", "s", "b", received_at=90.0))
        ready = batcher.ready_batches(now=200.0)
        self.assertEqual([b.sender_email for b in ready], ["a@b.com", "b@b.com"])

    def test_cross_thread_sources_preserved(self):
        batch = Batch(
            sender_email="a@b.com",
            messages=[
                EmailMessage("m1", "t-old", "a@b.com", "s1", "first", received_at=1.0),
                EmailMessage("m2", "t-new", "a@b.com", "s2", "second", received_at=2.0),
            ],
        )
        self.assertEqual(batch.latest_thread_id, "t-new")
        self.assertEqual(batch.thread_ids, ["t-old", "t-new"])
        merged = merge_messages(batch.messages)
        self.assertIn("t-old", merged)
        self.assertIn("t-new", merged)


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def test_persist_and_reload(self):
        store = StateStore(self.path)
        store.mark_processed("m1", intent="pricing")
        store.mark_effect_done("m1", "draft", draft_id="d1")
        reloaded = StateStore(self.path)
        self.assertTrue(reloaded.is_processed("m1"))
        self.assertTrue(reloaded.is_effect_done("m1", "draft"))
        self.assertFalse(reloaded.is_effect_done("m1", "send"))

    def test_ambiguous_records_compensation(self):
        store = StateStore(self.path)
        store.mark_effect_ambiguous("m1", "send", error="lost")
        self.assertTrue(store.is_effect_ambiguous("m1", "send"))
        self.assertIn("m1", store.pending_compensation())
        reloaded = StateStore(self.path)
        self.assertIn("m1", reloaded.pending_compensation())

    def test_corrupt_state_file_raises(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            StateStore(self.path)

    def test_batch_persistence(self):
        store = StateStore(self.path)
        store.save_batch("a@b.com", {"messages": [{"message_id": "m1"}]})
        self.assertIn("a@b.com", StateStore(self.path).load_batches())
        store.drop_batch("a@b.com")
        self.assertNotIn("a@b.com", StateStore(self.path).load_batches())


if __name__ == "__main__":
    unittest.main()
