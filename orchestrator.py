#!/usr/bin/env python3
"""AI Agent Orchestrator.

Drives coding-agent CLIs (claude, codex, opencode) through a
plan -> execute -> review -> merge workflow on a git branch. One agent writes
the plan, another implements it a checklist item at a time, and a third reviews
the diff and merges it.

Run without arguments for the interactive shell, or use a subcommand:

    python orchestrator.py                        # interactive shell
    python orchestrator.py new "add rate limits"
    python orchestrator.py runs
    python orchestrator.py --help
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 10):
    sys.exit("This tool requires Python 3.10 or newer.")

from agent_orchestrator.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
