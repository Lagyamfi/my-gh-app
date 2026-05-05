"""Shared pytest fixtures and import-time setup.

The ``ENABLE_CLAUDE_CODE`` env var must be set BEFORE ``app.main`` is imported
because ``main`` reads it at module load time to decide whether to register the
claude-code provider. The vast majority of tests assume the provider is
registered (they exercise the multi-provider switching, model listing, etc.),
so we enable it globally here. The ``test_claude_code_flag.py`` suite reloads
``app.main`` with the flag off to verify the disabled-by-default behavior.
"""
import os

os.environ.setdefault("ENABLE_CLAUDE_CODE", "1")
