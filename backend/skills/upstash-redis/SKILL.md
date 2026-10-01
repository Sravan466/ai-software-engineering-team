---
name: upstash-redis
title: Caching and rate limits with Upstash Redis
description: Use when the build caches data or rate limits requests with Upstash Redis.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [upstash]
fixes: [upstash, rate limit, rest token]
---

`UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` are read on the server only.
`Redis.fromEnv()` reads them by those names; it talks HTTP, so it works in serverless and
edge routes with no connection pool.

Rate limit with `@upstash/ratelimit`: `new Ratelimit({ redis, limiter:
Ratelimit.slidingWindow(10, "10 s") })`, keyed by user id or IP, and return 429 with a
Retry-After when `success` is false.

For caching, set a TTL on every key (`set(key, value, { ex: 300 })`) and namespace keys
by feature. The cache is a copy: the app must still work, slower, when Redis is down.
