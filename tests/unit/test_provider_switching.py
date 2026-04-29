"""Tests for runtime AI provider switching: SwitchableAIProvider + endpoints."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import SwitchableAIProvider, app
from app.ports.ai_provider import AIProvider, FixChunkEvent, ReviewChunkEvent


# --- SwitchableAIProvider ---


class _FakeAIProvider(AIProvider):
    """Minimal AIProvider for testing the switchable wrapper."""

    def __init__(self, tag: str) -> None:
        self.tag = tag

    async def stream_review(self, repo_full_name, pr_number, diff, model=None):
        yield ReviewChunkEvent(text=f"{self.tag}:review")

    async def analyze_comments(self, repo_full_name, pr_number, comments):
        return [{"tag": self.tag, "kind": "analyze"}]

    async def stream_fix(self, repo_dir, repo_full_name, pr_number, comment_body):
        yield FixChunkEvent(text=f"{self.tag}:fix")

    async def generate_text(self, prompt, timeout=60):
        return f"{self.tag}:text"


@pytest.fixture
def two_providers():
    return {"opencode": _FakeAIProvider("OC"), "claude-code": _FakeAIProvider("CC")}


def test_switchable_initial_active(two_providers):
    sw = SwitchableAIProvider(two_providers, active="opencode")
    assert sw.active == "opencode"


def test_switchable_unknown_initial_raises(two_providers):
    with pytest.raises(ValueError, match="Unknown active provider"):
        SwitchableAIProvider(two_providers, active="bogus")


def test_switchable_set_active(two_providers):
    sw = SwitchableAIProvider(two_providers, active="opencode")
    sw.set_active("claude-code")
    assert sw.active == "claude-code"


def test_switchable_set_active_unknown_raises(two_providers):
    sw = SwitchableAIProvider(two_providers, active="opencode")
    with pytest.raises(ValueError, match="Unknown provider 'bogus'"):
        sw.set_active("bogus")


@pytest.mark.asyncio
async def test_switchable_delegates_review(two_providers):
    sw = SwitchableAIProvider(two_providers, active="opencode")
    chunks = [e.text async for e in sw.stream_review("o/r", 1, "diff")]
    assert chunks == ["OC:review"]
    sw.set_active("claude-code")
    chunks = [e.text async for e in sw.stream_review("o/r", 1, "diff")]
    assert chunks == ["CC:review"]


@pytest.mark.asyncio
async def test_switchable_delegates_analyze(two_providers):
    sw = SwitchableAIProvider(two_providers, active="claude-code")
    out = await sw.analyze_comments("o/r", 1, [])
    assert out == [{"tag": "CC", "kind": "analyze"}]


@pytest.mark.asyncio
async def test_switchable_delegates_fix(two_providers):
    sw = SwitchableAIProvider(two_providers, active="opencode")
    chunks = [c.text async for c in sw.stream_fix("/tmp/x", "o/r", 1, "body")]
    assert chunks == ["OC:fix"]


@pytest.mark.asyncio
async def test_switchable_delegates_generate_text(two_providers):
    sw = SwitchableAIProvider(two_providers, active="claude-code")
    assert await sw.generate_text("hi") == "CC:text"


# --- HTTP endpoints ---


@pytest.fixture
def client():
    return TestClient(app)


def test_get_providers_returns_status(client):
    res = client.get("/api/providers")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) >= {"active", "from_env", "supported", "available", "clis"}
    assert body["active"] in body["supported"]
    assert set(body["supported"]) == {"opencode", "claude-code"}
    assert set(body["available"].keys()) == {"opencode", "claude-code"}
    assert body["clis"] == {"opencode": "opencode", "claude-code": "claude"}


def test_get_config_includes_provider_status(client):
    res = client.get("/api/config")
    assert res.status_code == 200
    body = res.json()
    # Backwards-compat fields preserved.
    assert body["ai_provider"] in {"opencode", "claude-code"}
    assert body["supported_providers"] == ["opencode", "claude-code"]
    # New nested status block.
    assert body["providers"]["active"] == body["ai_provider"]
    assert "available" in body["providers"]


def test_set_provider_switches_active(client):
    # Switch to claude-code.
    res = client.post("/api/provider", json={"name": "claude-code"})
    assert res.status_code == 200
    assert res.json()["active"] == "claude-code"

    # Confirm via /api/config.
    assert client.get("/api/config").json()["ai_provider"] == "claude-code"

    # Switch back.
    res = client.post("/api/provider", json={"name": "opencode"})
    assert res.status_code == 200
    assert res.json()["active"] == "opencode"


def test_set_provider_rejects_unknown(client):
    res = client.post("/api/provider", json={"name": "no-such-thing"})
    assert res.status_code == 400
    assert "supported" in res.json()["detail"].lower()


def test_set_provider_warns_if_cli_missing(client):
    """When the picked provider's CLI is missing, the response body carries a warning."""
    with patch("app.main._provider_available", return_value=False):
        res = client.post("/api/provider", json={"name": "claude-code"})
    assert res.status_code == 200
    body = res.json()
    assert body["active"] == "claude-code"
    assert body["warning"] is not None
    assert "claude" in body["warning"]
    # Restore active to opencode for subsequent tests.
    client.post("/api/provider", json={"name": "opencode"})


def test_models_returns_empty_with_warning_when_cli_missing(client):
    """The /api/models endpoint must not 500 when the active provider's CLI isn't installed."""
    # Force opencode active and pretend its CLI is missing.
    client.post("/api/provider", json={"name": "opencode"})
    with patch("app.main._provider_available", return_value=False):
        res = client.get("/api/models")
    assert res.status_code == 200
    body = res.json()
    assert body["models"] == []
    assert "warning" in body


# --- Claude model discovery ---


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Isolate $HOME and CWD so the discovery doesn't read the dev's real config."""
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    return home, cwd


def test_discover_claude_models_falls_back_to_aliases(isolated_home):
    from app.main import _discover_claude_models, _CLAUDE_ALIASES

    assert _discover_claude_models() == list(_CLAUDE_ALIASES)


def test_discover_claude_models_picks_up_anthropic_model_env(isolated_home, monkeypatch):
    from app.main import _discover_claude_models

    monkeypatch.setenv("ANTHROPIC_MODEL", "eu.anthropic.claude-sonnet-4-20250514-v1:0")
    models = _discover_claude_models()
    assert models[0] == "eu.anthropic.claude-sonnet-4-20250514-v1:0"
    # Aliases still appended after the discovered ID.
    assert "sonnet" in models


def test_discover_claude_models_reads_user_settings(isolated_home):
    from app.main import _discover_claude_models

    home, _ = isolated_home
    settings_dir = home / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        '{"model": "us.anthropic.claude-opus-4-20250101-v1:0"}'
    )

    models = _discover_claude_models()
    assert "us.anthropic.claude-opus-4-20250101-v1:0" in models
    # Order: discovery first, aliases last.
    assert models.index("us.anthropic.claude-opus-4-20250101-v1:0") < models.index("opus")


def test_discover_claude_models_project_overrides_user(isolated_home):
    from app.main import _discover_claude_models

    home, cwd = isolated_home
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text('{"model": "user-level-model"}')
    (cwd / ".claude").mkdir()
    (cwd / ".claude" / "settings.json").write_text('{"model": "project-level-model"}')

    models = _discover_claude_models()
    # Both are present, but project-level comes first.
    assert models.index("project-level-model") < models.index("user-level-model")


def test_discover_claude_models_dedupes(isolated_home, monkeypatch):
    from app.main import _discover_claude_models

    monkeypatch.setenv("ANTHROPIC_MODEL", "sonnet")
    models = _discover_claude_models()
    # "sonnet" must appear exactly once even though it's both an env value and an alias.
    assert models.count("sonnet") == 1


def test_discover_claude_models_ignores_malformed_settings(isolated_home):
    from app.main import _discover_claude_models, _CLAUDE_ALIASES

    home, _ = isolated_home
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text("not valid json {")

    # Falls back gracefully to aliases; doesn't raise.
    assert _discover_claude_models() == list(_CLAUDE_ALIASES)


def test_models_endpoint_uses_discovery_for_claude_code(client, isolated_home, monkeypatch):
    from app.main import _CLAUDE_ALIASES

    monkeypatch.setenv("ANTHROPIC_MODEL", "eu.anthropic.claude-sonnet-4-20250514-v1:0")
    client.post("/api/provider", json={"name": "claude-code"})
    res = client.get("/api/models")
    assert res.status_code == 200
    body = res.json()
    assert body["models"][0] == "eu.anthropic.claude-sonnet-4-20250514-v1:0"
    for alias in _CLAUDE_ALIASES:
        assert alias in body["models"]
    # Restore.
    client.post("/api/provider", json={"name": "opencode"})
