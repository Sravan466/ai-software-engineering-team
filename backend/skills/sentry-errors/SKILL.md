---
name: sentry-errors
title: Error tracking with Sentry
description: Use when the build reports errors and performance to Sentry.
agents: [backend_engineer, frontend_engineer, devops_engineer, security_engineer]
keywords: [sentry]
fixes: [sentry, dsn, source maps, auth token]
---

`NEXT_PUBLIC_SENTRY_DSN` is public by design and is all runtime reporting needs.
`SENTRY_AUTH_TOKEN` is only for uploading source maps at build time — a build secret,
never in the bundle, and the build must still pass without it.

With `@sentry/nextjs`, initialise in the client, server and edge config files from the
DSN, and wrap the Next config with `withSentryConfig`. Report a caught error with
`Sentry.captureException(error)`; don't swallow errors around it.

Set `sendDefaultPii: false`, scrub request bodies and headers that carry tokens, and keep
the traces sample rate low in production. Tag the release so errors map to the code.
