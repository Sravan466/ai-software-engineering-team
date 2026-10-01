---
name: posthog-analytics
title: Product analytics with PostHog
description: Use when the build captures product analytics or feature flags with PostHog.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [posthog, post hog]
fixes: [posthog, project api key, identify]
---

`NEXT_PUBLIC_POSTHOG_KEY` and `NEXT_PUBLIC_POSTHOG_HOST` are public: the project key can
only send events. Initialise `posthog-js` once on the client with that host, inside a
provider, not on every render.

Capture page views and a few named events that matter (`signed_up`, `checkout_started`)
with plain properties — never passwords, tokens, card details or free text a user typed.
After sign-in call `posthog.identify(userId)`, and `posthog.reset()` on sign-out.

Feature flags read with `useFeatureFlagEnabled` must have a sensible default while they
load. Respect a do-not-track or consent choice if the app has one.
