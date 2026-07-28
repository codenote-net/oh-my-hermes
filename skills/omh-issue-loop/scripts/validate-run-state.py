#!/usr/bin/env python3
"""Validate new-run or resume-run preflight state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from worker_protocol import new_run_errors, resume_state_errors


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)
    new = subparsers.add_parser("new")
    new.add_argument("--repository", type=Path, required=True)
    new.add_argument("--branch", required=True)
    resume = subparsers.add_parser("resume")
    resume.add_argument("--repository", type=Path, required=True)
    resume.add_argument("--run-dir", type=Path, required=True)
    arguments = parser.parse_args()

    if arguments.mode == "new":
        errors = new_run_errors(arguments.repository.resolve(), arguments.branch)
    else:
        errors = resume_state_errors(arguments.run_dir.resolve(), arguments.repository.resolve())
    print(json.dumps({"valid": not errors, "errors": errors}, indent=2, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
