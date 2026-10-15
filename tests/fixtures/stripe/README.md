# Stripe webhook fixtures

These events are **hand-written**, shaped on API version `2026-09-30.endive`, because there was no
Stripe account available when slice 3 was built. They are enough to exercise our handler but they
are not real captures, so field coverage is only what the handler reads.

Replace them with real events captured from a Stripe sandbox:

```
stripe listen --forward-to localhost:8000/webhooks/stripe
stripe trigger invoice.paid
```

`stripe listen --print-json` shows the full payloads to save here. One file per event type:
`invoice.paid`, `invoice.voided`, `invoice.marked_uncollectible`, `invoice.payment_failed`,
`invoice.overdue`, and `customer.created` (an unhandled type). All use the invoice id
`in_fixture_1`, which the tests insert into the DB.
