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
            try:
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
            finally:
                # ALWAYS push the sentinel so the consumer never deadlocks,
                # even if this task is cancelled mid-stream.
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
                if error is not None:
                    msg = (
                        f"Chunk failed: files={chunk.files} "
                        f"error={type(error).__name__}: {error}"
                    )
                else:
                    msg = f"Chunk failed: files={chunk.files} no result returned"
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
        logger.info(
            "review-service | split done | repo=%s pr=#%d chunks=%d succeeded=%d failed=%d findings=%d",
            repo_full_name, pr_number, len(chunks),
            len(chunks) - failed_count, failed_count, len(findings),
        )
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
