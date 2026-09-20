---
name: mvp-scoping
title: Scoping a first release that can actually ship
description: Use when deciding what goes in the first version and what waits — cutting scope to the shortest path that proves the product works.
agents: [product_manager, system_design]
keywords: [mvp, scope, scoping, roadmap, release, milestone, prioritise, prioritize, backlog, requirements]
---

An MVP is the smallest product that lets one real user finish the one job the product
exists for. It is not a thin slice of every feature.

Find that job first and name it in a sentence. Everything else is measured against it.

Cut by these rules, in order:

- If a feature does not sit on the path from a new user arriving to that job being
  finished, it is not in the first release.
- If the product works without it and is merely worse, it is not in the first release.
- If it exists because a competitor has it, it is not in the first release.
- If removing it makes the product unsafe, unusable, or dishonest, it stays. Auth on
  anything holding another person's data stays. So does the ability to undo.

Keep what survives small enough to build and run end to end. A first release with
four screens that work beats twelve that half work.

For everything cut, say when it comes back and what would trigger it — the number of
users, the first support request, a paying customer asking. A deferred feature with no
trigger is a feature nobody will ever revisit.

Be explicit about what is deliberately missing, so the people building it do not spend
a week adding it back. "No team accounts in v1; single user only" is a decision. Saying
nothing is a gap someone will fill on their own.

State the one measurement that says whether the release worked. If nothing would change
your mind about the next version, the release is not an experiment, it is a guess.
