"""Agent loop — streaming LLM + tool execution."""

import asyncio
import json
import signal
import time

from openai import AsyncOpenAI

from reentbot.display import Display
from reentbot.docker import AuditContainer
from reentbot.llm import build_reasoning_body
from reentbot.prompt import REPORT_INSTRUCTION
from reentbot.tools import PARALLEL_SAFE, TOOLS, execute_tool


# ── Context window defaults ────────────────────────────────────────────

DEFAULT_CONTEXT_WINDOW = 200_000
_OUTPUT_RESERVE = 16_384  # max_tokens for audit/chat turns
REPORT_OUTPUT_RESERVE = 65_536  # max_tokens for report generation (longer output)
_SAFETY_MARGIN = 2_500
_TOOLS_TOKEN_OVERHEAD = len(json.dumps(TOOLS, default=str)) // 4

# Reasoning effort → output token multiplier.  The multiplier preserves the
# content budget at roughly its original size: multiplier = 1/(1 - reasoning%).
# e.g. at "high" (~80% reasoning), 16k base → 82k total, ~65k reasoning + ~16k content.
_REASONING_MULTIPLIERS = {
    "low": 1.3,
    "medium": 2.0,
    "high": 5.0,
}

# Hard ceiling per API call.  Prevents the multiplier from producing absurd
# max_tokens values when applied to large base reserves (e.g. REPORT_OUTPUT_RESERVE
# at "high" would be 65k*5 = 327k without this cap).
_MAX_OUTPUT_TOKENS = 128_000


def _adjusted_output_reserve(base: int, reasoning_effort: str | None) -> int:
    """Adjust output token reserve for reasoning overhead."""
    if not reasoning_effort or reasoning_effort == "off":
        return base
    return int(base * _REASONING_MULTIPLIERS.get(reasoning_effort, 1.0))


def calculate_max_context(
    context_window: int,
    output_reserve: int = _OUTPUT_RESERVE,
    reasoning_effort: str | None = None,
) -> int:
    """Calculate how many tokens of conversation history to retain.

    Subtracts space reserved for the model's response (output_reserve,
    adjusted for reasoning overhead), a safety margin, and the token overhead
    of tool definitions (sent with every API call but not counted in message
    tokens) from the model's context window.  The adjusted reserve is capped
    at half the context window so output can never starve input of space.
    Floors at 10k to prevent negative or unusably small values.
    """
    adjusted_reserve = _adjusted_output_reserve(output_reserve, reasoning_effort)
    adjusted_reserve = min(adjusted_reserve, context_window // 2)
    return max(
        context_window - adjusted_reserve - _SAFETY_MARGIN - _TOOLS_TOKEN_OVERHEAD,
        10_000,
    )


# ── Helpers ──────────────────────────────────────────────────────────────


def _estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: 4 chars ≈ 1 token."""
    return len(json.dumps(messages, default=str)) // 4


def _group_into_turns(messages: list[dict]) -> list[list[dict]]:
    """Group messages into logical turns for pair-aware truncation.

    A turn is one of:
    - A standalone message (system, user, or assistant without tool_calls)
    - An assistant message with tool_calls + all its consecutive tool result
      messages

    This ensures tool_call/tool_result pairs are never split, which would
    produce a malformed conversation that the API may reject.
    """
    turns: list[list[dict]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            # Group this assistant message with all following tool results
            turn = [msg]
            i += 1
            while i < len(messages) and messages[i].get("role") == "tool":
                turn.append(messages[i])
                i += 1
            turns.append(turn)
        else:
            turns.append([msg])
            i += 1
    return turns


def _build_findings_summary(findings: list[dict]) -> str:
    """Format findings as a compact summary for injection into truncation note."""
    if not findings:
        return ""
    lines = []
    for f in findings:
        title = f.get("title", "Untitled")
        if len(title) > 120:
            title = title[:117] + "..."
        severity = f.get("severity", "info").upper()
        validated = "validated" if f.get("validated") else "unvalidated"
        lines.append(f"  - {f['id']} [{severity}] {title} — {validated}")
    return "\n".join(lines)


def _build_explored_summary(explored: dict) -> str:
    """Format explored state as a compact summary for the truncation note."""
    parts = []
    files = explored.get("files_read", set())
    tools = explored.get("tools_run", set())

    if files:
        sorted_files = sorted(files)
        if len(sorted_files) > 20:
            parts.append(
                f"Files analyzed ({len(files)} total, showing first 20): "
                + ", ".join(sorted_files[:20])
            )
        else:
            parts.append(f"Files analyzed: {', '.join(sorted_files)}")

    if tools:
        parts.append(f"Tools used: {', '.join(sorted(tools))}")

    return "\n".join(parts)


def _summarize_tool_result(tool_name: str, content: str) -> str:
    """Create a compact summary of a tool result for compressed context."""
    lines = content.strip().split("\n")
    line_count = len(lines)
    char_count = len(content)

    match tool_name:
        case "read_file":
            preview = "\n".join(lines[:5])
            return f"{preview}\n[... {line_count} lines, {char_count} chars — compressed]"
        case "run_command":
            if line_count > 10:
                preview = "\n".join(lines[:3] + ["..."] + lines[-3:])
            else:
                preview = "\n".join(lines[:5])
            return f"{preview}\n[... {line_count} lines — compressed]"
        case "search_code":
            preview = "\n".join(lines[:5])
            return f"{preview}\n[... {line_count} result lines — compressed]"
        case "fetch_url":
            return f"{content[:300]}\n[... {char_count} chars — compressed]"
        case _:
            return f"{content[:300]}\n[... {char_count} chars — compressed]"


def _compress_turn(turn: list[dict]) -> list[dict]:
    """Compress bulky tool results in a turn, keeping assistant reasoning intact.

    For turns with tool calls, replaces large tool result content with compact
    summaries while preserving the assistant message (reasoning + tool calls)
    and any small tool results. This lets the agent retain its chain of thought
    without the raw data payload.
    """
    if len(turn) < 2:
        return turn  # standalone message, nothing to compress

    assistant_msg = turn[0]
    if assistant_msg.get("role") != "assistant" or not assistant_msg.get("tool_calls"):
        return turn  # not a tool-call turn

    # Map tool_call_id → tool name for better summaries
    tc_names = {}
    for tc in assistant_msg.get("tool_calls", []):
        tc_names[tc["id"]] = tc["function"]["name"]

    # Strip reasoning from compressed turns to reclaim context space.
    # The conclusions live in content; reasoning is only needed on the
    # most recent assistant message for tool-call continuity.
    stripped_assistant = {
        k: v for k, v in assistant_msg.items()
        if k not in ("reasoning", "reasoning_details")
    }
    compressed = [stripped_assistant]
    for msg in turn[1:]:
        content = msg.get("content", "")
        if msg["role"] == "tool" and len(content) > 500:
            tool_name = tc_names.get(msg.get("tool_call_id"), "unknown")
            summary = _summarize_tool_result(tool_name, content)
            compressed.append({**msg, "content": summary})
        else:
            compressed.append(msg)
    return compressed


def _strip_old_reasoning(messages: list[dict]) -> None:
    """Remove reasoning content from all but the last assistant message.

    The most recent assistant message retains reasoning/reasoning_details
    because some providers require them for tool-call continuity.  Older
    messages have reasoning stripped to reclaim context space.
    """
    last_assistant_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_assistant_idx = i
            break

    if last_assistant_idx is None:
        return

    for i in range(len(messages)):
        msg = messages[i]
        if (i != last_assistant_idx
                and msg.get("role") == "assistant"
                and ("reasoning" in msg or "reasoning_details" in msg)):
            messages[i] = {
                k: v for k, v in msg.items()
                if k not in ("reasoning", "reasoning_details")
            }


def _update_explored(tool_calls: list[dict], explored: dict):
    """Update explored state based on executed tool calls."""
    for tc in tool_calls:
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            continue
        if name == "read_file":
            path = args.get("path", "")
            if path:
                explored["files_read"].add(path)
        elif name == "run_command":
            cmd = args.get("command", "")
            for tool in ("slither", "forge", "echidna", "medusa", "halmos"):
                if tool in cmd:
                    explored["tools_run"].add(tool)
                    break


def _truncate_messages(
    messages: list[dict],
    findings: list[dict] | None = None,
    explored: dict | None = None,
    max_estimated_tokens: int = 100_000,
) -> list[dict]:
    """Keep system prompt, early turns, and recent turns within token budget.

    Uses turn-based grouping so that assistant tool_call messages and their
    tool result messages are always kept or dropped together — never split.

    Reasoning content from old assistant messages is always stripped
    proactively (keeping only the most recent for tool-call continuity),
    since it can be very large and serves no purpose once the model has
    moved on.

    Truncation strategy (two-phase, only when over budget after stripping):
    1. Recent turns are kept at full fidelity (most recent first).
    2. Older turns that don't fit at full size are compressed — bulky tool
       results are replaced with short summaries while assistant content and
       tool calls are preserved. Compressed turns fill whatever budget remains.

    A truncation note is injected with a summary of findings and explored
    state so the agent retains knowledge of what it has discovered and
    analyzed even when the original conversation turns are gone.

    Turn costs are precomputed once to avoid repeated JSON serialization.
    """
    # Always strip old reasoning — it's huge and only the most recent
    # assistant message needs it for tool-call continuity.
    _strip_old_reasoning(messages)

    if _estimate_tokens(messages) <= max_estimated_tokens:
        return messages

    turns = _group_into_turns(messages)

    # Precompute per-turn token costs (avoids repeated serialization)
    turn_costs = [_estimate_tokens(turn) for turn in turns]

    # Always keep: system prompt (turn 0), first 2 turns for initial context
    system_turns = turns[:1]
    early_turns = turns[1:3]
    remaining_turns = turns[3:]
    remaining_costs = turn_costs[3:]

    # Build truncation note with findings + explored state
    findings_summary = _build_findings_summary(findings or [])
    explored_summary = _build_explored_summary(explored) if explored else ""

    if findings_summary or explored_summary:
        note_parts = [
            "[System: Earlier conversation was truncated to fit context window."
        ]
        if findings_summary:
            note_parts.append(f"Your findings so far:\n{findings_summary}")
        if explored_summary:
            note_parts.append(explored_summary)
        note_parts.append(
            "Continue your analysis. Do not re-investigate submitted findings "
            "unless you have new information.]"
        )
        note_content = "\n".join(note_parts)
    else:
        note_content = (
            "[System: Earlier conversation was truncated to fit context window. "
            "No findings submitted yet. Continue your analysis.]"
        )
    truncation_note = [{"role": "user", "content": note_content}]

    # Calculate base costs using precomputed values
    base_cost = sum(turn_costs[:min(3, len(turn_costs))])
    note_cost = _estimate_tokens(truncation_note)
    remaining_budget = max_estimated_tokens - base_cost - note_cost - 500

    # Phase 1: Fill recent turns (full fidelity) from the end
    recent_turns: list[list[dict]] = []
    for i in range(len(remaining_turns) - 1, -1, -1):
        cost = remaining_costs[i]
        if remaining_budget - cost < 0:
            break
        recent_turns.insert(0, remaining_turns[i])
        remaining_budget -= cost

    # Phase 2: Compress and fit dropped middle turns
    dropped_count = len(remaining_turns) - len(recent_turns)
    middle_turns = remaining_turns[:dropped_count]

    compressed_kept: list[list[dict]] = []
    if middle_turns:
        for turn in reversed(middle_turns):
            compressed = _compress_turn(turn)
            cost = _estimate_tokens(compressed)
            if remaining_budget - cost < 0:
                break
            compressed_kept.insert(0, compressed)
            remaining_budget -= cost

    # Assemble: system + early + truncation note + compressed middle + recent
    result = [msg for turn in system_turns + early_turns for msg in turn]
    result.extend(truncation_note)
    for turn in compressed_kept:
        result.extend(turn)
    for turn in recent_turns:
        result.extend(turn)

    # Strip reasoning from all but the most recent assistant message to
    # reclaim context space (_compress_turn already strips compressed turns;
    # this catches surviving recent turns that aren't the latest).
    _strip_old_reasoning(result)

    return result


# ── Main audit loop ─────────────────────────────────────────────────────


async def run_audit(
    client: AsyncOpenAI,
    model: str,
    system_prompt: str,
    container: AuditContainer,
    display: Display,
    max_tokens: int = 2_500_000,
    max_turns: int = 500,
    max_time_seconds: int = 3600,
    prior_findings: list[dict] | None = None,
    max_context: int = calculate_max_context(DEFAULT_CONTEXT_WINDOW),
    reasoning_config: dict | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """
    Run the audit agent loop.
    Returns (findings, messages, explored) — findings list, conversation
    history, and explored state (files read, tools run).

    If prior_findings is provided (e.g. from a previous audit via keep-auditing),
    a summary is injected at the start so the agent knows what was already
    discovered and can focus on unexplored areas.

    max_context controls how many estimated tokens of conversation history
    to keep before truncating. Calculated from the model's context window
    via calculate_max_context(), or overridden via --context-window.
    """
    messages = [{"role": "system", "content": system_prompt}]

    # On resumed audits, tell the agent what was already found
    if prior_findings:
        summary = _build_findings_summary(prior_findings)
        messages.append({
            "role": "user",
            "content": (
                "This is a resumed audit. The following findings were already "
                "submitted in a prior session — do not re-investigate these. "
                "Focus on unexplored areas and contracts.\n\n"
                f"Prior findings:\n{summary}"
            ),
        })

    findings: list[dict] = []
    explored: dict = {"files_read": set(), "tools_run": set()}
    total_tokens_used = 0
    reasoning_tokens_used = 0
    start_time = time.time()
    turn = 0
    budget_warned_90 = False
    budget_warned_95 = False
    wrap_up_requested = False
    wrap_up_injected = False  # ensures wrap-up message is only injected once

    # Signal handling for graceful shutdown
    shutdown_count = 0

    def _signal_handler(sig, frame):
        nonlocal shutdown_count, wrap_up_requested
        shutdown_count += 1
        if shutdown_count == 1:
            display.status("\nCtrl+C received — asking agent to wrap up...")
            wrap_up_requested = True
        else:
            display.status("\nForce quit — saving findings...")
            raise KeyboardInterrupt

    old_handler = signal.signal(signal.SIGINT, _signal_handler)

    try:
        while True:
            elapsed = time.time() - start_time
            budget_fraction = max(
                total_tokens_used / max_tokens if max_tokens > 0 else 0,
                turn / max_turns if max_turns > 0 else 0,
                elapsed / max_time_seconds if max_time_seconds > 0 else 0,
            )

            # Budget exhaustion — hard stop
            if turn >= max_turns or elapsed >= max_time_seconds or total_tokens_used >= max_tokens:
                if not wrap_up_requested:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Budget exhausted. Submit any remaining findings NOW "
                            "using submit_finding, then stop."
                        ),
                    })
                    wrap_up_requested = True
                    wrap_up_injected = True  # budget message serves as wrap-up
                    # Allow one final turn
                else:
                    break

            # Soft signals — at most one per turn, never duplicated
            elif wrap_up_requested and turn > 0 and not wrap_up_injected:
                # Ctrl+C wrap-up — inject once
                wrap_up_injected = True
                messages.append({
                    "role": "user",
                    "content": (
                        "Wrap up now. Submit your strongest findings using "
                        "submit_finding and stop."
                    ),
                })
            elif budget_fraction >= 0.95 and not budget_warned_95:
                budget_warned_95 = True
                messages.append({
                    "role": "user",
                    "content": "Final turn. Submit any remaining findings now.",
                })
            elif budget_fraction >= 0.90 and not budget_warned_90:
                budget_warned_90 = True
                messages.append({
                    "role": "user",
                    "content": (
                        "You have used 90% of your budget. Start wrapping up. "
                        "Focus on validating your strongest findings and submitting them."
                    ),
                })

            turn += 1

            # Show budget every 5 turns
            if turn % 5 == 0 or turn == 1:
                display.budget_status(
                    total_tokens_used, max_tokens,
                    elapsed, max_time_seconds,
                    turn, max_turns,
                    reasoning_tokens=reasoning_tokens_used,
                )

            # Context window management
            messages = _truncate_messages(messages, findings, explored, max_context)

            # Call LLM (streaming)
            try:
                response_message, tokens, r_tokens = await _stream_turn(
                    client, model, messages, display,
                    reasoning_config=reasoning_config,
                )
            except Exception as e:
                # Retry with backoff
                success = False
                for attempt in range(3):
                    display.error(f"LLM call failed ({e}), retrying ({attempt + 1}/3)...")
                    await asyncio.sleep(2 ** attempt)
                    try:
                        response_message, tokens, r_tokens = await _stream_turn(
                            client, model, messages, display,
                            reasoning_config=reasoning_config,
                        )
                        success = True
                        break
                    except Exception as e2:
                        e = e2
                if not success:
                    display.error(f"LLM call failed after 3 retries: {e}")
                    break

            total_tokens_used += tokens
            reasoning_tokens_used += r_tokens
            messages.append(response_message)

            # If no tool calls, agent is done
            if not response_message.get("tool_calls"):
                display.agent_done()
                break

            # Execute tool calls
            tool_results = await _execute_tool_calls(
                response_message["tool_calls"],
                container, findings, display,
            )
            messages.extend(tool_results)

            # Track explored state
            _update_explored(response_message["tool_calls"], explored)

    finally:
        signal.signal(signal.SIGINT, old_handler)

    return findings, messages, explored


async def _stream_turn(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    display: Display,
    max_tokens: int = _OUTPUT_RESERVE,
    reasoning_config: dict | None = None,
) -> tuple[dict, int, int]:
    """Make one streaming LLM call.

    Returns (assistant_message, total_token_count, reasoning_token_count).
    """
    # Adjust max_tokens for reasoning overhead
    effort = reasoning_config.get("effort") if reasoning_config else None
    adjusted_max = min(
        _adjusted_output_reserve(max_tokens, effort),
        _MAX_OUTPUT_TOKENS,
    )

    # Build extra_body for reasoning
    extra_body = build_reasoning_body(reasoning_config)

    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        tools=TOOLS,
        stream=True,
        max_tokens=adjusted_max,
        stream_options={"include_usage": True},
        extra_body=extra_body,
    )

    content_parts = []
    tool_calls_acc: dict[int, dict] = {}
    reasoning_text_parts: list[str] = []
    reasoning_details_acc: dict[int, dict] = {}
    reasoning_active = False
    usage_tokens = 0
    reasoning_token_count = 0

    async for chunk in stream:
        if not chunk.choices:
            # Final chunk may have usage info
            if hasattr(chunk, "usage") and chunk.usage:
                usage_tokens = getattr(chunk.usage, "total_tokens", 0)
                cd = getattr(chunk.usage, "completion_tokens_details", None)
                if cd:
                    reasoning_token_count = getattr(cd, "reasoning_tokens", 0) or 0
            continue

        delta = chunk.choices[0].delta

        # ── Reasoning (structured takes priority over simple) ──
        raw_reasoning = getattr(delta, "reasoning_details", None)
        raw_simple = (
            getattr(delta, "reasoning", None)
            or getattr(delta, "reasoning_content", None)
        )
        if raw_reasoning:
            if not reasoning_active:
                reasoning_active = True
            for detail in raw_reasoning:
                # Normalize to dict
                if hasattr(detail, "model_dump"):
                    d = detail.model_dump(exclude_none=True)
                elif isinstance(detail, dict):
                    d = detail
                else:
                    d = {}

                # Accumulate structured detail by index
                idx = d.get("index", 0)
                if idx not in reasoning_details_acc:
                    reasoning_details_acc[idx] = {}
                acc = reasoning_details_acc[idx]
                for key, value in d.items():
                    if value is None or key == "index":
                        continue
                    if key in ("text", "summary", "data") and isinstance(value, str):
                        acc[key] = acc.get(key, "") + value
                    else:
                        acc[key] = value

                # Stream reasoning text for display
                text_chunk = d.get("text", "")
                if text_chunk:
                    reasoning_text_parts.append(text_chunk)
                    display.stream_reasoning(text_chunk)

        elif raw_simple and isinstance(raw_simple, str):
            # Simple reasoning field — fallback for models that don't use
            # reasoning_details.  Skipped when reasoning_details was present
            # to avoid printing the same tokens twice.
            if not reasoning_active:
                reasoning_active = True
            reasoning_text_parts.append(raw_simple)
            display.stream_reasoning(raw_simple)

        # Detect transition from reasoning to content/tool_calls
        if reasoning_active and (delta.content or delta.tool_calls):
            reasoning_active = False
            display.end_reasoning()

        # ── Stream text content ──
        if delta.content:
            content_parts.append(delta.content)
            display.stream_text(delta.content)

        # ── Accumulate tool calls ──
        if delta.tool_calls:
            for tc_delta in delta.tool_calls:
                idx = tc_delta.index
                if idx not in tool_calls_acc:
                    tool_calls_acc[idx] = {
                        "id": tc_delta.id or "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                if tc_delta.id and not tool_calls_acc[idx]["id"]:
                    tool_calls_acc[idx]["id"] = tc_delta.id
                if tc_delta.function:
                    if tc_delta.function.name:
                        tool_calls_acc[idx]["function"]["name"] += tc_delta.function.name
                    if tc_delta.function.arguments:
                        tool_calls_acc[idx]["function"]["arguments"] += tc_delta.function.arguments

        # Check for usage in final chunk
        if hasattr(chunk, "usage") and chunk.usage:
            usage_tokens = getattr(chunk.usage, "total_tokens", 0)
            cd = getattr(chunk.usage, "completion_tokens_details", None)
            if cd:
                reasoning_token_count = getattr(cd, "reasoning_tokens", 0) or 0

    # End reasoning display if still active (e.g. model only produced reasoning)
    if reasoning_active:
        display.end_reasoning()

    content = "".join(content_parts)
    tool_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())] if tool_calls_acc else []
    reasoning_text = "".join(reasoning_text_parts)

    # Show reasoning token summary
    if reasoning_text and reasoning_token_count == 0:
        # Estimate if API didn't provide reasoning token count
        reasoning_token_count = len(reasoning_text) // 4
    if reasoning_token_count > 0:
        display.reasoning_summary(reasoning_token_count)

    # Estimate total tokens if API didn't provide them
    if usage_tokens == 0:
        completion_len = len(content) + len(reasoning_text)
        for tc in tool_calls:
            completion_len += len(tc["function"].get("arguments", ""))
        usage_tokens = (len(json.dumps(messages, default=str)) + completion_len) // 4

    # Build assistant message
    msg: dict = {"role": "assistant"}
    if content:
        msg["content"] = content
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if reasoning_details_acc:
        msg["reasoning_details"] = [
            reasoning_details_acc[i] for i in sorted(reasoning_details_acc.keys())
        ]
    if reasoning_text:
        msg["reasoning"] = reasoning_text

    return msg, usage_tokens, reasoning_token_count


async def _execute_tool_calls(
    tool_calls: list[dict],
    container: AuditContainer,
    findings: list[dict],
    display: Display,
) -> list[dict]:
    """Execute tool calls, parallelizing where safe. Returns tool result messages."""
    results: list[dict] = []

    # Separate parallel-safe and sequential tools
    parallel_calls = []
    sequential_calls = []
    for tc in tool_calls:
        name = tc["function"]["name"]
        if name in PARALLEL_SAFE:
            parallel_calls.append(tc)
        else:
            sequential_calls.append(tc)

    # Execute parallel-safe tools concurrently
    if parallel_calls:
        async def _exec_one(tc):
            display.tool_start(tc)
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}
            result = await execute_tool(
                tc["function"]["name"], args, container, findings, display
            )
            display.tool_result(tc, result)
            return {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            }

        parallel_results = await asyncio.gather(
            *[_exec_one(tc) for tc in parallel_calls]
        )
        results.extend(parallel_results)

    # Execute sequential tools in order
    for tc in sequential_calls:
        display.tool_start(tc)
        try:
            args = json.loads(tc["function"]["arguments"])
        except json.JSONDecodeError:
            args = {}
        result = await execute_tool(
            tc["function"]["name"], args, container, findings, display
        )
        display.tool_result(tc, result)
        results.append({
            "role": "tool",
            "tool_call_id": tc["id"],
            "content": result,
        })

    return results


# ── Report generation ────────────────────────────────────────────────────


async def run_report(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    container: AuditContainer,
    display: Display,
    findings: list[dict],
    explored: dict | None = None,
    max_context: int = calculate_max_context(DEFAULT_CONTEXT_WINDOW),
    reasoning_config: dict | None = None,
) -> str | None:
    """Generate the vulnerability report. Returns report content or None.

    Injects the full structured findings data alongside the report instruction
    so the agent has authoritative data regardless of what conversation history
    was truncated during the audit phase.

    Uses a larger output reserve (REPORT_OUTPUT_RESERVE) since reports are
    significantly longer than typical audit-phase responses.
    """
    display.phase("Report Phase")

    # Inject report instruction with full findings data so the agent doesn't
    # depend on truncated conversation history for finding details
    if findings:
        findings_json = json.dumps(findings, indent=2, default=str)
        report_msg = (
            REPORT_INSTRUCTION
            + "\n\nHere are all submitted findings for reference:\n\n"
            + findings_json
        )
    else:
        report_msg = (
            REPORT_INSTRUCTION
            + "\n\nNo findings were submitted during the audit. "
            "Note this in the report and document what was analyzed."
        )
    messages.append({"role": "user", "content": report_msg})

    # Give the agent a few turns to write the report
    for _ in range(10):
        messages = _truncate_messages(messages, findings, explored, max_context)
        try:
            response_message, _, _ = await _stream_turn(
                client, model, messages, display,
                max_tokens=REPORT_OUTPUT_RESERVE,
                reasoning_config=reasoning_config,
            )
        except Exception as e:
            display.error(f"Report generation failed: {e}")
            return None

        messages.append(response_message)

        if not response_message.get("tool_calls"):
            break

        tool_results = await _execute_tool_calls(
            response_message["tool_calls"],
            container, findings, display,
        )
        messages.extend(tool_results)

    # Try to read the report from the container
    try:
        report = await container.read_file("/output/report.md")
        return report
    except Exception:
        try:
            report = await container.read_file("/audit/report.md")
            return report
        except Exception:
            pass

    # Fallback: the model may have included the report in its response text
    # instead of using write_file. Use the last substantial assistant message.
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and len(msg.get("content", "")) > 500:
            return msg["content"]

    display.error("Could not read generated report from container")
    return None


# ── Interactive chat ─────────────────────────────────────────────────────


async def chat_loop(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    container: AuditContainer,
    display: Display,
    findings: list[dict],
    explored: dict | None = None,
    max_tokens: int = 2_500_000,
    max_turns: int = 500,
    max_time_seconds: int = 3600,
    max_context: int = calculate_max_context(DEFAULT_CONTEXT_WINDOW),
    reasoning_config: dict | None = None,
):
    """Interactive chat after audit."""
    display.chat_start()

    while True:
        try:
            user_input = await asyncio.to_thread(input, "\n[reentbot] > ")
        except (EOFError, KeyboardInterrupt):
            break

        user_input = user_input.strip()
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            break
        if user_input.lower() == "keep-auditing":
            display.resuming_audit()
            new_findings, messages, new_explored = await run_audit(
                client, model,
                messages[0]["content"],  # system prompt
                container, display,
                max_tokens=max_tokens,
                max_turns=max_turns,
                max_time_seconds=max_time_seconds,
                prior_findings=findings,
                max_context=max_context,
                reasoning_config=reasoning_config,
            )
            findings.extend(new_findings)
            # Merge explored state
            if explored is not None and new_explored:
                explored["files_read"].update(new_explored.get("files_read", set()))
                explored["tools_run"].update(new_explored.get("tools_run", set()))
            continue

        messages.append({"role": "user", "content": user_input})

        # Run agent turns until no more tool calls
        for _ in range(20):  # Safety limit per chat turn
            messages = _truncate_messages(messages, findings, explored, max_context)
            try:
                response_message, _, _ = await _stream_turn(
                    client, model, messages, display,
                    reasoning_config=reasoning_config,
                )
            except Exception as e:
                display.error(f"LLM call failed: {e}")
                break

            messages.append(response_message)

            if not response_message.get("tool_calls"):
                break

            tool_results = await _execute_tool_calls(
                response_message["tool_calls"],
                container, findings, display,
            )
            messages.extend(tool_results)

            # Track explored state from chat interactions
            if explored is not None:
                _update_explored(response_message["tool_calls"], explored)
