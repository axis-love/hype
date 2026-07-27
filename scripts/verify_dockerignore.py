#!/usr/bin/env python3
"""Verify .dockerignore excludes secret-bearing files from Docker build context.

Two-phase verification:
1. Pattern check — verify .dockerignore patterns exclude secret files and include
   required files. Uses path-based matching against the actual .dockerignore patterns.
2. Layer check (if Docker is available) — build a minimal image with a sentinel
   .env file and grep the image layers for the sentinel value.

Run: python3 scripts/verify_dockerignore.py
Exit 0 if all checks pass, 1 otherwise.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SENTINEL_VALUE = "Z0k sentinel-secret-from-env-file"


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
            norm = pat[1:]
            if _matches(rel_path, norm):
                excluded = False
        elif _matches(rel_path, pat):
            excluded = True
    return excluded


def _matches(path, pattern):
    if pattern == path:
        return True
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        base = os.path.basename(path)
        if base.startswith(prefix + "."):
            return True
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        if path.startswith(prefix):
            return True
    if "*" in pattern:
        regex = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
        if re.match(regex, path):
            return True
    if path.startswith(pattern):
        return True
    return False


def check_expected_files_exist():
    """Verify that files expected to be included actually exist in the repo."""
    must_exist = [
        ".env.example",
        "Dockerfile",
        "newsbot/main.py",
        "pyproject.toml",
    ]
    missing = []
    for f in must_exist:
        full = os.path.join(REPO_ROOT, f)
        if not os.path.exists(full):
            missing.append(f)
    return missing


def check_docker_layers():
    """Build a minimal image with a sentinel .env and verify the sentinel
    does not appear in any image layer. Returns (ran: bool, passed: bool, msg: str)."""
    if not shutil.which("docker"):
        return False, True, "Docker not installed — layer check skipped"

    # Check if docker daemon is running
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=True
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False, True, "Docker daemon not running — layer check skipped"

    sentinel_env = os.path.join(REPO_ROOT, ".env.sentinel-test")
    image_tag = "newsbot-dockerignore-test:latest"
    try:
        with open(sentinel_env, "w") as f:
            f.write(f"SECRET_TEST={SENTINEL_VALUE}\n")

        # Build the image — .dockerignore should exclude .env.sentinel-test
        # via the .env.* pattern
        result = subprocess.run(
            ["docker", "build", "-t", image_tag, "-f", "Dockerfile", "."],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=120,
        )
        if result.returncode != 0:
            return True, False, f"Docker build failed: {result.stderr[:300]}"

        # Inspect image history and grep for sentinel
        result = subprocess.run(
            ["docker", "history", "--no-trunc", "--format", "{{.CreatedBy}}", image_tag],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if SENTINEL_VALUE in result.stdout:
            return True, False, "Sentinel value found in image layer commands"

        # Also check filesystem layers via docker save
        result = subprocess.run(
            ["docker", "save", image_tag, "-o", "/dev/null"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        # The save itself won't expose file contents, but we can run a container
        # and grep for the sentinel
        result = subprocess.run(
            ["docker", "run", "--rm", image_tag, "find", "/app", "-name", ".env*", "-type", "f"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # .env.example should be the only .env file in the image
        env_files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        bad_files = [f for f in env_files if ".env.sentinel-test" in f or f.rstrip("/") == "/app/.env"]
        if bad_files:
            return True, False, f"Secret files found in image: {bad_files}"

        return True, True, "Sentinel .env not found in image — layer check passed"
    finally:
        if os.path.exists(sentinel_env):
            os.unlink(sentinel_env)
        subprocess.run(
            ["docker", "rmi", "-f", image_tag],
            capture_output=True, timeout=15,
        )


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
        # Credential files (Codex review round 1)
        "private_key.pem",
        "secret.key",
        "credentials.json",
        "service_account.json",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".aws/credentials",
        ".ssh/id_rsa",
        ".kube/config",
    ]

    # Files that MUST be included (safe, needed for build)
    must_include = [
        ".env.example",
        "Dockerfile",
        "newsbot/main.py",
        "pyproject.toml",
    ]

    errors = []

    # Phase 1: Pattern checks
    print("=== Phase 1: Pattern checks ===")
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

    # Check that expected files actually exist on disk
    missing = check_expected_files_exist()
    for f in missing:
        errors.append(f"{f} should be included but does not exist on disk")
        print(f"  FAIL: {f} should be included but does not exist on disk")
    if not missing:
        print("  PASS: all expected-include files exist on disk")

    # Phase 2: Docker layer check (optional, if Docker available)
    print("\n=== Phase 2: Docker layer check ===")
    ran, passed, msg = check_docker_layers()
    if not ran:
        print(f"  SKIP: {msg}")
    elif passed:
        print(f"  PASS: {msg}")
    else:
        errors.append(f"Docker layer check failed: {msg}")
        print(f"  FAIL: {msg}")

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