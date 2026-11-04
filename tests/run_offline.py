"""Exercise the support workflow with fictional mail and simulated services."""

import sys
import unittest
from pathlib import Path

# Allow direct execution from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCENARIOS = (
    ("Structured pricing decision", "test_llm.LLMClientTests.test_generate_success"),
    ("A pricing inquiry becomes a draft", "test_main_flow.NormalFlowTests.test_pricing_normal_draft_mode_creates_draft_only"),
    ("Three messages produce one reply", "test_portfolio_demo.PortfolioDemo.test_three_questions_one_draft"),
    ("Unknown answer goes to a colleague", "test_main_flow.HandoffTests.test_unknown_question_never_fabricates"),
    ("Two retries before success", "test_main_flow.RetryUtilityTests.test_retry_succeeds_after_two_failures"),
    ("OpenAI outage produces a draft and an alert", "test_main_flow.FailureTests.test_llm_failure_creates_fallback_draft_and_alert"),
    ("An uncertain send is held for reconciliation", "test_main_flow.FailureTests.test_ambiguous_send_not_retried"),
    ("DeepSeek JSON output is locally validated", "test_deepseek.DeepSeekTests.test_json_mode_request_and_validated_reply"),
    ("Live mode sends a thread reply", "test_main_flow.NormalFlowTests.test_live_mode_sends_reply_in_thread"),
    ("Off mode leaves the mailbox untouched", "test_main_flow.NormalFlowTests.test_off_mode_does_nothing"),
)


def main() -> int:
    """Report each scenario and return nonzero if any assertion fails."""
    passed = 0
    for description, case in SCENARIOS:
        print(f"\nBrewLab | {description}", flush=True)
        suite = unittest.defaultTestLoader.loadTestsFromName("tests." + case)
        result = unittest.TextTestRunner(stream=sys.stdout).run(suite)
        passed += int(result.wasSuccessful())
    print(f"\n{passed}/{len(SCENARIOS)} offline scenarios passed.")
    return int(passed != len(SCENARIOS))


if __name__ == "__main__":
    raise SystemExit(main())
