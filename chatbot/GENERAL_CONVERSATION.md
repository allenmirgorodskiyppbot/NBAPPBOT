# General conversation

Ordinary messages can use a semantic plan rather than a separate keyword rule for
each sentence. The plan can select up to three typed, read-only readers for
player logs, stored-history coverage, profiles, team finals, the next fixture,
the Knicks season calendar, availability, strict prop assessments, and saved
research derivatives. Calculations and current pick decisions stay in the
application. Conversation history supplies references, not verified statistics.

Clarification questions retain the player and market for a later short answer.
Changing a player or statistic clears the previous quote. Conceptual questions
and subject changes do not automatically repeat the last pick's schedule hold.

## Conversation engine

The default remains local Ollama. Actual trials of the configured Qwen3 4B
Instruct model and a Qwen2.5 7B Instruct model found substantial mistakes in tool
selection, requested history scope, and follow-up understanding. Passing code
tests does not establish reliable open-ended model behavior.

An optional OpenAI transport is implemented but disabled by default. An owner
must explicitly configure both:

- `DIALOGUE_BACKEND=openai` as a runtime variable.
- `OPENAI_API_KEY` as a private server secret.

`OPENAI_CHAT_MODEL` defaults to `gpt-4.1-mini`. This uses separately billed API
usage, not a ChatGPT subscription. No hosted requests occur merely because a key
exists. Missing or rejected configuration does not fall back to a paid provider.
The transport uses the official HTTPS endpoint, one request without retries,
and a 512-token output limit. A question may use a planning call and an
explanation call. Relevant question/history and selected sports records are
sent to the configured provider; secrets are excluded from status responses.

Hosted conversation can accept questions before the local model finishes
loading. `/ai_status` reports `conversation_engine` configuration separately
from local model readiness. Configuration is not a successful live connection
check. Collected-news selection also uses the selected conversation engine;
the application quotes the dated reports. The original raw worksheet tool loop
still uses the local model and waits for its readiness.

## Validation limits

Automated tests cover read-only behavior, arithmetic and missing values,
reference handling, decision authority, transport isolation, timeouts, and
credential redaction. Hosted transport tests use mock HTTP responses. A real
hosted quality test requires the owner's API setup and authorization for usage
charges. Neither a live hosted test nor an Akash redeployment has been performed
as part of this code change.

Daily refresh, reminders, screenshot/manual offers, and grading remain in their
existing services. This change adds no automatic PrizePicks API connection and
does not place entries.
