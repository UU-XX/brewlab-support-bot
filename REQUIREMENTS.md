# Functional scope

BrewLab is a Python 3.11+ portfolio project integrating Gmail OAuth, OpenAI structured decisions and Google Sheets configuration and audit logs.

- Poll unprocessed messages carrying the `support` label every 60 seconds.
- Group messages from the same sender until 120 seconds after their latest message.
- Use up to 20 messages of thread history and four Markdown knowledge files.
- Classify pricing, after-sales, casual conversation and human handoff.
- Support draft, live and off modes, operating days, hours and time zone.
- Escalate keyword matches, unsupported questions and low-confidence decisions.
- Retry eligible failures twice, preserve uncertain outcomes and attempt a fallback draft and administrator alert when appropriate.
- Log each original message separately, including source identifiers for reconciliation.
- Keep credentials, personal data and runtime state out of the public repository.

Automated tests use simulated services. A recorded Gmail demonstration must use a separately authorized test mailbox. See [the demonstration guide](docs/DEMO.md) for the recording plan and evidence requirements.
