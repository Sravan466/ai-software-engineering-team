---
name: anthropic-api
title: Calling Claude through the Anthropic API
description: Use when the build has AI features on Anthropic's Claude models.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [anthropic, claude]
fixes: [anthropic, anthropic_model, hardcoded model, x-api-key]
---

Read `ANTHROPIC_API_KEY` on the server only; the browser calls the app's own API route.
Read the model from `ANTHROPIC_MODEL` and never write a model name into the code — if it
is unset, return a clear error saying to set it.

Create one client (`new Anthropic()` reads the key by that name) and call
`client.messages.create({ model: process.env.ANTHROPIC_MODEL, max_tokens, system,
messages })`. `max_tokens` is required. Put the app's instructions in `system`, and the
user's text only in a user message — never concatenated into the system prompt.

Stream long answers with `client.messages.stream(...)` through the route. Handle 429 and
529 (overloaded) with a retry-after message, rate limit the route per user, cap input
length before sending, and never log prompts that hold personal data.
