---
name: oauth-signin
title: Sign in with Google or GitHub through Auth.js
description: Use when the build signs people in with Google or GitHub accounts.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [sign in with google, sign in with github, google sign-in, github sign-in, google login, github login]
fixes: [redirect_uri_mismatch, auth_secret, nextauth, oauth callback]
---

Use Auth.js (next-auth) with the provider the charter names. The variables are
`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` or `GITHUB_CLIENT_ID` /
`GITHUB_CLIENT_SECRET`, plus `AUTH_SECRET`, which signs the session cookie. All of them
stay on the server.

Configure the provider in one auth config file and mount its route handler at
`app/api/auth/[...nextauth]/route.ts`. The callback URL is
`/api/auth/callback/google` (or `/github`) on the app's address; it must match the one
registered with the provider exactly, or sign-in fails with a redirect mismatch.

Read the session on the server (`getServerSession` / `auth()`) and refuse the request
when there is none. Use the provider's stable user id, not the email, as the key for the
user's own data — emails change. Don't also build a password login unless asked.
