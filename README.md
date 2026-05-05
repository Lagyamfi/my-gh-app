# gh-review-tool — Local AI Code Review for GitHub Pull Requests

[![CI](https://github.com/Angelaben/my-gh-app/actions/workflows/ci.yml/badge.svg)](https://github.com/Angelaben/my-gh-app/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue?logo=python&logoColor=white)
![Svelte 5](https://img.shields.io/badge/frontend-Svelte%205-orange?logo=svelte&logoColor=white)
![Self-Hosted](https://img.shields.io/badge/self--hosted-✓-success)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-angelaben-FFDD00?style=flat&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/angelaben)

> **Stream AI code reviews on any GitHub pull request, post inline comments, request changes, and auto-implement reviewer feedback — all from a private local dashboard.** No SaaS, no telemetry, no data leaves your machine. Bring your own `opencode` or `Claude Code` install and review PRs the way you already do on github.com — just with AI doing the first pass.

**`gh-review-tool` is a self-hosted, open-source AI code reviewer for GitHub.** It wires the `gh` CLI to the AI provider of your choice and gives you a desktop-class dashboard for browsing pull requests, getting streaming AI analysis with P0–P3 severity ratings, publishing inline review comments or full *Request Changes* reviews, and implementing fixes directly on the PR branch — without ever uploading a diff to a third-party SaaS.

> ⚠️ **Heads up — `claude-code` provider is gated behind a feature flag.**
> The Claude Code adapter shipped recently and is not yet stable enough for
> day-to-day use (model discovery quirks on Bedrock deployments, occasional
> 4xx/5xx mishandling on the reviews endpoint). It is **disabled by default**.
> Set `ENABLE_CLAUDE_CODE=1` to opt in. Full rationale in
> [Claude Code is disabled by default](#claude-code-is-disabled-by-default).

### ✨ Highlights

- ⚡ **Streaming AI code reviews** — Server-Sent Events render the analysis as the model produces it; no waiting for a giant blob to land at the end.
- 🎯 **Severity-classified findings** — every issue is tagged P0 (critical) → P3 (suggestion) so you can triage at a glance.
- 💬 **Inline & PR-level comments** — publish each finding as a GitHub inline review comment on the exact `path:line`, or as a top-level PR comment. P1 findings can be promoted to a single-finding *Request Changes* review.
- 📦 **Batched Request Changes reviews** — stage multiple findings, then ship them as one GitHub review with state `REQUEST_CHANGES` and inline comments per finding.
- 🔁 **One-click AI fix flow** — let the AI apply the suggested change in an isolated git worktree, review the diff, then push to the PR branch or open a follow-up PR.
- 🧠 **Comment intelligence** — re-classify existing PR comments by criticality, validity and interest, and detect new replies since your last visit.
- 🔌 **Hexagonal architecture (ports & adapters)** — swap any external dependency (AI provider, VCS host, cache backend) by implementing one Python interface.
- 🔒 **Private by design** — runs on `localhost`, auth is delegated to the `gh` CLI keyring, and `GITHUB_TOKEN` / `GH_TOKEN` are stripped from every subprocess env to prevent token leakage.
- 💸 **No subscription, no rate limits beyond your provider's** — bring your own AI CLI; pay only what your `opencode` / `Claude Code` plan costs.

---

## Table of Contents

- [Who is this for?](#who-is-this-for)
- [How it compares to hosted AI review tools](#how-it-compares-to-hosted-ai-review-tools)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Claude Code is disabled by default](#claude-code-is-disabled-by-default)
- [Adapting for Your Needs](#adapting-for-your-needs)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Contributing](#contributing)
- [Security](#security)
- [Support](#support)

---

## Who is this for?

`gh-review-tool` is built for developers and small teams who want **AI-assisted GitHub code review without sending diffs to a third-party SaaS**. Typical users:

- **Solo maintainers** triaging community PRs faster.
- **Engineers in regulated environments** (finance, health, defense) where source code can't leave the laptop or VPC.
- **Teams already paying for `opencode` or `Claude Code`** who want to amortize that subscription across PR reviews.
- **Hexagonal-architecture enthusiasts** who want to plug in their own AI / VCS / cache backends.

If you want a hosted SaaS that auto-reviews every PR — this is not that. See [How it compares](#how-it-compares-to-hosted-ai-review-tools) below.

---

## How it compares to hosted AI review tools

| | gh-review-tool | Hosted AI review SaaS |
|---|---|---|
| Where reviews run | **Your machine** | Vendor's cloud |
| Diff upload required | ❌ Never | ✅ Always |
| Subscription | Pay your AI CLI provider only | Per-seat / per-repo monthly fee |
| AI model choice | Any model your CLI supports (`opus`, `sonnet`, `haiku`, custom Bedrock IDs, …) | Vendor-curated short list |
| Triggered by | You, on demand | Webhook on every PR |
| Review style | Streaming, severity-tagged, with inline + Request-Changes flows | Bulk comment per PR |
| Source code access | Local-only via `gh` CLI keyring | OAuth GitHub App with broad repo scopes |
| Self-hostable | ✅ It already is | ❌ Closed source |
| Telemetry | None | Often enabled by default |

**TL;DR:** if you want zero-config "auto-review every PR in our org" — pick a SaaS. If you want **opt-in, private, AI-powered code review on your terms**, you want this tool.

---

## Prerequisites

- **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/) (fast Python package manager)
- **Node.js 18+** and `npm` (for the Svelte 5 frontend build)
- **[GitHub CLI (`gh`)](https://cli.github.com/)** — authenticated via `gh auth login`
- **An AI CLI provider** — at least one of:
  - **[opencode CLI](https://opencode.ai)** — default provider, supports OpenRouter / Anthropic / OpenAI / local models
  - **[Claude Code CLI](https://docs.claude.com/claude-code)** — currently
    disabled by default; opt in with `ENABLE_CLAUDE_CODE=1` (see
    [Claude Code is disabled by default](#claude-code-is-disabled-by-default))

> **Heads up:** authentication is handled entirely by the `gh` CLI keyring and the AI provider's own login flow. The tool itself never reads or stores tokens.

---

## Installation

```bash
# Choose either SSH or HTTPS:
git clone git@github.com:Angelaben/my-gh-app.git
# git clone https://github.com/Angelaben/my-gh-app.git
cd my-gh-app

# Install Python dependencies
uv sync

# Build the Svelte frontend
cd frontend
npm install
npm run build
cd ..

# Start the server (default provider: opencode)
uv run uvicorn app.main:app --reload

# …or start with Claude Code as the active AI provider (requires opting in — see below)
ENABLE_CLAUDE_CODE=1 AI_PROVIDER=claude-code uv run uvicorn app.main:app --reload
```

Open **<http://localhost:8000>** in your browser. That's it — no config files, no signup, no API keys to paste.

### One-shot dev launcher (optional)

If you have `tmux` installed, `./launch.sh` boots backend + frontend dev server in a single split window:

```bash
./launch.sh                                              # default provider (opencode)
ENABLE_CLAUDE_CODE=1 ./launch.sh --provider claude-code  # use Claude Code (opt in)
./launch.sh --help                                       # full usage
```

### Switching AI providers at runtime

Pick the provider from the in-app picker, **or** set it at server startup via the `AI_PROVIDER` env var. No restart required when switching from the UI:

| Value | CLI required on `PATH` | Default? |
|------|----------------------|----------|
| `opencode` | `opencode` | ✅ default |
| `claude-code` | `claude` | ⚠️ disabled — set `ENABLE_CLAUDE_CODE=1` to opt in |

Both providers implement the same `AIProvider` port (see `app/ports/ai_provider.py`) and are interchangeable for review, comment analysis, and fix flows. Models are listed dynamically per provider.

---

## Usage

1. **Add a repo** — click *Add Repo* in the sidebar, search by `org/name`, and add it.
2. **Browse open PRs** — select a repo to see its open pull requests.
3. **Run a streaming review** — open a PR and click *▶ Run Review* to get an AI analysis streamed token-by-token.
4. **Triage findings** — the *Review* tab lists findings by severity (P0 → P3) with file/line context.
5. **Publish to GitHub** — for each finding choose:
   - `↗ Publish` → posts an inline review comment on the exact `path:line`, or a top-level PR comment if no line is available.
   - `⚠ Request changes` *(P1 default)* → posts a single-finding GitHub review with state `REQUEST_CHANGES`.
   - `+ Add to review` → stage the finding for a batched review.
6. **Batch-publish a Request Changes review** — once you've staged findings, click *⚠ Publish reviews* at the bottom of the list to ship them all in one GitHub review object.
7. **Auto-implement fixes** — on the *Comments* tab, click *⚡ Generate Fix* on any reviewer comment. The AI works in an isolated git worktree; you then either *↑ Push to PR branch* or *+ New PR*.

---

## Configuration

No environment variables are required. Authentication is handled entirely by the `gh` CLI and the AI provider's own configuration (`opencode auth` or `claude` login).

Optional environment variables:

| Variable              | Default     | Purpose                                                |
|-----------------------|-------------|--------------------------------------------------------|
| `AI_PROVIDER`         | `opencode`  | Selects the AI backend at startup. Supported: `opencode`, `claude-code` *(when enabled)*. Live-switchable from the UI. |
| `ENABLE_CLAUDE_CODE`  | unset       | Set to `1` (or `true` / `yes` / `on`) to enable the `claude-code` provider — see [Claude Code is disabled by default](#claude-code-is-disabled-by-default) |

Data is stored in:

- `.cache/` — cached PR lists, review results, and visit timestamps (relative to the project directory).
- `~/.gh-review-tool/` — bare git clones and worktrees used by the AI fix flow.

See [`.env.example`](.env.example) for the full list of optional variables.

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

The project uses a strict **hexagonal (ports & adapters) architecture**, so swapping any external dependency takes one new class.

### Use a different AI provider

Implement `AIProvider` (`app/ports/ai_provider.py`) and wire it in `app/main.py`:

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

### Use a different VCS provider (GitLab, Gitea, Bitbucket, …)

Implement `VCSPort` (`app/ports/vcs_port.py`). Anything that exposes PRs/MRs and inline-comment APIs is a drop-in fit.

### Use a different cache backend

Implement `CachePort` (`app/ports/cache_port.py`) to store data in PostgreSQL, Redis, SQLite, or any other backend instead of the default local JSON files.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full architecture overview, including service-layer boundaries and SSE event contracts.

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

Release notes for every version live in [`CHANGELOG.md`](CHANGELOG.md).

---

## Troubleshooting

**`opencode: command not found` / `claude: command not found`**
Ensure the AI provider CLI for your active `AI_PROVIDER` is installed and on your `PATH`. Run `which opencode` or `which claude` to verify. The server boots even if the CLI is missing — the picker will surface a red badge so you can install it without restarting.

**`gh: command not found` or authentication errors**
Install the GitHub CLI and authenticate: `gh auth login`. The tool delegates *all* GitHub auth to `gh`, so once `gh pr list` works the tool will too.

**Streaming review hangs or produces no output**
The AI provider may be waiting for credentials. For `opencode`, run `opencode auth` once. For `claude-code`, run `claude` interactively to complete login.

**`Failed to create review: HTTP 422`**
This usually means an inline comment line is outside the PR diff, or you're trying to *Request Changes* on your own PR (GitHub forbids self-`REQUEST_CHANGES`). The adapter falls back automatically — check the server log for the underlying GitHub message.

**Port already in use**
Change the port: `uv run uvicorn app.main:app --port 8001`.

**Worktree errors or stale fix sessions**
Worktrees can be deleted via the in-UI worktree manager, or manually:

```bash
rm -rf ~/.gh-review-tool/worktrees/<name>
```

**Corrupt or stale cache**
Clear the cache directory:

```bash
rm -rf .cache/
```

---

## FAQ

**Is `gh-review-tool` a hosted SaaS like CodeRabbit, Greptile, or Codium?**
No. It runs entirely on `localhost`. There is no vendor backend and no account to sign up for. Diffs are read from your local `gh` CLI and sent only to the AI provider you've explicitly configured.

**Does my source code leave my machine?**
Only insofar as your AI provider needs it. The tool's job is to assemble a review prompt and pipe it to your `opencode` or `Claude Code` install. Whether *that* CLI sends the prompt to a remote model (Anthropic, OpenAI, Bedrock, Vertex, OpenRouter…) or to a local one (Ollama, LM Studio, vLLM…) is entirely your provider's configuration.

**Which AI providers are supported out of the box?**
[`opencode`](https://opencode.ai) and [Claude Code](https://docs.claude.com/claude-code). Adding another provider is one new `AIProvider` subclass — see [Adapting for Your Needs](#adapting-for-your-needs).

**Which models can I use?**
Anything your AI CLI supports. `opencode` exposes its registered model list via `opencode models`; Claude Code accepts the universal aliases `opus`, `sonnet`, `haiku` plus any pinned ID your account has access to (including AWS Bedrock inference profiles).

**Can I use it with GitLab, Bitbucket, or Gitea?**
Not out of the box. The default `VCSPort` adapter shells out to the GitHub CLI. Implementing a GitLab or Gitea adapter is a few hundred lines — see `app/ports/vcs_port.py` for the surface area.

**Does it auto-review every PR like a webhook bot?**
No, by design. Reviews run on demand, when *you* click *Run Review*. This is what keeps it private, predictable, and cheap.

**Why do P1 findings publish as a *Request Changes* review instead of a plain comment?**
A P1 (Major) issue is something a human reviewer would block the PR on. Promoting it to a `REQUEST_CHANGES` review surfaces the *Changes requested* banner on the PR so the author can't merge until the issue is addressed. P0/P2/P3 findings publish as regular inline comments.

**How is this different from running `claude` or `opencode` against a diff manually?**
You get: a UI for browsing PRs, streaming output, structured P0–P3 findings with inline-comment publishing, batched *Request Changes* reviews, and a one-click "implement fix" flow that runs in an isolated git worktree and pushes back to the PR. None of that exists in the raw CLI.

**Where do I report a bug or request a feature?**
[Open an issue](https://github.com/Angelaben/my-gh-app/issues) on this repository.

---

## Contributing

Pull requests welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, architecture overview, test layout, and the conventions for `Finding` / `Review` / SSE event types.

---

## Security

This is a **local-only tool** — it is not designed to be exposed on a network. Authentication is delegated entirely to the `gh` CLI keyring; `GITHUB_TOKEN` and `GH_TOKEN` are stripped from every subprocess environment. See [SECURITY.md](SECURITY.md) for the full security model and vulnerability reporting instructions.

---

## Support

If `gh-review-tool` saves you time on PR reviews, consider buying me a coffee ☕ — it directly funds time spent on this project.

[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/yellow_img.png)](https://buymeacoffee.com/angelaben)

---

<sub>Keywords: AI code review, GitHub PR review tool, AI pull request reviewer, self-hosted code review, local AI code reviewer, opencode GitHub integration, Claude Code GitHub PRs, AI-assisted PR review, GitHub CLI dashboard, automated code review without SaaS.</sub>
