# ReentBot — Minimal Agentic Smart Contract Audit System

---

## 1. Overview

A CLI tool called `reentbot` that:
1. Takes a directory of Solidity smart contracts as input
2. Spins up a Docker container pre-loaded with audit tools (Slither, Foundry, Echidna, Medusa, Halmos) and build tooling (Node.js, npm, yarn, jq, tree)
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
- **`REENTBOT_MODEL`** — Default model. Default: `minimax/minimax-m2.5`. Can also be passed via `--model`.
- **`ETH_RPC_URL`** — Ethereum RPC URL for on-chain queries (`cast`, `anvil --fork-url`). Can also be passed via `--rpc-url`. Optional but strongly recommended.
- **Default attacker capital:** $1k USD (configurable via `--capital`).
- **Default budget:** 2.5M tokens | 500 turns | 60 minutes.
- **Default context window:** 200k tokens (configurable via `--context-window`). Used to calculate how much conversation history to retain before truncating. Set this to match your model's actual context window for best results (e.g., `--context-window 200000` for MiniMax M2.5).
- **Verbosity levels:** `off` (tool headers only), `partial` (truncated output, default), `full` (complete output). Findings and report writes are never truncated.
- **Reasoning:** `off` (default), `low`, `medium`, `high` (configurable via `--reasoning`). Controls thinking depth for models that support extended reasoning. Higher levels significantly increase token usage — output tokens are multiplied by 1.3x/2x/5x respectively to preserve the content budget while allocating space for reasoning. Output is capped at 128k tokens per call and at half the context window to prevent overflow. Reasoning display follows the verbosity setting: `off` hides it, `partial` shows a thinking indicator and token count, `full` streams all reasoning content. Silently ignored for models without reasoning support.
- **Output directory:** `./findings` (configurable via `--output`). Each run creates a timestamped subdirectory.
- **Docker image name:** `reentbot-tools` (configurable via `--image`).
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
