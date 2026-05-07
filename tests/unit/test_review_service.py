"""Tests for ReviewService."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.domain.models import Finding, Review
from app.ports.ai_provider import ReviewChunkEvent, ReviewResultEvent, ReviewWarningEvent
from app.services.review_service import ReviewService


def _make_review(summary: str = "LGTM") -> Review:
    return Review(summary=summary, findings=[])


@pytest.fixture
def ai_provider():
    provider = MagicMock()
    return provider


@pytest.fixture
def cache_port():
    cache = MagicMock()
    cache.get_review.return_value = None
    return cache


@pytest.fixture
def vcs_port():
    vcs = MagicMock()
    vcs.get_diff.return_value = "--- a/foo.py\n+++ b/foo.py\n"
    return vcs


@pytest.fixture
def service(ai_provider, cache_port, vcs_port):
    return ReviewService(ai=ai_provider, cache=cache_port, vcs=vcs_port)


class TestGetOrRunReview:
    async def test_returns_cached_review(self, service, cache_port):
        cached = _make_review("cached result")
        cache_port.get_review.return_value = cached
        result = await service.get_or_run_review("acme/backend", 1)
        assert result.summary == "cached result"
        cache_port.get_review.assert_called_once_with("acme/backend", 1)

    async def test_runs_and_caches_when_no_cache(self, service, ai_provider, cache_port, vcs_port):
        review = _make_review("fresh")
        result_event = ReviewResultEvent(review)

        async def mock_stream(repo, pr, diff, model=None):
            yield ReviewChunkEvent("some text")
            yield result_event

        ai_provider.stream_review = mock_stream
        result = await service.get_or_run_review("acme/backend", 1)
        assert result.summary == "fresh"
        cache_port.save_review.assert_called_once_with("acme/backend", 1, review)


class TestRerunReview:
    async def test_skips_cache_and_overwrites(self, service, ai_provider, cache_port, vcs_port):
        review = _make_review("new review")
        result_event = ReviewResultEvent(review)

        async def mock_stream(repo, pr, diff, model=None):
            yield ReviewChunkEvent("text")
            yield result_event

        ai_provider.stream_review = mock_stream
        result = await service.rerun_review("acme/backend", 1)
        assert result.summary == "new review"
        cache_port.get_review.assert_not_called()
        cache_port.save_review.assert_called_once()


class TestStreamReview:
    async def test_yields_chunks_and_result(self, service, ai_provider, cache_port, vcs_port):
        review = _make_review("streamed")
        events = [ReviewChunkEvent("a"), ReviewChunkEvent("b"), ReviewResultEvent(review)]

        async def mock_stream(repo, pr, diff, model=None):
            for e in events:
                yield e

        ai_provider.stream_review = mock_stream
        collected = []
        async for event in service.stream_review("acme/backend", 1):
            collected.append(event)

        assert len(collected) == 3
        assert isinstance(collected[0], ReviewChunkEvent)
        assert isinstance(collected[2], ReviewResultEvent)
        cache_port.save_review.assert_called_once()

    async def test_clear_cache_removes_review(self, service, cache_port):
        service.clear_cache("acme/backend", 1)
        cache_port.clear_review.assert_called_once_with("acme/backend", 1)


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


class TestReadIntEnv:
    """The helper that parses REVIEW_DIFF_MAX_CHARS / REVIEW_MAX_CONCURRENCY."""

    def test_returns_default_when_unset(self, monkeypatch):
        from app.services.review_service import _read_int_env
        monkeypatch.delenv("MY_TEST_VAR", raising=False)
        assert _read_int_env("MY_TEST_VAR", 42) == 42

    def test_returns_default_when_empty_string(self, monkeypatch):
        from app.services.review_service import _read_int_env
        monkeypatch.setenv("MY_TEST_VAR", "")
        assert _read_int_env("MY_TEST_VAR", 42) == 42

    def test_returns_default_when_not_an_int(self, monkeypatch):
        from app.services.review_service import _read_int_env
        monkeypatch.setenv("MY_TEST_VAR", "abc")
        assert _read_int_env("MY_TEST_VAR", 42) == 42

    def test_returns_default_when_zero_or_negative(self, monkeypatch):
        from app.services.review_service import _read_int_env
        monkeypatch.setenv("MY_TEST_VAR", "0")
        assert _read_int_env("MY_TEST_VAR", 42) == 42
        monkeypatch.setenv("MY_TEST_VAR", "-5")
        assert _read_int_env("MY_TEST_VAR", 42) == 42

    def test_returns_parsed_value_when_valid(self, monkeypatch):
        from app.services.review_service import _read_int_env
        monkeypatch.setenv("MY_TEST_VAR", "100")
        assert _read_int_env("MY_TEST_VAR", 42) == 100
