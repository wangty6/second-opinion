#!/usr/bin/env python3
"""Second Opinion — Claude Code Stop hook that sends plans/code to another model for review."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

# ─── Defaults ────────────────────────────────────────────────────────────────

DEFAULTS = {
    "enabled": True,
    "auto_review_on_stop": True,
    "backend": "opencode",
    "max_context_messages": 20,
    "max_context_chars": 30000,
    "timeout": 300,
    "cooldown": 30,
    "min_assistant_length": 200,
    "skip_patterns": [r"^(yes|no|ok|sure|done|thanks)$", r"^\s*$"],
    "review_language": "en",
    "backends": {
        "opencode": {
            "command": "opencode",
            "args_template": ["run", "{prompt}"],
            "env": {},
        },
        "codex": {
            "command": "codex",
            "args_template": ["-q", "--approval-mode", "never", "{prompt}"],
            "env": {},
        },
        "gemini": {
            "command": "gemini",
            "args_template": ["-p", "{prompt}"],
            "env": {},
        },
        "custom": {
            "command": "",
            "args_template": ["{prompt}"],
            "env": {},
        },
    },
}

# ─── Config Loader ───────────────────────────────────────────────────────────


def load_config(cwd: str) -> dict:
    """Load config from .claude/second-opinion.config.json, merged with defaults."""
    config = json.loads(json.dumps(DEFAULTS))  # deep copy
    config_path = os.path.join(cwd, ".claude", "second-opinion.config.json")
    try:
        with open(config_path, "r") as f:
            user_config = json.load(f)
        # Shallow merge top-level keys
        for key, value in user_config.items():
            if key == "backends" and isinstance(value, dict):
                # Merge backend configs
                if "backends" not in config:
                    config["backends"] = {}
                for bname, bconf in value.items():
                    if bname in config["backends"]:
                        config["backends"][bname].update(bconf)
                    else:
                        config["backends"][bname] = bconf
            else:
                config[key] = value
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return config


# ─── Transcript Parser ───────────────────────────────────────────────────────

# Tools that produce low-signal output for review purposes
LOW_SIGNAL_TOOLS = {"Read", "Grep", "Glob", "ToolSearch"}


def _extract_text(content) -> str:
    """Extract plain text from content that may be a string or list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", block.get("content", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content) if content else ""


def extract_context(transcript_path: str, max_messages: int, max_chars: int) -> str:
    """Parse a JSONL transcript file and extract formatted context.

    Supports two formats:
    1. Simple: {"role": "user"|"assistant"|"tool", "content": ...}
    2. Claude Code: {"type": "user"|"assistant"|..., "message": {"role": ..., "content": ...}}
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""

    lines = []
    try:
        with open(transcript_path, "r") as f:
            lines = f.readlines()
    except OSError:
        return ""

    # Take last N*3 lines to account for tool calls expanding message count
    tail = lines[-(max_messages * 3) :]

    entries = []
    for raw_line in tail:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        # Detect format: Claude Code wraps in {"type": ..., "message": {...}}
        entry_type = entry.get("type", "")
        if "message" in entry and isinstance(entry.get("message"), dict):
            role = entry["message"].get("role", entry_type)
            content = entry["message"].get("content", "")
        else:
            role = entry.get("role", entry_type)
            content = entry.get("content", "")

        # Skip non-message types (progress, file-history-snapshot, system, etc.)
        if entry_type and entry_type not in ("user", "assistant", "tool"):
            # But still process "user" entries that contain tool_result blocks
            if role != "user":
                continue

        if role == "user":
            # User messages may contain text or tool_result blocks
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "text":
                        text = block.get("text", "").strip()
                        if text:
                            entries.append(f"[USER]\n{text}")
                    elif btype == "tool_result":
                        # Tool result embedded in user message
                        result_content = block.get("content", "")
                        text = _extract_text(result_content)
                        if text.strip():
                            truncated = text.strip()[:500]
                            if len(text.strip()) > 500:
                                truncated += "\n... (truncated)"
                            entries.append(f"[TOOL RESULT]\n{truncated}")
            elif isinstance(content, str) and content.strip():
                entries.append(f"[USER]\n{content.strip()}")

        elif role == "assistant":
            # Handle both text and tool_use blocks
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "text":
                        text = block.get("text", "").strip()
                        if text:
                            entries.append(f"[ASSISTANT]\n{text}")
                    elif btype == "tool_use":
                        tool_name = block.get("name", "unknown")
                        if tool_name in LOW_SIGNAL_TOOLS:
                            continue
                        tool_input = block.get("input", {})
                        if tool_name == "Write":
                            path = tool_input.get("file_path", "?")
                            entries.append(f"[TOOL: Write → {path}]")
                        elif tool_name == "Edit":
                            path = tool_input.get("file_path", "?")
                            old = tool_input.get("old_string", "")[:80]
                            entries.append(f"[TOOL: Edit → {path}] {old}...")
                        elif tool_name == "Bash":
                            cmd = tool_input.get("command", "")[:200]
                            entries.append(f"[TOOL: Bash] {cmd}")
                        else:
                            entries.append(f"[TOOL: {tool_name}]")
                    # Skip thinking blocks
            elif isinstance(content, str) and content.strip():
                entries.append(f"[ASSISTANT]\n{content.strip()}")

        elif role == "tool":
            # Tool results — include a brief summary
            text = _extract_text(content)
            if text.strip():
                # Truncate long tool results
                truncated = text.strip()[:500]
                if len(text.strip()) > 500:
                    truncated += "\n... (truncated)"
                entries.append(f"[TOOL RESULT]\n{truncated}")

    result = "\n\n".join(entries)

    # Truncate from START if over max_chars (keep most recent context)
    if len(result) > max_chars:
        result = result[len(result) - max_chars :]
        # Clean up — find first complete entry marker
        for marker in ["[USER]", "[ASSISTANT]", "[TOOL:"]:
            idx = result.find(marker)
            if idx != -1:
                result = result[idx:]
                break

    return result


# ─── Skip Logic ──────────────────────────────────────────────────────────────


def should_skip(config: dict, stdin_data: dict, force: bool = False) -> str | None:
    """Return a reason string if review should be skipped, or None to proceed."""
    if not config.get("enabled", True):
        return "disabled in config"

    if not config.get("auto_review_on_stop", True):
        return "auto_review_on_stop is false"

    if force:
        return None

    # Check min_assistant_length using last_assistant_message from stdin
    last_msg = stdin_data.get("last_assistant_message", "")
    min_len = config.get("min_assistant_length", 200)
    if len(last_msg) < min_len:
        return f"assistant message too short ({len(last_msg)} < {min_len})"

    # Check skip patterns on last user message
    last_user = stdin_data.get("last_user_message", "")
    for pattern in config.get("skip_patterns", []):
        try:
            if re.match(pattern, last_user.strip(), re.IGNORECASE):
                return f"user message matches skip pattern: {pattern}"
        except re.error:
            continue

    # Check cooldown
    cwd = stdin_data.get("cwd", os.getcwd())
    last_run_path = os.path.join(cwd, ".claude", "reviews", ".last_run")
    cooldown = config.get("cooldown", 30)
    try:
        if os.path.exists(last_run_path):
            last_run = float(open(last_run_path).read().strip())
            elapsed = time.time() - last_run
            if elapsed < cooldown:
                return f"cooldown ({int(cooldown - elapsed)}s remaining)"
    except (ValueError, OSError):
        pass

    return None


# ─── Prompt Builder ──────────────────────────────────────────────────────────


def build_review_prompt(context: str, config: dict) -> str:
    """Build the review prompt to send to the backend model."""
    lang = config.get("review_language", "en")
    lang_instruction = f"\nRespond in: {lang}" if lang != "en" else ""

    return f"""You are a senior software engineer conducting a code review.

Review the following Claude Code session transcript. Focus on:
1. **Correctness** — Logic errors, edge cases, off-by-one errors
2. **Security** — Injection risks, exposed secrets, unsafe operations
3. **Design** — Architecture issues, coupling, missing abstractions
4. **Performance** — Obvious inefficiencies, N+1 queries, unnecessary allocations
5. **Maintainability** — Unclear code, missing error handling, poor naming

Output format (use exactly these headers):
## Verdict
One of: LGTM | MINOR ISSUES | NEEDS REVISION | BLOCKER

## Issues
Numbered list. Each item: severity (critical/warning/info), file:line if applicable, description.

## Suggestions
Concrete improvements with code snippets where helpful.

## Risk Assessment
One sentence on the overall risk level of the changes.
{lang_instruction}
---

SESSION TRANSCRIPT:

{context}"""


# ─── Backend Dispatcher ─────────────────────────────────────────────────────


def dispatch_review(prompt: str, config: dict, backend_override: str | None = None) -> tuple:
    """Send prompt to the configured backend. Returns (success, output)."""
    backend_name = backend_override or config.get("backend", "opencode")
    backends = config.get("backends", {})
    backend = backends.get(backend_name, {})

    command = backend.get("command", "")
    if not command:
        return (False, f"Backend '{backend_name}' has no command configured")

    args_template = backend.get("args_template", ["{prompt}"])
    timeout = config.get("timeout", 300)

    # Always write prompt to a temp file to avoid OS argument length limits.
    # The {prompt} placeholder in args_template is replaced with the temp file path.
    # Backend wrappers (e.g. openrouter-backend.py) detect file paths and read them.
    # CLI backends like opencode receive the file path as the message argument.
    prompt_file = None
    prompt_ref = prompt
    try:
        prompt_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="second_opinion_", delete=False
        )
        prompt_file.write(prompt)
        prompt_file.close()
        prompt_ref = prompt_file.name
    except OSError:
        prompt_ref = prompt  # Fall back to inline

    # Build command list
    cmd_list = [command]
    for arg in args_template:
        if "{prompt}" in arg:
            cmd_list.append(arg.replace("{prompt}", prompt_ref))
        else:
            cmd_list.append(arg)

    # Environment
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    for key, value in backend.get("env", {}).items():
        env[key] = str(value)

    try:
        result = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=os.getcwd(),
        )
        output = result.stdout.strip()
        if not output and result.stderr.strip():
            output = result.stderr.strip()
        if result.returncode != 0 and not output:
            return (False, f"Backend exited with code {result.returncode}")
        return (True, output)
    except subprocess.TimeoutExpired:
        return (False, f"Backend '{backend_name}' timed out after {timeout}s")
    except FileNotFoundError:
        return (False, f"Backend command '{command}' not found. Is it installed?")
    except OSError as e:
        return (False, f"Failed to run backend: {e}")
    finally:
        # Clean up temp file
        if prompt_file and os.path.exists(prompt_file.name):
            try:
                os.unlink(prompt_file.name)
            except OSError:
                pass


# ─── Review Writer ───────────────────────────────────────────────────────────


def write_review(cwd: str, output: str, backend_name: str, success: bool) -> str:
    """Write review to .claude/reviews/latest.md and timestamped archive."""
    reviews_dir = os.path.join(cwd, ".claude", "reviews")
    os.makedirs(reviews_dir, exist_ok=True)

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    file_timestamp = now.strftime("%Y%m%d_%H%M%S")
    status = "completed" if success else "failed"

    header = f"""# Second Opinion Review
- **Timestamp:** {timestamp}
- **Backend:** {backend_name}
- **Status:** {status}

---

"""
    content = header + output

    # Write latest.md (overwrite)
    latest_path = os.path.join(reviews_dir, "latest.md")
    with open(latest_path, "w") as f:
        f.write(content)

    # Write timestamped archive
    archive_path = os.path.join(reviews_dir, f"review_{file_timestamp}.md")
    with open(archive_path, "w") as f:
        f.write(content)

    # Update last_run timestamp
    last_run_path = os.path.join(reviews_dir, ".last_run")
    with open(last_run_path, "w") as f:
        f.write(str(time.time()))

    return latest_path


# ─── Summary Printer ────────────────────────────────────────────────────────


def print_summary(output: str, backend_name: str, success: bool) -> None:
    """Print a formatted summary to stdout (visible in Claude Code transcript)."""
    border = "─" * 50
    print(f"\n┌{border}┐")
    print(f"│ Second Opinion Review ({backend_name})")
    print(f"├{border}┤")

    if not success:
        print(f"│ STATUS: FAILED")
        # Show first few lines of error
        for line in output.split("\n")[:5]:
            print(f"│ {line[:70]}")
        print(f"└{border}┘")
        return

    # Try to parse verdict
    verdict = "unknown"
    verdict_match = re.search(r"##\s*Verdict\s*\n+(.+)", output, re.IGNORECASE)
    if verdict_match:
        verdict = verdict_match.group(1).strip()

    print(f"│ VERDICT: {verdict}")
    print(f"├{border}┤")

    # Try to extract issues
    issues_match = re.search(
        r"##\s*Issues\s*\n([\s\S]*?)(?=\n##|\Z)", output, re.IGNORECASE
    )
    if issues_match:
        issues_text = issues_match.group(1).strip()
        issue_lines = [l for l in issues_text.split("\n") if l.strip()]
        shown = 0
        for line in issue_lines:
            if shown >= 5:
                remaining = len(issue_lines) - shown
                if remaining > 0:
                    print(f"│ ... and {remaining} more issues")
                break
            print(f"│ {line.strip()[:70]}")
            shown += 1
    else:
        # Fallback: show first 10 lines
        for line in output.split("\n")[:10]:
            print(f"│ {line[:70]}")

    print(f"├{border}┤")
    print(f"│ Full review: .claude/reviews/latest.md")
    print(f"│ To apply: tell Claude to read .claude/reviews/latest.md")
    print(f"│ Or use: /second-opinion")
    print(f"└{border}┘")


# ─── Main ────────────────────────────────────────────────────────────────────


def find_transcript(cwd: str) -> str | None:
    """Try to find the active transcript JSONL file."""
    # Claude Code stores transcripts in ~/.claude/projects/
    # The path encodes the project directory
    home = os.path.expanduser("~")
    project_key = cwd.replace("/", "-")
    projects_dir = os.path.join(home, ".claude", "projects", project_key)

    if not os.path.isdir(projects_dir):
        return None

    # Find the most recently modified .jsonl file
    jsonl_files = []
    try:
        for fname in os.listdir(projects_dir):
            if fname.endswith(".jsonl"):
                fpath = os.path.join(projects_dir, fname)
                jsonl_files.append((os.path.getmtime(fpath), fpath))
    except OSError:
        return None

    if not jsonl_files:
        return None

    jsonl_files.sort(reverse=True)
    return jsonl_files[0][1]


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Second Opinion code review hook")
    parser.add_argument("--transcript", help="Path to transcript JSONL file")
    parser.add_argument("--cwd", help="Working directory (overrides auto-detection)")
    parser.add_argument("--force", action="store_true", help="Bypass cooldown and length checks")
    parser.add_argument("--backend", help="Override configured backend")
    args = parser.parse_args()

    # Determine mode: hook (stdin JSON) or manual (CLI args)
    stdin_data = {}
    hook_mode = not sys.stdin.isatty()

    if hook_mode:
        try:
            raw = sys.stdin.read()
            stdin_data = json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, OSError):
            stdin_data = {}

    # Determine working directory
    cwd = args.cwd or stdin_data.get("cwd", os.getcwd())

    # Load config
    config = load_config(cwd)

    # Skip logic
    force = args.force
    skip_reason = should_skip(config, stdin_data, force=force)
    if skip_reason:
        # Silent skip — don't pollute transcript
        return

    # Find transcript
    transcript_path = args.transcript or stdin_data.get("transcript_path")
    if not transcript_path:
        transcript_path = find_transcript(cwd)

    # Extract context
    context = extract_context(
        transcript_path,
        config.get("max_context_messages", 20),
        config.get("max_context_chars", 30000),
    )

    if not context.strip():
        # Nothing meaningful to review
        return

    # Build prompt
    prompt = build_review_prompt(context, config)

    # Dispatch to backend
    backend_name = args.backend or config.get("backend", "opencode")
    success, output = dispatch_review(prompt, config, backend_override=args.backend)

    # Write review
    write_review(cwd, output, backend_name, success)

    # Print summary
    print_summary(output, backend_name, success)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never block Claude Code — always exit 0
        pass
    sys.exit(0)
