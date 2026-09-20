---
name: ci-cd-pipeline
title: CI/CD pipelines that can be trusted
description: Use when planning build, test and deployment automation — pipeline stages, environments, rollbacks, and release safety.
agents: [devops_engineer]
keywords: [ci, cd, pipeline, deploy, deployment, docker, kubernetes, github actions, build, release, rollback, infrastructure, hosting]
---

One pipeline runs on every change, and it is the only way anything reaches an
environment. A deploy step someone can run from a laptop is a deploy nobody can
reconstruct afterwards.

Order the stages so the cheapest failure comes first: install and cache dependencies,
lint, type-check, unit tests, build the artifact, integration tests against that
artifact, then deploy. A pipeline that builds a container before running a linter pays
four minutes to find a missing import.

Build the artifact once and promote the same one through each environment. Rebuilding
per environment means what you tested is not what you shipped.

Pin everything — base images by digest, actions by version, dependencies by lockfile.
A pipeline that resolves `latest` produces a different result on a day nobody changed
anything, and the failure lands on whoever pushed next.

Keep environments separate and identical in shape: staging differs from production in
scale and data, never in configuration mechanism. Configuration and secrets come from
the environment, injected at deploy time, never baked into an image.

Run migrations as their own step before the new code starts, and keep them
backward-compatible so the previous version survives the window where both are running.

Every deploy has a way back that does not involve a fix-forward commit: the previous
artifact redeployable by version, and a health check the deploy watches before it
finishes. Say what triggers a rollback, and make it something a machine can decide.

Fail the pipeline on the things you claim to care about. A test suite whose failures are
allowed to pass is a test suite nobody reads.
