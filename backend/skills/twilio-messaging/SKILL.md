---
name: twilio-messaging
title: SMS and WhatsApp with Twilio
description: Use when the build sends SMS or WhatsApp messages through Twilio.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [twilio]
fixes: [twilio, auth token, e.164, from number]
---

`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_PHONE_NUMBER` are read on the server
only. Messages are sent from an API route or the backend, never the browser.

`twilio(sid, token).messages.create({ from: process.env.TWILIO_PHONE_NUMBER, to, body })`.
Phone numbers are E.164 (`+15551234567`): normalise and validate them before sending.
WhatsApp uses `whatsapp:+…` on both numbers.

Trial accounts only send to verified numbers — say so when a send is refused with 21608.
Rate limit anything that sends a code, and never put a full code or password in a log.
When Twilio calls the app back (status callbacks, replies), verify the
`X-Twilio-Signature` with the auth token before trusting the request.
