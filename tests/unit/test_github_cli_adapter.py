"""Tests for GitHubCLIAdapter — focused on the create_review fallback logic."""
import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from app.adapters.vcs.github_cli_adapter import GitHubCLIAdapter
from app.domain.exceptions import VCSError


@pytest.fixture
def adapter():
    return GitHubCLIAdapter()


def _ok(stdout: str) -> MagicMock:
    p = MagicMock(spec=subprocess.CompletedProcess)
    p.returncode = 0
    p.stdout = stdout
    p.stderr = ""
    return p


def _err(stderr: str, returncode: int = 1) -> MagicMock:
    p = MagicMock(spec=subprocess.CompletedProcess)
    p.returncode = returncode
    p.stdout = ""
    p.stderr = stderr
    return p


class TestCreateReview:
    def test_succeeds_with_inline_comments_when_github_accepts(self, adapter):
        comments = [{"path": "app/foo.py", "line": 10, "body": "Issue here"}]
        with patch.object(adapter, "get_pr_head_sha", return_value="abc123"), \
             patch("app.adapters.vcs.github_cli_adapter.subprocess.run") as mock_run:
            mock_run.return_value = _ok(json.dumps({"id": 42, "html_url": "https://x"}))
            result = adapter.create_review("acme/repo", 1, "body", "REQUEST_CHANGES", comments)
        assert result == {"id": 42, "html_url": "https://x"}
        # Single call since the first attempt succeeded
        assert mock_run.call_count == 1

    def test_falls_back_to_body_only_on_422(self, adapter):
        """When GitHub rejects inline comments with 422, retry with empty
        comments[] and the findings folded into the review body."""
        comments = [
            {"path": "app/foo.py", "line": 9999, "body": "AI-hallucinated line"},
            {"path": "app/bar.py", "line": 5, "body": "Real issue"},
        ]
        with patch.object(adapter, "get_pr_head_sha", return_value="abc123"), \
             patch("app.adapters.vcs.github_cli_adapter.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _err("gh: Unprocessable Entity (HTTP 422)"),
                _ok(json.dumps({"id": 7, "html_url": "https://x"})),
            ]
            result = adapter.create_review("acme/repo", 1, "Please address", "REQUEST_CHANGES", comments)
        assert result["id"] == 7
        assert result["_fallback_applied"] is True
        assert mock_run.call_count == 2
        # Second call's payload should have empty comments and folded body
        retry_payload = json.loads(mock_run.call_args_list[1].kwargs["input"])
        assert retry_payload["comments"] == []
        assert "app/foo.py:9999" in retry_payload["body"]
        assert "app/bar.py:5" in retry_payload["body"]
        assert "Please address" in retry_payload["body"]

    def test_does_not_retry_when_no_comments_to_drop(self, adapter):
        """If there are no inline comments, a 422 is a real failure — don't loop."""
        with patch.object(adapter, "get_pr_head_sha", return_value="abc123"), \
             patch("app.adapters.vcs.github_cli_adapter.subprocess.run") as mock_run:
            mock_run.return_value = _err("gh: Unprocessable Entity (HTTP 422)")
            with pytest.raises(VCSError, match="422"):
                adapter.create_review("acme/repo", 1, "body", "REQUEST_CHANGES", [])
        assert mock_run.call_count == 1

    def test_raises_on_non_retryable_non_422_error(self, adapter):
        """Permanent errors like 403 should NOT trigger fallback or retry."""
        with patch.object(adapter, "get_pr_head_sha", return_value="abc123"), \
             patch("app.adapters.vcs.github_cli_adapter.subprocess.run") as mock_run, \
             patch("app.adapters.vcs.github_cli_adapter.time.sleep"):
            mock_run.return_value = _err("gh: Forbidden (HTTP 403)")
            with pytest.raises(VCSError, match="403"):
                adapter.create_review(
                    "acme/repo", 1, "body", "REQUEST_CHANGES",
                    [{"path": "f.py", "line": 1, "body": "x"}],
                )
        assert mock_run.call_count == 1

    def test_rejects_invalid_event(self, adapter):
        with pytest.raises(VCSError, match="invalid review event"):
            adapter.create_review("acme/repo", 1, "body", "BLESS", [])

    def test_retries_on_502_then_succeeds(self, adapter):
        """502 Bad Gateway is a transient GitHub error — retry with backoff."""
        with patch.object(adapter, "get_pr_head_sha", return_value="abc123"), \
             patch("app.adapters.vcs.github_cli_adapter.subprocess.run") as mock_run, \
             patch("app.adapters.vcs.github_cli_adapter.time.sleep") as mock_sleep:
            mock_run.side_effect = [
                _err("gh: Server Error (HTTP 502)"),
                _ok(json.dumps({"id": 99, "html_url": "https://x"})),
            ]
            result = adapter.create_review(
                "acme/repo", 1, "body", "REQUEST_CHANGES",
                [{"path": "f.py", "line": 1, "body": "x"}],
            )
        assert result["id"] == 99
        assert mock_run.call_count == 2
        assert mock_sleep.call_count == 1  # one backoff between attempts

    def test_retries_on_503_and_504(self, adapter):
        """503 Service Unavailable and 504 Gateway Timeout are also retried."""
        with patch.object(adapter, "get_pr_head_sha", return_value="abc"), \
             patch("app.adapters.vcs.github_cli_adapter.subprocess.run") as mock_run, \
             patch("app.adapters.vcs.github_cli_adapter.time.sleep"):
            mock_run.side_effect = [
                _err("gh: HTTP 503"),
                _err("gh: HTTP 504"),
                _ok(json.dumps({"id": 1})),
            ]
            result = adapter.create_review("acme/repo", 1, "b", "COMMENT", [])
        assert result["id"] == 1
        assert mock_run.call_count == 3

    def test_gives_up_after_all_retries_exhausted(self, adapter):
        """4 total attempts (1 initial + 3 retries) on persistent 502."""
        with patch.object(adapter, "get_pr_head_sha", return_value="abc"), \
             patch("app.adapters.vcs.github_cli_adapter.subprocess.run") as mock_run, \
             patch("app.adapters.vcs.github_cli_adapter.time.sleep"):
            mock_run.return_value = _err("gh: Server Error (HTTP 502)")
            with pytest.raises(VCSError, match="502"):
                adapter.create_review("acme/repo", 1, "b", "COMMENT", [])
        # 1 initial + 3 retry delays = 4 attempts total
        assert mock_run.call_count == 4

    def test_does_not_retry_on_422(self, adapter):
        """422 isn't transient — fold-and-retry path handles it once, no
        exponential backoff loop."""
        with patch.object(adapter, "get_pr_head_sha", return_value="abc"), \
             patch("app.adapters.vcs.github_cli_adapter.subprocess.run") as mock_run, \
             patch("app.adapters.vcs.github_cli_adapter.time.sleep") as mock_sleep:
            mock_run.return_value = _err("gh: HTTP 422")
            with pytest.raises(VCSError, match="422"):
                adapter.create_review("acme/repo", 1, "b", "COMMENT", [])
        # No comments → no fallback → just the initial attempt
        assert mock_run.call_count == 1
        assert mock_sleep.call_count == 0

    def test_does_not_retry_on_404(self, adapter):
        """404 is a real error — don't waste retries."""
        with patch.object(adapter, "get_pr_head_sha", return_value="abc"), \
             patch("app.adapters.vcs.github_cli_adapter.subprocess.run") as mock_run, \
             patch("app.adapters.vcs.github_cli_adapter.time.sleep") as mock_sleep:
            mock_run.return_value = _err("gh: Not Found (HTTP 404)")
            with pytest.raises(VCSError, match="404"):
                adapter.create_review("acme/repo", 1, "b", "COMMENT", [])
        assert mock_run.call_count == 1
        assert mock_sleep.call_count == 0

    def test_fold_comments_preserves_path_and_line(self):
        body = "Top body"
        comments = [
            {"path": "a.py", "line": 1, "body": "first"},
            {"path": "b.py", "line": 22, "body": "second\nmultiline"},
        ]
        result = GitHubCLIAdapter._fold_comments_into_body(body, comments)
        assert "Top body" in result
        assert "`a.py:1`" in result
        assert "`b.py:22`" in result
        assert "first" in result
        assert "multiline" in result
