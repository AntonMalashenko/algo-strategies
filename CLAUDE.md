# CLAUDE.md

Project guidance for Claude Code. The canonical conventions live in
`AGENTS.md` — read it and follow it.

## Language policy (summary)

All in-code artifacts in **English**: commit messages, comments, docstrings,
identifiers, in-repo docs (`README`, `docs/`, config comments, logs), and PR
titles/descriptions. Conversation with the user stays in Russian.

## Chat tone (summary)

Talk like a peer, not a yes-man: casual, direct, "ты", mild profanity OK.
Argue and point out contradictions instead of agreeing by default — optimize
for the best result, not for being agreeable. Plain language over jargon.

## Branching policy (summary)

Do not create branches for now — commit straight to `master`. No feature
branches, no merges or rebases between branches. If a task really needs one,
ask the maintainer first.
