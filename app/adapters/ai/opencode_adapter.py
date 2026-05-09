"""OpenCode CLI implementation of AIProvider.

Most of the behaviour is inherited from :class:`BaseCLIAIAdapter`. This module
only declares opencode's specifics:

- argv assembly (``opencode run [--dir <cwd>] [--model <name>] [<prompt>]``)
- a stdin fallback for prompts above ~4 KB to dodge ARG_MAX issues
- per-fix permission config files written into the worktree
- stderr triage that distinguishes opencode's progress markers from real
  warnings
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncGenerator

from app.adapters.ai._base import (
    BaseCLIAIAdapter,
    CLIInvocation,
    DEFAULT_FIX_TIMEOUT,
    DEFAULT_GENERATE_TIMEOUT,
    DEFAULT_REVIEW_TIMEOUT,
    Mode,
    StderrCategory,
)

logger = logging.getLogger(__name__)

# Lines containing any of these substrings are noise we never want to surface.
_STDERR_NOISE = ["[STALE]", "fatal: options '--name-only'", "cannot be used together"]
# Lines starting with these are progress / model info — log at INFO, not WARNING.
_STDERR_INFO_PREFIXES = (">", "✓", "✗", "·", "Model:", "Session:")
# Above this length the prompt goes via stdin so we don't trip ARG_MAX.
_STDIN_THRESHOLD_BYTES = 4000

_OPENCODE_PERMISSIONS = {
    "permissions": {
        "allow": ["Bash", "Edit", "Write", "Read", "Grep", "Glob",
                  "bash", "edit", "write", "read", "grep", "glob"],
    }
}

_OPENCODE_PROJECT_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "agent": {
        "build": {
            "permission": {
                "bash": "allow",
                "edit": "allow",
                "write": "allow",
            }
        }
    },
}


class OpenCodeAdapter(BaseCLIAIAdapter):
    """Implements :class:`AIProvider` using the ``opencode`` CLI."""

    cli_name = "opencode"
    cli_executable = "opencode"
    raise_on_nonzero_exit = False

    def build_invocation(
        self,
        prompt: str,
        *,
        mode: Mode,
        cwd: str | None,
        model: str | None,
    ) -> CLIInvocation:
        argv: list[str] = ["opencode", "run"]
        if cwd:
            # opencode handles working-directory selection via its own flag
            # rather than via subprocess cwd.
            argv += ["--dir", cwd]
        if model:
            argv += ["--model", model]

        if len(prompt) > _STDIN_THRESHOLD_BYTES:
            return CLIInvocation(argv=argv, stdin_payload=prompt)
        argv.append(prompt)
        return CLIInvocation(argv=argv)

    def classify_stderr(self, line: str) -> StderrCategory:
        if any(noise in line for noise in _STDERR_NOISE):
            return "skip"
        if line.lstrip().startswith(_STDERR_INFO_PREFIXES):
            return "info"
        return "warning"

    def before_run(self, *, cwd: str | None, mode: Mode) -> None:
        # Only the fix flow needs the writable-tool permission grants in the
        # worktree; the read-only review/analyze/generate flows don't touch
        # files at all.
        if mode == "fix" and cwd:
            self._write_opencode_config(cwd)

    async def _invoke_stream(
        self,
        message: str,
        *,
        mode: Mode,
        context: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        timeout: int = DEFAULT_REVIEW_TIMEOUT,
    ) -> AsyncGenerator[str, None]:
        # Route through the module-level helper so existing tests can patch
        # `_stream_opencode` to inject a fake stream.
        async for chunk in _stream_opencode(
            message,
            context=context,
            cwd=cwd,
            model=model,
            timeout=timeout,
            mode=mode,
        ):
            yield chunk

    @staticmethod
    def _write_opencode_config(repo_dir: str) -> None:
        """Drop opencode permission grants into the worktree.

        Without this the ``opencode run`` invocation prompts the user for
        every Bash/Edit call, which deadlocks the fix flow.
        """
        settings_dir = os.path.join(repo_dir, ".opencode")
        os.makedirs(settings_dir, exist_ok=True)
        with open(os.path.join(settings_dir, "settings.json"), "w") as f:
            json.dump(_OPENCODE_PERMISSIONS, f)

        config_path = os.path.join(repo_dir, "opencode.json")
        with open(config_path, "w") as f:
            json.dump(_OPENCODE_PROJECT_CONFIG, f, indent=2)
        logger.info("Wrote opencode.json at %s", config_path)


# Singleton shared by the module-level helper so we don't pay for object
# construction on every invocation.
_SHARED_ADAPTER: OpenCodeAdapter | None = None


def _shared_adapter() -> OpenCodeAdapter:
    global _SHARED_ADAPTER
    if _SHARED_ADAPTER is None:
        _SHARED_ADAPTER = OpenCodeAdapter()
    return _SHARED_ADAPTER


async def _stream_opencode(
    message: str,
    context: str | None = None,
    timeout: int = DEFAULT_REVIEW_TIMEOUT,
    cwd: str | None = None,
    model: str | None = None,
    *,
    mode: Mode = "generate",
) -> AsyncGenerator[str, None]:
    """Stream raw opencode stdout and ``\\x00STDERR\\x00``-tagged stderr.

    Kept as a module-level helper because tests monkeypatch this name to feed
    a fake stream into the adapter without spawning a real subprocess.
    """
    async for chunk in _shared_adapter().stream_cli(
        message, context=context, cwd=cwd, model=model, timeout=timeout, mode=mode,
    ):
        yield chunk


__all__ = [
    "DEFAULT_FIX_TIMEOUT",
    "DEFAULT_GENERATE_TIMEOUT",
    "DEFAULT_REVIEW_TIMEOUT",
    "OpenCodeAdapter",
    "_stream_opencode",
]
