---
name: auth0-auth
title: Sign-in with Auth0
description: Use when the build's sign-in comes from Auth0.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [auth0]
fixes: [auth0, callback url, auth0_secret, session]
---

Auth0 is the sign-in system: don't also store passwords. Keep your own data keyed by the
user's `sub` from the session.

The Next.js SDK (`@auth0/nextjs-auth0` v4) reads `AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`,
`AUTH0_CLIENT_SECRET`, `AUTH0_SECRET` and `APP_BASE_URL` by those exact names. Create one
`new Auth0Client()` in `lib/auth0`, and add `middleware.ts` that returns
`auth0.middleware(request)` — it mounts `/auth/login`, `/auth/logout` and
`/auth/callback`. All of these values stay on the server.

Read the session on the server with `await auth0.getSession()` and refuse the request
when it is null; never trust a user id sent by the browser. Link to `/auth/login` and
`/auth/logout` with plain anchors.

The callback URL `APP_BASE_URL + /auth/callback` must be in the application's Allowed
Callback URLs, or sign-in fails with a callback mismatch.
