"""Tool schemas, dispatch, and execution for the audit agent."""

import ipaddress
import posixpath
import shlex
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

from reentbot.docker import AuditContainer

# ── Tool Schemas (OpenAI function-calling format) ────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files and directories at a path in the audit workspace. "
                "Returns names with / suffix for directories. Use depth > 1 to "
                "see nested directory structure."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to list (default: /audit)",
                        "default": "/audit",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Maximum directory depth to recurse into (default: 10, max: 10)",
                        "default": 10,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in the audit workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (1-based)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to return (default: 2000)",
                        "default": 2000,
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": (
                "Search for a regex pattern across files in the workspace. "
                "Returns matching lines with file paths and line numbers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in (default: /audit)",
                        "default": "/audit",
                    },
                    "glob": {
                        "type": "string",
                        "description": 'File pattern to filter (e.g., "*.sol")',
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write content to a file in the workspace. Use this to create "
                "exploit PoCs, test files, config files, etc. Parent directories "
                "are created automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to write to (must be under /audit, /workspace, or /output)",
                    },
                    "content": {
                        "type": "string",
                        "description": "File content to write",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Execute a shell command inside the audit container. Use this to "
                "run tools (forge, slither, echidna, medusa, halmos), compile code, "
                "run tests, or inspect the environment. Commands run from /audit by default."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                    "working_dir": {
                        "type": "string",
                        "description": "Working directory (default: /audit)",
                        "default": "/audit",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 600, max: 1800)",
                        "default": 600,
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for information. Use this to find documentation "
                "about protocols and dependencies, known vulnerabilities in libraries, "
                "details about deployed contracts, flash loan provider APIs, DEX pool "
                "information, and any other context that helps you understand and "
                "exploit the target."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (default: 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Fetch the content of a web page. Use this to read documentation, "
                "audit reports, Etherscan contract source, or any other web resource. "
                "Returns the page content as plain text (HTML tags stripped)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to fetch",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Request timeout in seconds (default: 60)",
                        "default": 60,
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_finding",
            "description": (
                "Submit a vulnerability finding. Call this for each distinct "
                "vulnerability you discover. Include proof-of-concept code and "
                "test results whenever possible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Concise description of the vulnerability",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low", "info"],
                        "description": "Vulnerability severity",
                    },
                    "description": {
                        "type": "string",
                        "description": "Root cause, mechanism, and exploit scenario",
                    },
                    "impact": {
                        "type": "string",
                        "description": (
                            "What an attacker can achieve, estimated economic impact. "
                            "MUST include: upfront capital required, whether a flash loan "
                            "is needed, estimated net profit in USD after gas costs."
                        ),
                    },
                    "affected_code": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "lines": {"type": "string"},
                            },
                            "required": ["file", "lines"],
                        },
                        "description": "Specific code locations",
                    },
                    "proof_of_concept": {
                        "type": "string",
                        "description": "Exploit code or test code",
                    },
                    "validated": {
                        "type": "boolean",
                        "description": "True if PoC was tested and passed",
                        "default": False,
                    },
                    "test_output": {
                        "type": "string",
                        "description": "forge test output or similar",
                    },
                    "remediation": {
                        "type": "string",
                        "description": "Suggested fix",
                    },
                },
                "required": ["title", "severity", "description", "impact", "affected_code"],
            },
        },
    },
]


# ── Tool Execution ───────────────────────────────────────────────────────

# Tools that are safe to run in parallel (read-only in-container)
PARALLEL_SAFE = {"list_files", "read_file", "search_code", "web_search", "fetch_url"}


async def execute_tool(
    name: str,
    arguments: dict,
    container: AuditContainer,
    findings: list[dict],
    display=None,
) -> str:
    """Dispatch and execute a tool call. Always returns a string."""
    try:
        match name:
            case "list_files":
                return await _list_files(container, arguments)
            case "read_file":
                return await _read_file(container, arguments)
            case "search_code":
                return await _search_code(container, arguments)
            case "write_file":
                return await _write_file(container, arguments)
            case "run_command":
                return await _run_command(container, arguments)
            case "web_search":
                return await _web_search(arguments)
            case "fetch_url":
                return await _fetch_url(arguments)
            case "submit_finding":
                return _submit_finding(arguments, findings, display)
            case _:
                return f"Unknown tool: {name}"
    except Exception as e:
        return f"Tool error ({name}): {e}"


def _truncate(text: str, max_chars: int = 50000) -> str:
    """Truncate long output, keeping beginning and end."""
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2 - 50
    return (
        text[:keep]
        + f"\n\n... [truncated — {len(text)} total chars, showing first and last {keep}] ...\n\n"
        + text[-keep:]
    )


# ── Individual tool implementations ─────────────────────────────────────


async def _list_files(container: AuditContainer, args: dict) -> str:
    path = args.get("path", "/audit")
    depth = min(args.get("depth", 10), 10)
    if depth < 1:
        depth = 1

    if depth == 1:
        # Simple listing for depth 1
        exit_code, output = await container.exec(
            f"ls -la {shlex.quote(path)} 2>&1", timeout=10
        )
    else:
        # Recursive tree-style listing for depth > 1
        exit_code, output = await container.exec(
            f"find {shlex.quote(path)} -maxdepth {depth} -not -path '*/\\.git/*' "
            f"| sort 2>&1",
            timeout=30,
        )

    lines = output.strip().split("\n")
    if len(lines) > 200:
        total = len(lines)
        lines = lines[:200]
        lines.append(f"... [truncated, showing first 200 of {total} lines]")
    return "\n".join(lines)


async def _read_file(container: AuditContainer, args: dict) -> str:
    path = args.get("path", "")
    if not path:
        return "Error: 'path' is required"
    offset = args.get("offset", 1)
    limit = args.get("limit", 2000)
    if offset < 1:
        offset = 1

    # Get total line count first
    safe_path = shlex.quote(path)
    _, wc_out = await container.exec(f"wc -l < {safe_path} 2>&1", timeout=10)
    total_lines = 0
    try:
        total_lines = int(wc_out.strip())
    except ValueError:
        pass

    end_line = offset + limit - 1
    exit_code, output = await container.exec(
        f"sed -n '{offset},{end_line}p' {safe_path} 2>&1", timeout=15
    )
    if exit_code != 0:
        return f"Error reading file: {output}"

    output = _truncate(output)

    if total_lines > end_line:
        output += f"\n... [truncated, {total_lines} total lines. Use offset to read more.]"
    return output


async def _search_code(container: AuditContainer, args: dict) -> str:
    pattern = args.get("pattern", "")
    if not pattern:
        return "Error: 'pattern' is required"
    path = args.get("path", "/audit")
    glob = args.get("glob", "")

    safe_pattern = shlex.quote(pattern)
    safe_path = shlex.quote(path)
    include = f"--include={shlex.quote(glob)}" if glob else ""

    # Fetch 101 results: if we get exactly 101, more matches exist
    # (avoids a second grep | wc -l call)
    exit_code, output = await container.exec(
        f"grep -rn {include} -e {safe_pattern} {safe_path} 2>&1 | head -101",
        timeout=30,
    )

    if not output.strip():
        return "No matches found."

    lines = output.strip().split("\n")
    if len(lines) > 100:
        lines = lines[:100]
        return "\n".join(lines) + "\n... [showing first 100 matches, more results exist]"
    return "\n".join(lines)


async def _write_file(container: AuditContainer, args: dict) -> str:
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return "Error: 'path' is required"

    # Normalize to resolve .. and . components, then validate prefix
    path = posixpath.normpath(path)
    allowed_prefixes = ("/audit", "/workspace", "/output")
    if not any(path.startswith(p) for p in allowed_prefixes):
        return f"Error: writes only allowed under {', '.join(allowed_prefixes)}"

    # Ensure parent directory exists
    parent = "/".join(path.rsplit("/", 1)[:-1]) or "/"
    await container.exec(f"mkdir -p {shlex.quote(parent)}", timeout=5)

    await container.write_file(path, content)
    return f"Written {len(content)} bytes to {path}"


async def _run_command(container: AuditContainer, args: dict) -> str:
    command = args.get("command", "")
    if not command:
        return "Error: 'command' is required"
    working_dir = args.get("working_dir", "/audit")
    timeout = min(args.get("timeout", 600), 1800)

    # Wrap with shell-level timeout so the process is killed if it hangs.
    # --kill-after=5: send SIGKILL 5s after SIGTERM if still alive.
    # The asyncio timeout (timeout + 10) is a backstop in case `timeout` itself hangs.
    wrapped = f"timeout --kill-after=5 {timeout}s bash -c {shlex.quote(command)}"
    exit_code, output = await container.exec(
        wrapped, working_dir=working_dir, timeout=timeout + 10
    )
    output = _truncate(output)

    result = output
    if exit_code == 124:
        # 124 = shell `timeout` killed the process
        result = f"Command timed out after {timeout}s (killed)"
    elif exit_code == -1:
        result = f"Command timed out after {timeout}s"
    elif exit_code != 0:
        result += f"\n[exit code: {exit_code}]"
    return result


async def _web_search(args: dict) -> str:
    query = args.get("query", "")
    if not query:
        return "Error: 'query' is required"
    max_results = args.get("max_results", 5)

    try:
        from duckduckgo_search import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))

        if not results:
            return "No results found."

        lines = []
        for r in results:
            lines.append(f"**{r['title']}**\n{r['href']}\n{r['body']}\n")
        return "\n".join(lines)
    except Exception as e:
        return f"Search error: {e}"


async def _fetch_url(args: dict) -> str:
    url = args.get("url", "")
    if not url:
        return "Error: 'url' is required"

    # SSRF guard: block requests to private/internal networks (runs on host)
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "Error: only http and https URLs are supported"
        hostname = parsed.hostname
        if hostname:
            addrs = socket.getaddrinfo(hostname, None)
            for _, _, _, _, sockaddr in addrs:
                ip = ipaddress.ip_address(sockaddr[0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return "Error: cannot fetch URLs pointing to private/internal networks"
    except (socket.gaierror, ValueError):
        pass  # Let httpx handle DNS/parsing failures naturally

    try:
        import re

        import httpx

        req_timeout = args.get("timeout", 60)
        async with httpx.AsyncClient(follow_redirects=True, timeout=req_timeout) as client:
            resp = await client.get(url, headers={"User-Agent": "ReentBot/0.1"})
            resp.raise_for_status()
            text = resp.text

        # Simple HTML to text
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:50000]
    except Exception as e:
        return f"Fetch error: {e}"


def _check_test_output(test_output: str) -> str | None:
    """Check if test_output contains signs that the test actually failed.

    Returns a warning string if failure indicators are found, None otherwise.
    This catches the real problem: agent claims validated but the test failed.
    """
    if not test_output:
        return None
    lowered = test_output.lower()
    # Forge failure indicators
    fail_indicators = ["fail", "error", "revert", "panic"]
    pass_indicators = ["pass", "ok"]
    has_fail = any(ind in lowered for ind in fail_indicators)
    has_pass = any(ind in lowered for ind in pass_indicators)
    if has_fail and not has_pass:
        return (
            "WARNING: The test_output you provided appears to contain "
            "failure indicators but no passing tests. Double-check that "
            "your exploit actually works before treating this as validated. "
            "If the test truly fails, set validated=false and explain why."
        )
    return None


def _submit_finding(args: dict, findings: list[dict], display=None) -> str:
    validated = args.get("validated", False)
    test_output = args.get("test_output", "")

    finding = {
        "id": f"f-{len(findings) + 1:03d}",
        "title": args.get("title", "Untitled"),
        "severity": args.get("severity", "info"),
        "description": args.get("description", ""),
        "impact": args.get("impact", ""),
        "affected_code": args.get("affected_code", []),
        "proof_of_concept": args.get("proof_of_concept", ""),
        "validated": validated,
        "test_output": test_output,
        "remediation": args.get("remediation", ""),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }

    # Check for contradictions: agent says validated but output shows failure
    warning = ""
    if validated:
        output_warning = _check_test_output(test_output)
        if output_warning:
            finding["validated"] = False
            finding["system_note"] = output_warning
            warning = f"\n{output_warning}"

    findings.append(finding)

    if display:
        display.finding(finding)

    status = "validated" if finding["validated"] else "unvalidated"
    return (
        f"Finding #{len(findings)} submitted: [{finding['severity'].upper()}] "
        f"{finding['title']} (id: {finding['id']}, {status}){warning}"
    )
