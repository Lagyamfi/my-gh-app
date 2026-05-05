"""Tests for the ENABLE_CLAUDE_CODE feature flag.

These tests reload ``app.main`` with the flag explicitly off because the
gating is evaluated at module load time. They run in a single subprocess so
the reload doesn't pollute other tests' module state.
"""
import importlib
import os

import pytest


@pytest.fixture
def main_with_claude_disabled(monkeypatch):
    """Reload app.main with ENABLE_CLAUDE_CODE unset so claude-code is gated off."""
    monkeypatch.delenv("ENABLE_CLAUDE_CODE", raising=False)
    # Also clear AI_PROVIDER so the auto-detect path is exercised cleanly.
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    import app.main as main
    main = importlib.reload(main)
    try:
        yield main
    finally:
        # Restore the conftest default so subsequent tests see claude-code enabled.
        os.environ["ENABLE_CLAUDE_CODE"] = "1"
        importlib.reload(main)


def test_supported_providers_excludes_claude_code(main_with_claude_disabled):
    main = main_with_claude_disabled
    assert "claude-code" not in main._SUPPORTED_PROVIDERS
    assert "claude-code" not in main._PROVIDER_CLIS
    assert "opencode" in main._SUPPORTED_PROVIDERS


def test_build_ai_provider_refuses_claude_code(main_with_claude_disabled):
    main = main_with_claude_disabled
    with pytest.raises(ValueError, match="ENABLE_CLAUDE_CODE"):
        main._build_ai_provider("claude-code")


def test_set_provider_returns_helpful_400_for_claude_code(main_with_claude_disabled):
    """The /api/provider endpoint should return a 400 with a message that
    explains exactly how to re-enable the provider."""
    from fastapi.testclient import TestClient
    main = main_with_claude_disabled
    client = TestClient(main.app)
    res = client.post("/api/provider", json={"name": "claude-code"})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "ENABLE_CLAUDE_CODE=1" in detail
    assert "disabled" in detail.lower()


def test_get_providers_excludes_claude_code(main_with_claude_disabled):
    from fastapi.testclient import TestClient
    main = main_with_claude_disabled
    client = TestClient(main.app)
    res = client.get("/api/providers")
    assert res.status_code == 200
    body = res.json()
    assert "claude-code" not in body["supported"]
    assert "claude-code" not in body["available"]


def test_ai_provider_env_var_falls_back_when_claude_requested_but_disabled(
    monkeypatch, caplog,
):
    """If AI_PROVIDER=claude-code but the flag is off, the server should
    log a clear warning and fall back to auto-detect rather than crashing."""
    monkeypatch.delenv("ENABLE_CLAUDE_CODE", raising=False)
    monkeypatch.setenv("AI_PROVIDER", "claude-code")
    import app.main as main
    with caplog.at_level("WARNING"):
        main = importlib.reload(main)
    try:
        # Resolved active provider should NOT be claude-code.
        assert main._ai.active != "claude-code"
        # Warning should mention how to re-enable.
        assert any("ENABLE_CLAUDE_CODE=1" in rec.message for rec in caplog.records)
    finally:
        os.environ["ENABLE_CLAUDE_CODE"] = "1"
        os.environ.pop("AI_PROVIDER", None)
        importlib.reload(main)


def test_truthy_env_values_enable_claude_code(monkeypatch):
    """The flag accepts 1 / true / yes / on (case-insensitive)."""
    import app.main as main
    for value in ("1", "true", "TRUE", "yes", "On"):
        monkeypatch.setenv("ENABLE_CLAUDE_CODE", value)
        main = importlib.reload(main)
        assert "claude-code" in main._SUPPORTED_PROVIDERS, f"value={value!r} should enable"

    # Reset for downstream tests
    os.environ["ENABLE_CLAUDE_CODE"] = "1"
    importlib.reload(main)


def test_falsy_env_values_keep_claude_code_disabled(monkeypatch):
    """Empty / 0 / false / off all keep the provider gated off."""
    import app.main as main
    for value in ("", "0", "false", "no", "off", "anything-else"):
        monkeypatch.setenv("ENABLE_CLAUDE_CODE", value)
        main = importlib.reload(main)
        assert "claude-code" not in main._SUPPORTED_PROVIDERS, f"value={value!r} should NOT enable"

    os.environ["ENABLE_CLAUDE_CODE"] = "1"
    importlib.reload(main)
