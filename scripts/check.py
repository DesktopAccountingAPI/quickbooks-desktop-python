"""Runs every check of this repository (`mise run check`; CI runs the same).

1. ``uv sync``: the pinned dev tools (pytest, ruff, mypy) in ``.venv``. Set ``UV_PYTHON`` to test
   another interpreter (``UV_PYTHON=3.9 mise run check``); uv downloads it if needed.
2. ``ruff check`` and ``ruff format --check``.
3. ``mypy --strict`` (configured in pyproject.toml) on the package, tests, examples and scripts.
4. ``pytest``: unit tests and the conformance suite (starts ``node conformance/mock-server.mjs``).
5. ``scripts/publish.py --dry-run``: builds the sdist and wheel, installs the wheel into a fresh
   virtual environment and runs the smoke program against it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(*args: str) -> None:
    print("+ " + " ".join(args), flush=True)
    started = time.monotonic()
    result = subprocess.run(args, cwd=ROOT, check=False)
    if result.returncode != 0:
        print(f"check failed: {' '.join(args)} (exit {result.returncode})", file=sys.stderr)
        sys.exit(result.returncode)
    print(f"  ok ({time.monotonic() - started:.1f} s)", flush=True)


def main() -> None:
    run("uv", "sync")
    run("uv", "run", "--no-sync", "python", "--version")
    run("uv", "run", "--no-sync", "ruff", "check", ".")
    run("uv", "run", "--no-sync", "ruff", "format", "--check", ".")
    run("uv", "run", "--no-sync", "mypy", "--strict")
    run("uv", "run", "--no-sync", "pytest")
    run(sys.executable, "scripts/publish.py", "--dry-run")
    print("all checks passed")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    main()
