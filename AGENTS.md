# Repository conventions

- When committing on Zachary's behalf, use `Zachary <84103628+zapulam@users.noreply.github.com>` for both author and committer, and use the `zapulam` GitHub login.
- AI-assisted contributions are welcome. When Claude, Codex, or Cursor helps create a change, add the tool's own `Co-authored-by` trailer and keep the human author and committer identities above. Preserve those tool credits in pull requests and squash commits. Do not add Dependabot or other bot credit trailers, or use a tool or bot as the author or committer.
- Keep real human contributor attribution intact. Commit messages may name the products integrated by the application.
- Use feature branches and automatically open a PR when changes are complete. Leave it open for review unless the user asks to merge, and run CI before merging. Published history must not be rewritten without explicit user authorization.
- Python functions need complete docstrings and arguments on new lines with double indentation. Group functions by workload with titled separators.
- Log substantive work through the log-work skill in the Obsidian vault. Project ownership is determined by the changed paths and each project note's `repo:` field.
- Release versions come from `pideck/__init__.py`; tags must match exactly. Release automation must test and smoke-test the exact tagged commit before publishing.
