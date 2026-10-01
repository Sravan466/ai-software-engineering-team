---
name: openrouter-api
title: Calling models through OpenRouter
description: Use when the build's AI features go through OpenRouter.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [openrouter, open router]
fixes: [openrouter, openrouter_model, hardcoded model]
---

Read `OPENROUTER_API_KEY` on the server only and the model from `OPENROUTER_MODEL`
(`provider/model`) — never hardcoded.

OpenRouter is OpenAI-compatible: use the OpenAI SDK with `baseURL:
"https://openrouter.ai/api/v1"` and `apiKey: process.env.OPENROUTER_API_KEY`, then
`chat.completions.create({ model: process.env.OPENROUTER_MODEL, messages })`. Send the
`HTTP-Referer` and `X-Title` headers with the app's own address and name.

A key can have a credit limit: a 402 means it ran out — show that, don't retry. Stream
through an API route, rate limit it, set `max_tokens`, and keep user text out of the
system message.
