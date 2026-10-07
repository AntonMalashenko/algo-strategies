# Project conventions

Canonical rules for this repository. Applies to every contributor and to all
AI assistants (Claude Code, GitHub Copilot, Cursor, etc.). Tool-specific files
(`CLAUDE.md`, `.github/copilot-instructions.md`, `.cursorrules`) reference this file.

## Language policy

Everything that lives *in the code* must be written in **English**:

- Commit messages (subject and body).
- Code comments and docstrings.
- Identifiers: variable, function, class, module and file names.
- In-repo technical documentation: `README`, files under `docs/`,
  `CONTRIBUTING`, config comments, error/log messages.
- Pull request titles and descriptions.

**Exception:** conversational replies to the maintainer (chat) stay in
Russian. The English rule is about artifacts committed to the repository,
not about how the assistant talks to the user.

Rationale: keep the codebase and its history consistent, reviewable and
portable, independent of the language used while working.

## Chat tone

Talk to the maintainer like a peer, not a subordinate trying to please:
casual, direct, on "ты", mild profanity is fine. Push back and argue when
something looks wrong instead of agreeing by default — the goal is the best
outcome (code quality, correctness, P&L), not keeping the maintainer happy.
Use plain words over jargon; when a technical term is genuinely needed, use
it, but don't dress up a simple point in fancy terminology.

## Branching policy

**For now, do not create branches.** Commit directly to `master`.

- Do not open a feature branch for a task, even a large one.
- Do not merge, rebase or cherry-pick between branches.
- If a task genuinely needs its own branch, ask the maintainer first and
  wait for an explicit go-ahead.

Rationale: this is a single-maintainer repository where several tasks are
often in flight at once. Parallel branches made it easy to lose track of
which task a change belonged to, and to run or deploy code from the wrong
branch. A single linear `master` removes that whole class of mistake.
