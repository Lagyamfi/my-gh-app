# Large PR review: split & merge instead of silent truncation

**Date:** 2026-05-06
**Status:** Approved, ready for plan
**Scope:** `app/services/review_service.py`, new `app/services/_diff_splitter.py`, `app/adapters/ai/claude_code_adapter.py`, `app/adapters/ai/opencode_adapter.py`, tests, env docs

## Problem

Both AI adapters silently truncate the PR diff to 30 000 characters before sending it to the LLM:

- `app/adapters/ai/claude_code_adapter.py:259` → `context=diff[:30000]`
- `app/adapters/ai/opencode_adapter.py:218` → `context=diff[:30000]`

For a large PR (often >100 KB of diff), the model only sees roughly the first 30 KB — typically 1 to 3 files in the order returned by `gh pr diff`. Files beyond the threshold are never analyzed and produce zero findings. The user sees "around a dozen issues on a huge PR" and has no signal that the review was incomplete.

The threshold is hardcoded, not configurable, and emits no warning to either the API client or the frontend.

## Goals

1. Make the threshold configurable via env (`REVIEW_DIFF_MAX_CHARS`, default 30 000).
2. When the diff exceeds the threshold, split it by file and review each chunk in parallel, then merge findings into a single `Review`.
3. Surface what happened to the user via `ReviewWarningEvent` (split count, files covered, files truncated, chunks that failed).

## Non-goals

- **Auto-adjusting the threshold based on the selected model.** Explicitly out of scope; the user rejected this. Operators tune the env var if Sonnet/Opus warrants a larger window.
- **Caching individual chunk reviews.** The cache layer keeps caching the merged `Review` only. Per-chunk caching is a separate, larger design (cache key, invalidation, store).
- **LLM-synthesized global summary.** The merged summary is structured concatenation, no extra LLM call.
- **Automatic retry of failed chunks.** A chunk that fails emits a warning and the review proceeds without it; the user can re-run.

## Architecture

The split and merge logic lives in `ReviewService`. AI adapters keep their single-call shape and stop auto-truncating; the service guarantees that what it passes them is sized correctly. A new `app/services/_diff_splitter.py` module owns the parsing and packing logic, isolated and unit-testable without subprocess calls.

```
                                  ┌──────────────────────┐
PR diff ────► ReviewService ─────►│ _diff_splitter       │
                  │               │  split_unified_diff  │
                  │               │  pack_chunks         │
                  │               └──────────────────────┘
                  │                          │
                  │                          ▼
                  │                  list[Chunk]
                  │
                  ▼
        asyncio.gather(
          _review_chunk(c) for c in chunks,
          semaphore=N,
          return_exceptions=True
        )
                  │
                  ▼
        merge findings + structured summary
                  │
                  ▼
        cache + yield ReviewResultEvent
```

## Components

### `app/services/_diff_splitter.py` (new)

Pure functions, no I/O:

```python
@dataclass
class FileDiff:
    path: str       # extracted from "diff --git a/<path> b/<path>"
    content: str    # full diff section starting with "diff --git "

@dataclass
class Chunk:
    content: str              # concatenated diff sections, ≤ max_chars
    files: list[str]          # paths covered
    truncated_files: list[str]  # paths whose content was cut to fit

def split_unified_diff(diff: str) -> list[FileDiff]:
    """Split a unified diff on `diff --git ` boundaries."""

def pack_chunks(files: list[FileDiff], max_chars: int) -> list[Chunk]:
    """First-Fit Decreasing bin-packing.

    - Sort files by len(content) descending.
    - For each file, place into the first chunk where it fits.
    - If no chunk fits, open a new chunk.
    - If a single file exceeds max_chars, it forms a solo chunk with its
      content cut to max_chars and its path added to truncated_files.
    """
```

Splitter regex: `re.split(r'(?m)^(?=diff --git )', diff)`. Empty leading element is dropped. Path is parsed from the first line of each section.

### `ReviewService.stream_review` (modified)

Pseudocode (real impl uses an `asyncio.Queue`, see "Streaming the chunk events" below):

```python
async def stream_review(self, repo_full_name, pr_number, model=None):
    diff = self._vcs.get_diff(repo_full_name, pr_number)
    max_chars = int(os.getenv("REVIEW_DIFF_MAX_CHARS", "30000"))
    max_concurrency = int(os.getenv("REVIEW_MAX_CONCURRENCY", "3"))

    if len(diff) <= max_chars:
        # Fast path — byte-for-byte unchanged behavior.
        async for event in self._ai.stream_review(repo_full_name, pr_number, diff, model=model):
            if isinstance(event, ReviewResultEvent):
                self._cache.save_review(repo_full_name, pr_number, event.review)
            yield event
        return

    # Split path.
    files = split_unified_diff(diff)
    chunks = pack_chunks(files, max_chars)
    yield ReviewWarningEvent([
        f"Diff too large ({len(diff)} chars), split into {len(chunks)} chunks "
        f"across {len(files)} files.",
        *(f"Truncated file: {f}" for c in chunks for f in c.truncated_files),
    ])

    # Run sub-reviews bounded by Semaphore(max_concurrency); each task drains
    # its provider's event stream into a shared queue and returns its final
    # Review (or raises). The outer loop in stream_review consumes the queue
    # and yields ReviewChunkEvent / ReviewWarningEvent to the caller in real
    # time. Failed tasks become ReviewWarningEvent; successful ones contribute
    # their findings to the merge step.

    results = await run_chunks_in_parallel(chunks, max_concurrency)  # list[Review | Exception]

    findings: list[Finding] = []
    sub_summaries: list[str] = []
    failed_count = 0
    for chunk, result in zip(chunks, results):
        if isinstance(result, Exception):
            failed_count += 1
            yield ReviewWarningEvent([
                f"Chunk failed: files={chunk.files} error={result}"
            ])
            continue
        findings.extend(result.findings)
        sub_summaries.append(result.summary)

    summary = (
        f"Reviewed across {len(chunks)} chunks "
        f"({len(files)} files, {failed_count} failed).\n\n"
        + "\n\n".join(f"- {s}" for s in sub_summaries if s)
    )
    merged = Review(summary=summary, findings=findings)
    self._cache.save_review(repo_full_name, pr_number, merged)
    yield ReviewResultEvent(review=merged)
```

#### Streaming the chunk events

`asyncio.gather` collects coroutine return values, so `ReviewChunkEvent`s emitted by sub-calls cannot simply be `yield`ed from inside `review_chunk`. The implementation uses an `asyncio.Queue`:

- Each `review_chunk` task pushes `ReviewChunkEvent` onto the queue as it streams, and pushes its final `Review` (or exception) onto a separate `results` list.
- A consumer loop in `stream_review` drains the queue and yields events to the caller until all tasks finish.
- A sentinel (e.g. `None`) per task signals task completion to the consumer.

This keeps the streaming UX (live text appearing as the LLM thinks) intact across the parallel split.

### Adapters

`app/adapters/ai/claude_code_adapter.py:259`:

```python
# before
async for chunk in _stream_claude_code(message, context=diff[:30000], model=model):
# after
async for chunk in _stream_claude_code(message, context=diff, model=model):
```

`app/adapters/ai/opencode_adapter.py:218`: same change. The service guarantees `len(diff) <= max_chars` before calling.

`analyze_comments` (line 296 in claude, 252 in opencode) keeps its 20 000-char truncation — out of scope, comment volume is naturally bounded by GitHub UX.

## Event flow (client perspective)

For a 95 KB diff splitting into 4 chunks, with chunk #3 timing out:

1. `ReviewWarningEvent(["Diff too large (95214 chars), split into 4 chunks across 12 files."])`
2. `ReviewChunkEvent` × M (interleaved live text from chunks 1, 2, 4)
3. `ReviewWarningEvent(["Chunk failed: files=['big_module.py'] error=claude exited with code 124: ..."])`
4. `ReviewResultEvent(merged_review)` — summary explicitly says `"Reviewed across 4 chunks (12 files, 1 failed)."`

The existing frontend already consumes `ReviewWarningEvent` and renders it; no UI work needed.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `REVIEW_DIFF_MAX_CHARS` | `30000` | Threshold above which the diff is split. Raise it for large-context models (Sonnet, Opus). |
| `REVIEW_MAX_CONCURRENCY` | `3` | Max concurrent sub-reviews. Each spawns a `claude` / `opencode` subprocess; 3 is conservative for typical local setups. |

Both documented in `.env.example` and the configuration section of `README.md`.

## Testing

`tests/services/test_diff_splitter.py` (new):

- `split_unified_diff` on a 3-file diff returns 3 `FileDiff` with correct paths.
- `split_unified_diff` on an empty string returns `[]`.
- `split_unified_diff` strips the empty leading element from `re.split`.
- `pack_chunks`: 5 small files (sum < threshold) → 1 chunk listing all 5.
- `pack_chunks`: 1 file at exactly `max_chars` → 1 chunk, no truncation.
- `pack_chunks`: 1 file > `max_chars` → 1 solo chunk with `truncated_files=[path]` and `len(content) == max_chars`.
- `pack_chunks`: FFD ordering — 3 files of 25 KB / 8 KB / 3 KB with `max_chars=30000` → 2 chunks, first holds the 25 KB file, second holds 8 KB + 3 KB.

`tests/services/test_review_service.py` (extend):

- Diff under threshold → exactly 1 call to `ai.stream_review`, no warning, behavior unchanged.
- Diff over threshold → N calls, initial warning emitted, merged findings preserve order from chunks.
- 1 sub-call raises `ProviderError` → warning emitted naming the failed chunk's files, other findings preserved, no exception escapes.
- All sub-calls fail → review yields with empty findings + warnings + summary `"Reviewed across N chunks (M files, N failed)."`.
- Concurrency cap respected: with 5 chunks and `REVIEW_MAX_CONCURRENCY=2`, no more than 2 sub-calls are in-flight at any moment (verified via a counting fake adapter).

Tests use `pytest-asyncio` (already in `pyproject.toml` `[dev]` deps with `asyncio_mode = "auto"`).

## Migration / rollout

No DB, no schema, no public API change. The contract of `stream_review` is unchanged from the caller's perspective: same event types, same final `Review`. Existing clients (FastAPI SSE endpoint, frontend) work unmodified.

Default threshold = old hardcoded value (30 000), so behavior on small PRs is byte-identical. Behavior on large PRs strictly improves.

## Risks

- **Subprocess fan-out memory.** 3 concurrent `claude` processes load 3× the model context. Conservative default mitigates; operators with constrained machines can lower `REVIEW_MAX_CONCURRENCY=1`.
- **Diff splitter regex correctness.** Pathological diffs (binary files, renames with spaces in paths, submodule changes) may parse oddly. The regex anchors on line-start `diff --git ` which `gh` always emits; tests cover the common cases. Worst case for an unparsed edge case: that file lands in the previous chunk (still reviewed, just attributed to the wrong file in the warning message).
- **Streaming queue backpressure.** If sub-calls flood the queue faster than the SSE consumer drains it, memory grows. Bounded queue (e.g. `maxsize=1000`) caps it; in practice chunk text is human-readable speed.

## Out-of-scope follow-ups (intentionally deferred)

- Per-chunk cache keyed on the sub-diff hash, to skip re-reviewing unchanged files on a re-run.
- LLM-synthesized global summary instead of structured concatenation.
- Auto-tune `max_chars` from the selected model's context window.
- Retry policy on chunk failures.
