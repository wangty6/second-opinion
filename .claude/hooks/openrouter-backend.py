#!/usr/bin/env python3
"""OpenRouter backend wrapper for Second Opinion. Reads prompt from a file path argument."""

import json
import os
import sys
import urllib.request

def main():
    if len(sys.argv) < 2:
        print("Usage: openrouter-backend.py <prompt_or_filepath>", file=sys.stderr)
        sys.exit(1)

    prompt = sys.argv[1]

    # If the argument looks like a file path, read from it
    try:
        with open(prompt) as f:
            prompt = f.read()
    except (FileNotFoundError, IsADirectoryError, OSError):
        pass  # treat as literal prompt string

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("Error: OPENROUTER_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    model = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324")

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/second-opinion",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(text)
    except Exception as e:
        print(f"OpenRouter API error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
