---
name: lemonsqueezy-payments
title: Selling with Lemon Squeezy
description: Use when the build sells products or subscriptions through Lemon Squeezy checkout.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [lemon squeezy, lemonsqueezy]
fixes: [lemon squeezy, webhook signature, x-signature, variant id]
---

Read `LEMONSQUEEZY_API_KEY` and `LEMONSQUEEZY_STORE_ID` on the server only, and the
optional `LEMONSQUEEZY_WEBHOOK_SECRET`. Call the API with `Authorization: Bearer` and
`Accept: application/vnd.api+json` — it speaks JSON:API.

Create a checkout on the server (`POST /v1/checkouts` with the store and the variant
relationship) and send the browser to its `url`. Put your own user id in
`checkout_data.custom` so the webhook can tell whose order it is.

Lemon Squeezy is the merchant of record: it charges tax and sends receipts, so the app
doesn't. It tells you about orders and subscriptions by webhook. Verify the
`X-Signature` header — HMAC-SHA256 of the raw body with the webhook secret, compared in
constant time — before trusting `order_created` or `subscription_updated`. Without a
webhook secret, read the order back from the API after the redirect instead.
