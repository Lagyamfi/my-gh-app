"""Pure helpers for splitting a unified diff by file and bin-packing chunks.

Used by ReviewService when the PR diff exceeds the LLM context budget.
No I/O, no asyncio — keep it trivially testable.
"""
import re
from dataclasses import dataclass

# Splits on every line that starts with "diff --git ". Keeps that line in the
# *next* element via the lookahead (?=...).
_DIFF_HEADER_RE = re.compile(r"(?m)^(?=diff --git )")
# Parses "diff --git a/<path> b/<path>" — captures the a/ side.
_DIFF_PATH_RE = re.compile(r"^diff --git a/(\S+) b/")


@dataclass
class FileDiff:
    """One file's section inside a unified diff."""

    path: str
    content: str  # full section, starting with "diff --git "


def split_unified_diff(diff: str) -> list[FileDiff]:
    """Split a unified diff on `diff --git ` boundaries.

    Returns an empty list if the input contains no `diff --git ` header.
    Sections without a parseable path are skipped (defensive).
    """
    if not diff:
        return []
    parts = _DIFF_HEADER_RE.split(diff)
    # The first element is anything before the first header — drop it
    # (re.split with a lookahead emits an empty string here when the diff
    # starts with the header, or pre-header garbage otherwise).
    files: list[FileDiff] = []
    for part in parts[1:]:
        m = _DIFF_PATH_RE.match(part)
        if not m:
            continue
        files.append(FileDiff(path=m.group(1), content=part))
    return files
