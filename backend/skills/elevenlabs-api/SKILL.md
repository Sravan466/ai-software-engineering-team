---
name: elevenlabs-api
title: Text to speech with ElevenLabs
description: Use when the build turns text into speech with ElevenLabs.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [elevenlabs, eleven labs]
fixes: [elevenlabs, xi-api-key, voice id]
---

Read `ELEVENLABS_API_KEY` on the server only; the page asks your API route for audio and
plays what it returns.

Convert on the server (`client.textToSpeech.convert(voiceId, { text, modelId })` or the
streaming variant) and stream the audio back with `Content-Type: audio/mpeg`. Keep the
voice id as data or a setting, not scattered through the code.

Speech is billed by characters: cap the text length, cache audio by a hash of
(voice, text) so the same sentence isn't paid for twice, and rate limit the route. A 401
with `missing_permissions` means the key lacks Text to Speech access — say so.
