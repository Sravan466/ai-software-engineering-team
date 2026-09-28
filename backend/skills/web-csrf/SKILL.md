---
name: web-csrf
title: Protecting state-changing requests from CSRF
description: Use when an app keeps a session in a cookie and has forms or endpoints that create, change or delete anything.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [csrf, xsrf, cross-site request forgery, cookie, session, form, login, auth, authentication]
fixes: [csrf, xsrf, cross-site request forgery, request forgery]
rejects: [helmet, csurf, lusca]
---

CSRF only matters when the browser sends credentials by itself — a session cookie. An
API that reads a bearer token from an `Authorization` header the page sets is not
exposed; say so rather than bolting a token onto it.

When cookies carry the session, every state-changing route (POST, PUT, PATCH, DELETE)
checks a CSRF token. Use one of the two patterns OWASP recommends:

Synchronizer token (server keeps state): generate a random token per session, store it
in the session, render it into each form or hand it to the SPA from a GET endpoint, and
reject any unsafe request whose `X-CSRF-Token` header or form field does not match.
For Express, `csrf-sync` implements this.

Signed double-submit cookie (stateless): set a random token in a cookie signed with a
server secret and bound to the session id, have the client echo it in a header, and
compare the two in constant time. For Express, `csrf-csrf` implements this. Django,
Rails, Laravel and Spring ship their own middleware — turn it on, never off.

On the client, read the token once and send it on every unsafe request through the one
fetch or axios wrapper the app uses, so no call can forget it.

Set the session cookie `HttpOnly`, `Secure` and `SameSite=Lax` (or `Strict`). SameSite
is defence in depth on top of a token, never a replacement for one. Keep GET requests
free of side effects, because no token check will run on them.

Two common pieces of advice are wrong. Helmet only sets response headers and provides
no CSRF protection at all. `csurf` is deprecated and archived; do not add it.
