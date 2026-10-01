---
name: gemini-api
title: Calling Google Gemini
description: Use when the build has AI features on Google's Gemini models.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [google gemini, gemini api]
fixes: [gemini, gemini_model, api key not valid, hardcoded model]
---

Read `GEMINI_API_KEY` on the server only; the browser calls your API route, never Google
directly. Read the model from `GEMINI_MODEL` — never hardcode a model name, and return a
clear error when it is unset.

Use Google's current SDK, `@google/genai` (the older `@google/generative-ai` is
deprecated): `const ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY })`, then
`ai.models.generateContent({ model: process.env.GEMINI_MODEL, contents, config: {
systemInstruction } })`, or `generateContentStream` to stream through the route. Keep the
app's instructions in `systemInstruction` and the user's text in `contents`.

A blocked answer (see the prompt feedback and finish reason) is not an error to retry —
it is a message to show. Rate limit the route, cap input length, and handle 429.
