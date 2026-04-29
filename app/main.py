"""FastAPI backend — thin controllers wired to services via dependency injection."""

import json
import logging
import os
import shutil
from collections.abc import AsyncGenerator
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.adapters.ai.claude_code_adapter import ClaudeCodeAdapter
from app.adapters.ai.opencode_adapter import OpenCodeAdapter
from app.adapters.cache.json_file_cache import JsonFileCache
from app.adapters.vcs.github_cli_adapter import GitHubCLIAdapter
from app.adapters.worktree.git_worktree_adapter import GitWorktreeAdapter
from app.domain.exceptions import WorktreeNoChangesError, WorktreeNotFoundError
from app.domain.models import Comment
from app.ports.ai_provider import (
    AIProvider,
    FixChunkEvent,
    ReviewChunkEvent,
    ReviewResultEvent,
    ReviewStreamEvent,
    ReviewWarningEvent,
)
from app.services.comment_service import CommentService
from app.services.fix_service import FixService
from app.services.review_service import ReviewService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="gh-review-tool")

# --- Provider registry & availability ---

# CLI binary expected on PATH for each provider, in display order.
_PROVIDER_CLIS: dict[str, str] = {
    "opencode": "opencode",
    "claude-code": "claude",
}
_SUPPORTED_PROVIDERS: tuple[str, ...] = tuple(_PROVIDER_CLIS.keys())


def _provider_available(name: str) -> bool:
    """True if the provider's CLI binary is on PATH."""
    cli = _PROVIDER_CLIS.get(name)
    return cli is not None and shutil.which(cli) is not None


def _build_ai_provider(name: str) -> AIProvider:
    """Instantiate the AI provider adapter selected by name."""
    if name == "claude-code":
        return ClaudeCodeAdapter()
    if name == "opencode":
        return OpenCodeAdapter()
    raise ValueError(
        f"Unknown AI_PROVIDER {name!r}. Supported: {', '.join(_SUPPORTED_PROVIDERS)}"
    )


class SwitchableAIProvider(AIProvider):
    """Delegates to one of several AI provider adapters, chosen at runtime.

    The active provider can be changed via :meth:`set_active` without
    rebuilding the services that hold a reference to this wrapper.
    """

    def __init__(self, providers: dict[str, AIProvider], active: str) -> None:
        if active not in providers:
            raise ValueError(f"Unknown active provider {active!r}")
        self._providers = providers
        self._active = active

    @property
    def active(self) -> str:
        return self._active

    def set_active(self, name: str) -> None:
        if name not in self._providers:
            raise ValueError(
                f"Unknown provider {name!r}. Supported: {', '.join(self._providers)}"
            )
        self._active = name

    def _delegate(self) -> AIProvider:
        return self._providers[self._active]

    async def stream_review(
        self, repo_full_name: str, pr_number: int, diff: str, model: str | None = None
    ) -> AsyncGenerator[ReviewStreamEvent, None]:
        async for event in self._delegate().stream_review(
            repo_full_name, pr_number, diff, model=model
        ):
            yield event

    async def analyze_comments(
        self, repo_full_name: str, pr_number: int, comments: list[Comment]
    ) -> list[dict]:
        return await self._delegate().analyze_comments(repo_full_name, pr_number, comments)

    async def stream_fix(
        self, repo_dir: str, repo_full_name: str, pr_number: int, comment_body: str
    ) -> AsyncGenerator[FixChunkEvent, None]:
        async for chunk in self._delegate().stream_fix(
            repo_dir, repo_full_name, pr_number, comment_body
        ):
            yield chunk

    async def generate_text(self, prompt: str, timeout: int = 60) -> str:
        return await self._delegate().generate_text(prompt, timeout=timeout)


def _resolve_initial_provider() -> tuple[str, bool]:
    """Pick the initial active provider.

    Returns ``(name, from_env)``. Resolution order:

    1. ``AI_PROVIDER`` env var if set and supported (warn if its CLI is missing).
    2. First provider whose CLI is on PATH.
    3. ``opencode`` as last-resort fallback (so the app still boots and the
       user can pick a provider from the UI).
    """
    raw = os.environ.get("AI_PROVIDER", "").strip().lower()
    if raw:
        if raw not in _SUPPORTED_PROVIDERS:
            logger.warning(
                "AI_PROVIDER=%r is not supported (expected one of %s); falling back to auto-detect.",
                raw, ", ".join(_SUPPORTED_PROVIDERS),
            )
        else:
            if not _provider_available(raw):
                logger.warning(
                    "AI_PROVIDER=%r is set, but the %r CLI is not on PATH. "
                    "Reviews will fail until you install it or switch providers via the UI.",
                    raw, _PROVIDER_CLIS[raw],
                )
            return raw, True

    for name in _SUPPORTED_PROVIDERS:
        if _provider_available(name):
            return name, False

    logger.warning(
        "No supported AI provider CLI found on PATH (looked for %s). "
        "The server will boot but reviews will fail until a CLI is installed.",
        ", ".join(_PROVIDER_CLIS.values()),
    )
    return _SUPPORTED_PROVIDERS[0], False


_initial_provider, _provider_from_env = _resolve_initial_provider()

_cache = JsonFileCache()
_vcs = GitHubCLIAdapter()
_worktree = GitWorktreeAdapter()
_ai = SwitchableAIProvider(
    providers={name: _build_ai_provider(name) for name in _SUPPORTED_PROVIDERS},
    active=_initial_provider,
)
logger.info(
    "AI provider | active=%s | from_env=%s | available=%s",
    _ai.active, _provider_from_env,
    {name: _provider_available(name) for name in _SUPPORTED_PROVIDERS},
)

_review_service = ReviewService(ai=_ai, cache=_cache, vcs=_vcs)
_fix_service = FixService(ai=_ai, vcs=_vcs, worktree=_worktree)
_comment_service = CommentService(ai=_ai, vcs=_vcs)


def get_review_service() -> ReviewService:
    return _review_service


def get_fix_service() -> FixService:
    return _fix_service


def get_comment_service() -> CommentService:
    return _comment_service


# --- Request / Response models ---

class RepoAdd(BaseModel):
    owner: str
    name: str


class RepoRemove(BaseModel):
    full_name: str


class PublishComment(BaseModel):
    repo: str
    pr_number: int
    body: str


class DeleteComment(BaseModel):
    repo: str
    comment_id: int
    comment_type: str  # 'pr_comment' | 'review_comment'


class PublishInlineComment(BaseModel):
    repo: str
    pr_number: int
    body: str
    path: str
    line: int


class ImplementFix(BaseModel):
    repo: str
    pr_number: int
    comment_body: str
    thread: list[dict] | None = None  # [{"author": str, "body": str}, ...]


class PushFix(BaseModel):
    repo: str
    pr_number: int
    diff: str
    comment_body: str
    branch: str


# --- Repo endpoints ---

@app.get("/api/repos")
def list_repos():
    return _cache.get_repos()


@app.post("/api/repos")
def add_repo(data: RepoAdd):
    return _cache.add_repo(data.owner, data.name)


@app.delete("/api/repos")
def remove_repo(data: RepoRemove):
    return _cache.remove_repo(data.full_name)


@app.get("/api/repos/search")
def search_repos(org: str, q: str = ""):
    try:
        return _vcs.search_repos(org, q)
    except Exception as e:
        logger.exception("search_repos failed | org=%s q=%s", org, q)
        raise HTTPException(status_code=500, detail=str(e))


# --- Config & provider endpoints ---


def _provider_status() -> dict:
    """Snapshot of provider availability + active selection."""
    return {
        "active": _ai.active,
        "from_env": _provider_from_env,
        "supported": list(_SUPPORTED_PROVIDERS),
        "available": {name: _provider_available(name) for name in _SUPPORTED_PROVIDERS},
        "clis": dict(_PROVIDER_CLIS),
    }


@app.get("/api/config")
def get_config():
    """Return static runtime configuration consumed by the frontend.

    Includes ``ai_provider`` for backwards compatibility with older frontend
    builds; new frontends should prefer ``GET /api/providers``.
    """
    status = _provider_status()
    return {
        "ai_provider": status["active"],
        "supported_providers": status["supported"],
        "providers": status,
    }


@app.get("/api/providers")
def get_providers():
    """Return active provider, env-var origin, and per-provider availability."""
    return _provider_status()


class ProviderSelect(BaseModel):
    name: str


@app.post("/api/provider")
def set_provider(data: ProviderSelect):
    """Switch the active AI provider at runtime.

    Returns 400 if ``name`` isn't a supported provider, and warns (via the
    response body) if the provider's CLI is not on PATH — the call still
    succeeds so the user can install the CLI without restarting.
    """
    name = data.name.strip().lower()
    if name not in _SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider {name!r}. Supported: {', '.join(_SUPPORTED_PROVIDERS)}",
        )
    _ai.set_active(name)
    available = _provider_available(name)
    if not available:
        logger.warning(
            "Switched active provider to %r but %r CLI is missing on PATH.",
            name, _PROVIDER_CLIS[name],
        )
    else:
        logger.info("AI provider switched | active=%s", name)
    return {**_provider_status(), "warning": None if available else (
        f"Provider {name!r} selected, but its CLI ({_PROVIDER_CLIS[name]!r}) is not on PATH."
    )}


@app.get("/api/models")
def list_models():
    """Return all models available in the active AI provider's installation."""
    import subprocess
    active = _ai.active
    if active == "opencode":
        cmd = ["opencode", "models"]
    elif active == "claude-code":
        # Claude Code does not expose a `models` listing command, and pinned
        # version names (e.g. 'claude-opus-4-7') are rejected by older CLI
        # installs or accounts without access to that exact version. Aliases
        # always resolve to the latest model the user's CLI supports.
        return {"models": ["opus", "sonnet", "haiku"]}
    else:
        raise HTTPException(status_code=500, detail=f"Unknown provider {active!r}")

    if not _provider_available(active):
        # Don't raise — return an empty list so the UI can show the "default"
        # option and surface its own missing-CLI message.
        return {"models": [], "warning": f"{_PROVIDER_CLIS[active]!r} CLI not found on PATH."}

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
        )
        models = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return {"models": models}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- PR endpoints ---

@app.get("/api/prs/{owner}/{repo}")
def list_prs(owner: str, repo: str):
    return _cache.get_prs(f"{owner}/{repo}")


@app.post("/api/prs/{owner}/{repo}/refresh")
def refresh_prs(owner: str, repo: str):
    full_name = f"{owner}/{repo}"
    logger.info("refresh_prs | start | repo=%s", full_name)
    try:
        prs = _vcs.list_prs(full_name)
        prs_dicts = [
            {
                "number": pr.number,
                "title": pr.title,
                "author": pr.author,
                "branch": pr.branch,
                "base_branch": pr.base_branch,
                "additions": pr.additions,
                "deletions": pr.deletions,
                "updated_at": pr.updated_at,
                "url": pr.url,
            }
            for pr in prs
        ]
        _cache.save_prs(full_name, prs_dicts)
        logger.info("refresh_prs | done | repo=%s prs=%d", full_name, len(prs_dicts))
        return prs_dicts
    except Exception as e:
        logger.exception("refresh_prs | failed | repo=%s", full_name)
        raise HTTPException(status_code=500, detail=str(e))


# --- PR detail ---

@app.get("/api/pr/{owner}/{repo}/{pr_number}")
def get_pr_detail(owner: str, repo: str, pr_number: int):
    try:
        repo_full_name = f"{owner}/{repo}"

        our_login = _cache.get_github_login()
        if our_login is None:
            our_login = _vcs.get_authenticated_user()
            _cache.set_github_login(our_login)

        last_visited_at = _cache.get_last_visited(repo_full_name, pr_number)

        enriched, new_comment_ids = _comment_service.get_enriched_comments(
            repo_full_name, pr_number, our_login, last_visited_at
        )

        _cache.set_last_visited(repo_full_name, pr_number, datetime.now(timezone.utc))

        return {
            "comments": [asdict(c) for c in enriched],
            "new_comment_ids": new_comment_ids,
        }
    except Exception as e:
        logger.exception("get_pr_detail | failed | repo=%s/%s pr=#%d", owner, repo, pr_number)
        raise HTTPException(status_code=500, detail=str(e))


def _serialize_review(review) -> dict:
    """Serialize a Review domain object to a JSON-compatible dict."""
    result: dict = {
        "summary": review.summary,
        "findings": [
            {
                "priority": f.priority,
                "title": f.title,
                "description": f.description,
                "file": f.file,
                "line": f.line,
                "suggestion": f.suggestion,
            }
            for f in review.findings
        ],
    }
    if review.raw_output:
        result["raw_output"] = review.raw_output
    if review.raw_length:
        result["raw_length"] = review.raw_length
    return result


# --- Review endpoints ---

@app.post("/api/review/{owner}/{repo}/{pr_number}")
async def run_review(
    owner: str,
    repo: str,
    pr_number: int,
    svc: ReviewService = Depends(get_review_service),
):
    try:
        review = await svc.get_or_run_review(f"{owner}/{repo}", pr_number)
        return _serialize_review(review)
    except Exception as e:
        logger.exception("run_review | failed | repo=%s/%s pr=#%d", owner, repo, pr_number)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/review/{owner}/{repo}/{pr_number}/rerun")
async def rerun_review(
    owner: str,
    repo: str,
    pr_number: int,
    svc: ReviewService = Depends(get_review_service),
):
    try:
        review = await svc.rerun_review(f"{owner}/{repo}", pr_number)
        return _serialize_review(review)
    except Exception as e:
        logger.exception("rerun_review | failed | repo=%s/%s pr=#%d", owner, repo, pr_number)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/review/{owner}/{repo}/{pr_number}/stream")
async def stream_review(
    owner: str,
    repo: str,
    pr_number: int,
    model: str | None = Query(default=None, description="AI provider model override (e.g. anthropic/claude-opus-4-5 for opencode, claude-opus-4-7 for claude-code)"),
    svc: ReviewService = Depends(get_review_service),
):
    """SSE endpoint that streams AI output in real-time, then emits the parsed result."""
    async def event_stream():
        try:
            async for event in svc.stream_review(f"{owner}/{repo}", pr_number, model=model):
                if isinstance(event, ReviewChunkEvent):
                    for line in event.text.splitlines(keepends=True):
                        yield f"data: {json.dumps({'type': 'chunk', 'text': line})}\n\n"
                elif isinstance(event, ReviewWarningEvent):
                    yield f"data: {json.dumps({'type': 'warning', 'lines': event.lines})}\n\n"
                elif isinstance(event, ReviewResultEvent):
                    yield f"data: {json.dumps({'type': 'result', 'review': _serialize_review(event.review)})}\n\n"
            yield 'data: {"type": "done"}\n\n'
        except Exception as e:
            logger.exception("stream_review | failed | repo=%s/%s pr=#%d", owner, repo, pr_number)
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
            yield 'data: {"type": "done"}\n\n'

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# --- Comment endpoints ---

@app.post("/api/comment/publish")
def publish_comment(data: PublishComment, svc: CommentService = Depends(get_comment_service)):
    try:
        svc.post_comment(data.repo, data.pr_number, data.body)
        return {"status": "published"}
    except Exception as e:
        logger.exception("publish_comment | failed | repo=%s pr=#%d", data.repo, data.pr_number)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/comment/inline")
def publish_inline_comment(
    data: PublishInlineComment, svc: CommentService = Depends(get_comment_service)
):
    try:
        svc.post_inline_comment(data.repo, data.pr_number, data.body, data.path, data.line)
        return {"status": "published"}
    except Exception as e:
        logger.exception("publish_inline_comment | failed | repo=%s pr=#%d", data.repo, data.pr_number)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/comment")
def delete_comment(data: DeleteComment):
    try:
        _vcs.delete_comment(data.repo, data.comment_id, data.comment_type)
        return {"status": "deleted"}
    except Exception as e:
        logger.exception("delete_comment | failed | repo=%s id=%d", data.repo, data.comment_id)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/comments/{owner}/{repo}/{pr_number}/analyze")
async def analyze_comments(
    owner: str,
    repo: str,
    pr_number: int,
    svc: CommentService = Depends(get_comment_service),
):
    try:
        return await svc.analyze_comments(f"{owner}/{repo}", pr_number)
    except Exception as e:
        logger.exception("analyze_comments | failed | repo=%s/%s pr=#%d", owner, repo, pr_number)
        raise HTTPException(status_code=500, detail=str(e))


# --- Fix endpoints ---

@app.post("/api/comment/fix")
async def implement_fix(
    data: ImplementFix, svc: FixService = Depends(get_fix_service)
):
    async def event_stream():
        async for event in svc.stream_fix(data.repo, data.pr_number, data.comment_body):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/comment/fix/push")
async def push_fix(data: PushFix, svc: FixService = Depends(get_fix_service)):
    try:
        return await svc.push_fix(data.repo, data.pr_number, data.diff, data.comment_body)
    except WorktreeNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except WorktreeNoChangesError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("push_fix | failed | repo=%s pr=#%d", data.repo, data.pr_number)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/comment/fix/new-pr")
async def submit_new_pr(data: PushFix, svc: FixService = Depends(get_fix_service)):
    try:
        return await svc.create_pr_from_fix(
            data.repo, data.pr_number, data.branch, data.diff, data.comment_body
        )
    except WorktreeNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except WorktreeNoChangesError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("submit_new_pr | failed | repo=%s pr=#%d", data.repo, data.pr_number)
        raise HTTPException(status_code=500, detail=str(e))


# --- Worktree management ---

@app.get("/api/worktrees")
def list_worktrees():
    return _worktree.list_worktrees()


@app.delete("/api/worktree/{owner}/{repo}/{pr_number}")
def remove_worktree(owner: str, repo: str, pr_number: int):
    try:
        _worktree.remove(f"{owner}/{repo}", pr_number)
        return {"status": "removed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/review/{owner}/{repo}/{pr_number}/cache")
def clear_review_cache(owner: str, repo: str, pr_number: int):
    _review_service.clear_cache(f"{owner}/{repo}", pr_number)
    return {"status": "cleared"}


# --- Static files ---

Path("dist").mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory="dist", html=True), name="static")
