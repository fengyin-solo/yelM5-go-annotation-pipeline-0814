"""Shared minimums for bugfix functional-code changes."""

MIN_FUNCTIONAL_CHANGED_FILES = 1
MIN_FUNCTIONAL_CHANGED_LINES = 5


def meets_minimum_functional_change(changed_files: int, changed_lines: int) -> bool:
    return (
        changed_files >= MIN_FUNCTIONAL_CHANGED_FILES
        and changed_lines >= MIN_FUNCTIONAL_CHANGED_LINES
    )
