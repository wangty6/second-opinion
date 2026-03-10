#!/usr/bin/env python3
"""Second Opinion — Claude Code Stop hook that sends plans/code to another model for review."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone

def progress(msg: str) -> None:
    """Print progress to stderr (visible in terminal, not in Claude transcript)."""
    print(f"[second-opinion] {msg}", file=sys.stderr, flush=True)


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
    config = copy.deepcopy(DEFAULTS)
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


def _truncate(text: str, max_len: int = 500) -> str:
    text = text.strip()
    if len(text) > max_len:
        return text[:max_len] + "\n... (truncated)"
    return text


def extract_context(transcript_path: str, max_messages: int, max_chars: int) -> str:
    """Parse a JSONL transcript file and extract formatted context.

    Supports two formats:
    1. Simple: {"role": "user"|"assistant"|"tool", "content": ...}
    2. Claude Code: {"type": "user"|"assistant"|..., "message": {"role": ..., "content": ...}}
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""

    try:
        with open(transcript_path, "r") as f:
            tail = deque(f, maxlen=max_messages * 3)
    except OSError:
        return ""

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
                            entries.append(f"[TOOL RESULT]\n{_truncate(text)}")
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
                entries.append(f"[TOOL RESULT]\n{_truncate(text)}")

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


# ─── File Content Reader ────────────────────────────────────────────────

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
    ".c", ".cpp", ".h", ".rb", ".sh", ".bash", ".zsh",
    ".md", ".json", ".yaml", ".yml", ".toml",
}

SKIP_DIRS = {"__pycache__", "node_modules", ".git"}

MAX_FILE_SIZE = 100 * 1024  # 100KB


def extract_file_content(paths: list[str], max_chars: int, cwd: str | None = None) -> str:
    """Read files/directories and format as reviewable context."""
    sections: list[str] = []
    budget = max_chars
    rel_base = cwd or os.getcwd()

    def _add_file(filepath: str, display_path: str) -> None:
        nonlocal budget
        if budget <= 0:
            return
        # Skip files that are too large or binary
        try:
            size = os.path.getsize(filepath)
            if size > MAX_FILE_SIZE or size == 0:
                return
        except OSError:
            return

        try:
            with open(filepath, "r", encoding="utf-8", errors="strict") as f:
                content = f.read(budget)
        except (OSError, UnicodeDecodeError):
            return  # skip binary / unreadable files

        if len(content) > budget:
            content = content[:budget] + "\n... (truncated)"
        sections.append(f"[FILE: {display_path}]\n{content}")
        budget -= len(content)

    for path in paths:
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(rel_base, path)
        path = os.path.abspath(path)

        if os.path.isfile(path):
            # Explicit file path: include regardless of extension (user chose it)
            _add_file(path, os.path.relpath(path, rel_base))
        elif os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                # Prune skipped directories
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                dirs.sort()
                for fname in sorted(files):
                    _, ext = os.path.splitext(fname)
                    if ext not in CODE_EXTENSIONS:
                        continue
                    fpath = os.path.join(root, fname)
                    _add_file(fpath, os.path.relpath(fpath, rel_base))
                    if budget <= 0:
                        break
                if budget <= 0:
                    break
        else:
            progress(f"Warning: path not found: {path}")

    return "\n\n".join(sections)


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
        with open(last_run_path) as f:
            last_run = float(f.read().strip())
        elapsed = time.time() - last_run
        if elapsed < cooldown:
            return f"cooldown ({int(cooldown - elapsed)}s remaining)"
    except (FileNotFoundError, ValueError, OSError):
        pass

    return None


# ─── Prompt Builder ──────────────────────────────────────────────────────────


def build_review_prompt(context: str, config: dict, mode: str = "transcript") -> str:
    """Build the review prompt to send to the backend model."""
    lang = config.get("review_language", "en")
    lang_instruction = f"\nRespond in: {lang}" if lang != "en" else ""

    if mode == "files":
        intro = "Review the following code files."
        context_label = "CODE FILES:"
    else:
        intro = "Review the following Claude Code session transcript."
        context_label = "SESSION TRANSCRIPT:"

    return f"""You are a senior software engineer conducting a code review.

{intro} Focus on:
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

{context_label}

{context}"""


# ─── Backend Dispatcher ─────────────────────────────────────────────────────


def _heartbeat(stop_event: threading.Event, backend_name: str) -> None:
    """Emit periodic progress messages while waiting for backend response."""
    start = time.time()
    while not stop_event.wait(10):
        elapsed = int(time.time() - start)
        progress(f"  Waiting for {backend_name}... ({elapsed}s)")


def dispatch_review(prompt: str, config: dict, cwd: str, backend_override: str | None = None) -> tuple:
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
        reviews_dir = os.path.join(cwd, ".claude", "reviews")
        os.makedirs(reviews_dir, exist_ok=True)
        prompt_file = open(
            os.path.join(reviews_dir, ".prompt.tmp"), "w"
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

    stop_event = threading.Event()
    beat = threading.Thread(target=_heartbeat, args=(stop_event, backend_name), daemon=True)
    beat.start()
    try:
        result = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
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
        stop_event.set()
        beat.join(timeout=2)
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
    """Print detailed review to stderr (terminal) and a brief pointer to stdout (Claude transcript)."""
    border = "─" * 50

    # ── Detailed view on stderr (visible in terminal, not paraphrased by Claude) ──
    print(f"\n┌{border}┐", file=sys.stderr)
    print(f"│ Second Opinion Review ({backend_name})", file=sys.stderr)
    print(f"├{border}┤", file=sys.stderr)

    if not success:
        print(f"│ STATUS: FAILED", file=sys.stderr)
        for line in output.split("\n")[:5]:
            print(f"│ {line[:70]}", file=sys.stderr)
        print(f"└{border}┘", file=sys.stderr)
    else:
        for line in output.split("\n"):
            print(f"│ {line[:70]}", file=sys.stderr)
        print(f"└{border}┘", file=sys.stderr)

    # ── Minimal stdout (Claude sees this — just a pointer, not the full review) ──
    status = "completed" if success else "FAILED"
    print(f"Second Opinion review {status}. Read .claude/reviews/latest.md for details.")


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
    parser.add_argument(
        "--prep-only", action="store_true",
        help="Prep only: skip checks, extract context, save prompt to file, then exit",
    )
    parser.add_argument(
        "--dispatch", metavar="PROMPT_FILE",
        help="Dispatch only: read prompt from file, call backend, save review",
    )
    parser.add_argument(
        "--files", nargs="+",
        help="Review specific files/directories instead of transcript",
    )
    args = parser.parse_args()

    # ── Dispatch-only mode (called by teammate agent) ──────────────────────
    if args.dispatch:
        cwd = args.cwd or os.getcwd()
        config = load_config(cwd)
        backend_name = args.backend or config.get("backend", "opencode")

        prompt_file = args.dispatch
        try:
            with open(prompt_file) as f:
                prompt = f.read()
        except OSError as e:
            print(f"Error reading prompt file: {e}", file=sys.stderr)
            return

        progress(f"Requesting review from {backend_name}...")
        success, output = dispatch_review(prompt, config, cwd, backend_override=args.backend)
        progress("Review received. Saving...")

        write_review(cwd, output, backend_name, success)
        print_summary(output, backend_name, success)

        # Clean up prompt file
        try:
            os.unlink(prompt_file)
        except OSError:
            pass
        return

    # ── Normal / prep-only mode ────────────────────────────────────────────
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
        progress(f"Skipped: {skip_reason}")
        return

    # Determine review mode: file-based or transcript-based
    if args.files:
        progress(f"Reading {len(args.files)} file/directory path(s)...")
        context = extract_file_content(args.files, config.get("max_context_chars", 30000), cwd=cwd)
        review_mode = "files"
    else:
        # Find transcript
        transcript_path = args.transcript or stdin_data.get("transcript_path")
        if not transcript_path:
            transcript_path = find_transcript(cwd)
        if transcript_path:
            progress("Reading transcript...")
        context = extract_context(
            transcript_path,
            config.get("max_context_messages", 20),
            config.get("max_context_chars", 30000),
        )
        review_mode = "transcript"

    if not context.strip():
        progress("Nothing to review (empty context)")
        return

    # Build prompt
    prompt = build_review_prompt(context, config, mode=review_mode)
    backend_name = args.backend or config.get("backend", "opencode")

    # ── Prep-only: save prompt file and exit (for teammate mode) ───────
    if args.prep_only:
        reviews_dir = os.path.join(cwd, ".claude", "reviews")
        os.makedirs(reviews_dir, exist_ok=True)
        prompt_path = os.path.join(reviews_dir, ".pending-prompt.txt")
        with open(prompt_path, "w") as f:
            f.write(prompt)
        # Output the prompt path and backend on stdout for the caller
        output_data = {"prompt_file": prompt_path, "backend": backend_name}
        if args.files:
            output_data["files"] = args.files
        print(json.dumps(output_data))
        progress(f"Prompt saved ({len(prompt)} chars). Ready for teammate dispatch.")
        return

    # ── Full synchronous mode ──────────────────────────────────────────
    progress(f"Requesting review from {backend_name}...")
    success, output = dispatch_review(prompt, config, cwd, backend_override=args.backend)
    progress("Review received. Saving...")

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
