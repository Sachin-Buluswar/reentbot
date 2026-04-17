# ReentBot — Minimal Agentic Smart Contract Audit System

---

## 1. Overview

A CLI tool called `reentbot` that:
1. Takes a directory of Solidity smart contracts as input
2. Spins up a Docker container pre-loaded with audit tools (Slither, Foundry, Echidna, Medusa, Halmos) and build tooling (Node.js, npm, yarn, pnpm, jq, tree)
3. Runs an LLM-powered agent loop that autonomously analyzes the contracts using those tools
4. The agent can search the web and query on-chain state to understand the protocol's live context, find related contracts, and chain attack vectors (e.g., flash loans)
5. Streams the agent's reasoning and actions to the terminal with rich formatting
6. When the audit phase completes, the agent generates a full vulnerability report with complete attack contracts
7. After the report, enters an interactive chat mode where the user can ask follow-up questions, request deeper analysis, or ask the agent to continue auditing
8. Saves structured findings as JSON and a human-readable markdown report

The core insight: the LLM IS the orchestrator, planner, and decision-maker. There is no pipeline, no stage system, no policy engine. Just an LLM with tools, a clear objective, and a budget.

### Three Phases

1. **Audit phase** — Autonomous. The agent explores code, runs tools, discovers and validates vulnerabilities, submits findings. No human input required.
2. **Report phase** — Automatic. After audit completes, the agent generates a comprehensive markdown report with executive summary, detailed findings, complete deployable attack contracts, and remediation guidance. Saved to disk.
3. **Chat phase** — Interactive. The user can ask questions using the full audit context. Examples: "explain finding #2 in more detail", "write a full flash-loan-integrated attack contract for finding #1", "did you check the staking module?", "keep-auditing" (re-enters audit phase). Type `exit` to quit.

---

## 2. Dependencies

Use `uv` for Python project management. Python 3.11+. Six dependencies:
- `openai` — OpenRouter is OpenAI API-compatible. We use the openai SDK with a custom base_url.
- `docker` — Python Docker SDK for container management.
- `rich` — Terminal formatting (panels, syntax highlighting, streaming).
- `click` — CLI argument parsing.
- `httpx` — HTTP client for fetching web pages (used by `fetch_url` tool on the host side).
- `ddgs` — Web search via DuckDuckGo with no API key required (used by `web_search` tool on the host side).

---

## 3. Key Defaults and Configuration

Configuration priority (highest to lowest): CLI flags → environment variables → interactive prompt → defaults.

- **`OPENROUTER_API_KEY`** — Required. OpenRouter API key. Can also be passed via `--api-key`.
- **`REENTBOT_MODEL`** — Default model. Default: `minimax/minimax-m2.7`. Can also be passed via `--model`.
- **`ETH_RPC_URL`** — Ethereum RPC URL for on-chain queries (`cast`, `anvil --fork-url`). Can also be passed via `--rpc-url`. Optional but strongly recommended.
- **Default attacker capital:** $1k USD (configurable via `--capital`).
- **Default budget:** 2.5M tokens | 500 turns | 60 minutes.
- **Default context window:** 200k tokens (configurable via `--context-window`). Used to calculate how much conversation history to retain before truncating. Set this to match your model's actual context window for best results (e.g., `--context-window 200000` for MiniMax M2.5).
- **Verbosity levels:** `off` (tool headers only), `partial` (truncated output, default), `full` (complete output). Findings and report writes are never truncated.
- **Reasoning:** `off` (default), `low`, `medium`, `high` (configurable via `--reasoning`). Controls thinking depth for models that support extended reasoning. Higher levels significantly increase token usage — output tokens are multiplied by 1.3x/2x/5x respectively to preserve the content budget while allocating space for reasoning. Output is capped at 128k tokens per call and at half the context window to prevent overflow. Reasoning display follows the verbosity setting: `off` hides it, `partial` shows a thinking indicator and token count, `full` streams all reasoning content. Silently ignored for models without reasoning support.
- **Output directory:** `./findings` (configurable via `--output`). Each run creates a timestamped subdirectory.
- **Docker image name:** `reentbot-tools` (configurable via `--image`).
- **Docker platform:** Always `linux/amd64`, even on ARM64 hosts. Container memory limit is 8 GB. See architecture notes below.
- **Skip chat:** `--no-chat` flag skips the interactive chat phase after the report.

---

## 4. Architecture Notes

### Single agent, designed for multi-agent later

This is a single LLM instance — one continuous conversation. This is the right v1 because:
- Simpler to build and debug
- No coordination overhead
- Full context across the entire audit

Multi-agent (e.g., a reviewer that checks findings, a specialist for exploit synthesis) is a natural v2 extension. The tool system doesn't change — you just spawn multiple agent loops with different system prompts and share findings between them.

### On `web_search` and `fetch_url` running on the host

These two tools execute on the host machine, not inside the Docker container. This is because:
- The container may have restricted or no network access depending on configuration
- The search library requires Python packages that don't need to be in the audit container
- It keeps the audit container focused on Solidity tooling

In `tools.py`, the dispatch function routes these two tools to host-side execution while all other tools go through `container.exec()`.

### On the Docker bind mount

The source directory is bind-mounted at `/audit`. This means the agent's file modifications (adding test files, exploit contracts) persist on the host. This is actually desirable — after the audit, the user can inspect the exploit contracts the agent wrote.

If the user wants isolation, they can copy the source directory first:
```
cp -r ./my-protocol /tmp/audit-copy && reentbot /tmp/audit-copy
```

### On forcing `linux/amd64`

The container always runs as `linux/amd64`, even on Apple Silicon Macs. This is because the Solidity compiler (`solc`) and related tools do not publish native Linux ARM64 binaries. On ARM64, tools like Foundry's `svm-rs` and `solc-select` fall back to WASM/Emscripten builds of solc, which run inside Node.js and hit memory limits on complex projects. Forcing amd64 ensures native x86_64 binaries are used via Docker's built-in emulation (QEMU or Rosetta 2). The emulation overhead is modest and far preferable to broken WASM compilation.

The platform is enforced at two levels: the Dockerfile declares `FROM --platform=linux/amd64` to pin the base image and all layers, and `ensure_image()` builds via `docker buildx build --platform linux/amd64 --load` (subprocess) rather than the Python Docker SDK's `images.build()`. The SDK's `platform` parameter is unreliable for cross-platform builds on Apple Silicon — it can silently produce images with the host architecture. The `buildx` CLI with `--load` correctly sets image platform metadata. After building, the image architecture is verified before proceeding.

### On container initialization

At startup, `_init_source()` in `docker.py` does minimal setup and reports results honestly. The philosophy is **"do less, report more"** — the agent is an LLM that can diagnose and fix dependency issues better than brittle init code that guesses at versions and silently clones wrong dependencies.

**Init steps:**

1. **Git repo** — if no `.git` directory exists (e.g., the source was a ZIP download), creates one via `git init`. The init and commit are split into separate steps with a generous timeout for the commit, since large repos may be slow to commit but `git init` alone is sufficient for tooling.
2. **Git config** — configures `safe.directory` so git works with the bind-mounted ownership mismatch, and rewrites SSH URLs (`git@github.com:`) to HTTPS so git operations work without SSH keys.
3. **Project root detection** — `_find_project_root()` locates where the Solidity project actually lives by searching for `foundry.toml` or `hardhat.config.*` up to 3 levels deep. For monorepos (e.g., Liquity Bold where the Foundry project is in `contracts/`), this finds the subdirectory rather than installing at the mount root.
4. **Submodules** — runs `git submodule update --init --recursive` once. If it fails (common with ZIP downloads where gitlinks don't exist), reports the failure and lists which submodule directories are empty via `_list_empty_submodules()`. Does NOT attempt to clone individual submodules — the agent handles missing dependencies itself, which avoids the problem of init code cloning wrong versions.
5. **Dependencies** — `_install_node_deps()` detects the package manager by searching for lock files at both the project root and `/audit` (monorepos often keep the lock file at the repo root). Priority: `pnpm-lock.yaml` → `yarn.lock` → `package-lock.json` → bare `npm install`. Uses frozen install first, falling back to unfrozen if the lock file is stale.
6. **forge-std** — installed at the project root if this is a Foundry project and `lib/forge-std` doesn't exist.

**Init report flow**: `_init_source()` collects status lines into `self.init_report`. After `container.start()`, `cli.py` reads `container.init_report`, joins the lines, and passes the string to `run_audit()` as `init_report`. `agent.py` injects it as the first user message before the agent's first turn. This gives the agent immediate visibility into what's working and what needs fixing.

### On agent loop resilience

The agent loop in `agent.py` has two guards against premature termination:

1. **Truncation retry** — inspects `finish_reason` from the LLM API. When a response is truncated by `max_tokens` (`finish_reason == "length"`) and tool calls are missing, the loop nudges the model to retry with shorter reasoning. After 3 consecutive truncation retries, termination is accepted.

2. **Minimum turn enforcement** — the agent cannot voluntarily stop before `min_audit_turns`, computed as `max(_MIN_AUDIT_TURNS_FLOOR, max_turns // 10)` — i.e., 10% of the turn budget with a floor of 10. For the default 500-turn budget this is 50 turns. If the model returns no tool calls before this threshold (e.g., it gives up after one failed `forge build`), a nudge message instructs it to diagnose the issue or fall back to manual code review. After 3 consecutive nudges without the agent making tool calls, termination is accepted. This guard is bypassed when wrap-up has been requested (Ctrl+C or budget exhaustion).
