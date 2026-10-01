---
name: sendgrid-email
title: Sending email with SendGrid
description: Use when the build sends transactional email through SendGrid.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [sendgrid]
fixes: [sendgrid, sender identity, verified sender]
---

Read `SENDGRID_API_KEY` and `EMAIL_FROM` on the server only; email is sent from an API
route or the backend, never the browser.

Set the key once (`sgMail.setApiKey(process.env.SENDGRID_API_KEY)`) and send with
`sgMail.send({ to, from: process.env.EMAIL_FROM, subject, html })`. The `from` address
must be a verified sender or on an authenticated domain, or SendGrid rejects the send
with 403 — say so in the error, it's the usual first failure.

Catch the error and log `response.body.errors` messages, never the message content or
recipient list. Send as a side effect of something that already succeeded, and don't
fail that action if the email fails. Escape user-typed text before it goes into HTML.
