"""CLI entry point and interactive setup."""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import click
from rich.console import Console
from rich.panel import Panel

from reentbot.agent import (
    DEFAULT_CONTEXT_WINDOW, REPORT_OUTPUT_RESERVE, calculate_max_context,
    chat_loop, run_audit, run_report,
)
from reentbot.display import Display
from reentbot.docker import AuditContainer
from reentbot.llm import DEFAULT_MODEL, create_client
from reentbot.prompt import build_system_prompt


def _parse_number(raw: str) -> int:
    """Parse a number with optional k/M suffix. Rejects commas and non-positive values."""
    if "," in raw:
        raise ValueError("Commas not supported — use k/M shorthand or type the full number")
    s = raw.lower().strip()
    try:
        if s.endswith("m"):
            result = int(float(s[:-1]) * 1_000_000)
        elif s.endswith("k"):
            result = int(float(s[:-1]) * 1_000)
        else:
            result = int(s)
    except (ValueError, OverflowError):
        raise ValueError(f"Invalid number: {raw}")
    if result <= 0:
        raise ValueError("Must be a positive number")
    return result


def _interactive_setup(
    console: Console,
    api_key: str | None,
    model: str | None,
    rpc_url: str | None,
    capital: int,
    max_time: int,
    max_tokens: int,
    max_turns: int,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    verbosity: str | None = None,
    default_verbosity: str = "partial",
    reasoning: str | None = None,
) -> dict:
    """Prompt for missing configuration values interactively."""
    config: dict = {}

    console.print(Panel("[bold cyan]ReentBot Setup[/]", border_style="cyan"))

    # API key
    if api_key:
        config["api_key"] = api_key
    else:
        console.print("\n  [yellow]OpenRouter API key not found.[/]")
        key = console.input("  Enter API key (or set OPENROUTER_API_KEY env var): ").strip()
        if not key:
            console.print("[bold red]API key is required. Exiting.[/]")
            sys.exit(1)
        config["api_key"] = key

    # RPC URL
    if rpc_url:
        config["rpc_url"] = rpc_url
    else:
        rpc = console.input(
            "\n  Ethereum RPC URL (for on-chain queries, optional — press Enter to skip): "
        ).strip()
        if rpc:
            config["rpc_url"] = rpc
        else:
            config["rpc_url"] = None
            console.print("  [dim]Skipped — on-chain tools (cast, anvil fork) won't work.[/]")

    # Model
    if model:
        config["model"] = model
    else:
        m = console.input(f"\n  Model \\[[not bold blue]{DEFAULT_MODEL}[/]]: ").strip()
        config["model"] = m if m else DEFAULT_MODEL

    # Context window — the model's total context size
    if context_window >= 1_000_000 and context_window % 1_000_000 == 0:
        cw_display = f"{context_window // 1_000_000}M"
    elif context_window >= 1_000 and context_window % 1_000 == 0:
        cw_display = f"{context_window // 1_000}k"
    else:
        cw_display = str(context_window)
    while True:
        cw = console.input(f"\n  Model's context window \\[[not bold blue]{cw_display}[/]]: ").strip()
        if not cw:
            config["context_window"] = context_window
            break
        try:
            config["context_window"] = _parse_number(cw)
            break
        except ValueError as e:
            console.print(f"  [not bold red]{e}[/]")

    # Capital
    if capital >= 1_000_000 and capital % 1_000_000 == 0:
        capital_display = f"{capital // 1_000_000}M"
    elif capital >= 1_000 and capital % 1_000 == 0:
        capital_display = f"{capital // 1_000}k"
    else:
        capital_display = str(capital)
    while True:
        c = console.input(f"\n  Attacker capital budget in USD \\[[not bold blue]{capital_display}[/]]: ").strip()
        if not c:
            config["capital"] = capital
            break
        try:
            config["capital"] = _parse_number(c)
            break
        except ValueError as e:
            console.print(f"  [not bold red]{e}[/]")

    # Max time
    time_min = max_time // 60
    while True:
        t = console.input(f"\n  Max audit time in minutes \\[[not bold blue]{time_min}[/]]: ").strip()
        if not t:
            config["max_time"] = max_time
            break
        try:
            val = int(t)
        except ValueError:
            console.print(f"  [not bold red]Invalid number: {t}[/]")
            continue
        if val <= 0:
            console.print("  [not bold red]Must be a positive number[/]")
            continue
        config["max_time"] = val * 60
        break

    # Max tokens
    if max_tokens >= 1_000_000 and max_tokens % 100_000 == 0:
        tokens_display = f"{max_tokens / 1_000_000:g}M"
    elif max_tokens >= 1_000 and max_tokens % 1_000 == 0:
        tokens_display = f"{max_tokens // 1_000}k"
    else:
        tokens_display = str(max_tokens)
    while True:
        tk = console.input(f"\n  Max token budget \\[[not bold blue]{tokens_display}[/]]: ").strip()
        if not tk:
            config["max_tokens"] = max_tokens
            break
        try:
            config["max_tokens"] = _parse_number(tk)
            break
        except ValueError as e:
            console.print(f"  [not bold red]{e}[/]")

    # Max turns
    while True:
        tr = console.input(f"\n  Max agent turns \\[[not bold blue]{max_turns}[/]]: ").strip()
        if not tr:
            config["max_turns"] = max_turns
            break
        try:
            val = int(tr)
        except ValueError:
            console.print(f"  [not bold red]Invalid number: {tr}[/]")
            continue
        if val <= 0:
            console.print("  [not bold red]Must be a positive number[/]")
            continue
        config["max_turns"] = val
        break

    # Verbosity
    if verbosity:
        config["verbosity"] = verbosity
    else:
        while True:
            v = console.input(
                f"\n  Tool output verbosity — off / partial / full \\[[not bold blue]{default_verbosity}[/]]: "
            ).strip().lower()
            if not v:
                config["verbosity"] = default_verbosity
                break
            if v in ("off", "partial", "full"):
                config["verbosity"] = v
                break
            console.print("  [not bold red]Invalid choice — must be off, partial, or full[/]")

    # Reasoning
    if reasoning:
        config["reasoning"] = reasoning
    else:
        while True:
            r = console.input(
                "\n  Reasoning effort — off / low / medium / high \\[[not bold blue]off[/]]: "
            ).strip().lower()
            if not r:
                config["reasoning"] = "off"
                break
            if r in ("off", "low", "medium", "high"):
                config["reasoning"] = r
                break
            console.print("  [not bold red]Invalid choice — must be off, low, medium, or high[/]")

    console.print()
    return config


async def _run(
    source_dir: str,
    api_key: str | None,
    model: str | None,
    max_tokens: int,
    max_turns: int,
    max_time: int,
    output: str,
    image: str,
    rpc_url: str | None,
    capital: int,
    no_chat: bool,
    verbosity: str | None,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    reasoning: str | None = None,
):
    """Async main entry point."""
    console = Console()

    # Resolve env vars (CLI flags take priority over env vars)
    if api_key is None:
        api_key = os.environ.get("OPENROUTER_API_KEY")
    if rpc_url is None:
        rpc_url = os.environ.get("ETH_RPC_URL")
    if model is None:
        model = os.environ.get("REENTBOT_MODEL")

    # Non-interactive mode: skip setup wizard when stdin is not a TTY (CI, pipes)
    if sys.stdin.isatty():
        config = _interactive_setup(
            console, api_key, model, rpc_url, capital, max_time,
            max_tokens, max_turns, context_window=context_window,
            verbosity=verbosity, reasoning=reasoning,
        )
    else:
        if not api_key:
            console.print("[bold red]Error: OPENROUTER_API_KEY required in non-interactive mode.[/]")
            sys.exit(1)
        config = {
            "api_key": api_key,
            "model": model or DEFAULT_MODEL,
            "rpc_url": rpc_url,
            "capital": capital,
            "max_time": max_time,
            "max_tokens": max_tokens,
            "max_turns": max_turns,
            "context_window": context_window,
            "verbosity": verbosity or "partial",
            "reasoning": reasoning or "off",
        }
    api_key = config["api_key"]
    model = config["model"]
    rpc_url = config.get("rpc_url")
    capital = config["capital"]
    max_time = config["max_time"]
    max_tokens = config["max_tokens"]
    max_turns = config["max_turns"]
    context_window = config["context_window"]
    reasoning_effort = config["reasoning"]
    reasoning_config = (
        {"effort": reasoning_effort} if reasoning_effort != "off" else None
    )
    max_context = calculate_max_context(
        context_window, reasoning_effort=reasoning_effort,
    )
    report_max_context = calculate_max_context(
        context_window, output_reserve=REPORT_OUTPUT_RESERVE,
        reasoning_effort=reasoning_effort,
    )
    verbosity = config["verbosity"]

    display = Display(console=console, verbosity=verbosity)

    # Resolve paths and record start time
    source_dir = os.path.abspath(source_dir)
    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    started_at = datetime.now(timezone.utc).isoformat()
    output_dir = os.path.join(os.path.abspath(output), run_id)
    os.makedirs(output_dir, exist_ok=True)

    # Show header
    display.header(source_dir, model, {
        "max_tokens": max_tokens,
        "max_turns": max_turns,
        "max_time": max_time,
        "rpc_url": rpc_url,
        "capital": capital,
        "context_window": context_window,
        "reasoning": reasoning_effort,
    })

    if reasoning_config:
        display.status(
            f"Reasoning enabled ({reasoning_effort}) — "
            "token usage will be significantly higher"
        )

    # Create LLM client
    client = create_client(api_key)

    # Start container
    container = AuditContainer(image_name=image)
    try:
        await container.start(source_dir, rpc_url=rpc_url, on_status=display.status)

        # Build system prompt
        system_prompt = build_system_prompt(capital_usd=capital)

        # ── Audit phase ──
        display.phase("Audit Phase")
        findings, messages, explored = await run_audit(
            client=client,
            model=model,
            system_prompt=system_prompt,
            container=container,
            display=display,
            max_tokens=max_tokens,
            max_turns=max_turns,
            max_time_seconds=max_time,
            max_context=max_context,
            reasoning_config=reasoning_config,
        )

        # ── Report phase ──
        report_content = await run_report(
            client, model, messages, container, display, findings,
            explored=explored,
            max_context=report_max_context,
            reasoning_config=reasoning_config,
        )

        # Save report to host and render in TUI
        if report_content:
            report_path = os.path.join(output_dir, "report.md")
            with open(report_path, "w") as f:
                f.write(report_content)
            display.report(report_content)

        # Save findings JSON
        findings_data = {
            "run_id": run_id,
            "source_dir": source_dir,
            "model": model,
            "rpc_url": (rpc_url[:40] + "...") if rpc_url else "not set",
            "started_at": started_at,
            "total_turns": len([m for m in messages if m.get("role") == "assistant"]),
            "findings": findings,
        }
        findings_path = os.path.join(output_dir, "findings.json")
        with open(findings_path, "w") as f:
            json.dump(findings_data, f, indent=2, default=str)

        # Show summary
        display.summary(findings, output_dir)

        # ── Chat phase ──
        if not no_chat:
            pre_chat_count = len(findings)
            await chat_loop(
                client, model, messages, container, display, findings,
                explored=explored,
                max_tokens=max_tokens,
                max_turns=max_turns,
                max_time_seconds=max_time,
                max_context=max_context,
                reasoning_config=reasoning_config,
            )

            # Re-save if findings were added during chat (e.g. keep-auditing)
            if len(findings) > pre_chat_count:
                findings_data = {
                    "run_id": run_id,
                    "source_dir": source_dir,
                    "model": model,
                    "rpc_url": (rpc_url[:40] + "...") if rpc_url else "not set",
                    "started_at": started_at,
                    "total_turns": len([m for m in messages if m.get("role") == "assistant"]),
                    "findings": findings,
                }
                with open(findings_path, "w") as f:
                    json.dump(findings_data, f, indent=2, default=str)
                display.status(f"Updated findings saved ({len(findings)} total)")

    except KeyboardInterrupt:
        display.status("Interrupted — saving findings...")
        # Save whatever we have
        findings_data = {
            "run_id": run_id,
            "source_dir": source_dir,
            "model": model,
            "interrupted": True,
            "findings": findings if "findings" in dir() else [],
        }
        findings_path = os.path.join(output_dir, "findings.json")
        with open(findings_path, "w") as f:
            json.dump(findings_data, f, indent=2, default=str)
        display.status(f"Findings saved to {findings_path}")
    except Exception as e:
        display.error(str(e))
        raise
    finally:
        display.status("Cleaning up container...")
        await container.stop()
        display.status("Done.")


@click.command()
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--api-key", default=None, help="OpenRouter API key (or set OPENROUTER_API_KEY env var)")
@click.option("--model", default=None, help="Model to use (OpenRouter model ID)")
@click.option("--max-tokens", default=2_500_000, help="Token budget for audit phase")
@click.option("--max-turns", default=500, help="Maximum agent turns in audit phase")
@click.option("--max-time", default=3600, help="Wall clock budget in seconds for audit phase")
@click.option("--output", default="./findings", help="Output directory for findings and report")
@click.option("--image", default="reentbot-tools", help="Docker image name")
@click.option("--rpc-url", default=None, help="Ethereum RPC URL for on-chain queries (or set ETH_RPC_URL env var)")
@click.option("--capital", default=1000, help="Attacker's upfront capital budget in USD")
@click.option("--no-chat", is_flag=True, help="Skip interactive chat after audit")
@click.option(
    "--verbosity", default=None, type=click.Choice(["off", "partial", "full"], case_sensitive=False),
    help="Tool output verbosity: off (headers only), partial (truncated, default), full (complete)",
)
@click.option(
    "--context-window", default=DEFAULT_CONTEXT_WINDOW, type=int,
    help="Model's context window size in tokens (default: 200k)",
)
@click.option(
    "--reasoning", default=None,
    type=click.Choice(["off", "low", "medium", "high"], case_sensitive=False),
    help="Reasoning effort level (off=disabled, low/medium/high=thinking depth)",
)
def main(source_dir, api_key, model, max_tokens, max_turns, max_time, output, image, rpc_url, capital, no_chat, verbosity, context_window, reasoning):
    """Audit smart contracts for exploitable vulnerabilities."""
    asyncio.run(_run(
        source_dir=source_dir,
        api_key=api_key,
        model=model,
        max_tokens=max_tokens,
        max_turns=max_turns,
        max_time=max_time,
        output=output,
        image=image,
        rpc_url=rpc_url,
        capital=capital,
        no_chat=no_chat,
        verbosity=verbosity,
        context_window=context_window,
        reasoning=reasoning,
    ))
