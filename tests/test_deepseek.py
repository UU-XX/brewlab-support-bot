"""Exercise DeepSeek's request contract and local validation without network access."""

import unittest
from unittest.mock import Mock

from llm import HANDOFF_REPLY_EN, LLMClient, LLMError, build_system_prompt
from tests.test_llm import VALID, make_response


class DeepSeekTests(unittest.TestCase):
    def make_client(self, responses):
        """Inject completion responses while retaining the real model adapter."""
        api = Mock()
        api.chat.completions.create.side_effect = responses
        return LLMClient(api, "deepseek-flash", provider="deepseek", sleep=lambda _: None,
                         known_sources=["pricing.md"]), api

    def generate(self, client):
        """Use the production prompt builder and a synthetic pricing request."""
        return client.generate(system_prompt=build_system_prompt("Example catalog", "concise"),
                               history=[], user_content="How much is one kit?")

    def test_json_mode_request_and_validated_reply(self):
        client, api = self.make_client([make_response(VALID)])
        self.assertEqual(self.generate(client).intent, "pricing")
        request = api.chat.completions.create.call_args.kwargs
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(request["max_tokens"], 2048)
        self.assertIn('"needs_human":true', request["messages"][0]["content"])

    def test_invalid_fields_and_blank_output_share_retry_budget(self):
        invalid = make_response({**VALID, "confidence": "0.9"})
        blank = make_response(VALID)
        blank.choices[0].message.content = "   "
        client, api = self.make_client([invalid, blank, make_response(VALID)])
        self.assertEqual(self.generate(client).intent, "pricing")
        self.assertEqual(api.chat.completions.create.call_count, 3)

    def test_unknown_sources_force_handoff(self):
        client, _ = self.make_client([make_response({**VALID, "knowledge_used": ["invented.md"]})])
        result = self.generate(client)
        self.assertTrue(result.needs_human)
        self.assertEqual(result.reply, HANDOFF_REPLY_EN)

    def test_exhausted_invalid_output_fails(self):
        client, api = self.make_client([make_response({}) for _ in range(3)])
        with self.assertRaises(LLMError):
            self.generate(client)
        self.assertEqual(api.chat.completions.create.call_count, 3)

    def test_openai_schema_mode_is_preserved(self):
        api = Mock()
        api.chat.completions.create.return_value = make_response(VALID)
        self.generate(LLMClient(api, "example", sleep=lambda _: None))
        request = api.chat.completions.create.call_args.kwargs
        self.assertEqual(request["response_format"]["type"], "json_schema")
        self.assertNotIn("extra_body", request)
