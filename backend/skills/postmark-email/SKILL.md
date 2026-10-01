---
name: postmark-email
title: Sending email with Postmark
description: Use when the build sends transactional email through Postmark.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [postmark]
fixes: [postmark, sender signature, server token]
---

Read `POSTMARK_SERVER_TOKEN` and `EMAIL_FROM` on the server only. The server token sends
from one Postmark server; it never reaches the browser.

Create one client (`new postmark.ServerClient(process.env.POSTMARK_SERVER_TOKEN)`) and
send with `client.sendEmail({ From: process.env.EMAIL_FROM, To, Subject, HtmlBody,
MessageStream: "outbound" })`. The From address needs a confirmed sender signature or a
verified domain.

Postmark answers errors with an `ErrorCode`; 300-series codes are bad input (an invalid
address), 406 is an inactive recipient who bounced — don't retry those. Send after the
action that triggers the email has succeeded, and don't let a failed email undo it.
