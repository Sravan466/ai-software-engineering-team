---
name: react-component-architecture
title: React component architecture
description: Use when structuring a React or Next.js front end — component boundaries, state placement, data fetching, and props.
agents: [frontend_engineer]
keywords: [react, component, frontend, next.js, nextjs, hook, state, props, ui, jsx, tsx, page]
---

Draw component boundaries around what changes together. A component that takes eleven
props is two components; a component nobody would ever render twice is a section of its
parent, not a component.

Keep state as close to where it is used as possible, and lift it only when two siblings
need the same value. State held at the root and threaded down through four layers is a
re-render of the whole tree every keystroke.

Derive, do not duplicate. If a value can be computed from props or state, compute it
during render. A second piece of state holding the same fact is a bug waiting for the
two to disagree.

Separate the component that fetches from the component that draws. A presentational
component that takes data as props can be rendered in any state you like; one that
fetches inside itself can only be seen by making the request happen.

Every asynchronous view has four states and all four are real: loading, empty, error,
and loaded. Design the empty and error states deliberately — a spinner that never ends
is what a forgotten error branch looks like to a user.

Give every effect a correct dependency list and a cleanup. An effect that sets state
from a request has to handle the component unmounting first, or the response arrives to
nothing and warns.

Key lists by a stable id from the data, never by array index — index keys make React
reuse the wrong row the moment anything is inserted or sorted.

Type props explicitly, including the callbacks. `onSave: (draft: Draft) => void` says
what a parent has to do; `onSave: Function` says nothing and type-checks anything.
