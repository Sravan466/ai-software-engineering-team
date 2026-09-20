---
name: prd-user-stories
title: User stories with testable acceptance criteria
description: Use when turning a product idea into requirements — stories, personas, and acceptance criteria someone else has to verify.
agents: [product_manager]
keywords: [requirements, user story, stories, acceptance criteria, persona, feature, prd, scope, product]
---

Write every requirement as something a specific person is trying to finish, not as a
feature the product has. "As a returning shopper, I can see what I bought last month"
is work someone can verify. "Order history module" is not.

For each story:

- Name the actor. If every story has the same actor, the product has one user and the
  personas are decoration — say so instead of inventing three.
- State the goal in the user's words, and the reason behind it. The reason is what
  tells an engineer which of two implementations is right.
- Give acceptance criteria as observable conditions: a starting state, an action, and
  what is true afterwards. "Works correctly" and "is fast" are not conditions. "Loads
  50 orders in under 2 seconds on a cold cache" is.
- Cover the unhappy path in the criteria, not in a separate paragraph. Empty state,
  permission denied, the network failing mid-action, the same action twice.

Every criterion has to be checkable by someone who did not write it, using only the
running product. If checking it needs the author's opinion, rewrite it until it does
not.

Size stories so one person finishes one in a day or two. A story that cannot be
finished that fast is a theme; split it by the steps the user actually takes, never
by the layers the engineers work in — "the API part" is not a story.

Say which stories are in the first release and which are not, and put the reason
beside the cut. A requirements document that claims everything is essential has not
made any decisions yet.
