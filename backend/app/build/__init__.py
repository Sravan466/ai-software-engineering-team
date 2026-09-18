"""The generated project as a *build*: laid out, scaffolded, and checked to compile.

Eight agents wrote files at whatever paths occurred to them, none wrote a
`package.json`, and nothing checked that any of it parsed — the archive held 6.7 KB of
code that could not be installed, let alone run. This package is where that stops:

  `layout`    — where each agent's file goes (`backend/`, `frontend/`, the root)
  `packages`  — the dependencies the platform can provide, pinned by range
  `scaffold`  — the boilerplate, written by the platform from the stack charter
  `check`     — does it parse, and does every import resolve
  `contract`  — what the code-writing agents are told about all of the above
"""
