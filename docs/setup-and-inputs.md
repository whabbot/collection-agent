# Setup, accounts and inputs

## Credentials rule

**No secrets in these docs or in chat.** The repo will have a `.env.example`; real values go in a local `.env` and in the host's secret store.

Environment variables the service will need:

```
STRIPE_SECRET_KEY=          # test mode first (sk_test_...)
STRIPE_WEBHOOK_SECRET=
ADMIN_API_TOKEN=             # bearer token for internal endpoints (POST /invoices)
POSTMARK_SERVER_TOKEN=      # or chosen provider
POSTMARK_MESSAGE_STREAM=    # optional, defaults to outbound
EMAIL_FROM_ADDRESS=         # confirmed Postmark sender signature
EMAIL_FROM_NAME=            # sender display name (person or team)
REPLY_TO_DOMAIN=            # inbound domain for reply+<token>@<domain>
COMPANY_NAME=               # used in the invoice email subject and sign-off
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=            # optional, defaults to claude-opus-5-5
TYPESAFE_API_KEY=           # Jev reply classifier (name the SDK reads)
JEV_MODEL=                  # optional, defaults to jev-1.13.0 (pinned)
CLASSIFIER_MIN_CONFIDENCE=  # optional, defaults to 0.6; below this a reply goes to a human
INBOUND_WEBHOOK_USER=       # HTTP Basic user Postmark sends to /webhooks/inbound
INBOUND_WEBHOOK_PASSWORD=   # HTTP Basic password for the same
COGNEE_BASE_URL=
COGNEE_API_KEY=
COGNEE_POLICIES_DATASET=    # optional, defaults to policies
DATABASE_URL=
```

## Cognee (Cognee Cloud)

- Tenant created; API key verified on 15 Oct 2026 (`GET /api/v1/datasets/` returned 200).
- Tenant currently holds one empty dataset: `default_dataset`.
- **Rotate the API key** — it was pasted into chat on 15 Oct. Store the new one only in `.env` / host secrets.
- Auth: `X-Api-Key: <key>` header; endpoints under `/api/v1/...` with trailing slashes.

### Re-creating the setup in a new session

Workspace files don't carry between sessions, so in a new session:

1. Write the credentials file (substitute the tenant URL and the **rotated** key — get them from the Cognee dashboard, not from chat history):

```bash
mkdir -p ~/.cognee && ( umask 077; { grep -vE '^[[:space:]]*(export[[:space:]]+)?(COGNEE_BASE_URL|COGNEE_API_KEY)=' ~/.cognee/.env 2>/dev/null; printf '%s\n' 'COGNEE_BASE_URL="<tenant url>"' 'COGNEE_API_KEY="<api key>"'; } > ~/.cognee/.env.new ) && mv ~/.cognee/.env.new ~/.cognee/.env && chmod 600 ~/.cognee/.env
```

2. Install the Claude Code plugin (optional; gives Claude memory tools in sessions started afterwards):

```bash
claude plugin marketplace add topoteretes/cognee-integrations
claude plugin install cognee-memory@cognee
```

The plugin's 5 "unset" config options (icon, status row, spinner, tools, first-run tour) are display options with fine defaults — no configuration needed.

3. Verify:

```bash
set -a; . ~/.cognee/.env; set +a
curl -sS -H "X-Api-Key: $COGNEE_API_KEY" "$COGNEE_BASE_URL/api/v1/datasets/"
```

## Accounts still needed

| Account | What's needed | Status |
|---|---|---|
| Cognee Cloud | Tenant + API key | Done (rotate key) |
| GitHub | New repo; attach it to the session | Needed |
| Stripe | A **sandbox** (not the shared test mode) with a **restricted key** (`rk_…`) in `STRIPE_SECRET_KEY`; a webhook endpoint created on API version **the API version the installed `stripe` SDK pins (`2026-09-30.endive` as of Oct 2026)** (so payloads match the test fixtures) and its signing secret in `STRIPE_WEBHOOK_SECRET`; built-in reminder emails off. Invoices are created with `auto_advance=false`, so Stripe doesn't email or remind | Needed |
| Email domain | Subdomain (e.g. `billing.<domain>`) with SPF, DKIM, DMARC, plus MX for inbound | Needed |
| Email provider | Postmark (or chosen) account, server token, inbound webhook URL | Needed |
| Anthropic API | API key | Needed |
| Jev (TypeSafe) | API key | User has one. When the reply-classifier slice starts, add `TYPESAFE_API_KEY=` to `.env.example` and tell the user to put the key in `.env` as `TYPESAFE_API_KEY` |
| Postgres | Managed DB from host, or local Docker for dev | Needed |
| Hosting | Render / Railway / Fly (worker + cron) | Needed |

## Inputs still needed

- **Policy documents** as they exist today, even rough — loaded into Cognee `policies`; hard rules extracted from them.
- **A sample contract** (anonymised fine) — for contract Q&A and clause quoting.
- **2–3 chase emails you'd be happy to send, and one you'd hate** — sets tone.
- **10–20 real customer replies to chase emails** (anonymised) — test cases for the reply classifier. If none, Claude writes synthetic ones for review.
- **Escalation contacts** — who gets disputes, who gets extension requests.

## Notes on the build environment

- The cloud workspace's shell can reach the Cognee Cloud tenant, but generally not Stripe or other APIs (network allowlist). Build and test against recorded Stripe webhook fixtures and mocked LLM responses; do the first live end-to-end run in Stripe test mode on a local machine or the deployed host.
