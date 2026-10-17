#!/usr/bin/env python3
"""Validate a RazorVid training-library JSONL manifest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from multicam_pipeline.training_library import load_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="Path to a JSONL training manifest")
    args = parser.parse_args()

    count = sum(1 for _ in load_manifest(args.manifest))
    print(f"Valid training manifest: {count} example(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())