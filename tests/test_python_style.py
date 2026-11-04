"""

Regression tests for the repository's Python documentation and formatting checks.

"""

import pytest

from scripts.check_python_style import check_source

# --- Source fixtures ---

VALID_SOURCE = '\n'.join([
    '"""', '', 'Describe the example module.', '', '"""', '',
    '# --- Helpers ---', '',
    'def format_name(',
    '    name: str,',
    '    ) -> str:',
    '    """',
    '    Format a display name.', '',
    '    Args:',
    '        name (str): Name to format.', '',
    '    Returns:',
    '        str: Formatted display name.',
    '    """',
    '    return name.strip()', '',
])


# --- Convention regressions ---


def test_valid_documented_source() -> None:
    """
    Accept the requested module header, section, signature, and typed docstring format.

    Returns:
        None: Assertions complete when the valid source has no violations.
    """
    assert check_source(VALID_SOURCE) == []


@pytest.mark.parametrize(("old", "new", "message"), [
    ('"""\n\nDescribe', '"""\nDescribe', 'start with a descriptive'),
    ('module.\n\n"""', 'module.\n"""', 'before the module docstring closes'),
    ('# --- Helpers ---', '# Helpers', 'label code sections'),
    ('# --- Helpers ---', '\'\'\'# --- Helpers ---\'\'\'', 'section labels must be comments'),
    ('name (str): Name', 'name: Name', 'document name with its type'),
    ('str: Formatted', 'Formatted', 'document the return type'),
    ('name: str,', 'name,', 'annotate parameter name'),
    (') -> str:', '):', 'annotate the return type'),
    ('    name: str,', '        name: str,', 'indented four spaces'),
    ('    ) -> str:', ') -> str:', 'closing parenthesis four spaces'),
    ('return name.strip()\n', 'return name.strip()\n\n', 'exactly one newline'),
    ('\n', '\r\n', 'LF line endings'),
])
def test_invalid_source_is_rejected(
    old: str,
    new: str,
    message: str,
    ) -> None:
    """
    Reject malformed documentation, signatures, sections, and file endings.

    Args:
        old (str): Compliant source fragment to replace.
        new (str): Invalid replacement fragment for this regression case.
        message (str): Expected diagnostic text identifying the violated convention.

    Returns:
        None: Assertions complete when the expected diagnostic is reported.
    """
    errors = check_source(VALID_SOURCE.replace(old, new))
    assert any(message in error for error in errors), errors


def test_nested_variadic_signature() -> None:
    """
    Accept properly indented methods with typed variadic arguments and return documentation.

    Returns:
        None: Assertions complete when nested method and star-argument offsets are accepted.
    """
    source = '\n'.join([
        '"""', '', 'Describe the example module.', '', '"""', '', '# --- Helpers ---', '',
        'class Example:',
        '    def consume(',
        '        self,',
        '        *values: str,',
        '        **options: int,',
        '        ) -> None:',
        '        """',
        '        Consume supplied values and options.', '',
        '        Args:',
        '            *values (str): Values to consume.',
        '            **options (int): Numeric options to apply.', '',
        '        Returns:',
        '            None: No value is returned.',
        '        """',
        '        pass', '',
    ])
    assert check_source(source) == []
