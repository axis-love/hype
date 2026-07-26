#!/usr/bin/env python3
"""Verify .dockerignore excludes secret-bearing files from Docker build context.

Run: python3 scripts/verify_dockerignore.py
Exit 0 if all checks pass, 1 otherwise.
"""
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dockerignore():
    path = os.path.join(REPO_ROOT, ".dockerignore")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    return lines


def is_excluded(rel_path, patterns):
    """Check if a path would be excluded by .dockerignore patterns."""
    excluded = False
    for pat in patterns:
        if pat.startswith("!"):
            # Negation — un-exclude
            norm = pat[1:]
            if _matches(rel_path, norm):
                excluded = False
        elif _matches(rel_path, pat):
            excluded = True
    return excluded


def _matches(path, pattern):
    # Simple glob-style matching for .dockerignore
    if pattern == path:
        return True
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        # .env.* matches .env.runtime, .env.local, etc.
        base = os.path.basename(path)
        if base.startswith(prefix + "."):
            return True
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        if path.startswith(prefix):
            return True
    if "*" in pattern:
        # Convert to regex for more complex patterns
        regex = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
        if re.match(regex, path):
            return True
    # Directory prefix match (e.g., deploy/docker/.env matches deploy/docker/.env.runtime)
    if path.startswith(pattern):
        return True
    return False


def main():
    patterns = load_dockerignore()

    # Files that MUST be excluded (contain secrets or runtime state)
    must_exclude = [
        ".env",
        ".env.runtime",
        ".env.local",
        ".env.production",
        "deploy/docker/.env",
        "deploy/docker/.env.runtime",
        "data/newsbot.sqlite",
        "deploy/docker/state/data/newsbot.sqlite",
    ]

    # Files that MUST be included (safe, needed for build)
    must_include = [
        ".env.example",
        "Dockerfile",
        "newsbot/main.py",
        "requirements.txt",
        "pyproject.toml",
    ]

    errors = []

    for f in must_exclude:
        if is_excluded(f, patterns):
            print(f"  PASS: {f} is excluded")
        else:
            errors.append(f"{f} should be excluded but is NOT")
            print(f"  FAIL: {f} should be excluded but is NOT")

    for f in must_include:
        if not is_excluded(f, patterns):
            print(f"  PASS: {f} is included")
        else:
            errors.append(f"{f} should be included but is NOT")
            print(f"  FAIL: {f} should be included but is NOT")

    if errors:
        print(f"\nFAILED: {len(errors)} check(s) failed")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("\nAll checks passed — no secrets in build context")
        sys.exit(0)


if __name__ == "__main__":
    main()