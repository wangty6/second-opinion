Run a second-opinion code review using a **reviewer teammate** (agent team).

## Steps

### 1. Prep — extract context and build prompt

```bash
python3 $CLAUDE_PROJECT_DIR/.claude/hooks/second-opinion.py --cwd $CLAUDE_PROJECT_DIR --force --prep-only
```

If the user provided `--backend <name>`, append it to the command above.

This outputs a JSON object like `{"prompt_file": "...", "backend": "openrouter"}`. Capture both values.

If the script exits with no output or prints a `[second-opinion] Skipped:` message, inform the user and stop.

### 2. Spawn a reviewer teammate

Create a team called `second-opinion` using TeamCreate, then spawn a **background** teammate with the Agent tool:

- **team_name**: `"second-opinion"`
- **name**: `"reviewer"`
- **run_in_background**: `true`
- **mode**: `"bypassPermissions"`
- **prompt**: Tell the teammate to:
  1. Run the dispatch command:
     ```
     python3 <project_dir>/.claude/hooks/second-opinion.py --dispatch <prompt_file> --cwd <project_dir> [--backend <backend>]
     ```
     (substitute the actual paths and backend from step 1)
  2. After the command completes, read `.claude/reviews/latest.md`
  3. Send the full review content back to the lead using SendMessage
  4. Then go idle (the lead will handle shutdown)

### 3. Continue working

Tell the user: "Review dispatched to teammate. I'll present the results when the reviewer reports back."

Continue with whatever the user was doing. When the reviewer teammate sends back the review results:

1. Present the findings — for each issue show severity, location, and suggested fix
2. Ask which issues the user wants to address
3. Send a shutdown_request to the "reviewer" teammate
4. Run TeamDelete to clean up
