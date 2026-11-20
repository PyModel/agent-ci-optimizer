#!/usr/bin/env python3
"""Run every bundled test and the repository-only policy tests."""
from pathlib import Path
import os
import sys
import unittest


def main():
    root = Path(__file__).resolve().parent.parent
    bundle = root / "skills/ci-optimiser"
    # Preserve the installed suite's normal `python -m unittest` import/cwd
    # contract even when this repository runner is launched from the root.
    sys.path.insert(0, str(bundle))
    os.chdir(bundle)
    suite = unittest.TestSuite(
        unittest.TestLoader().discover(str(directory))
        for directory in (bundle / "tests", root / ".github/tests")
    )
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
