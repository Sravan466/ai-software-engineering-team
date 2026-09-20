---
name: data-modelling
title: Relational data modelling
description: Use when designing a schema — entities, keys, relationships, indexes, constraints, and migrations.
agents: [system_design, backend_engineer]
keywords: [database, schema, model, table, migration, sql, index, entity, relational, postgres, foreign key, architecture, data model, storage]
---

Model the nouns the product actually has, one table per entity, and let the
relationships between them carry the meaning. A table with twelve nullable columns
that are used in two mutually exclusive combinations is two tables.

Give every table a surrogate primary key, and put a unique constraint on whatever
makes a row unique in the real world — the email, the slug, the order number. Those
are different jobs and one key cannot do both.

Every foreign key gets a real constraint and an explicit rule for what happens when the
parent goes: cascade when the child cannot exist alone, restrict when deleting the
parent should be refused. Deciding this later means deciding it after the orphans exist.

Constrain at the database, not only in the application. Not-null, unique, check
constraints, and enumerations belong next to the data, because the application is not
the only thing that will ever write to it.

Index what is filtered, joined and sorted on — foreign keys first, since almost nothing
indexes them for you. Add a composite index in the order the query uses the columns.
Do not index every column: each one costs on every write.

Store timestamps as UTC with a time zone type, money as integer minor units with the
currency beside it, and enumerations as a constrained text value rather than a raw
integer nobody can read in a query.

Every schema change is a migration that is additive and reversible: add a nullable
column, backfill, then start reading it. Renaming or dropping in one step breaks every
process still running the previous version of the code.

Keep deletion explicit. Decide per table whether rows are removed or marked, and say
which — a mix that nobody wrote down is how deleted data comes back.
