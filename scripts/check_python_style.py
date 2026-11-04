"""

Check repository Python documentation, section comments, signatures, and newlines.

"""

from __future__ import annotations

import ast
from io import StringIO
from pathlib import Path
import re
import subprocess
import tokenize

# --- Source discovery ---


def source_paths(
    root: Path,
    ) -> list[Path]:
    """
    Find tracked and unignored Python source files in the repository.

    Args:
        root (Path): Repository root containing the Git checkout.

    Returns:
        list[Path]: Sorted absolute paths to Python source files.
    """
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.py"],
        cwd=root, capture_output=True, check=True,
    )
    return sorted({root / name.decode("utf-8") for name in result.stdout.split(b"\0") if name})


# --- Documentation and layout validation ---


def check_source(
    source: str,
    filename: str = "<string>",
    ) -> list[str]:
    """
    Validate one Python module against the documented repository conventions.

    Args:
        source (str): Decoded Python source with original line endings preserved.
        filename (str): File label included in syntax errors and diagnostics.

    Returns:
        list[str]: Human-readable violations, or an empty list for compliant source.
    """
    errors = []
    if "\r" in source:
        errors.append("use LF line endings")
    if not source.endswith("\n") or source.endswith("\n\n"):
        errors.append("end with exactly one newline")
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as error:
        return [*errors, f"line {error.lineno}: {error.msg}"]
    lines = source.splitlines()
    module_doc = ast.get_docstring(tree)
    if not source.startswith('"""\n\n') or not module_doc:
        errors.append("start with a descriptive triple-quoted module docstring and a blank line")
    elif tree.body[0].end_lineno < 4 or lines[tree.body[0].end_lineno - 2].strip():
        errors.append("leave a blank line before the module docstring closes")
    if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))
           for node in tree.body) and not re.search(r"^\s*# --- .+ ---$", source, re.M):
        errors.append("label code sections with # --- Group --- comments")
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str) and node.value.value.startswith("# --- "):
                errors.append(f"line {node.lineno}: section labels must be comments")
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        prefix = f"line {node.lineno} ({node.name}): "
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        arguments += [arg for arg in (node.args.vararg, node.args.kwarg) if arg is not None]
        public = [arg for arg in arguments if arg.arg not in {"self", "cls"}]
        for arg in public:
            if arg.annotation is None:
                errors.append(prefix + f"annotate parameter {arg.arg}")
        if node.returns is None:
            errors.append(prefix + "annotate the return type")
        if arguments:
            argument_lines = [arg.lineno for arg in arguments]
            if any(arg.lineno <= node.lineno or arg.col_offset != node.col_offset + 4
                   + (1 if arg is node.args.vararg else 2 if arg is node.args.kwarg else 0)
                   for arg in arguments) or len(set(argument_lines)) != len(argument_lines):
                errors.append(prefix + "put each parameter on its own line, indented four spaces")
            depth = 0
            opened = False
            for token in tokenize.generate_tokens(StringIO("\n".join(lines[node.lineno - 1:]) + "\n").readline):
                if token.type != tokenize.OP:
                    continue
                if token.string == "(":
                    depth += 1
                    opened = True
                elif token.string == ")":
                    depth -= 1
                    if opened and depth == 0:
                        close_line = node.lineno + token.start[0] - 1
                        if token.start[1] != node.col_offset + 4 or close_line <= max(argument_lines):
                            errors.append(prefix + "indent the closing parenthesis four spaces on its own line")
                        break
        doc = ast.get_docstring(node)
        if not doc:
            errors.append(prefix + "write a full function docstring")
            continue
        if lines[node.body[0].lineno - 1].strip() != '"""':
            errors.append(prefix + "put the docstring opening quotes on their own line")
        if not doc.splitlines()[0].strip() or doc.splitlines()[0].strip().endswith(":"):
            errors.append(prefix + "describe the function before its fields")
        args_match = re.search(r"(?:^|\n)Args:\n(.*?)(?=\n[A-Z][A-Za-z ]*:\n|\Z)", doc, re.S)
        for arg in public:
            if arg.annotation is None:
                continue
            annotation = ast.unparse(arg.annotation)
            pattern = r"^\s+\*{0,2}" + re.escape(arg.arg) + r" \(" + re.escape(annotation) + r"\): \S"
            if not args_match or not re.search(pattern, args_match.group(1), re.M):
                errors.append(prefix + f"document {arg.arg} with its type and description in Args")
        if node.returns is not None:
            pattern = r"(?:^|\n)Returns:\n\s+" + re.escape(ast.unparse(node.returns)) + r": \S"
            if not re.search(pattern, doc):
                errors.append(prefix + "document the return type and description in Returns")
    return errors


def check_file(
    path: Path,
    ) -> list[str]:
    """
    Read a Python file without normalizing its original line endings and check it.

    Args:
        path (Path): Python source file to read as UTF-8.

    Returns:
        list[str]: Style violations prefixed with the source path.
    """
    return [f"{path}: {error}" for error in check_source(path.read_bytes().decode("utf-8"), str(path))]


# --- Command-line entry point ---


def main() -> int:
    """
    Check all repository Python files and print actionable diagnostics.

    Returns:
        int: Zero when every file complies, or one when violations are found.
    """
    root = Path(__file__).resolve().parents[1]
    paths = source_paths(root)
    errors = [error for path in paths for error in check_file(path)]
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Python standards passed for {len(paths)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
