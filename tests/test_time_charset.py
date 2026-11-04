"""Operating-window boundaries and multilingual charset regression tests."""

import base64
from unittest import TestCase
from unittest.mock import Mock

from gmail_client import GmailClient, OutsideActiveWindow, decode_body
from llm import fallback_reply, detect_language
from tests.test_main_flow import OrchestratorTestBase, build_email_raw


class WindowBoundaryTests(OrchestratorTestBase):
    def test_chinese_subject_selects_fallback_for_bodyless_mail(self):
        """An empty body and failed history read still use the Chinese subject for fallback."""
        from retry_utils import RetryExhaustedError
        self.add_raw_email(build_email_raw("m1", subject="\u8ba2\u5355\u7834\u635f\uff0c\u8bf7\u534f\u52a9\u552e\u540e", body=""))
        self.gmail.get_thread = Mock(side_effect=RetryExhaustedError(3, TimeoutError()))
        self.poll(self.make_orchestrator())
        self.assertEqual([draft["body"] for draft in self.gmail.drafts], [fallback_reply("zh")])
        self.assertEqual([msg["to"] for msg in self.gmail.sent], ["admin@brewlab.example"])

    def test_external_body_is_fetched_for_inbound_and_history(self):
        """Full bodies, not truncated snippets, feed context and handoff keyword detection."""
        body = "\u8bf7\u95ee\u4ef7\u683c\uff1f" + "\u66f4\u591a\u4fe1\u606f\u3002" * 40 + "\u8bf7\u5e2e\u6211\u627e\u771f\u4eba"
        api = Mock()
        messages = api.users.return_value.messages.return_value
        messages.attachments.return_value.get.return_value.execute.return_value = {
            "data": base64.urlsafe_b64encode(body.encode()).decode(),
        }
        raw = {"id": "m1", "threadId": "t1", "snippet": "\u8bf7\u95ee\u4ef7\u683c\uff1f", "payload": {
            "mimeType": "text/plain", "body": {"attachmentId": "part1"},
            "headers": [{"name": "From", "value": "alice@example.com"}],
        }}
        client = GmailClient(api, sleep=lambda seconds: None)
        api.users.return_value.threads.return_value.get.return_value.execute.return_value = {"messages": [raw]}
        self.assertEqual(client.get_thread("t1")[0].body, body)
        self.gmail.parse_message = client.parse_message
        self.add_raw_email(raw)
        self.poll(self.make_orchestrator())
        self.assertEqual(messages.attachments.return_value.get.call_count, 2)
        self.assertIn("\u627e\u771f\u4eba", self.llm.calls[0]["user_content"])
        self.assertIn("needs-human", self.gmail.labels["m1"])

    def test_external_body_failure_never_answers_from_snippet(self):
        """Exhausted external-body reads preserve intake and alert without quoting from a snippet."""
        api = Mock()
        fetch = api.users.return_value.messages.return_value.attachments.return_value.get
        fetch.return_value.execute.side_effect = TimeoutError("Body read timeout")
        self.gmail.parse_message = GmailClient(api, sleep=lambda seconds: None).parse_message
        raw = {"id": "m1", "threadId": "t1", "snippet": "pricing?", "payload": {
            "mimeType": "text/plain", "body": {"attachmentId": "part1"},
            "headers": [{"name": "From", "value": "alice@example.com"}],
        }}
        self.add_raw_email(raw)
        self.poll(self.make_orchestrator())
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(self.llm.calls, [])
        self.assertFalse(self.state.is_processed("m1"))
        self.assertEqual([s["to"] for s in self.gmail.sent], ["admin@brewlab.example"])

    def test_text_attachment_does_not_hide_handoff_request(self):
        """An earlier MIME attachment must not hide the customer's body from the model."""
        def part(text):
            return {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}
        attachment = part("USD 79")
        attachment["headers"] = [{"name": "Content-Disposition", "value": 'attachment; filename="price.txt"'}]
        body = decode_body({"mimeType": "multipart/mixed", "parts": [attachment, part("\u6211\u8981\u6295\u8bc9\uff0c\u627e\u771f\u4eba")]})
        self.assertEqual(body, "\u6211\u8981\u6295\u8bc9\uff0c\u627e\u771f\u4eba")
        self.add_raw_email(build_email_raw("m1", body=body))
        self.poll(self.make_orchestrator())
        self.assertIn("needs-human", self.gmail.labels["m1"])
        self.assertEqual(len(self.gmail.forwarded), 1)

    def test_html_entities_keep_fallback_draft_in_chinese(self):
        """HTML-encoded Chinese content still receives a localized fallback on model failure."""
        from llm import LLMError

        html = "<p>&#20320;&#22909;&#65292;&#35831;&#38382;&#20215;&#26684;&#65311;</p>"
        body = decode_body({"mimeType": "text/html", "body": {
            "data": base64.urlsafe_b64encode(html.encode()).decode(),
        }})
        self.assertEqual(body, "\u4f60\u597d\uff0c\u8bf7\u95ee\u4ef7\u683c\uff1f")
        self.add_raw_email(build_email_raw("m1", body=body))
        self.llm.error = LLMError("Model temporarily unavailable")
        self.poll(self.make_orchestrator())
        self.assertEqual([draft["body"] for draft in self.gmail.drafts], [fallback_reply("zh")])

    def test_paused_live_reply_can_resume_as_draft(self):
        """A paused unsent batch switched to draft records its actual execution mode."""
        self.sheet.settings.mode = "live"
        self.sheet.settings.active_hours = "09:00-10:01"
        self.add_raw_email(build_email_raw("m1"))
        generate = self.llm.generate

        def slow_generate(**kwargs):
            self.now += 120
            return generate(**kwargs)

        self.llm.generate = slow_generate
        self.poll(self.make_orchestrator())
        self.assertEqual(self.gmail.sent, [])
        self.llm.generate = generate
        self.sheet.settings.mode = "draft"
        self.now += 24 * 3600 - 120
        self.poll(self.make_orchestrator())
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual(len(self.gmail.drafts), 1)
        self.assertEqual(self.sheet.log_rows[0][3], "draft")

    def test_quoted_display_name_cannot_change_reply_recipient(self):
        """Angle brackets in display names must not alter recipient selection or grouping."""
        self.sheet.settings.mode = "live"
        self.add_raw_email(build_email_raw("m1", sender='"Sales <billing@example.com>" <alice@example.com>'))
        self.poll(self.make_orchestrator())
        self.assertEqual([item["to"] for item in self.gmail.sent], ["alice@example.com"])
        self.assertEqual(self.sheet.log_rows[0][1], "alice@example.com")

    def test_model_crosses_closing_time_then_resumes_once(self):
        """If generation crosses closing time, defer customer actions until the next window."""
        self.sheet.settings.mode = "live"
        self.sheet.settings.active_hours = "09:00-10:01"
        self.add_raw_email(build_email_raw("m1"))
        generate = self.llm.generate

        def slow_generate(**kwargs):
            self.now += 120
            return generate(**kwargs)

        self.llm.generate = slow_generate
        self.poll(self.make_orchestrator())
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual(self.gmail.drafts, [])
        self.assertEqual(self.sheet.log_rows, [])
        self.assertTrue(self.state.load_batches_to_reply())
        self.llm.generate = generate
        self.now += 24 * 3600 - 120
        self.poll(self.make_orchestrator())
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertEqual(len(self.sheet.log_rows), 1)

    def test_label_work_waits_when_reply_finishes_at_closing_time(self):
        """After a successful send crosses closing time, resume only labels and logs."""
        self.sheet.settings.mode = "live"
        self.sheet.settings.active_hours = "09:00-10:01"
        self.add_raw_email(build_email_raw("m1"))
        send = self.gmail.send_reply

        def slow_send(**kwargs):
            sent_id = send(**kwargs)
            self.now += 120
            return sent_id

        self.gmail.send_reply = slow_send
        self.poll(self.make_orchestrator())
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertEqual(self.gmail.labels, {})
        self.assertEqual(self.sheet.log_rows, [])
        self.now += 24 * 3600 - 120
        self.poll(self.make_orchestrator())
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertIn("bot-processed", self.gmail.labels["m1"])
        self.assertEqual(len(self.sheet.log_rows), 1)


class AdapterBoundaryTests(TestCase):
    def test_write_retry_rechecks_window(self):
        """Backoff crossing closing time must not start a second API attempt."""
        service = Mock()
        send = service.users.return_value.messages.return_value.send
        rejected = RuntimeError("rate limited")
        rejected.status_code = 429
        send.return_value.execute.side_effect = rejected
        client = GmailClient(service, sleep=lambda seconds: None)
        client.before_write = Mock(side_effect=[None, OutsideActiveWindow()])
        with self.assertRaises(OutsideActiveWindow):
            client.send_reply(thread_id="t1", to="a@example.com", subject="Re: price", body="79 USD")
        send.assert_called_once()

    def test_chinese_charset_and_fallback_language(self):
        """Decode GBK and GB18030 accurately and preserve localized fallback behavior."""
        for charset in ("gbk", "gb18030"):
            with self.subTest(charset=charset):
                body = "\u4f60\u597d\uff0c\u8bf7\u95ee\u5496\u5561\u673a\u7684\u4ef7\u683c\u662f\u591a\u5c11\uff1f"
                payload = {
                    "mimeType": "text/plain",
                    "headers": [{"name": "Content-Type", "value": f"text/plain; charset={charset}"}],
                    "body": {"data": base64.urlsafe_b64encode(body.encode(charset)).decode()},
                }
                decoded = decode_body(payload)
                self.assertEqual(decoded, body)
                self.assertEqual(detect_language(decoded), "zh")
                self.assertIn("\u5df2\u6536\u5230", fallback_reply(detect_language(decoded)))
                payload["headers"][0]["value"] = "text/plain; charset=unknown-encoding"
                self.assertEqual(decode_body(payload), "")
