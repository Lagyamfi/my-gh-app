# Large PR Review Split & Merge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop silently truncating large PR diffs at 30 000 chars; split them by file, review chunks in parallel, and merge findings into one `Review`, with explicit warnings to the user.

**Architecture:** A new pure helper `app/services/_diff_splitter.py` splits the unified diff and bin-packs files into ≤ N-char chunks. `ReviewService.stream_review` keeps its current fast path when the diff is small; when it exceeds the threshold it fans out N parallel sub-reviews under an `asyncio.Semaphore`, drains their streamed events through an `asyncio.Queue` for live UI feedback, and merges the per-chunk `Review` results into a single one with a structured summary. Both AI adapters drop their hardcoded `diff[:30000]` since the service now guarantees the size.

**Tech Stack:** Python 3.12 · asyncio · pytest + pytest-asyncio (auto mode, already configured in `pyproject.toml`) · `unittest.mock.MagicMock` for ports.

**Spec reference:** `docs/superpowers/specs/2026-05-06-large-pr-review-split-design.md`.

**Worktree:** `/Users/angelardbenjamin/working_directory/gh-review-tool/.claude/worktrees/cool-rhodes-3b18ff/`. Run all commands from the worktree root.

---

## File map

| File | Action | Responsibility |
|------|--------|----------------|
| `app/services/_diff_splitter.py` | create | Pure functions: `split_unified_diff`, `pack_chunks`, dataclasses `FileDiff`, `Chunk`. Zero I/O, zero asyncio. |
| `app/services/review_service.py` | modify | Add the split path + parallel orchestration in `stream_review`. Fast path unchanged. |
| `app/adapters/ai/claude_code_adapter.py` | modify (line 259) | Replace `diff[:30000]` with `diff`. |
| `app/adapters/ai/opencode_adapter.py` | modify (line 218) | Replace `diff[:30000]` with `diff`. |
| `tests/unit/test_diff_splitter.py` | create | Cover splitter + packer (split, FFD ordering, oversized-file truncation, empty input). |
| `tests/unit/test_review_service.py` | extend | Add tests for split path, failure resilience, concurrency cap. Existing tests unchanged. |
| `.env.example` | modify | Document `REVIEW_DIFF_MAX_CHARS` and `REVIEW_MAX_CONCURRENCY`. |
| `README.md` | modify (Configuration section, around line 170) | Add the two env vars to the table. |

Tests live under `tests/unit/` (existing project convention — overrides the `tests/services/` path mentioned in the spec).

---

### Task 1: Splitter primitives — `FileDiff` and `split_unified_diff`

**Files:**
- Create: `app/services/_diff_splitter.py`
- Test: `tests/unit/test_diff_splitter.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_diff_splitter.py`:

```python
"""Tests for app.services._diff_splitter."""
from app.services._diff_splitter import FileDiff, split_unified_diff


SAMPLE_DIFF = """diff --git a/foo.py b/foo.py
index 1111..2222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 line one
+added line
 line two
diff --git a/bar/baz.py b/bar/baz.py
index 3333..4444 100644
--- a/bar/baz.py
+++ b/bar/baz.py
@@ -10,1 +10,1 @@
-old
+new
diff --git a/README.md b/README.md
index 5555..6666 100644
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-Hello
+Hello world
"""


class TestSplitUnifiedDiff:
    def test_splits_three_files(self):
        files = split_unified_diff(SAMPLE_DIFF)
        assert len(files) == 3
        assert [f.path for f in files] == ["foo.py", "bar/baz.py", "README.md"]

    def test_each_section_starts_with_diff_git(self):
        files = split_unified_diff(SAMPLE_DIFF)
        for f in files:
            assert f.content.startswith("diff --git ")

    def test_concatenated_sections_equal_original(self):
        files = split_unified_diff(SAMPLE_DIFF)
        assert "".join(f.content for f in files) == SAMPLE_DIFF

    def test_empty_input_returns_empty_list(self):
        assert split_unified_diff("") == []

    def test_input_without_diff_header_returns_empty_list(self):
        # Defensive: gh always emits headers, but malformed input shouldn't crash.
        assert split_unified_diff("not a diff\nat all\n") == []

    def test_path_with_subdirectory_is_preserved(self):
        files = split_unified_diff(SAMPLE_DIFF)
        assert files[1].path == "bar/baz.py"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_diff_splitter.py -v`
Expected: `ModuleNotFoundError: No module named 'app.services._diff_splitter'` (or all tests collected and failing on import).

- [ ] **Step 3: Implement `FileDiff` and `split_unified_diff`**

Create `app/services/_diff_splitter.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_diff_splitter.py -v`
Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/_diff_splitter.py tests/unit/test_diff_splitter.py
git commit -m "feat(splitter): add split_unified_diff helper"
```

---

### Task 2: Splitter — `Chunk` and `pack_chunks` (FFD bin-packing)

**Files:**
- Modify: `app/services/_diff_splitter.py`
- Test: `tests/unit/test_diff_splitter.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_diff_splitter.py`:

```python
import pytest

from app.services._diff_splitter import Chunk, pack_chunks


def _make_file(path: str, size: int) -> FileDiff:
    """Helper: produce a FileDiff with a content of exactly `size` chars."""
    header = f"diff --git a/{path} b/{path}\n"
    body = "x" * (size - len(header))
    assert len(header + body) == size
    return FileDiff(path=path, content=header + body)


class TestPackChunks:
    def test_empty_input(self):
        assert pack_chunks([], max_chars=1000) == []

    def test_single_small_file_one_chunk(self):
        files = [_make_file("a.py", 100)]
        chunks = pack_chunks(files, max_chars=1000)
        assert len(chunks) == 1
        assert chunks[0].files == ["a.py"]
        assert chunks[0].truncated_files == []
        assert chunks[0].content == files[0].content

    def test_files_under_threshold_combine_into_one_chunk(self):
        files = [_make_file(f"f{i}.py", 100) for i in range(5)]
        chunks = pack_chunks(files, max_chars=1000)
        assert len(chunks) == 1
        assert sorted(chunks[0].files) == sorted([f.path for f in files])
        assert chunks[0].truncated_files == []

    def test_ffd_ordering_packs_efficiently(self):
        # 25 KB + 8 KB + 3 KB with a 30 KB threshold should produce 2 chunks:
        # the big one alone, then the two smaller ones together.
        big = _make_file("big.py", 25000)
        mid = _make_file("mid.py", 8000)
        small = _make_file("small.py", 3000)
        chunks = pack_chunks([small, big, mid], max_chars=30000)
        assert len(chunks) == 2
        assert chunks[0].files == ["big.py"]
        assert sorted(chunks[1].files) == ["mid.py", "small.py"]
        for c in chunks:
            assert len(c.content) <= 30000

    def test_oversized_file_becomes_truncated_solo_chunk(self):
        huge = _make_file("huge.py", 50000)
        chunks = pack_chunks([huge], max_chars=30000)
        assert len(chunks) == 1
        assert chunks[0].files == ["huge.py"]
        assert chunks[0].truncated_files == ["huge.py"]
        assert len(chunks[0].content) == 30000
        # First chars preserved (the diff header is what the LLM needs most).
        assert chunks[0].content.startswith("diff --git a/huge.py")

    def test_chunk_content_never_exceeds_max_chars(self):
        files = [_make_file(f"f{i}.py", size) for i, size in enumerate([5000, 7000, 8000, 12000])]
        chunks = pack_chunks(files, max_chars=15000)
        for c in chunks:
            assert len(c.content) <= 15000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_diff_splitter.py::TestPackChunks -v`
Expected: `ImportError: cannot import name 'Chunk'` (or all tests fail on import).

- [ ] **Step 3: Implement `Chunk` and `pack_chunks`**

Append to `app/services/_diff_splitter.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_diff_splitter.py -v`
Expected: all 12 tests pass (6 from Task 1 + 6 from Task 2).

- [ ] **Step 5: Commit**

```bash
git add app/services/_diff_splitter.py tests/unit/test_diff_splitter.py
git commit -m "feat(splitter): add pack_chunks FFD bin-packing"
```

---

### Task 3: ReviewService — split path with parallel orchestration

**Files:**
- Modify: `app/services/review_service.py`
- Test: `tests/unit/test_review_service.py`

- [ ] **Step 1: Write the failing test (under-threshold fast path unchanged)**

This pins the existing behavior: small diffs trigger exactly one `ai.stream_review` call and emit no warning. Append to `tests/unit/test_review_service.py`:

```python
import asyncio
import os

from app.ports.ai_provider import ReviewWarningEvent


class TestStreamReviewSplit:
    async def test_diff_under_threshold_uses_single_call(
        self, service, ai_provider, vcs_port, monkeypatch
    ):
        monkeypatch.setenv("REVIEW_DIFF_MAX_CHARS", "10000")
        vcs_port.get_diff.return_value = "diff --git a/a.py b/a.py\n" + "x" * 100

        review = Review(summary="small", findings=[])
        call_count = 0

        async def mock_stream(repo, pr, diff, model=None):
            nonlocal call_count
            call_count += 1
            yield ReviewChunkEvent("chunk")
            yield ReviewResultEvent(review)

        ai_provider.stream_review = mock_stream
        events = [e async for e in service.stream_review("acme/backend", 1)]

        assert call_count == 1
        warnings = [e for e in events if isinstance(e, ReviewWarningEvent)]
        assert warnings == []
        results = [e for e in events if isinstance(e, ReviewResultEvent)]
        assert len(results) == 1
        assert results[0].review.summary == "small"
```

- [ ] **Step 2: Write the failing test (over-threshold split path)**

Append to the same `TestStreamReviewSplit` class:

```python
    async def test_diff_over_threshold_splits_and_merges(
        self, service, ai_provider, vcs_port, monkeypatch
    ):
        monkeypatch.setenv("REVIEW_DIFF_MAX_CHARS", "200")
        monkeypatch.setenv("REVIEW_MAX_CONCURRENCY", "5")

        # Two files, each ~150 chars, totalling ~300 → must split into 2 chunks.
        diff = (
            "diff --git a/a.py b/a.py\n" + "a" * 130 + "\n"
            "diff --git a/b.py b/b.py\n" + "b" * 130 + "\n"
        )
        vcs_port.get_diff.return_value = diff

        call_diffs: list[str] = []

        async def mock_stream(repo, pr, sub_diff, model=None):
            call_diffs.append(sub_diff)
            tag = "a" if "a.py" in sub_diff else "b"
            yield ReviewChunkEvent(f"text-{tag}")
            yield ReviewResultEvent(
                Review(
                    summary=f"summary-{tag}",
                    findings=[Finding(priority="P1", title=f"finding-{tag}", description="d")],
                )
            )

        ai_provider.stream_review = mock_stream
        events = [e async for e in service.stream_review("acme/backend", 1)]

        # Two parallel sub-reviews fired.
        assert len(call_diffs) == 2

        # Initial split warning emitted before any chunk text.
        warnings = [e for e in events if isinstance(e, ReviewWarningEvent)]
        assert len(warnings) >= 1
        first_warning_text = " ".join(warnings[0].lines)
        assert "split into 2 chunks" in first_warning_text
        assert "2 files" in first_warning_text

        # Both sub-streams' chunk events surfaced to the caller.
        chunk_texts = [e.text for e in events if isinstance(e, ReviewChunkEvent)]
        assert "text-a" in chunk_texts
        assert "text-b" in chunk_texts

        # Final merged review: both findings, structured summary.
        results = [e for e in events if isinstance(e, ReviewResultEvent)]
        assert len(results) == 1
        merged = results[0].review
        titles = sorted(f.title for f in merged.findings)
        assert titles == ["finding-a", "finding-b"]
        assert "Reviewed across 2 chunks" in merged.summary
        assert "2 files" in merged.summary

    async def test_split_path_caches_merged_review(
        self, service, ai_provider, cache_port, vcs_port, monkeypatch
    ):
        monkeypatch.setenv("REVIEW_DIFF_MAX_CHARS", "100")
        diff = (
            "diff --git a/a.py b/a.py\n" + "a" * 80 + "\n"
            "diff --git a/b.py b/b.py\n" + "b" * 80 + "\n"
        )
        vcs_port.get_diff.return_value = diff

        async def mock_stream(repo, pr, sub_diff, model=None):
            yield ReviewResultEvent(Review(summary="s", findings=[]))

        ai_provider.stream_review = mock_stream
        _ = [e async for e in service.stream_review("acme/backend", 1)]

        cache_port.save_review.assert_called_once()
        cached = cache_port.save_review.call_args[0][2]
        assert isinstance(cached, Review)
        assert "Reviewed across" in cached.summary
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_review_service.py::TestStreamReviewSplit -v`
Expected: `test_diff_under_threshold_uses_single_call` may pass (existing behavior); the two `_splits_and_merges` / `_caches_merged_review` tests fail (only one `ai.stream_review` call observed, no warning emitted, etc.).

- [ ] **Step 4: Implement the split path in `ReviewService.stream_review`**

Replace `app/services/review_service.py` entirely with:

```python
"""ReviewService — orchestrates AI, VCS, and cache ports for PR review.

When the PR diff exceeds REVIEW_DIFF_MAX_CHARS, the review is fanned out
across multiple parallel sub-calls (one per packed chunk) and the per-chunk
Reviews are merged into a single Review with a structured summary.
"""
import asyncio
import logging
import os
from collections.abc import AsyncGenerator

from app.domain.models import Finding, Review
from app.ports.ai_provider import (
    AIProvider,
    ReviewChunkEvent,
    ReviewResultEvent,
    ReviewStreamEvent,
    ReviewWarningEvent,
)
from app.ports.cache_port import CachePort
from app.ports.vcs_port import VCSPort
from app.services._diff_splitter import Chunk, pack_chunks, split_unified_diff

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 30000
_DEFAULT_MAX_CONCURRENCY = 3


class ReviewService:
    def __init__(self, ai: AIProvider, cache: CachePort, vcs: VCSPort) -> None:
        self._ai = ai
        self._cache = cache
        self._vcs = vcs

    async def get_or_run_review(self, repo_full_name: str, pr_number: int) -> Review:
        """Return cached review or run a fresh one."""
        cached = self._cache.get_review(repo_full_name, pr_number)
        if cached is not None:
            return cached
        return await self._run_review(repo_full_name, pr_number)

    async def rerun_review(self, repo_full_name: str, pr_number: int) -> Review:
        """Always run a fresh review, overwriting any cache."""
        return await self._run_review(repo_full_name, pr_number)

    async def stream_review(
        self, repo_full_name: str, pr_number: int, model: str | None = None
    ) -> AsyncGenerator[ReviewStreamEvent, None]:
        """Stream review events. Splits & merges large diffs transparently."""
        diff = self._vcs.get_diff(repo_full_name, pr_number)
        max_chars = _read_int_env("REVIEW_DIFF_MAX_CHARS", _DEFAULT_MAX_CHARS)
        max_concurrency = _read_int_env("REVIEW_MAX_CONCURRENCY", _DEFAULT_MAX_CONCURRENCY)

        if len(diff) <= max_chars:
            async for event in self._ai.stream_review(
                repo_full_name, pr_number, diff, model=model
            ):
                if isinstance(event, ReviewResultEvent):
                    self._cache.save_review(repo_full_name, pr_number, event.review)
                yield event
            return

        async for event in self._stream_split_review(
            repo_full_name, pr_number, diff, model, max_chars, max_concurrency
        ):
            yield event

    async def _stream_split_review(
        self,
        repo_full_name: str,
        pr_number: int,
        diff: str,
        model: str | None,
        max_chars: int,
        max_concurrency: int,
    ) -> AsyncGenerator[ReviewStreamEvent, None]:
        files = split_unified_diff(diff)
        chunks = pack_chunks(files, max_chars)
        truncated = [path for c in chunks for path in c.truncated_files]

        warning_lines = [
            f"Diff too large ({len(diff)} chars), split into {len(chunks)} chunks "
            f"across {len(files)} files."
        ]
        warning_lines += [f"Truncated file: {p}" for p in truncated]
        yield ReviewWarningEvent(warning_lines)

        logger.info(
            "review-service | split | repo=%s pr=#%d diff=%d chars chunks=%d files=%d truncated=%d concurrency=%d",
            repo_full_name, pr_number, len(diff), len(chunks), len(files),
            len(truncated), max_concurrency,
        )

        # Run all chunks under a semaphore; stream their ChunkEvents/Warnings
        # through a queue while collecting their final Review (or exception).
        queue: asyncio.Queue = asyncio.Queue()
        sentinel = object()
        sem = asyncio.Semaphore(max_concurrency)

        async def run_one(chunk: Chunk) -> tuple[Chunk, Review | None, BaseException | None]:
            review: Review | None = None
            error: BaseException | None = None
            async with sem:
                try:
                    async for ev in self._ai.stream_review(
                        repo_full_name, pr_number, chunk.content, model=model
                    ):
                        if isinstance(ev, (ReviewChunkEvent, ReviewWarningEvent)):
                            await queue.put(ev)
                        elif isinstance(ev, ReviewResultEvent):
                            review = ev.review
                except Exception as exc:  # noqa: BLE001 — propagate as warning
                    error = exc
            await queue.put((sentinel, chunk, review, error))
            return chunk, review, error

        tasks = [asyncio.create_task(run_one(c)) for c in chunks]
        results: list[tuple[Chunk, Review | None, BaseException | None]] = []

        try:
            done_count = 0
            while done_count < len(chunks):
                item = await queue.get()
                if isinstance(item, tuple) and item and item[0] is sentinel:
                    _, chunk, review, error = item
                    results.append((chunk, review, error))
                    done_count += 1
                else:
                    yield item
        finally:
            # Make sure all tasks complete (they should, since we drained
            # all sentinels). Cancel any still pending if we exit early.
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        # Emit per-failure warnings.
        failed_count = 0
        sub_summaries: list[str] = []
        findings: list[Finding] = []
        for chunk, review, error in results:
            if error is not None or review is None:
                failed_count += 1
                msg = (
                    f"Chunk failed: files={chunk.files} "
                    f"error={type(error).__name__ if error else 'no result'}: {error}"
                )
                yield ReviewWarningEvent([msg])
                logger.warning("review-service | chunk failed | %s", msg)
                continue
            findings.extend(review.findings)
            if review.summary:
                sub_summaries.append(review.summary)

        merged_summary = (
            f"Reviewed across {len(chunks)} chunks "
            f"({len(files)} files, {failed_count} failed)."
        )
        if sub_summaries:
            merged_summary += "\n\n" + "\n\n".join(f"- {s}" for s in sub_summaries)

        merged = Review(summary=merged_summary, findings=findings)
        self._cache.save_review(repo_full_name, pr_number, merged)
        yield ReviewResultEvent(review=merged)

    async def _run_review(self, repo_full_name: str, pr_number: int) -> Review:
        review: Review | None = None
        async for event in self.stream_review(repo_full_name, pr_number):
            if isinstance(event, ReviewResultEvent):
                review = event.review
        if review is None:
            review = Review(summary="No review result received.", findings=[])
        return review

    def clear_cache(self, repo_full_name: str, pr_number: int) -> None:
        self._cache.clear_review(repo_full_name, pr_number)


def _read_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        logger.warning("review-service | invalid %s=%r, using default %d", name, raw, default)
        return default
```

Note the `_run_review` change: it now delegates to `stream_review` instead of calling `self._ai.stream_review` directly. This ensures `get_or_run_review` and `rerun_review` also benefit from the split path. The cache write is handled inside `stream_review`, so `_run_review` no longer calls `save_review` itself (avoids the previous double-save).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_review_service.py -v`
Expected: all tests pass (existing 5 + 3 new from this task). If `TestRerunReview::test_skips_cache_and_overwrites` or `TestGetOrRunReview::test_runs_and_caches_when_no_cache` fails because they assert `cache_port.save_review` was called once, the new flow still calls it once (inside `stream_review`), so they should pass. If a fixture's `mock_stream` lacks the `model=None` kwarg, fix by adding it.

- [ ] **Step 6: Commit**

```bash
git add app/services/review_service.py tests/unit/test_review_service.py
git commit -m "feat(review): split & merge large diffs across parallel sub-reviews"
```

---

### Task 4: ReviewService — failure resilience

**Files:**
- Modify: `tests/unit/test_review_service.py`

- [ ] **Step 1: Write the failing tests**

Append to `TestStreamReviewSplit`:

```python
    async def test_one_chunk_failure_emits_warning_and_preserves_others(
        self, service, ai_provider, vcs_port, monkeypatch
    ):
        from app.domain.exceptions import ProviderError

        monkeypatch.setenv("REVIEW_DIFF_MAX_CHARS", "100")
        diff = (
            "diff --git a/a.py b/a.py\n" + "a" * 80 + "\n"
            "diff --git a/b.py b/b.py\n" + "b" * 80 + "\n"
        )
        vcs_port.get_diff.return_value = diff

        async def mock_stream(repo, pr, sub_diff, model=None):
            if "a.py" in sub_diff:
                raise ProviderError("claude exited with code 124")
            yield ReviewResultEvent(
                Review(
                    summary="b ok",
                    findings=[Finding(priority="P2", title="from-b", description="d")],
                )
            )

        ai_provider.stream_review = mock_stream
        events = [e async for e in service.stream_review("acme/backend", 1)]

        warnings = [e for e in events if isinstance(e, ReviewWarningEvent)]
        flat = " ".join(line for w in warnings for line in w.lines)
        assert "Chunk failed" in flat
        assert "a.py" in flat
        assert "ProviderError" in flat

        results = [e for e in events if isinstance(e, ReviewResultEvent)]
        assert len(results) == 1
        merged = results[0].review
        assert [f.title for f in merged.findings] == ["from-b"]
        assert "1 failed" in merged.summary

    async def test_all_chunks_fail_yields_empty_review_with_warnings(
        self, service, ai_provider, vcs_port, monkeypatch
    ):
        from app.domain.exceptions import ProviderError

        monkeypatch.setenv("REVIEW_DIFF_MAX_CHARS", "100")
        diff = (
            "diff --git a/a.py b/a.py\n" + "a" * 80 + "\n"
            "diff --git a/b.py b/b.py\n" + "b" * 80 + "\n"
        )
        vcs_port.get_diff.return_value = diff

        async def mock_stream(repo, pr, sub_diff, model=None):
            raise ProviderError("boom")
            yield  # noqa: pragma: no cover — make this an async generator

        ai_provider.stream_review = mock_stream
        events = [e async for e in service.stream_review("acme/backend", 1)]

        warnings = [e for e in events if isinstance(e, ReviewWarningEvent)]
        chunk_failure_warnings = [
            w for w in warnings if any("Chunk failed" in line for line in w.lines)
        ]
        assert len(chunk_failure_warnings) == 2

        results = [e for e in events if isinstance(e, ReviewResultEvent)]
        assert len(results) == 1
        assert results[0].review.findings == []
        assert "2 failed" in results[0].review.summary
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_review_service.py::TestStreamReviewSplit -v`
Expected: both new tests pass without code changes (the implementation from Task 3 already handles this). If they fail, debug the failure-handling branch.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_review_service.py
git commit -m "test(review): cover chunk failure + all-fail merge behavior"
```

---

### Task 5: ReviewService — concurrency cap

**Files:**
- Modify: `tests/unit/test_review_service.py`

- [ ] **Step 1: Write the failing test**

Append to `TestStreamReviewSplit`:

```python
    async def test_concurrency_cap_is_respected(
        self, service, ai_provider, vcs_port, monkeypatch
    ):
        monkeypatch.setenv("REVIEW_DIFF_MAX_CHARS", "100")
        monkeypatch.setenv("REVIEW_MAX_CONCURRENCY", "2")

        # 5 files, each ~80 chars → 5 chunks (each file is its own chunk
        # because two ~80-char files would exceed the 100-char threshold).
        diff = "".join(
            f"diff --git a/f{i}.py b/f{i}.py\n" + "x" * 80 + "\n"
            for i in range(5)
        )
        vcs_port.get_diff.return_value = diff

        in_flight = 0
        max_in_flight = 0
        gate = asyncio.Event()  # released after the test asserts ramp-up
        lock = asyncio.Lock()

        async def mock_stream(repo, pr, sub_diff, model=None):
            nonlocal in_flight, max_in_flight
            async with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            try:
                # Hold the call long enough for parallel scheduling to settle.
                await asyncio.sleep(0.01)
                yield ReviewResultEvent(Review(summary="ok", findings=[]))
            finally:
                async with lock:
                    in_flight -= 1

        ai_provider.stream_review = mock_stream
        _ = [e async for e in service.stream_review("acme/backend", 1)]

        assert max_in_flight <= 2, f"semaphore breached: max_in_flight={max_in_flight}"
        # Sanity: we did actually overlap (otherwise the cap test is vacuous).
        assert max_in_flight >= 2
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_review_service.py::TestStreamReviewSplit::test_concurrency_cap_is_respected -v`
Expected: passes (semaphore from Task 3 already enforces this). If `max_in_flight == 1`, increase the `asyncio.sleep` to give scheduling more headroom; if `max_in_flight > 2`, the semaphore wrapping is buggy.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_review_service.py
git commit -m "test(review): verify REVIEW_MAX_CONCURRENCY caps in-flight sub-reviews"
```

---

### Task 6: Drop hardcoded truncation in both AI adapters

**Files:**
- Modify: `app/adapters/ai/claude_code_adapter.py:259`
- Modify: `app/adapters/ai/opencode_adapter.py:218`

- [ ] **Step 1: Edit `claude_code_adapter.py`**

Find line 259:

```python
        async for chunk in _stream_claude_code(message, context=diff[:30000], model=model):
```

Replace with:

```python
        async for chunk in _stream_claude_code(message, context=diff, model=model):
```

The 20 000-char limit on `analyze_comments` (line 296) stays — out of scope.

- [ ] **Step 2: Edit `opencode_adapter.py`**

Find line 218:

```python
        async for chunk in _stream_opencode(message, context=diff[:30000], model=model):
```

Replace with:

```python
        async for chunk in _stream_opencode(message, context=diff, model=model):
```

- [ ] **Step 3: Run the existing adapter tests**

Run: `uv run pytest tests/unit/test_claude_code_adapter.py tests/unit/test_review_service.py tests/unit/test_diff_splitter.py -v`
Expected: all pass. The adapter tests don't pin the 30 000 truncation, so removing it is invisible to them.

- [ ] **Step 4: Run the full test suite as a smoke check**

Run: `uv run pytest -x`
Expected: all tests pass. If anything breaks, the most likely culprit is a fixture that asserts `save_review` call count differently after the `_run_review` refactor in Task 3.

- [ ] **Step 5: Commit**

```bash
git add app/adapters/ai/claude_code_adapter.py app/adapters/ai/opencode_adapter.py
git commit -m "fix(adapters): stop truncating diff at 30000 chars (service handles sizing)"
```

---

### Task 7: Document the two new env vars

**Files:**
- Modify: `.env.example`
- Modify: `README.md` (Configuration section, around line 170)

- [ ] **Step 1: Append to `.env.example`**

Add at the end of the file (preserve the trailing newline):

```dotenv
# Maximum characters of unified diff sent to the LLM in a single review call.
# When the PR diff exceeds this, ReviewService splits it by file, runs N
# sub-reviews in parallel (see REVIEW_MAX_CONCURRENCY), and merges the
# findings. Default 30000 — raise it (e.g. 100000) for large-context models
# such as Sonnet or Opus to reduce the number of split sub-calls.
# REVIEW_DIFF_MAX_CHARS=30000

# Maximum number of sub-reviews to run in parallel when a large diff is split.
# Each sub-review spawns a claude/opencode subprocess, so memory and rate
# limits scale with this number. Default 3.
# REVIEW_MAX_CONCURRENCY=3
```

- [ ] **Step 2: Update the README Configuration table**

Find the table starting around line 170:

```markdown
| Variable              | Default     | Purpose                                                |
|-----------------------|-------------|--------------------------------------------------------|
| `AI_PROVIDER`         | `opencode`  | Selects the AI backend at startup. Supported: `opencode`, `claude-code` *(when enabled)*. Live-switchable from the UI. |
| `ENABLE_CLAUDE_CODE`  | unset       | Set to `1` (or `true` / `yes` / `on`) to enable the `claude-code` provider — see [Claude Code is disabled by default](#claude-code-is-disabled-by-default) |
```

Add two rows so it becomes:

```markdown
| Variable                  | Default     | Purpose                                                |
|---------------------------|-------------|--------------------------------------------------------|
| `AI_PROVIDER`             | `opencode`  | Selects the AI backend at startup. Supported: `opencode`, `claude-code` *(when enabled)*. Live-switchable from the UI. |
| `ENABLE_CLAUDE_CODE`      | unset       | Set to `1` (or `true` / `yes` / `on`) to enable the `claude-code` provider — see [Claude Code is disabled by default](#claude-code-is-disabled-by-default) |
| `REVIEW_DIFF_MAX_CHARS`   | `30000`     | Threshold above which the PR diff is split by file and reviewed in parallel sub-calls. Raise for large-context models. |
| `REVIEW_MAX_CONCURRENCY`  | `3`         | Maximum number of parallel sub-reviews launched when a diff is split. Each spawns one provider subprocess. |
```

- [ ] **Step 3: Verify the docs look right**

Run: `grep -A2 "REVIEW_DIFF_MAX_CHARS" README.md .env.example`
Expected: both files mention the var with its default and a one-line purpose.

- [ ] **Step 4: Commit**

```bash
git add .env.example README.md
git commit -m "docs: document REVIEW_DIFF_MAX_CHARS and REVIEW_MAX_CONCURRENCY"
```

---

### Task 8: Final verification

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: all tests pass, including the 12 splitter tests and the new ReviewService split tests.

- [ ] **Step 2: Lint / type-check (if configured)**

Run: `uv run ruff check app/ tests/ 2>/dev/null || echo "ruff not configured, skipping"`
Expected: no errors (or skip gracefully if ruff isn't part of the project).

- [ ] **Step 3: Manual smoke check (optional, requires `gh` + `claude`/`opencode` set up)**

Pick a real large PR (>50 KB diff) and run a review through the UI or API. Verify:
- A `ReviewWarningEvent` appears at the start with `"split into N chunks"`.
- The final `Review.summary` starts with `"Reviewed across N chunks"`.
- Findings are returned for files spread across the whole PR, not just the first ones.

- [ ] **Step 4: Final commit if anything was tweaked, otherwise skip**

If the smoke check surfaced anything to fix, commit the fix; otherwise the branch is ready for PR.

---

## Self-review checklist (already performed)

- **Spec coverage:** every spec section maps to a task — splitter (T1, T2), service refactor (T3), failure handling (T4), concurrency (T5), adapter cleanup (T6), env docs (T7).
- **Out-of-scope items honoured:** no auto-tune by model, no per-chunk caching, no retry, no LLM-synthesized summary.
- **Type consistency:** `FileDiff`, `Chunk`, `Finding`, `Review` are used with the same field names everywhere.
- **No placeholders:** every code-change step shows the actual code.
- **Tests precede code:** TDD cycle for splitter tasks; for service tasks, tests are written first and the implementation lands in step 4 of Task 3.
- **Commit boundaries:** one commit per task, each leaves the tree green.
