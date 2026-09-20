---
name: cloud-cost-modelling
title: Estimating what a product costs to run
description: Use when projecting infrastructure or model spend — what drives the bill, which numbers to use, and how to show the assumptions.
agents: [cost_estimation]
keywords: [cost, pricing, budget, spend, infrastructure, hosting, scale, usage, estimate, cloud, tokens, estimation]
---

Estimate from the thing that drives the bill, never from a plan name. Requests per
month, gigabytes stored, gigabytes leaving the network, rows scanned, tokens generated.
A figure with no driver behind it cannot be checked and cannot be reduced.

State the assumption beside every number: how many users, how active each one is, how
much each action costs. A projection that says $340 a month without saying at how many
users is not an estimate anyone can act on.

Give a low, expected and high case from the same assumptions, and say what moves the
build between them. Most products are cheap until one dimension is not, and naming that
dimension is the most useful thing in the estimate.

Count the items people forget: egress, backups and snapshots, logging and metrics
retention, a staging environment, managed database storage growing separately from
compute, per-seat charges on tools, and anything billed per request rather than per
hour.

For anything calling a language model, cost input and output tokens separately at their
real rates, multiply by calls per user action, and check whether caching or a smaller
model on the cheap calls changes the total. This is usually the largest line and the
easiest to halve.

Separate fixed monthly cost from marginal cost per user, and give the marginal figure
per thousand users. That is the number that decides whether the pricing works.

Finish with the two or three changes that would cut the bill most, each with what it
would save and what it would cost in effort or latency.
