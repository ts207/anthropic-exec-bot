from __future__ import annotations

import json
import os
import subprocess
from typing import Any

# Shared Claude Code CLI transport (`claude -p`): classification through the
# operator's Claude SUBSCRIPTION (OAuth/keychain session) instead of the
# metered Anthropic API. Flags mirror the battle-tested location/iran paths:
#   --safe-mode                skip CLAUDE.md/skills/plugins/MCP/auto-memory
#                              context while keeping subscription auth
#   --tools ""                 pure text classification, no tool-use overhead
#   --no-session-persistence   nothing written to session history
#   --json-schema              structured output enforced by the CLI
#   --max-budget-usd           hard per-call cost ceiling
# ANTHROPIC_API_KEY/LLM_API_KEY are stripped from the subprocess env so the
# CLI can never silently bill the metered API instead of the subscription.


def run_claude_cli(
    prompt: str,
    *,
    model: str,
    output_schema: dict[str, Any],
    cli_binary: str = "claude",
    timeout_seconds: float = 180.0,
) -> str:
    """One headless CLI call; returns raw stdout (a JSON envelope)."""
    env = {key: value for key, value in os.environ.items() if key not in {"ANTHROPIC_API_KEY", "LLM_API_KEY"}}
    try:
        completed = subprocess.run(
            [
                cli_binary,
                "-p",
                "--safe-mode",
                "--model",
                model,
                "--tools",
                "",
                "--no-session-persistence",
                "--max-budget-usd",
                "0.50",
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(output_schema),
                "--dangerously-skip-permissions",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"claude CLI binary {cli_binary!r} not found; install it "
            "(npm install -g @anthropic-ai/claude-code) and run `claude login`"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude CLI timed out after {timeout_seconds}s") from exc
    if completed.returncode != 0:
        # With --output-format json the CLI reports errors (rate limits,
        # auth problems) in the stdout envelope's "result" field and leaves
        # stderr empty, so fall back to stdout for the diagnostic.
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"claude CLI exited {completed.returncode}: {detail[:500]}")
    return completed.stdout


def extract_claude_cli_result(stdout: str) -> tuple[str, dict[str, Any] | None]:
    """Unwrap the CLI's JSON envelope -> (result_text, usage_fields)."""
    try:
        wrapper: Any = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"claude CLI did not return a JSON envelope: {stdout[:300]!r}") from exc
    if isinstance(wrapper, list):
        result_events = [item for item in wrapper if isinstance(item, dict) and item.get("type") == "result"]
        wrapper = result_events[-1] if result_events else (wrapper[-1] if wrapper else {})
    if not isinstance(wrapper, dict):
        raise RuntimeError(f"unexpected claude CLI output shape: {stdout[:300]!r}")
    if wrapper.get("is_error"):
        raise RuntimeError(f"claude CLI reported an error: {stdout[:500]!r}")
    usage = {key: wrapper[key] for key in ("total_cost_usd", "num_turns", "duration_ms", "usage") if key in wrapper}
    structured = wrapper.get("structured_output")
    if isinstance(structured, dict):
        return json.dumps(structured), usage or None
    result_text = wrapper.get("result")
    if not isinstance(result_text, str) or not result_text.strip():
        raise RuntimeError(f"claude CLI returned no result text: {stdout[:300]!r}")
    return result_text, usage or None


CLI_PROVIDERS = {"claude_cli", "claude-cli", "claude_code_cli"}


def is_claude_cli_provider(provider: str) -> bool:
    return provider.strip().lower() in CLI_PROVIDERS


def claude_cli_available(cli_binary: str = "claude") -> bool:
    import shutil

    return shutil.which(cli_binary) is not None


__all__ = [
    "run_claude_cli",
    "extract_claude_cli_result",
    "is_claude_cli_provider",
    "claude_cli_available",
    "CLI_PROVIDERS",
]
