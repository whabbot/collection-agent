# Setup, accounts and inputs

## Credentials rule

**No secrets in these docs or in chat.** The repo will have a `.env.example`; real values go in a local `.env` and in the host's secret store.

Environment variables the service will need:

```
STRIPE_SECRET_KEY=          # test mode first (sk_test_...)
STRIPE_WEBHOOK_SECRET=
POSTMARK_SERVER_TOKEN=      # or chosen provider
ANTHROPIC_API_KEY=
COGNEE_BASE_URL=
COGNEE_API_KEY=
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
| Stripe | Test-mode secret key, webhook signing secret; built-in reminder emails off; Automations for `invoice.overdue` | Needed |
| Email domain | Subdomain (e.g. `billing.<domain>`) with SPF, DKIM, DMARC, plus MX for inbound | Needed |
| Email provider | Postmark (or chosen) account, server token, inbound webhook URL | Needed |
| Anthropic API | API key | Needed |
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
