---
name: groq-api
title: Calling models on Groq
description: Use when the build's AI features run on Groq.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [groq]
fixes: [groq, groq_model, hardcoded model]
---

Read `GROQ_API_KEY` on the server only and the model from `GROQ_MODEL` — never a
hardcoded model name; return a clear error when it is unset.

`new Groq()` reads the key by that name; `client.chat.completions.create({ model:
process.env.GROQ_MODEL, messages })` is OpenAI-shaped. Groq is fast, so streaming is
optional, but long answers still stream through an API route rather than holding the
request.

Models come and go on Groq: when the configured one returns 404 `model_not_found`, say
which variable to change. Rate limit the route, set `max_tokens`, cap input length, and
keep user text out of the system message.
