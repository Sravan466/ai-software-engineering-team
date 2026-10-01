---
name: paypal-checkout
title: Taking payments with PayPal
description: Use when the build takes payments through PayPal checkout.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [paypal]
fixes: [paypal, capture, sandbox, client secret]
---

Read `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` and `PAYPAL_ENV` (`sandbox` or `live`) on
the server only. The API host follows the environment: `api-m.sandbox.paypal.com` for
sandbox, `api-m.paypal.com` for live — never hardcode one.

The flow is two server calls. The page asks your API route to create an order
(`POST /v2/checkout/orders` with `intent: "CAPTURE"` and the amount looked up on the
server, never taken from the browser), PayPal's button approves it, and a second route
captures it (`POST /v2/checkout/orders/{id}/capture`). Only a capture with status
`COMPLETED` means the money moved — record the order then, not when the button closes.

Get an access token with the client ID and secret (client-credentials grant) and cache
it until it expires; don't fetch one per request. The browser's PayPal script needs only
the client ID, which is public.

Use idempotency (`PayPal-Request-Id`) on create and capture so a retried request doesn't
charge twice. Sandbox buyer accounts from the developer dashboard pay test orders.
