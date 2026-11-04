"""Model JSON validation, grounding, language detection and retry tests."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from llm import (
    HANDOFF_REPLY_EN,
    HANDOFF_REPLY_ZH,
    LLMClient,
    LLMError,
    LLMResult,
    apply_safety_rules,
    build_system_prompt,
    detect_language,
    fallback_reply,
    validate_payload,
)


def make_response(payload: dict) -> mock.Mock:
    """Build the chat completion response shape."""
    message = mock.Mock()
    message.content = json.dumps(payload, ensure_ascii=False)
    choice = mock.Mock()
    choice.message = message
    response = mock.Mock()
    response.choices = [choice]
    return response


VALID = {
    "intent": "pricing",
    "language": "en",
    "confidence": 0.88,
    "needs_human": False,
    "knowledge_used": ["pricing.md#batch-quotes"],
    "reply": "It is $79.",
}


class ValidatePayloadTests(unittest.TestCase):
    def test_valid_payload(self):
        result = validate_payload(VALID)
        self.assertEqual(result.intent, "pricing")
        self.assertAlmostEqual(result.confidence, 0.88)

    def test_missing_field(self):
        broken = dict(VALID)
        del broken["intent"]
        with self.assertRaises(LLMError):
            validate_payload(broken)

    def test_bad_intent(self):
        with self.assertRaises(LLMError):
            validate_payload({**VALID, "intent": "sales"})

    def test_bad_language(self):
        with self.assertRaises(LLMError):
            validate_payload({**VALID, "language": "fr"})

    def test_confidence_out_of_range(self):
        with self.assertRaises(LLMError):
            validate_payload({**VALID, "confidence": 1.5})

    def test_needs_human_not_bool(self):
        with self.assertRaises(LLMError):
            validate_payload({**VALID, "needs_human": "yes"})

    def test_knowledge_used_not_list(self):
        with self.assertRaises(LLMError):
            validate_payload({**VALID, "knowledge_used": "pricing.md"})

    def test_not_a_dict(self):
        with self.assertRaises(LLMError):
            validate_payload(["nope"])


class SafetyRuleTests(unittest.TestCase):
    def test_no_knowledge_forces_handoff(self):
        result = LLMResult(
            intent="pricing", language="en", confidence=0.9, needs_human=False,
            knowledge_used=[], reply="I think it is $50.",
        )
        out = apply_safety_rules(result)
        self.assertTrue(out.needs_human)
        self.assertIn(HANDOFF_REPLY_EN, out.reply)

    def test_handoff_intent_forces_handoff(self):
        result = LLMResult(
            intent="handoff", language="zh", confidence=0.9, needs_human=False,
            knowledge_used=["faq.md"], reply="\u8bf7\u7a0d\u7b49",
        )
        out = apply_safety_rules(result)
        self.assertTrue(out.needs_human)
        self.assertIn(HANDOFF_REPLY_ZH, out.reply)

    def test_chitchat_without_knowledge_is_ok(self):
        result = LLMResult(
            intent="chitchat", language="en", confidence=0.9, needs_human=False,
            knowledge_used=[], reply="Thanks! Enjoy your coffee.",
        )
        out = apply_safety_rules(result)
        self.assertFalse(out.needs_human, "Casual conversation does not require knowledge citations")

    def test_existing_handoff_text_not_duplicated(self):
        result = LLMResult(
            intent="handoff", language="en", confidence=0.9, needs_human=True,
            knowledge_used=[], reply="I've passed this to a colleague.",
        )
        out = apply_safety_rules(result)
        self.assertEqual(out.reply.count("colleague"), 1)


class PromptTests(unittest.TestCase):
    def test_prompt_prefix_is_stable(self):
        """Unchanged knowledge and tone must produce an identical cacheable prefix."""
        first = build_system_prompt("KB TEXT", "friendly")
        second = build_system_prompt("KB TEXT", "friendly")
        self.assertEqual(first, second)
        self.assertIn("KB TEXT", first)
        # Knowledge belongs in the fixed prefix after the instructions.
        self.assertLess(first.index("RULES"), first.index("KB TEXT"))

    def test_knowledge_appears_once(self):
        prompt = build_system_prompt("UNIQUE_KB_MARKER", "friendly")
        self.assertEqual(prompt.count("UNIQUE_KB_MARKER"), 1)


class LanguageTests(unittest.TestCase):
    def test_detect_chinese(self):
        self.assertEqual(detect_language("\u4f60\u597d\uff0c\u8bf7\u95ee\u5496\u5561\u673a\u591a\u5c11\u94b1\uff1f"), "zh")

    def test_detect_english(self):
        self.assertEqual(detect_language("How much is it?"), "en")

    def test_mixed_dominant_chinese(self):
        self.assertEqual(detect_language("\u4f60\u597d\uff0c\u6211\u60f3\u95ee\u4e00\u4e0b price \u662f\u591a\u5c11"), "zh")

    def test_empty_defaults_english(self):
        self.assertEqual(detect_language(""), "en")

    def test_fallback_reply_language(self):
        self.assertIn("\u7a0d\u540e\u56de\u590d", fallback_reply("zh"))
        self.assertIn("received your email", fallback_reply("en"))


class LLMClientTests(unittest.TestCase):
    def _client(self, create_side_effect=None, return_value=None):
        openai_client = mock.Mock()
        if create_side_effect is not None:
            openai_client.chat.completions.create.side_effect = create_side_effect
        else:
            openai_client.chat.completions.create.return_value = return_value
        return LLMClient(openai_client, "gpt-test", sleep=lambda _s: None), openai_client

    def test_generate_success(self):
        client, openai_client = self._client(return_value=make_response(VALID))
        result = client.generate(
            system_prompt="sys", history=[], user_content="How much?"
        )
        self.assertEqual(result.intent, "pricing")
        call = openai_client.chat.completions.create.call_args
        self.assertEqual(call.kwargs["response_format"]["type"], "json_schema")
        self.assertTrue(call.kwargs["response_format"]["json_schema"]["strict"])

    def test_generate_retries_on_rate_limit(self):
        client, openai_client = self._client(
            create_side_effect=[RateLimitError("slow down"), make_response(VALID)]
        )
        result = client.generate(system_prompt="sys", history=[], user_content="q")
        self.assertEqual(result.intent, "pricing")
        self.assertEqual(openai_client.chat.completions.create.call_count, 2)

    def test_structurally_invalid_response_uses_same_retry_budget(self):
        """Retry parseable responses with missing fields until a valid third response."""
        client, api = self._client(create_side_effect=[make_response({}), make_response({}), make_response(VALID)])
        result = client.generate(system_prompt="sys", history=[], user_content="Price?")
        self.assertEqual(result.intent, "pricing")
        self.assertEqual(api.chat.completions.create.call_count, 3)

    def test_generate_raises_after_two_retries(self):
        client, openai_client = self._client(
            create_side_effect=RateLimitError("always")
        )
        with self.assertRaises(LLMError):
            client.generate(system_prompt="sys", history=[], user_content="q")
        self.assertEqual(openai_client.chat.completions.create.call_count, 3)

    def test_auth_error_not_retried(self):
        client, openai_client = self._client(
            create_side_effect=BadRequestError("bad key")
        )
        with self.assertRaises(LLMError):
            client.generate(system_prompt="sys", history=[], user_content="q")
        self.assertEqual(openai_client.chat.completions.create.call_count, 1)

    def test_invalid_json_raises(self):
        bad = mock.Mock()
        bad.choices = [mock.Mock(message=mock.Mock(content="not json"))]
        client, _ = self._client(return_value=bad)
        with self.assertRaises(LLMError):
            client.generate(system_prompt="sys", history=[], user_content="q")

    def test_empty_content_raises(self):
        empty = mock.Mock()
        empty.choices = [mock.Mock(message=mock.Mock(content=""))]
        client, _ = self._client(return_value=empty)
        with self.assertRaises(LLMError):
            client.generate(system_prompt="sys", history=[], user_content="q")

    def test_sdk_auto_retry_disabled_in_runtime(self):
        """Runtime assembly must disable SDK retries with max_retries=0."""
        import inspect

        import main

        source = inspect.getsource(main.build_runtime)
        self.assertIn("max_retries=0", source)

    def test_history_roles_mapped(self):
        from llm import HistoryTurn

        client, openai_client = self._client(return_value=make_response(VALID))
        client.generate(
            system_prompt="sys",
            history=[HistoryTurn("user", "hello"), HistoryTurn("assistant", "hi")],
            user_content="new question",
        )
        messages = openai_client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1], {"role": "user", "content": "hello"})
        self.assertEqual(messages[2], {"role": "assistant", "content": "hi"})
        self.assertEqual(messages[3], {"role": "user", "content": "new question"})


# Fake exceptions match SDK names used by the classifier.
class RateLimitError(Exception):
    pass


class BadRequestError(Exception):
    pass


if __name__ == "__main__":
    unittest.main()
