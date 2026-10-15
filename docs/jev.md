# Jev (TypeSafe) — integration notes for the reply classifier

Read from https://docs.typesafe.ai on 15 Oct 2026 (index: https://docs.typesafe.ai/llms.txt). Input for the reply-classifier slice spec.

## What Jev is

A "System One" decision model. One call sends a **state** (text or JSON) plus named **questions**; each question is answered independently and in parallel against the same state. It **does not generate text**.

| Question type | Use here | Returns |
|---|---|---|
| `Choice` | Intent (one of a fixed set) | `choice`, per-option `probabilities`, `confidence` |
| `Noul` | Yes/no checks (e.g. "the sender says they have already paid") | `noul` (0–1 probability of yes) |
| `Score` | Ordered levels (probably not needed) | `score`, `probabilities`, `confidence` |

`confidence` for a Choice is the peak probability normalised against the number of options. This is what `ReplyClassification.confidence` should hold, and what per-intent auto-send thresholds gate on.

## Python SDK

- Package: `typesafe-sdk` (`uv add typesafe-sdk`), import `typesafe_sdk`. Sync `TypeSafeClient` and async `AsyncTypeSafeClient`; `client.system_one(state=..., questions={...})`; answers in `response.choices[...]`, `response.nouls[...]`, `response.scores[...]`.
- Env vars the SDK reads: **`TYPESAFE_API_KEY`**, `TYPESAFE_BASE_URL` (default `https://api.typesafe.ai`), `TYPESAFE_DEFAULT_MODEL`. Default timeout 10 s per HTTP operation. `RetryPolicy` for retries; typed exceptions for rate limits/timeouts.
- Models: `jev-latest` → `jev-1.13.0` (stable). `jev-1.14.0` also listed. **Pin an explicit version** so logged classification accuracy maps to one model.
- Context: 64k tokens per request, 32k per question. An email reply is well within that.
- A Claude Code skill exists (`typesafe-ai/skills`, raw SKILL.md on GitHub) for agents writing TypeSafe code.

## Known weaknesses (jev-1.13) and what they mean for us

| Weakness | Consequence for this project |
|---|---|
| **No generation** | No free-text output, so `ReplyClassification` has no `summary` field (dropped for v1). |
| **Dates are read as text; date comparisons are unreliable** | `promised_date` must be extracted as Choices over date parts (absolute/relative/none, month, day, weekday anchor, with a "not stated" option), then assembled and validated **in code** against the email's received date. TypeSafe's date-extraction cookbook does exactly this. Then `pause_chasing` validates it against `ChasePolicy`, as already planned. |
| **Literal reading** | Each intent option needs an explicit description with boundary cases (e.g. "promise to pay" vs "extension request" vs "already paid"). |
| **Choice option order bias** (leans to the first option) | Tests should reorder options and check the answer is stable. |
| **Adversarial content** — state isn't treated as hostile | Matches our "customer emails are untrusted" rule. Safety still comes from the code routing table, the no-money-tools rule and the approval queue, not from Jev. Include injection-style test emails. |
| **Irrelevant context hurts accuracy** | Send only the latest reply body (quoted history stripped) plus minimal invoice facts, not the whole thread. |

## Data handling

Customer emails will be sent to TypeSafe. Per their legal page: a Data Processing Agreement exists, they commit not to train on user data, and zero data retention is enterprise-only. Check the DPA fits before real customer data is sent.

## Decided (15 Oct 2026)

- Key lives in `TYPESAFE_API_KEY`, the name the SDK reads.
- `summary` is dropped from `ReplyClassification` for v1; reviewers read the email itself.
