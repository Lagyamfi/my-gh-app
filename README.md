# gh-review-tool

[![CI](https://github.com/Angelaben/my-gh-app/actions/workflows/ci.yml/badge.svg)](https://github.com/Angelaben/my-gh-app/actions/workflows/ci.yml)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-angelaben-FFDD00?style=flat&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/angelaben)
![Python](https://img.shields.io/badge/python-3.12+-blue)
![Svelte](https://img.shields.io/badge/frontend-Svelte%205-orange)
![License](https://img.shields.io/badge/license-MIT-green)

A local web dashboard for reviewing GitHub PRs with AI — pluggable backend, supports [opencode](https://opencode.ai) and [Claude Code](https://docs.claude.com/claude-code) (the latter is currently disabled by default — see [below](#claude-code-is-disabled-by-default)).

Add repos, browse open PRs, get AI code reviews streamed in real-time, post comments, and auto-implement fixes directly on the PR branch.

> ⚠️ **Heads up — `claude-code` provider is gated behind a feature flag.**
> The Claude Code adapter shipped recently and is not yet stable enough for
> day-to-day use (model discovery quirks on Bedrock deployments, occasional
> 4xx/5xx mishandling on the reviews endpoint). It is **disabled by default**.
> Set `ENABLE_CLAUDE_CODE=1` to opt in. Full rationale in
> [Claude Code is disabled by default](#claude-code-is-disabled-by-default).

### Highlights

- **Streaming reviews** — Server-Sent Events render the AI's analysis as it's produced
- **Two AI backends** — switch between `opencode` and `claude-code` via one env variable
- **Inline & PR comments** — publish findings as inline review comments or top-level PR comments
- **AI fix flow** — implement reviewer feedback in an isolated git worktree, review the diff, then push to the PR branch or open a follow-up PR
- **Comment intelligence** — analyze existing PR comments by criticality / validity / interest, detect new replies since your last visit
- **Hexagonal architecture** — swap any external dep (AI, VCS, cache) by implementing one interface
- **No secrets stored** — auth is delegated to the `gh` CLI keyring; tokens are stripped from subprocess envs

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Claude Code is disabled by default](#claude-code-is-disabled-by-default)
- [Adapting for Your Needs](#adapting-for-your-needs)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [Security](#security)
- [Support](#support)

---

## Prerequisites

- **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/)
- **Node.js 18+** and `npm`
- **[gh CLI](https://cli.github.com/)** — authenticated (`gh auth login`)
- **An AI CLI provider** — at least one of:
  - **[opencode CLI](https://opencode.ai)** — default provider
  - **[Claude Code CLI](https://docs.claude.com/claude-code)** — currently
    disabled by default; opt in with `ENABLE_CLAUDE_CODE=1` (see
    [Claude Code is disabled by default](#claude-code-is-disabled-by-default))

---

## Installation

```bash
# Choose either SSH or HTTPS:
git clone git@github.com:Angelaben/my-gh-app.git
# git clone https://github.com/Angelaben/my-gh-app.git
cd my-gh-app

# Install Python dependencies
uv sync

# Build the frontend
cd frontend
npm install
npm run build
cd ..

# Start the server (default provider: opencode)
uv run uvicorn app.main:app --reload

# …or start with Claude Code as the provider (requires opting in — see below)
ENABLE_CLAUDE_CODE=1 AI_PROVIDER=claude-code uv run uvicorn app.main:app --reload
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

### One-shot dev launcher (optional)

If you have `tmux` installed, `./launch.sh` boots backend + frontend dev server
in a single split window:

```bash
./launch.sh                          # default provider (opencode)
./launch.sh --provider claude-code   # use Claude Code
./launch.sh --help                   # full usage
```

### Switching AI providers

The provider is selected at server startup via the `AI_PROVIDER` environment
variable, OR live from the UI's provider picker (no restart needed):

| Value | CLI required in PATH | Default? |
|------|----------------------|----------|
| `opencode` | `opencode` | ✅ default |
| `claude-code` | `claude` | ⚠️ disabled — set `ENABLE_CLAUDE_CODE=1` to opt in |

Both providers implement the same `AIProvider` port (see
`app/ports/ai_provider.py`) and are interchangeable for review, comment
analysis, and fix flows.

---

## Usage

1. **Add a repo** — click "Add Repo" in the sidebar, search by org/name, and add it
2. **Browse PRs** — select a repo to see its open pull requests
3. **Run a review** — open a PR and click "Run Review" to get a streaming AI analysis
4. **Review tab** — see findings by severity, post them as inline or PR-level comments on GitHub
5. **Comments tab** — analyze existing PR comments, and use "Implement Fix" to let the AI provider apply the fix in a local worktree
6. **Push fixes** — review the generated diff and push directly to the PR branch, or open a new PR

---

## Configuration

No environment variables are required. Authentication is handled entirely by
the `gh` CLI and the AI provider's own configuration (`opencode auth` or
`claude` login).

Optional environment variables:

| Variable              | Default     | Purpose                                                |
|-----------------------|-------------|--------------------------------------------------------|
| `AI_PROVIDER`         | `opencode`  | Selects the AI backend. Supported: `opencode`, `claude-code` *(when enabled)* |
| `ENABLE_CLAUDE_CODE`  | unset       | Set to `1` (or `true` / `yes` / `on`) to enable the `claude-code` provider — see [Claude Code is disabled by default](#claude-code-is-disabled-by-default) |

Data is stored in:
- `.cache/` — PR lists, review results, visit timestamps (relative to the project directory)
- `~/.gh-review-tool/` — bare git clones and worktrees used for fix implementation

See [`.env.example`](.env.example) for details.

---

## Claude Code is disabled by default

The `claude-code` provider ships in this repo but is **gated behind the
`ENABLE_CLAUDE_CODE` feature flag** and excluded from the provider picker
when the flag is unset. The opencode provider remains available without any
flag.

### Why is it off?

The Claude Code adapter shipped recently and still has rough edges that we
want to fix before recommending it for everyday use:

- **Model discovery is approximate.** Claude Code does not expose a
  `models` listing command, so the adapter only returns the three universal
  aliases (`opus`, `sonnet`, `haiku`). On AWS Bedrock deployments the alias
  layer maps to the correct backend-specific ID automatically — but if you
  want to pin a specific version you currently have to type it manually
  (e.g. `eu.anthropic.claude-sonnet-4-5-20250929-v1:0` for Bedrock EU).
- **Bedrock inference profile errors are not always actionable.** When the
  CLI reports `"may not exist or you may not have access"` for a model that
  IS available on your account, it usually means the model ID format is
  Anthropic-direct (`claude-sonnet-4-6`) rather than the Bedrock profile
  format your account expects. The error surfaces but the fix is manual.
- **Headless invocation requires `--bare`.** Without it the CLI's keychain
  read fails inside subprocess invocations and falls back to a strict
  model-validation path. The adapter passes `--bare` for read-only flows
  (review, analyze, generate), but this is the kind of detail that's still
  fragile across CLI versions.
- **Reviews-API publishing has multiple fallbacks.** Posting a
  `REQUEST_CHANGES` review can fail with `422` when an inline comment line
  is outside the PR diff, or when the reviewer is the PR author. The
  adapter handles those cases by folding comments into the body or
  degrading to a regular PR comment, but the path is non-trivial.

None of these are showstoppers for adventurous users — but they're enough
that we don't want a freshly-cloned install pointing at claude-code by
default.

### How do I turn it back on?

Restart the server with the flag set. Any of these values work
(case-insensitive): `1`, `true`, `yes`, `on`.

```bash
# Pick the provider live from the UI after enabling the flag
ENABLE_CLAUDE_CODE=1 uv run uvicorn app.main:app --reload

# …or boot directly into claude-code as the active provider
ENABLE_CLAUDE_CODE=1 AI_PROVIDER=claude-code uv run uvicorn app.main:app --reload

# launch.sh helper inherits the env var
ENABLE_CLAUDE_CODE=1 ./launch.sh --provider claude-code
```

When the flag is off and you try to switch to claude-code from the UI or
via `POST /api/provider`, the server returns a `400` with a message
explaining how to enable it. Setting `AI_PROVIDER=claude-code` while the
flag is off logs a warning and falls back to auto-detect — the server
still boots.

### When will it become the default again?

When the issues above are fixed. Track progress in
[CHANGELOG.md](CHANGELOG.md) under "Unreleased". If you hit a specific
problem with the provider, please [open an
issue](https://github.com/Angelaben/my-gh-app/issues) so we can prioritize.

---

## Adapting for Your Needs

The project uses a **hexagonal (ports & adapters)** architecture, making it straightforward to swap out any external dependency.

### Use a different AI provider

Implement the `AIProvider` interface (`app/ports/ai_provider.py`) and wire it in `app/main.py`:

```python
from app.ports.ai_provider import AIProvider

class MyClaudioAdapter(AIProvider):
    async def stream_review(self, repo_full_name, pr_number, diff, model=None):
        ...  # call your AI API and yield ReviewChunkEvent / ReviewResultEvent
    async def analyze_comments(self, repo_full_name, pr_number, comments):
        ...
    async def stream_fix(self, repo_dir, repo_full_name, pr_number, comment_body):
        ...
    async def generate_text(self, prompt, timeout=60):
        ...
```

### Use a different VCS provider

Implement `VCSPort` (`app/ports/vcs_port.py`) to work with GitLab, Gitea, or any platform that exposes PR/diff APIs.

### Use a different cache backend

Implement `CachePort` (`app/ports/cache_port.py`) to store data in a database, Redis, or any other backend instead of local JSON files.

See [CONTRIBUTING.md](CONTRIBUTING.md) for a full architecture overview.

---

## Updating

```bash
git pull

uv sync

cd frontend
npm install
npm run build
cd ..

# Restart the server
uv run uvicorn app.main:app --reload
```

---

## Troubleshooting

**`opencode: command not found` / `claude: command not found`**
Ensure the AI provider CLI for your active `AI_PROVIDER` is installed and in
your `PATH`. Run `which opencode` or `which claude` to verify.

**`gh: command not found` or authentication errors**
Install the GitHub CLI and authenticate: `gh auth login`

**Streaming review hangs or produces no output**
The AI provider may be waiting for credentials. For `opencode`, check its own
auth configuration. For `claude-code`, run `claude` once interactively to
complete login.

**Port already in use**
Change the port: `uv run uvicorn app.main:app --port 8001`

**Worktree errors or stale fix sessions**
Worktrees can be deleted via the UI (worktree manager icon), or manually:
```bash
rm -rf ~/.gh-review-tool/worktrees/<name>
```

**Corrupt or stale cache**
Clear the cache directory:
```bash
rm -rf .cache/
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, architecture details, and guidelines for submitting changes.

---

## Security

This is a **local-only tool** — it is not designed to be exposed on a network. See [SECURITY.md](SECURITY.md) for the full security model and vulnerability reporting instructions.

---

## Support

If this tool saves you time, consider buying me a coffee ☕

[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/yellow_img.png)](https://buymeacoffee.com/angelaben)
