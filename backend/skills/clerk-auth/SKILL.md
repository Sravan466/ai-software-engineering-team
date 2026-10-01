---
name: clerk-auth
title: Sign-in with Clerk
description: Use when the build's user accounts and sign-in come from Clerk.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [clerk]
fixes: [clerk, clerkmiddleware, publishable key, auth middleware]
---

Clerk is the whole sign-in system: don't also write password hashing, sessions or a
users table for credentials. Store your own data keyed by Clerk's `userId`.

The keys: `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` is the only one the browser sees, and
`CLERK_SECRET_KEY` is read only on the server. Both are read by Clerk's SDK from those
exact names, so they need no code to load.

In Next.js (App Router): wrap the root layout in `<ClerkProvider>`, add `middleware.ts`
exporting `clerkMiddleware()` with a `config.matcher` that covers the app and its API
routes, and protect routes with `createRouteMatcher([...])` plus `auth.protect()`
inside the middleware. Use `<SignIn />` and `<SignUp />` on their own pages and
`<UserButton />` in the header.

On the server, get the signed-in user with `const { userId } = await auth()` from
`@clerk/nextjs/server` and refuse the request when it is null. Never trust a user id
sent by the browser in a body or query — take it from `auth()`.

With a separate backend, the frontend sends the session token as a bearer token and
the backend verifies it with Clerk's backend SDK and `CLERK_SECRET_KEY` before reading
the user id.

Keys starting `pk_test_` / `sk_test_` belong to a development instance; production
keys come from a production instance with its own domain.
