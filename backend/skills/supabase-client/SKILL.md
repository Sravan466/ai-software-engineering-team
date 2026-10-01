---
name: supabase-client
title: Building on Supabase
description: Use when the build uses Supabase for its data, sign-in, storage or realtime.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [supabase]
fixes: [supabase, row level security, service role, anon key, rls]
---

The browser gets `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY` — the
publishable key, safe in the bundle *only because* row-level security protects the
tables. `SUPABASE_SERVICE_ROLE_KEY` bypasses every policy: server code only, for admin
jobs, never in a client component or a `NEXT_PUBLIC_` variable.

In Next.js use `@supabase/ssr`: `createBrowserClient(url, anonKey)` in client components,
and `createServerClient(url, anonKey, { cookies })` in server components, route handlers
and middleware, so the session lives in cookies and is refreshed by the middleware.

Turn on row-level security on every table the app touches, and write a policy per action
(`select`, `insert`, `update`, `delete`) keyed on `auth.uid()`. A table without RLS is
readable by anyone holding the public key. Ship the schema and policies as SQL migration
files rather than dashboard clicks.

On the server, get the user with `supabase.auth.getUser()` — it checks the token with
Supabase — rather than trusting `getSession()` alone. Store files in buckets with storage
policies the same way, and keep the object path, not a public URL, in your data.
