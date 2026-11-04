"""Drive simulated providers through the actual orchestrator."""

from unittest.mock import Mock

from llm import LLMClient
from tests.test_llm import VALID, make_response
from tests.test_main_flow import OrchestratorTestBase, build_email_raw


class PortfolioDemo(OrchestratorTestBase):
    def test_followup_cannot_see_the_previous_unsent_draft(self):
        """Unsent drafts must not displace real messages in the twenty-message history."""
        import base64
        from gmail_client import GmailClient

        def message(mid, body, labels, number):
            sender = "alice@example.com" if "INBOX" in labels else "support@brewlab.example"
            return {"id": mid, "internalDate": str(number), "labelIds": labels, "payload": {
                "mimeType": "text/plain", "headers": [{"name": "From", "value": sender}],
                "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
            }}

        self.add_raw_email(build_email_raw("first", body="First inquiry"))
        orch = self.make_orchestrator()
        self.poll(orch)
        api = Mock()
        messages = [message("first", "First inquiry", ["INBOX"], 1),
                    message("sent", "An actual reply", ["SENT"], 2)]
        messages.extend(message(f"d{i}", self.gmail.drafts[0]["body"], ["DRAFT"], 3+i) for i in range(20))
        messages.append(message("followup", "Second inquiry", ["INBOX"], 23))
        api.users.return_value.threads.return_value.get.return_value.execute.return_value = {"messages": messages}
        self.gmail.get_thread = GmailClient(api).get_thread
        self.add_raw_email(build_email_raw("followup", body="Second inquiry"))
        self.poll(orch)
        history = self.llm.calls[-1]["history"]
        self.assertEqual([(t.role, t.content) for t in history],
                         [("user", "First inquiry"), ("assistant", "An actual reply")])
        self.assertEqual(len(self.gmail.drafts), 2)

    def test_three_questions_one_draft(self):
        """Use the real model adapter to verify one decision, one draft and three source logs."""
        questions = (
            "How much is the Pour-Over Starter Kit?",
            "Is that price in USD?",
            "Please confirm the price for one kit.",
        )
        api = Mock()
        api.chat.completions.create.return_value = make_response({
            **VALID, "knowledge_used": ["pricing.md"],
            "reply": "One Pour-Over Starter Kit costs USD 79.",
        })
        self.llm = LLMClient(api, "offline", sleep=lambda seconds: None, known_sources=["pricing.md"])
        for index, question in enumerate(questions):
            raw = build_email_raw(str(index), body=question, thread=f"thread-{index}")
            raw["internal_date"] = int((self.now - (2 - index) * 20) * 1000)
            self.add_raw_email(raw)
        orch = self.make_orchestrator(quiet_seconds=120)
        self.poll(orch)
        self.assertFalse(self.gmail.drafts)
        self.now += 120
        self.poll(orch)
        request = api.chat.completions.create.call_args.kwargs["messages"][-1]["content"]
        for question in questions:
            self.assertIn(question, request)
        self.assertEqual(api.chat.completions.create.call_count, 1)
        self.assertEqual(len(self.gmail.drafts), 1)
        self.assertEqual(self.gmail.drafts[0]["thread_id"], "thread-2")
        self.assertEqual([row[2] for row in self.sheet.log_rows], ["pricing"] * 3)
        self.assertEqual(self.gmail.sent, [])
        print(f"3 messages -> 1 pricing draft -> 3 log rows\nDraft: {self.gmail.drafts[0]['body']}")
