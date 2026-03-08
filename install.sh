#!/usr/bin/env bash
set -euo pipefail

# Second Opinion Plugin — Installer
# Installs the hook, config, and slash command into the current project.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_DIR="${1:-.}"
TARGET_DIR="$(cd "$TARGET_DIR" && pwd)"

echo "Second Opinion Plugin — Installer"
echo "================================="
echo "Source:  $SCRIPT_DIR"
echo "Target:  $TARGET_DIR"
echo ""

# Create directories
mkdir -p "$TARGET_DIR/.claude/hooks"
mkdir -p "$TARGET_DIR/.claude/commands"
mkdir -p "$TARGET_DIR/.claude/reviews"

# Copy hook script
cp "$SCRIPT_DIR/.claude/hooks/second-opinion.py" "$TARGET_DIR/.claude/hooks/second-opinion.py"
chmod +x "$TARGET_DIR/.claude/hooks/second-opinion.py"
echo "✓ Installed hook script"

# Copy config (don't overwrite existing)
if [ ! -f "$TARGET_DIR/.claude/second-opinion.config.json" ]; then
    cp "$SCRIPT_DIR/.claude/second-opinion.config.json" "$TARGET_DIR/.claude/second-opinion.config.json"
    echo "✓ Installed default config"
else
    echo "• Config already exists, skipping (check for new options in source)"
fi

# Copy slash command
cp "$SCRIPT_DIR/.claude/commands/review.md" "$TARGET_DIR/.claude/commands/review.md"
echo "✓ Installed /review command"

# Update .gitignore
GITIGNORE="$TARGET_DIR/.gitignore"
if [ -f "$GITIGNORE" ]; then
    if ! grep -q ".claude/reviews/" "$GITIGNORE" 2>/dev/null; then
        echo "" >> "$GITIGNORE"
        echo "# Second Opinion reviews" >> "$GITIGNORE"
        echo ".claude/reviews/" >> "$GITIGNORE"
        echo "✓ Updated .gitignore"
    else
        echo "• .gitignore already has reviews entry"
    fi
else
    echo ".claude/reviews/" > "$GITIGNORE"
    echo "✓ Created .gitignore"
fi

# Check for settings.json and provide hook registration guidance
SETTINGS="$TARGET_DIR/.claude/settings.local.json"
echo ""
echo "─── Hook Registration ───"
echo ""
if [ -f "$SETTINGS" ]; then
    echo "Found existing $SETTINGS"
    echo ""
    if grep -q "second-opinion" "$SETTINGS" 2>/dev/null; then
        echo "• Hook already registered in settings"
    else
        echo "Add this to your .claude/settings.local.json hooks section:"
        echo ""
        cat "$SCRIPT_DIR/settings-snippet.json"
        echo ""
        echo "Or merge manually. See settings-snippet.json for the full block."
    fi
else
    echo "No settings file found. Creating $SETTINGS with hook registration..."
    cp "$SCRIPT_DIR/settings-snippet.json" "$SETTINGS"
    echo "✓ Created settings with hook registration"
fi

# Check available backends
echo ""
echo "─── Backend Availability ───"
echo ""
for cmd in opencode codex gemini; do
    if command -v "$cmd" &>/dev/null; then
        echo "✓ $cmd — found at $(command -v "$cmd")"
    else
        echo "✗ $cmd — not found"
    fi
done

echo ""
echo "─── Done ───"
echo ""
echo "Edit .claude/second-opinion.config.json to configure backend and options."
echo "The hook will run automatically when Claude Code stops."
echo "Use /review in Claude Code to see the latest review."
