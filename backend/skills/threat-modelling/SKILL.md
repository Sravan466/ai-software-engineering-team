---
name: threat-modelling
title: Threat modelling a web application
description: Use when reviewing a design or an implementation for security — trust boundaries, the OWASP categories that apply, and findings someone can act on.
agents: [security_engineer, system_design]
keywords: [security, threat, owasp, vulnerability, attack, injection, auth, authorization, xss, csrf, audit, architecture, payment, personal data]
---

Start from the data, not the code. List what this product holds that someone would
want — credentials, personal details, card data, other people's content — and work
outwards to every path that reaches it.

Draw the trust boundaries: the browser, the API, the database, each third party. Every
crossing is where input becomes untrusted and where authentication and authorisation
have to be proven again. Most real breaches happen at a crossing nobody drew.

Walk the categories that actually apply to a web application and say, for each, what
this design does about it:

- Broken access control — every endpoint checks that this identity may act on this
  specific object, not merely that someone is logged in. Object ids in a URL are the
  most common way this fails.
- Injection — parameterised queries everywhere, escaping on output, no shell built by
  string concatenation.
- Identification and authentication — password hashing with a slow algorithm, session
  tokens that expire and rotate, rate limiting on anything guessable.
- Cryptographic failures — TLS everywhere, nothing sensitive in logs or URLs, no
  secret in the source tree.
- Security misconfiguration — errors that say nothing useful to an attacker, CORS
  restricted to known origins, no default credentials.
- Software supply chain — dependencies pinned, and known vulnerabilities checked.
- Server-side request forgery — any URL supplied by a user is validated against an
  allowlist before anything fetches it.

For every finding, say where it is, what an attacker gets from it, how likely it is,
and the specific change that fixes it. A severity with no location and no fix is not a
finding, it is a worry.
