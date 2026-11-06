#!/usr/bin/env python3
"""Backwards-compatible wrapper: run only the Dune/IMAX watch entry.

Delegates to check.py with --watch dune-imax; any other flags pass through.

Usage:
    python check_dune.py --dry-run
    python check_dune.py --force
"""

import sys

from check import main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "--watch", "dune-imax", *sys.argv[1:]]
    main()
