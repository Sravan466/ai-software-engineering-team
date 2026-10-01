---
name: openai-api
title: Calling OpenAI from the app
description: Use when the build has AI features — chat, summaries, generated text — through the OpenAI API.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [openai]
fixes: [openai, api key, openai_model, hardcoded model]
---

Read the key from `OPENAI_API_KEY`, on the server only. The browser never calls OpenAI
directly: it calls the app's own API route, which calls OpenAI. A key in the bundle is
a key anyone can spend.

Read the model from `OPENAI_MODEL`. Never write a model name into the code; if the
variable is unset, return a clear error saying to set it rather than guessing one.

Create one client, `new OpenAI({ apiKey: process.env.OPENAI_API_KEY })` (Python:
`OpenAI()` reads the same variable), and call `client.chat.completions.create({ model:
process.env.OPENAI_MODEL, messages })`.

Stream long answers through the API route: request with `stream: true`, forward the
chunks as a streaming response, and render them as they arrive. Set a timeout and a
`max_tokens` cap on every call.

Treat the user's text as data. Put the app's instructions in the system message,
never concatenate user input into it, and don't let model output run as code, SQL or
HTML without the same escaping any user input gets.

Rate limit the route per user or per IP, check the input length before sending it,
and handle the provider's errors (401, 429, 5xx) with a message the page can show.
Never log a prompt that holds personal data, or the key.
