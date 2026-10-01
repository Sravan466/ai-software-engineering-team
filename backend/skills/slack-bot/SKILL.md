---
name: slack-bot
title: Posting to Slack
description: Use when the build posts notifications or messages to Slack.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [slack]
fixes: [slack, xoxb, not_in_channel, chat.postMessage]
---

`SLACK_BOT_TOKEN` is read on the server only, and `SLACK_CHANNEL_ID` names the channel —
a setting, not a hardcoded id.

`new WebClient(token).chat.postMessage({ channel, text, blocks })`. Always send `text`
too: it is the notification and the fallback for blocks. Slack answers errors with a 200
and `ok: false`; check `ok`, and turn `not_in_channel` into "invite the bot to the
channel" and `invalid_auth` into "the token is wrong".

Post from a background step after the event succeeded, so a slow Slack never slows the
request that caused it. Escape user text for Slack's mrkdwn (`&`, `<`, `>`) before
putting it in a message.
