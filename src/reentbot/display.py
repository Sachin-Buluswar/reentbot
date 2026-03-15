"""Rich terminal output formatting for the audit agent."""

import json

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text


SEVERITY_STYLES = {
    "critical": "bold white on red",
    "high": "bold red",
    "medium": "bold yellow",
    "low": "bold blue",
    "info": "dim",
}


class Display:
    """Handles all terminal output formatting."""

    # Verbosity levels:
    #   "off"     — tool invocation line only, no result panels
    #   "partial" — truncate long results (default)
    #   "full"    — show complete tool output, no truncation
    VERBOSITY_LEVELS = ("off", "partial", "full")

    def __init__(self, console: Console | None = None, verbosity: str = "partial"):
        self.console = console or Console()
        if verbosity not in self.VERBOSITY_LEVELS:
            verbosity = "partial"
        self.verbosity = verbosity
        self.finding_count = 0
        self._streaming = False

    def header(self, source_dir: str, model: str, budget: dict):
        """Print audit header with config info."""
        rpc = budget.get("rpc_url", "not set")
        if rpc and len(rpc) > 50:
            rpc = rpc[:40] + "..."
        lines = [
            f"[bold]Target:[/]  {source_dir}",
            f"[bold]Model:[/]   {model}",
            f"[bold]Budget:[/]  {budget['max_tokens'] // 1000}k tokens | {budget['max_turns']} turns | {budget['max_time'] // 60} min | {budget.get('context_window', 200_000) // 1000}k context",
            f"[bold]Capital:[/] ${budget.get('capital', 1000):,}",
        ]
        if rpc and rpc != "not set":
            lines.append(f"[bold]RPC:[/]     {rpc}")
        self.console.print(Panel(
            "\n".join(lines),
            title="[bold cyan]ReentBot[/]",
            border_style="cyan",
        ))

    def phase(self, name: str):
        """Print a phase separator."""
        self._end_stream()
        self.console.print()
        self.console.rule(f"[bold]{name}[/]", style="dim")
        self.console.print()

    def stream_text(self, text: str):
        """Stream agent reasoning text."""
        if not self._streaming:
            self._streaming = True
        self.console.print(Text(text, style="dim"), end="")

    def _end_stream(self):
        """End a streaming block."""
        if self._streaming:
            self.console.print()
            self._streaming = False

    def tool_start(self, tool_call: dict):
        """Show that a tool is being invoked."""
        self._end_stream()
        name = tool_call["function"]["name"]
        try:
            args = json.loads(tool_call["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            args = {}

        # Build a short summary of the call
        summary = _tool_summary(name, args)
        self.console.print(f"\n[bold cyan]>> {name}:[/] {summary}")

    def tool_result(self, tool_call: dict, result: str):
        """Show tool result according to verbosity level.

        submit_finding results are always shown (via finding()).
        write_file to /output/report.md is never truncated.
        Everything else respects the verbosity setting.
        """
        name = tool_call["function"]["name"]
        if name == "submit_finding":
            return  # Findings get their own display via finding()

        # Never truncate the report write
        is_report_write = False
        if name == "write_file":
            try:
                args = json.loads(tool_call["function"]["arguments"])
                if "/output/report" in args.get("path", ""):
                    is_report_write = True
            except (json.JSONDecodeError, KeyError):
                pass

        if self.verbosity == "off" and not is_report_write:
            return  # Show nothing beyond the tool_start line

        display_result = result
        if self.verbosity == "partial" and not is_report_write and len(display_result) > 800:
            display_result = display_result[:350] + "\n... [truncated] ...\n" + display_result[-350:]

        self.console.print(Panel(
            display_result,
            title=f"[dim]{name}[/]",
            border_style="dim",
            expand=False,
            width=min(self.console.width, 120),
        ))

    def finding(self, finding: dict):
        """Prominently display a new finding."""
        self._end_stream()
        self.finding_count += 1
        severity = finding.get("severity", "info")
        style = SEVERITY_STYLES.get(severity, "dim")
        title = finding.get("title", "Untitled")
        validated = finding.get("validated", False)
        check = " [green]PoC validated[/]" if validated else ""

        affected = ""
        for loc in finding.get("affected_code", []):
            affected += f"\n  {loc.get('file', '?')}:{loc.get('lines', '?')}"

        description = finding.get("description", "")
        self.console.print(Panel(
            f"[bold]{title}[/]\n"
            f"{description}\n"
            f"[dim]Affected:{affected}[/]{check}",
            title=f"Finding #{self.finding_count} \u2014 {severity.upper()}",
            border_style=style.split()[-1] if " " in style else style,
            expand=False,
            width=min(self.console.width, 120),
        ))

    def budget_status(
        self,
        tokens_used: int,
        tokens_max: int,
        elapsed: float,
        time_max: float,
        turn: int,
        max_turns: int,
    ):
        """Show budget status line."""
        self._end_stream()
        mins_elapsed = int(elapsed) // 60
        secs_elapsed = int(elapsed) % 60
        mins_max = int(time_max) // 60
        tok_k = tokens_used // 1000
        tok_max_k = tokens_max // 1000
        self.console.print(
            f"[dim]\u23f1 Turn {turn}/{max_turns} | "
            f"Tokens: {tok_k}k/{tok_max_k}k | "
            f"Time: {mins_elapsed}:{secs_elapsed:02d}/{mins_max}:00[/]"
        )

    def agent_done(self):
        """Show completion message."""
        self._end_stream()
        self.console.print("\n[bold green]Agent completed.[/]")

    def chat_start(self):
        """Show chat mode header."""
        self.console.print()
        self.console.rule("[bold]Chat Mode[/]", style="dim")
        self.console.print(
            "[dim]Ask questions, request attack contracts, or type 'exit' to quit.\n"
            "Type 'keep-auditing' to resume the audit.[/]\n"
        )

    def resuming_audit(self):
        """Show that audit is resuming."""
        self.console.print("\n[bold cyan]Resuming audit...[/]\n")

    def error(self, message: str):
        """Show error message."""
        self._end_stream()
        self.console.print(f"[bold red]Error:[/] {message}")

    def status(self, message: str):
        """Show a status message."""
        self.console.print(f"[dim]{message}[/]")

    def report(self, content: str):
        """Render the markdown report in the terminal."""
        self._end_stream()
        self.console.print()
        self.console.rule("[bold]Vulnerability Report[/]", style="cyan")
        self.console.print()
        self.console.print(Markdown(content))
        self.console.print()

    def summary(self, findings: list[dict], output_dir: str):
        """Show audit summary."""
        self._end_stream()
        by_severity = {}
        for f in findings:
            sev = f.get("severity", "info")
            by_severity[sev] = by_severity.get(sev, 0) + 1

        parts = []
        for sev in ["critical", "high", "medium", "low", "info"]:
            count = by_severity.get(sev, 0)
            if count > 0:
                style = SEVERITY_STYLES.get(sev, "")
                parts.append(f"[{style}]{count} {sev}[/]")

        findings_str = ", ".join(parts) if parts else "No findings"

        self.console.print(Panel(
            f"[bold]Findings:[/] {findings_str}\n"
            f"[bold]Report:[/]   {output_dir}/report.md\n"
            f"[bold]JSON:[/]     {output_dir}/findings.json",
            title="[bold]Audit Complete[/]",
            border_style="green",
        ))


def _tool_summary(name: str, args: dict) -> str:
    """Create a short summary string for a tool call."""
    match name:
        case "list_files":
            return args.get("path", "/audit")
        case "read_file":
            path = args.get("path", "?")
            extra = ""
            if "offset" in args:
                extra = f" (from line {args['offset']})"
            return f"{path}{extra}"
        case "search_code":
            pattern = args.get("pattern", "?")
            path = args.get("path", "")
            return f"'{pattern}' in {path or '/audit'}"
        case "write_file":
            return args.get("path", "?")
        case "run_command":
            cmd = args.get("command", "?")
            if len(cmd) > 80:
                cmd = cmd[:77] + "..."
            return cmd
        case "web_search":
            return f'"{args.get("query", "?")}"'
        case "fetch_url":
            url = args.get("url", "?")
            if len(url) > 60:
                url = url[:57] + "..."
            return url
        case "submit_finding":
            return f'[{args.get("severity", "?")}] {args.get("title", "?")}'
        case _:
            return str(args)[:80]
