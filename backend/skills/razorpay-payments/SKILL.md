---
name: razorpay-payments
title: Taking payments with Razorpay
description: Use when the build takes payments in India through Razorpay — UPI, cards, netbanking.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [razorpay]
fixes: [razorpay, signature, order id, key secret]
---

`RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` are read on the server; the browser reads
only `NEXT_PUBLIC_RAZORPAY_KEY_ID`, which is public. The secret never leaves the server.

Create an order on the server first (`razorpay.orders.create({ amount, currency: "INR",
receipt })`) with the amount in paise, looked up on the server. Pass only the order id
and key id to Razorpay Checkout in the page.

After payment, the page receives `razorpay_payment_id`, `razorpay_order_id` and
`razorpay_signature`. Verify the signature on the server — HMAC-SHA256 of
`order_id + "|" + payment_id` with the key secret, compared in constant time — before
marking anything paid. An unverified success callback is not a payment.

Keys starting `rzp_test_` never move real money; UPI test ids like `success@razorpay`
succeed in test mode. Handle `payment.failed` in the page with a message, not silence.
