# Contributing

Agent Pooper Scooper is a pre-alpha public project. Open an issue to discuss meaningful changes before submitting a pull request.

Requirements for filesystem-modifying code:

- Dry-run and keep-on-uncertainty must be default.
- Never treat a file's age or a completed task as sufficient deletion evidence.
- Add regression tests for concurrent agents, session resume, uncommitted work, symlinks, hard links, path traversal and filesystem races.
- Make provider-specific integrations optional; do not assume access to the maintainer's private machines.
- Do not commit secrets, private task logs or machine paths.
- Independent review is required before relaxing the safety boundary.

We especially welcome tests proving that a file must be **kept**. Read [SECURITY.md](SECURITY.md) for responsible reporting.
