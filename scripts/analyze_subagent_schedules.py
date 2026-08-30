#!/usr/bin/env python3
"""Analyze Rivumi subagent schedule traces from run event logs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rivumi.subagents import analyze_subagent_schedule_jsonl


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events_jsonl", type=Path, help="Path to a Rivumi events.jsonl file.")
    args = parser.parse_args()
    analysis = analyze_subagent_schedule_jsonl(args.events_jsonl)
    print(json.dumps(analysis.as_dict(), indent=2, sort_keys=True))
    return 0 if analysis.trace_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
