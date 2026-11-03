# Repository conventions

- When committing on Zachary's behalf, use `Zachary <84103628+zapulam@users.noreply.github.com>` for both author and committer, and use the `zapulam` GitHub login.
- Do not add AI or bot attribution to commits or PR descriptions. Do not copy bot co-author or sign-off trailers into squash commits.
- Keep real human contributor attribution intact. Commit messages may name the products integrated by the application.
- Use feature branches and PRs. Run the CI checks before merging. Published history must not be rewritten without explicit user authorization.
- Python functions need complete docstrings and arguments on new lines with double indentation. Group functions by workload with titled separators.
- Log substantive work through the log-work skill in the Obsidian vault. Project ownership is determined by the changed paths and each project note's `repo:` field.
- Release versions come from `pideck/__init__.py`; tags must match exactly. Release automation must test and smoke-test the exact tagged commit before publishing.
