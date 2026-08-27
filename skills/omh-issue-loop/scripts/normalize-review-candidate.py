#!/usr/bin/env python3
"""Normalize scope-approved untracked review candidates with exact-path intent-to-add."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from normalization_protocol import normalize_review_candidate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--allowed-paths-json", type=Path, required=True)
    parser.add_argument("--round-id", required=True)
    args = parser.parse_args()
    try:
        result = normalize_review_candidate(
            run_dir=args.run_dir,
            repository=args.repository,
            allowed_paths_json=args.allowed_paths_json,
            round_id=args.round_id,
        )
    except Exception as error:
        result = {"classification": "failed", "reasons": [str(error)]}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["classification"] == "confirmed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
