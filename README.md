# Second Opinion

A Claude Code plugin that automatically sends Claude's plans and code to another AI model for review. Reviews are surfaced in the transcript view and can be injected back into Claude's context.

## How It Works

When Claude Code stops (completes a task or pauses), the Stop hook:
1. Reads the conversation transcript
2. Formats it as context for review
3. Sends it to a configured backend model (opencode, codex, gemini, or custom)
4. Writes the review to `.claude/reviews/latest.md`
5. Prints a summary in the transcript

## Prerequisites

- Python 3.8+
- At least one backend CLI installed:

| Backend | Command | Install |
|---------|---------|---------|
| OpenCode | `opencode` | [github.com/opencode-ai/opencode](https://github.com/opencode-ai/opencode) |
| Codex | `codex` | [github.com/openai/codex](https://github.com/openai/codex) |
| Gemini CLI | `gemini` | [github.com/google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli) |
| Custom | any CLI | Configure in config |

## Installation

### Quick Install

```bash
cd your-project
bash /path/to/second-opinion/install.sh .
```

### Manual Install

1. Copy `.claude/hooks/second-opinion.py` to your project
2. Copy `.claude/second-opinion.config.json` to your project
3. Copy `.claude/commands/second-opinion.md` to your project
4. Add the hook to `.claude/settings.local.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 $CLAUDE_PROJECT_DIR/.claude/hooks/second-opinion.py",
            "timeout": 600
          }
        ]
      }
    ]
  }
}
```

5. Add `.claude/reviews/` to `.gitignore`

## Configuration

Edit `.claude/second-opinion.config.json`:

| Option | Default | Description |
|--------|---------|-------------|
| `enabled` | `true` | Enable/disable the plugin |
| `auto_review_on_stop` | `true` | Auto-review on every Stop event |
| `backend` | `"opencode"` | Which backend to use |
| `max_context_messages` | `20` | Max transcript messages to include |
| `max_context_chars` | `30000` | Max characters of context |
| `timeout` | `300` | Backend timeout in seconds |
| `cooldown` | `30` | Min seconds between reviews |
| `min_assistant_length` | `200` | Skip review if assistant response is shorter |
| `skip_patterns` | `[...]` | Regex patterns to skip (matched against user message) |
| `review_language` | `"en"` | Language for the review output |

### Backend Configuration

Each backend in `backends` has:
- `command` — CLI executable name
- `args_template` — Arguments list; `{prompt}` is replaced with the review prompt
- `env` — Extra environment variables

## Usage

### Automatic (default)

Reviews happen automatically when Claude Code stops. Look for the summary box in the transcript.

### Manual Review

```bash
python3 .claude/hooks/second-opinion.py --transcript /path/to/transcript.jsonl --cwd . --force
```

### Read the Review

In Claude Code, use the `/second-opinion` slash command, or tell Claude:

> Read .claude/reviews/latest.md and address the issues found.

### CLI Flags

| Flag | Description |
|------|-------------|
| `--transcript PATH` | Path to JSONL transcript file |
| `--cwd PATH` | Working directory override |
| `--force` | Bypass cooldown and length checks |
| `--backend NAME` | Override configured backend |

## Security Notes

- The transcript context is sent to the configured backend CLI, which forwards it to its respective AI service
- Review files are written locally and excluded from git by default
- No network calls are made directly — all communication goes through the backend CLI
- The hook always exits with code 0 to never block Claude Code

## Troubleshooting

**Hook doesn't run:**
- Verify `.claude/settings.local.json` has the Stop hook registered
- Check `enabled` is `true` in config
- Check cooldown hasn't been hit (delete `.claude/reviews/.last_run` to reset)

**Backend not found:**
- Ensure the CLI is installed and on your PATH
- Run `which opencode` (or your backend) to verify

**Empty reviews:**
- Transcript may be too short — check `min_assistant_length`
- Try `--force` flag to bypass skip checks

**Review quality:**
- Increase `max_context_messages` and `max_context_chars` for more context
- Try a different backend for different perspectives
