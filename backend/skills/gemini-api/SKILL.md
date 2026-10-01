---
name: gemini-api
title: Calling Google Gemini
description: Use when the build has AI features on Google's Gemini models.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [gemini]
fixes: [gemini, gemini_model, api key not valid, hardcoded model]
---

Read `GEMINI_API_KEY` on the server only; the browser calls your API route, never Google
directly. Read the model from `GEMINI_MODEL` — never hardcode a model name, and return a
clear error when it is unset.

With `@google/generative-ai`: `new GoogleGenerativeAI(key).getGenerativeModel({ model:
process.env.GEMINI_MODEL, systemInstruction })`, then `generateContent` or
`generateContentStream` for streaming through the route. Keep the app's instructions in
`systemInstruction` and the user's text in the contents.

Check `response.promptFeedback` and finish reasons: a blocked answer is not an error to
retry, it is a message to show. Rate limit the route, cap input length, and handle 429.
