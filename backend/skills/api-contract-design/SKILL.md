---
name: api-contract-design
title: API contract design
description: Use when defining HTTP endpoints — resource paths, request and response shapes, status codes, pagination, and errors.
agents: [system_design, backend_engineer]
keywords: [api, rest, endpoint, http, pagination, contract, route, crud, request, status code, architecture, interface]
---

Name resources as plural nouns and act on them with methods: `GET /invoices`,
`POST /invoices`, `GET /invoices/{id}`. A verb in a path (`/getInvoice`,
`/createUser`) means the method is doing nothing, and every client has to learn the
vocabulary twice.

Pick status codes that tell a client what to do next: 200 for a body, 201 with a
`Location` for something created, 204 for a deletion, 400 for a malformed request, 401
for no identity, 403 for an identity without rights, 404 for a resource that is absent
or that this caller may not know exists, 409 for a conflict with current state, 422
for a well-formed request that fails a rule.

A list endpoint sends back an envelope, never a bare array: the items, plus the cursor
or page for what comes next and a total when one is cheap to compute. A bare array
cannot grow a field without breaking every client. Cap the page size on the server and
say what the cap is; a client that asks for a million rows gets the cap, not an error
and not a million rows.

Errors carry a stable machine-readable code, a sentence a person can act on, and the
field at fault when there is one. `{"error": "invalid_email", "message": "...",
"field": "email"}` can be handled; `{"detail": "Bad Request"}` cannot.

Use ISO-8601 with an offset for every timestamp, integers of the smallest unit for
money with the currency beside them, and the same field name for the same idea in
every endpoint. Never put a secret, a token or an internal id in a query string.

Version the contract before the first client exists — a path prefix is enough. Adding a
field is not a breaking change; removing one, renaming one, or narrowing what a field
accepts is.
