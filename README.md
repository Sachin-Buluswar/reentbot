# ReentBot

Automated smart contract auditor. Runs an LLM agent loop over Slither, Foundry, Echidna, Medusa, and Halmos inside a Docker sandbox to find vulnerabilities in Solidity code.

## Quick Start

```bash
export OPENROUTER_API_KEY=sk-or-...
uv sync
reentbot ./path/to/contracts
```

## Setup

```bash
# Install with uv
uv pip install -e .

# Or install dependencies
uv sync
```

### Requirements

- Python 3.11+
- Docker (running)
- OpenRouter API key

### Environment Variables

```bash
export OPENROUTER_API_KEY=sk-or-...          # Required
export ETH_RPC_URL=https://eth-mainnet...    # Optional, enables on-chain queries
export REENTBOT_MODEL=anthropic/claude-sonnet-4-20250514  # Optional
```

## Usage

```bash
# Basic audit — launches the interactive setup wizard
reentbot ./path/to/contracts

# With options (skips wizard prompts for values provided)
reentbot ./contracts --model anthropic/claude-sonnet-4-20250514 --max-time 1800 --capital 5000

# Set token and turn budgets
reentbot ./contracts --max-tokens 500000 --max-turns 50

# Set context window to match your model (e.g., 200k for Claude Sonnet 4)
reentbot ./contracts --context-window 200000

# Skip interactive chat
reentbot ./contracts --no-chat

# Custom output directory and Docker image name
reentbot ./contracts --output ./my-audit-results --image my-custom-tools

# Control tool output verbosity
reentbot ./contracts --verbosity full     # Complete untruncated output
reentbot ./contracts --verbosity partial  # Truncated output (default)
reentbot ./contracts --verbosity off      # Tool headers only, no result panels
```

If any configuration values are missing (not provided via CLI flags or environment variables), the setup wizard will prompt you interactively before starting the audit. Values provided via CLI flags or env vars skip their respective prompts.

### Verbosity Levels

| Level | Behavior |
|-------|----------|
| `off` | Shows tool invocation headers only (e.g. `>> run_command: forge test`), no result panels |
| `partial` | Truncates long results (first/last 350 chars). Default. |
| `full` | Shows complete tool output with no truncation |

Findings (`submit_finding`) and audit reports are always displayed in full regardless of verbosity.

The first run builds the Docker image with all audit tools (takes several minutes). Subsequent runs use the cached image.

## Output

Each run creates a timestamped directory under `./findings/` (e.g., `./findings/2025-06-15_14-30-00/`) containing:

- `report.md` — Full vulnerability report with attack contracts
- `findings.json` — Machine-readable findings

Exploit contracts and test files written by the agent during the audit persist in the source directory.

## Budget Defaults

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--max-tokens` | 2,000,000 | Total token budget for the audit loop |
| `--max-turns` | 200 | Maximum agent turns |
| `--max-time` | 3600s | Wall-clock time limit |
| `--context-window` | 128,000 | Model's context window size; controls conversation history retention |
| Per-response (audit/chat) | 16,384 | Max output tokens per LLM response during audit and chat phases |
| Per-response (report) | 65,536 | Max output tokens per LLM response during report generation |

## Three Phases

1. **Audit** — Autonomous. The agent explores code, runs tools, discovers and validates vulnerabilities.
2. **Report** — Automatic. Generates a comprehensive markdown report with attack contracts.
3. **Chat** — Interactive. Ask follow-up questions, request deeper analysis, or type `keep-auditing` to resume the audit.

## Disclaimer

This tool is intended for authorized security testing, educational use, and CTF competitions. It is not a replacement for a professional manual audit. Users are responsible for ensuring they have authorization to test the target contracts.

## License

MIT
