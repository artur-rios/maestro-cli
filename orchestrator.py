#!/usr/bin/env python3
"""Entry point: python orchestrator.py"""

from __future__ import annotations

import sys

if sys.version_info < (3, 10):
    sys.exit("This tool requires Python 3.10 or newer.")

from agent_orchestrator.shell.repl import main

if __name__ == "__main__":
    raise SystemExit(main())
