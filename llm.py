"""Classify intent and compose replies through an OpenAI-compatible client.

Use a stable knowledge prefix, structured output and local validation. Missing
evidence triggers a human handoff. Disable SDK retries to keep one application
retry budget. Confidence and source references do not guarantee factual accuracy."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from retry_utils import (
    AmbiguousOutcomeError,
    NonRetryableError,
    RetryExhaustedError,
    call_with_retry,
)

logger = logging.getLogger(__name__)

INTENTS = ("pricing", "aftersales", "chitchat", "handoff")
LANGUAGES = ("zh", "en")
HANDOFF_REPLY_ZH = "\u6211\u5e2e\u60a8\u8f6c\u7ed9\u540c\u4e8b\u8ddf\u8fdb\u3002"
HANDOFF_REPLY_EN = "I've passed this to a colleague who will follow up with you."

# Response schema enforced by local validation and compatible provider schema mode.
REPLY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "intent",
        "language",
        "confidence",
        "needs_human",
        "knowledge_used",
        "reply",
    ],
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "language": {"type": "string", "enum": list(LANGUAGES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "needs_human": {"type": "boolean"},
        "knowledge_used": {"type": "array", "items": {"type": "string"}},
        "reply": {"type": "string"},
    },
}


class LLMError(Exception):
    """A model request or parsing error handled by the fallback workflow.

    Preserve non_retryable for diagnostics; callers can catch LLMError to
    attempt a fallback draft and administrator alert."""

    def __init__(self, message: str, *, non_retryable: bool = False) -> None:
        super().__init__(message)
        self.non_retryable = non_retryable


@dataclass
class LLMResult:
    intent: str
    language: str
    confidence: float
    needs_human: bool
    knowledge_used: List[str] = field(default_factory=list)
    reply: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def is_low_confidence(self, threshold: float) -> bool:
        return self.confidence < threshold


@dataclass
class HistoryTurn:
    role: str
    content: str


def build_system_prompt(knowledge_text: str, tone: str) -> str:
    """Build a stable system prefix without timestamps or random content."""
    return (
        "You are BrewLab's customer support assistant for a coffee-equipment web store.\n"
        "BrewLab sells pour-over and espresso gear. Answer using ONLY the knowledge base below.\n"
        "\n"
        "RULES\n"
        f"1. Tone: {tone}.\n"
        "2. Reply in the customer's language: Chinese -> zh, English -> en. Mixed input: match "
        "the dominant language.\n"
        "3. Never invent prices, stock, delivery dates, refund amounts, or policies that are not "
        "in the knowledge base.\n"
        "4. If the knowledge base does not clearly answer the question, set needs_human=true and "
        "reply exactly with the handoff sentence. Never guess.\n"
        "5. Refunds, complaints, warranty decisions, legal or compensation requests always need a "
        "human: set needs_human=true.\n"
        "6. Intent must be one of: pricing (price/quote/discount requests), aftersales "
        "(shipping, returns, warranty, order status), chitchat (greetings, thanks), "
        "handoff (explicitly asks for a human, complaints, legal).\n"
        "7. confidence is your own 0-1 estimate that the reply is fully grounded in the knowledge "
        "base. It is a heuristic, not a guarantee.\n"
        "8. knowledge_used lists the knowledge sections/files you actually relied on, e.g. "
        "\"pricing.md#batch-quotes\". Empty array if none.\n"
        "9. Keep replies short (<= 160 words), sign as \"BrewLab Support\".\n"
        "\n"
        "OUTPUT: a single JSON object with keys intent, language, confidence, needs_human, "
        "knowledge_used, reply.\n"
        "\n"
        "=== KNOWLEDGE BASE (stable prefix) ===\n"
        f"{knowledge_text}\n"
        "=== END KNOWLEDGE BASE ===\n"
    )


def validate_payload(payload: Any) -> LLMResult:
    """Validate types and fields without implicit coercion.

    Reject missing or extra fields, numeric strings, booleans masquerading as
    confidence, out-of-range values and unknown intent or language values."""
    if not isinstance(payload, dict):
        raise LLMError(f"Model output is not a JSON object: {type(payload)!r}")
    required = ["intent", "language", "confidence", "needs_human", "knowledge_used", "reply"]
    missing = [key for key in required if key not in payload]
    if missing:
        raise LLMError(f"Model output is missing fields: {missing}")
    # Reject extra fields locally, matching additionalProperties=false.
    extra = [key for key in payload if key not in required]
    if extra:
        raise LLMError(f"Model output contains unexpected fields: {extra}")
    intent = payload["intent"]
    if not isinstance(intent, str) or intent.strip().lower() not in INTENTS:
        raise LLMError(f"Invalid intent: {intent!r}")
    intent = intent.strip().lower()
    language = payload["language"]
    if not isinstance(language, str) or language.strip().lower() not in LANGUAGES:
        raise LLMError(f"Invalid language: {language!r}")
    language = language.strip().lower()
    confidence = payload["confidence"]
    # Reject bool explicitly because it subclasses int; never coerce strings.
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise LLMError(f"confidence must be numeric: {confidence!r}")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise LLMError(f"confidence is out of range: {confidence}")
    needs_human = payload["needs_human"]
    if not isinstance(needs_human, bool):
        raise LLMError("needs_human must be a boolean")
    knowledge_used = payload["knowledge_used"]
    if not isinstance(knowledge_used, list) or not all(
        isinstance(item, str) for item in knowledge_used
    ):
        raise LLMError("knowledge_used must be an array of strings")
    reply = payload["reply"]
    if not isinstance(reply, str):
        raise LLMError("reply must be a string")
    return LLMResult(
        intent=intent,
        language=language,
        confidence=confidence,
        needs_human=needs_human,
        knowledge_used=list(knowledge_used),
        reply=reply.strip(),
        raw=dict(payload),
    )


def apply_safety_rules(
    result: LLMResult,
    *,
    known_sources: Optional[Sequence[str]] = None,
) -> LLMResult:
    """Apply local constraints after model generation.

    Missing or unknown sources on factual answers and empty replies trigger
    a fixed handoff response and lower confidence. Preserve supported text
    for other handoffs and append the standard handoff sentence."""
    language = result.language
    handoff_reply = HANDOFF_REPLY_ZH if language == "zh" else HANDOFF_REPLY_EN
    if result.intent == "handoff":
        result.needs_human = True

    insufficient = False
    if not result.knowledge_used and result.intent != "chitchat":
        # A substantive answer without a knowledge reference requires a person.
        logger.info("No knowledge references for a factual answer; forcing human handoff")
        insufficient = True
    if result.knowledge_used and known_sources is not None:
        # Every cited source must exist in the loaded knowledge base.
        valid_sources = {str(src).lower() for src in known_sources}
        if not _all_sources_known(result.knowledge_used, valid_sources):
            logger.info("Unknown knowledge source; forcing human handoff")
            insufficient = True
    if not result.reply.strip():
        logger.info("Empty model reply; forcing human handoff")
        insufficient = True

    if insufficient:
        result.needs_human = True
        result.confidence = min(result.confidence, 0.3)
        # Replace unsupported content completely with the fixed handoff sentence.
        result.reply = handoff_reply
    elif result.needs_human:
        # Retain supported handoff explanations and append the standard sentence.
        if _looks_like_handoff(result.reply):
            pass
        else:
            result.reply = f"{result.reply.strip()}\n\n{handoff_reply}"
    return result


def _all_sources_known(sources: Sequence[str], valid_sources: set) -> bool:
    """Match every reference to a known filename; section anchors are allowed."""
    for raw in sources:
        name = str(raw).strip().lower().split("#", 1)[0].strip()
        if not name:
            return False
        # Accept either pricing.md or pricing.
        if name not in valid_sources and f"{name}.md" not in valid_sources:
            return False
    return True


def _looks_like_handoff(reply: str) -> bool:
    lowered = reply.lower()
    return (
        "\u8f6c\u7ed9\u540c\u4e8b" in reply
        or "\u8f6c\u4eba\u5de5" in reply
        or "\u540c\u4e8b\u8ddf\u8fdb" in reply
        or "colleague" in lowered
        or "human" in lowered
        or "passed this" in lowered
    )


def fallback_reply(language: str) -> str:
    """Return a localized fallback acknowledgement."""
    if language == "zh":
        return "\u6211\u4eec\u5df2\u6536\u5230\u60a8\u7684\u90ae\u4ef6\uff0c\u7a0d\u540e\u56de\u590d\u3002"
    return "We have received your email and will get back to you shortly."


def detect_language(text: str) -> str:
    """Use a Chinese-character ratio above 15% for fallback language detection."""
    if not text:
        return "en"
    chinese = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    letters = sum(1 for ch in text if ch.isalpha() or "\u4e00" <= ch <= "\u9fff")
    if letters == 0:
        return "en"
    return "zh" if chinese / letters > 0.15 else "en"


class LLMClient:
    """OpenAI-compatible client wrapper with injectable transport for tests."""

    def __init__(
        self,
        client: Any,
        model: str,
        *,
        sleep: Any = time.sleep,
        known_sources: Optional[Sequence[str]] = None,
        provider: str = "openai",
    ) -> None:
        self.client = client
        self.model = model
        if provider not in {"openai", "deepseek"}:
            raise ValueError("provider must be openai or deepseek")
        self.provider = provider
        # Validate model references against the actual loaded source files.
        self.known_sources = list(known_sources) if known_sources is not None else None
        # Use real backoff in production and an explicit no-op in tests.
        self._sleep = time.sleep if sleep is None else sleep

    def _call_json(self, messages: Sequence[Dict[str, str]]) -> Dict[str, Any]:
        """Request and parse a JSON object; preserve retry classifications."""
        try:
            # DeepSeek uses JSON mode; both providers still pass local schema validation.
            output_options: Dict[str, Any] = {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "brewlab_reply", "strict": True, "schema": REPLY_SCHEMA},
                }
            }
            if self.provider == "deepseek":
                output_options = {
                    "response_format": {"type": "json_object"},
                    "max_tokens": 2048,
                    "extra_body": {"thinking": {"type": "disabled"}},
                }
            response = self.client.chat.completions.create(
                model=self.model,
                messages=list(messages),
                **output_options,
                temperature=0.2,
            )
        except Exception as exc:  # Map exceptions lazily without requiring SDK-specific exception imports.
            raise _classify_error(exc) from exc

        content = ""
        try:
            content = response.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected response structure: {exc}") from exc
        if not content.strip():
            raise LLMError("The model returned empty content")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model output is not valid JSON: {content[:200]!r}") from exc

    def generate(
        self,
        *,
        system_prompt: str,
        history: Sequence[HistoryTurn],
        user_content: str,
    ) -> LLMResult:
        """Generate a validated reply with up to two eligible retries."""
        # Include an explicit format example for JSON-mode providers, before variable input.
        if self.provider == "deepseek":
            system_prompt += (
                '\nJSON format example for an unsupported question: '
                '{"intent":"handoff","language":"en","confidence":0.2,'
                '"needs_human":true,"knowledge_used":[],"reply":'
                + json.dumps(HANDOFF_REPLY_EN) + '}\n'
            )
        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for turn in history:
            role = "assistant" if turn.role in ("assistant", "bot") else "user"
            messages.append({"role": role, "content": turn.content})
        messages.append({"role": "user", "content": user_content})

        try:
            # Invalid structured output shares the same retry budget as API failures.
            result = call_with_retry(
                lambda: validate_payload(self._call_json(messages)),
                operation="LLM generate reply",
                non_retryable=(AmbiguousOutcomeError, NonRetryableError),
                sleep=self._sleep,
            )
        except RetryExhaustedError as exc:
            # Expose exhausted retries through the common LLMError contract.
            raise LLMError(str(exc)) from exc
        except NonRetryableError as exc:
            # Do not retry invalid credentials or parameters; still allow fallback handling.
            raise LLMError(str(exc), non_retryable=True) from exc
        except AmbiguousOutcomeError as exc:
            # An uncertain billed request is handled as a failure without another submission.
            raise LLMError(f"Uncertain model request outcome: {exc}") from exc
        except LLMError:
            raise
        return apply_safety_rules(result, known_sources=self.known_sources)


def _classify_error(exc: BaseException) -> BaseException:
    """Map SDK errors to retryable, non-retryable or uncertain outcomes."""
    name = type(exc).__name__
    # Known temporary provider or transport failures use the bounded retry budget.
    retryable_names = {
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
        "APIConnectionError",
        "Timeout",
        "TimeoutError",
        "ReadTimeout",
        "ConnectTimeout",
    }
    non_retryable_names = {
        "AuthenticationError",
        "PermissionDeniedError",
        "BadRequestError",
        "NotFoundError",
        "UnprocessableEntityError",
    }
    if name in non_retryable_names:
        return NonRetryableError(f"Non-retryable model error ({name}）：{exc}")
    if name in retryable_names:
        return exc
    # Unknown errors retain their original type within the bounded retry budget.
    return exc
