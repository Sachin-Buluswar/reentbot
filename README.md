# ReentBot

Automated smart contract auditor. Runs an LLM agent loop over Slither, Foundry, Echidna, Medusa, and Halmos inside a Docker sandbox to find vulnerabilities in Solidity code.

## Quick Start

```bash
export OPENROUTER_API_KEY=sk-or-...
git clone https://github.com/anthropics/ReentBot && cd ReentBot
uv sync
uv run reentbot ./path/to/contracts
```

## Setup

### Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Docker (running)
- OpenRouter API key

### Development setup (run from the ReentBot repo)

```bash
cd /path/to/ReentBot
uv sync
uv run reentbot /path/to/contracts
```

`uv sync` creates an isolated virtualenv and installs all dependencies. `uv run` executes `reentbot` inside that virtualenv. You must run `uv run` from the ReentBot project directory.

### Global install (run from anywhere)

```bash
cd /path/to/ReentBot
uv tool install -e .
uv tool update-shell   # adds ~/.local/bin to your PATH if needed
```

Restart your terminal after `update-shell`. The `-e` (editable) flag means code changes take effect immediately without reinstalling. After this, `reentbot` works from any directory:

```bash
cd ~/Desktop/my-contracts
reentbot .
```

To uninstall: `uv tool uninstall reentbot`

### Environment Variables

```bash
export OPENROUTER_API_KEY=sk-or-...          # Required
export ETH_RPC_URL=https://eth-mainnet...    # Optional, enables on-chain queries
export REENTBOT_MODEL=minimax/minimax-m2.5  # Optional
```

## Usage

If you used the development setup, prefix all commands below with `uv run` and run from the ReentBot directory. If you used the global install, the commands work as shown from any directory.

```bash
# Basic audit — launches the interactive setup wizard
reentbot ./path/to/contracts

# With options (skips wizard prompts for values provided)
reentbot ./contracts --model minimax/minimax-m2.5 --max-time 1800 --capital 5000

# Set token and turn budgets
reentbot ./contracts --max-tokens 500000 --max-turns 50

# Set context window to match your model (e.g., 200k for MiniMax M2.5)
reentbot ./contracts --context-window 200000

# Skip interactive chat
reentbot ./contracts --no-chat

# Custom output directory and Docker image name
reentbot ./contracts --output ./my-audit-results --image my-custom-tools

# Control tool output verbosity
reentbot ./contracts --verbosity full     # Complete untruncated output
reentbot ./contracts --verbosity partial  # Truncated output (default)
reentbot ./contracts --verbosity off      # Tool headers only, no result panels

# Enable reasoning for models that support extended thinking
reentbot ./contracts --reasoning high     # Deep thinking (5x output tokens)
reentbot ./contracts --reasoning medium   # Moderate thinking (2x output tokens)
reentbot ./contracts --reasoning low      # Light thinking (1.3x output tokens)
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
| `--max-tokens` | 2,500,000 | Total token budget for the audit loop |
| `--max-turns` | 500 | Maximum agent turns |
| `--max-time` | 3600s | Wall-clock time limit |
| `--context-window` | 200,000 | Model's context window size; controls conversation history retention |
| `--reasoning` | off | Reasoning effort: off/low/medium/high. Multiplies output tokens by 1.3x/2x/5x |
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
