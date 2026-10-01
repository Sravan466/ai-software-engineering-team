---
name: stripe-payments
title: Taking payments with Stripe
description: Use when the build takes payments, subscriptions or checkout through Stripe.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [stripe]
fixes: [stripe, webhook signature, constructevent, publishable key, secret key]
---

Read the keys from exactly the names the charter gives: `STRIPE_SECRET_KEY` on the
server only, `NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY` in the browser, and the optional
`STRIPE_WEBHOOK_SECRET` on the server. The secret key never appears in a client
component, a `NEXT_PUBLIC_` variable or anything sent to the page.

Create one server-side client, `new Stripe(process.env.STRIPE_SECRET_KEY)` (Python:
`stripe.api_key = os.environ["STRIPE_SECRET_KEY"]`), and fail with a clear message when
the key is missing rather than crashing on the first request.

Start a payment on the server: an API route or server action creates a Checkout
Session (`mode: "payment"` or `"subscription"`, `line_items` with a `price`, and
`success_url` / `cancel_url`) and returns its `url`; the page redirects to it. Never
take an amount from the browser for a fixed product — look the price up on the server.

Confirm payment without a webhook first: on the success page, retrieve the session by
the `session_id` in the URL and check `payment_status === "paid"` (or the subscription's
`status`). This works the moment keys exist, with no endpoint to register.

When `STRIPE_WEBHOOK_SECRET` is set, verify every webhook against the **raw** request
body: `stripe.webhooks.constructEvent(rawBody, signatureHeader, secret)`. A body that
was parsed as JSON first fails verification. In a Next.js route handler read
`await req.text()`; in Express mount `express.raw({ type: "application/json" })` on
that one route. Handle `checkout.session.completed` and subscription updates, and make
handlers idempotent — Stripe retries, so record the event id and skip one seen before.

Pass an idempotency key when creating charges or sessions from a retried action.

For a test run, card 4242 4242 4242 4242 with any future date and any CVC succeeds.
Keys starting `sk_test_` / `pk_test_` never move real money.
