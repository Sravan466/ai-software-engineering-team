---
name: python-fastapi-service
title: Building a FastAPI service
description: Use when writing a Python HTTP backend with FastAPI — layout, dependencies, validation, sessions, and error handling.
agents: [backend_engineer]
keywords: [fastapi, python, backend, service, pydantic, sqlalchemy, uvicorn, router, dependency, rest]
---

Split the service by responsibility, not by framework feature: routes that speak HTTP,
a layer that holds the domain logic, and models that own persistence. A route function
should read as a short paragraph — take the validated request, call one thing, hand
back a typed object.

Declare request and response models as Pydantic classes and put the response model on
the decorator. A route annotated only as `dict` documents nothing, validates nothing on
the way out, and leaks whatever the ORM object happens to hold — including columns the
caller must never see.

Get the database session through `Depends`, one per request, closed in a finally. Never
open a session at module scope or share one between requests; a session that outlives a
request carries a poisoned transaction into the next one.

Raise `HTTPException` with the status the client should act on and a sentence a person
can read. Let unexpected exceptions reach a handler that logs the detail and sends back
something generic — a stack trace in a body is an information leak and tells the caller
nothing useful.

Read configuration once through `BaseSettings` and pass it in. Reaching for
`os.environ` deep inside a function makes the setting invisible and untestable.

Use `async def` only for a route that awaits something. A synchronous database driver
inside an async route blocks the event loop for every other request; a plain `def`
route is run in a threadpool and is the correct choice there.

Validate at the edge and trust afterwards. Length limits, ranges and allowed values
belong on the Pydantic model, so the handler can assume what it was given is sane.

Define the same status codes and error payload shape the contract promises, and keep
them identical across routers.
