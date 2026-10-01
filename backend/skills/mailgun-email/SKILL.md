---
name: mailgun-email
title: Sending email with Mailgun
description: Use when the build sends email through Mailgun.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [mailgun]
fixes: [mailgun, region, eu endpoint, sending domain]
---

Read `MAILGUN_API_KEY`, `MAILGUN_DOMAIN` and `MAILGUN_REGION` on the server only.

The region decides the endpoint: `https://api.eu.mailgun.net` for `eu`, otherwise
`https://api.mailgun.net`. A key used against the wrong region gets 401, so build the
client's `url` from `MAILGUN_REGION` rather than assuming the US.

With mailgun.js: `new Mailgun(FormData).client({ username: "api", key, url })`, then
`mg.messages.create(process.env.MAILGUN_DOMAIN, { from, to, subject, html })`. The from
address must be on the sending domain.

Send from an API route after the triggering action succeeds; log the message id, never
the body or recipients. Sandbox domains only deliver to authorised recipients — say so
when a send to someone else is rejected.
