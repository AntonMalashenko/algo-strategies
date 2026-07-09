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
