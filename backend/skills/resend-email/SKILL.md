---
name: resend-email
title: Sending email with Resend
description: Use when the build sends transactional email — confirmations, receipts, password resets — through Resend.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [resend]
fixes: [resend, email from, sender, domain not verified]
---

Read the key from `RESEND_API_KEY`, on the server only, and the sender from
`EMAIL_FROM`. Email is always sent from server code — an API route, a server action
or the backend — never from the browser, where the key would be public.

Create one client, `new Resend(process.env.RESEND_API_KEY)` (Python: `resend.api_key =
os.environ["RESEND_API_KEY"]`), and send with `resend.emails.send({ from, to, subject,
html })`. Check the returned `error` instead of assuming success, and log only the
message id — never the recipient list or the body.

The sender matters: until the account has verified its own domain, only
`onboarding@resend.dev` works, and only to the account owner's own address. Default
`from` to `process.env.EMAIL_FROM || "onboarding@resend.dev"` so a fresh install sends.

A sending-only key can send and nothing else: it can't list domains or read emails,
so don't call those endpoints at startup to "check" the key.

Send email as a side effect of something that already succeeded — after the booking
is saved, after the payment is confirmed — and don't fail that action when the email
fails. Record that it failed and move on.

Escape anything a user typed before it goes into the HTML, and never put a password,
a full token or a card detail in an email. A magic link carries a single-use,
short-lived token that the server checks and then discards.
