---
name: replicate-api
title: Generating images and media with Replicate
description: Use when the build generates images, audio or video through Replicate.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [replicate]
fixes: [replicate, prediction, polling, model version]
---

Read `REPLICATE_API_TOKEN` on the server only. The page asks your API route to start a
generation; it never calls Replicate itself.

Start a prediction on the server (`replicate.predictions.create({ version or model,
input })`) and return its id. Poll its status from the page through your route — or use
a webhook when the app has a public URL — until it is `succeeded`, `failed` or
`canceled`. Don't hold one request open for a long run.

Pin the model version in code so output doesn't change under you. Outputs are temporary
URLs: copy what you keep to your own storage. Rate limit generation per user and cap
input sizes — every prediction costs money.
