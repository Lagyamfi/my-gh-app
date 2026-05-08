"""Pure helpers for splitting a unified diff by file and bin-packing chunks.

Used by ReviewService when the PR diff exceeds the LLM context budget.
No I/O, no asyncio — keep it trivially testable.
"""
import re
from dataclasses import dataclass, field

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


@dataclass
class Chunk:
    """A bundle of file diffs sized to fit one LLM call.

    `truncated_files` lists paths whose content was cut to fit `max_chars`.
    Empty in the common case; populated only when a single file exceeds the
    threshold (e.g. regenerated lockfile).
    """

    content: str
    files: list[str] = field(default_factory=list)
    truncated_files: list[str] = field(default_factory=list)


def pack_chunks(files: list[FileDiff], max_chars: int) -> list[Chunk]:
    """First-Fit Decreasing bin-packing of file diffs into chunks ≤ max_chars.

    - Files are sorted by content length descending.
    - Each file is placed in the first existing chunk where it fits, else a
      new chunk is opened.
    - A single file longer than max_chars forms a solo chunk with its content
      cut to max_chars and its path added to `truncated_files`.

    Order of files inside a chunk is the order they were inserted (which,
    given the descending sort, means largest first).
    """
    if not files:
        return []

    sorted_files = sorted(files, key=lambda f: len(f.content), reverse=True)
    chunks: list[Chunk] = []

    for f in sorted_files:
        size = len(f.content)
        if size > max_chars:
            chunks.append(
                Chunk(
                    content=f.content[:max_chars],
                    files=[f.path],
                    truncated_files=[f.path],
                )
            )
            continue
        placed = False
        for chunk in chunks:
            if chunk.truncated_files:
                # Don't pile more files into a chunk that's already at max.
                continue
            if len(chunk.content) + size <= max_chars:
                chunk.content += f.content
                chunk.files.append(f.path)
                placed = True
                break
        if not placed:
            chunks.append(Chunk(content=f.content, files=[f.path]))

    return chunks
