Run the second-opinion review hook manually by executing this command:

```
python3 $CLAUDE_PROJECT_DIR/.claude/hooks/second-opinion.py --cwd $CLAUDE_PROJECT_DIR --force
```

After it completes, read `.claude/reviews/latest.md` and present the findings. For each issue, show the severity, location, and a suggested fix. Then ask which issues to address.

If the user provided arguments after the command (e.g. `--backend openrouter`), append them to the command above.
