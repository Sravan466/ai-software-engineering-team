---
name: test-strategy
title: Test strategy that catches real failures
description: Use when planning tests — what to cover at which level, what a good case asserts, and which edge cases matter.
agents: [qa_engineer]
keywords: [test, testing, qa, coverage, unit, integration, e2e, regression, edge case, assertion, test case]
---

Pick the level by what could break. Unit tests for logic with branches and arithmetic,
integration tests for anything crossing a boundary — the database, the HTTP layer,
another service — and a small number of end-to-end tests for the paths a user pays for.
A pyramid of unit tests over code with no branches catches nothing.

Every test names the behaviour it protects, not the function it calls.
"rejects an order whose total is negative" survives a refactor; "test_validate_3" does
not tell the next person what broke.

Assert on the outcome someone would notice: the value handed back, the row written, the
mail queued, the status code. A test that asserts a mock was called proves the code
still calls the mock.

Cover the edges deliberately, for every input: nothing, one, many, far too many; empty
string, whitespace, a name with an apostrophe, text in another script; zero, negative,
the maximum, one past it; a date at midnight, on a leap day, across a time zone; the
same request twice; the request arriving out of order.

For anything with permissions, test the denial as carefully as the success. The test
that matters is the one where the wrong user asks.

Make every test independent and repeatable. Shared state between tests, a sleep instead
of a wait for a condition, and anything reading the real clock or the network are how a
suite becomes something people re-run until it goes green.

When a bug is found, the first thing written is the failing test that reproduces it.
Fixing without it means nothing stops it coming back.
