# Repository conventions

- When committing on Zachary's behalf, use `Zachary <84103628+zapulam@users.noreply.github.com>` for both author and committer, and use the `zapulam` GitHub login.
- AI-assisted contributions are welcome. When Claude, Codex, or Cursor helps create a change, add the tool's own `Co-authored-by` trailer and keep the human author and committer identities above. Preserve those tool credits in pull requests and squash commits. Do not add Dependabot or other bot credit trailers, or use a tool or bot as the author or committer.
- Keep real human contributor attribution intact. Commit messages may name the products integrated by the application.
- Use feature branches and automatically open a PR when changes are complete. Leave it open for review unless the user asks to merge, and run CI before merging. Published history must not be rewritten without explicit user authorization.
- Start every Python file with a descriptive triple-quoted module docstring, with a blank line after the opening quotes and before the closing quotes. End each file with exactly one LF newline.
- Label Python sections by workload using actual comments such as `# --- Helper functions ---`.
- Put Python function parameters on individual lines with one additional four-space indentation level. Put the closing parenthesis on its own line at that same indentation. Annotate parameters and return values; `self` and `cls` do not need annotations.
- Give every Python function a full multiline docstring describing its behavior, an `Args:` entry with each parameter's type and description, and a `Returns:` entry with the return type and description (including `None`). Omit `Args:` when there are no parameters beyond `self` or `cls`.
- Run `python scripts/check_python_style.py` before committing Python changes; CI enforces these conventions.
- Log substantive work through the log-work skill in the Obsidian vault. Project ownership is determined by the changed paths and each project note's `repo:` field.
- Release versions come from `pideck/__init__.py`; tags must match exactly. Release automation must rerun CI against the exact tagged commit before publishing the source archive.
