---
name: algolia-search
title: Search with Algolia
description: Use when the build has instant search through Algolia.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [algolia]
fixes: [algolia, admin key, search-only key, index]
---

Two keys, two sides. The browser searches with `NEXT_PUBLIC_ALGOLIA_APP_ID` and
`NEXT_PUBLIC_ALGOLIA_SEARCH_KEY` (search-only, public). The server indexes with
`ALGOLIA_ADMIN_KEY`, which can change and delete indexes and never reaches the page.

Index from the server when records change: `client.saveObjects({ indexName, objects })`
with `objectID` set to your record's id, and delete from the index when the record is
deleted. Send only the fields search needs — never private fields, since the search key
can read every indexed field.

Search from the browser with the search client, debounced, showing results as they come.
Per-user data needs secured API keys generated on the server with a filter.
