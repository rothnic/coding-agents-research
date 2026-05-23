#!/usr/bin/env python3
"""Higher-level planner/replanner loop composed with local_agent_loop_v0 worker loop."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent_loop_v0 import read_json, run, write_json


TERMINAL = {"DONE", "FAILED"}
UNBLOCKED = {"OPEN", "CLAIMED", "PLANNED", "EXECUTING", "VALIDATING"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def count_tasks(tasks: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "open": sum(1 for t in tasks if t.get("state") == "OPEN"),
        "unblocked": sum(1 for t in tasks if t.get("state") in UNBLOCKED),
        "blocked": sum(1 for t in tasks if t.get("state") == "BLOCKED"),
        "terminal": sum(1 for t in tasks if t.get("state") in TERMINAL),
    }


def maybe_replan(program: dict[str, Any], queue: dict[str, Any], min_open: int, timeout_s: int) -> bool:
    tasks = queue.get("tasks", [])
    c = count_tasks(tasks)
    now = datetime.now(timezone.utc)
    last = program.get("last_roadmap_review_ts")
    elapsed = timeout_s + 1
    if last:
        elapsed = int((now - datetime.fromisoformat(last)).total_seconds())

    needs_review = c["open"] < min_open or c["unblocked"] == 0 or elapsed >= timeout_s
    if not needs_review:
        return False

    backlog = program.get("roadmap_backlog", [])
    generated = 0
    while backlog and c["open"] < min_open:
        item = backlog.pop(0)
        queue.setdefault("tasks", []).append(
            {
                "id": item["id"],
                "title": item["title"],
                "state": "OPEN",
                "retries": 0,
                "required_checks": ["test", "lint"],
            }
        )
        c["open"] += 1
        generated += 1

    # simplistic unblocking: convert one blocked task back to OPEN if none unblocked
    if c["unblocked"] == 0:
        for task in queue.get("tasks", []):
            if task.get("state") == "BLOCKED":
                task["state"] = "OPEN"
                task["unblocked_by"] = "program-loop"
                break

    program["last_roadmap_review_ts"] = utc_now()
    program.setdefault("review_log", []).append(
        {
            "ts": program["last_roadmap_review_ts"],
            "reason": {
                "open_below_threshold": c["open"] < min_open,
                "no_unblocked_tasks": c["unblocked"] == 0,
                "timeout_elapsed": elapsed >= timeout_s,
            },
            "generated_tasks": generated,
        }
    )
    return True


def run_program(program_file: Path, queue_file: Path, artifacts: Path, min_open: int, timeout_s: int, max_retries: int) -> None:
    program = read_json(program_file)
    queue = read_json(queue_file)

    changed = maybe_replan(program, queue, min_open=min_open, timeout_s=timeout_s)
    if changed:
        write_json(queue_file, queue)
        write_json(program_file, program)

    # compose with worker loop
    run(queue_file=queue_file, artifacts_root=artifacts, max_retries=max_retries)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run higher-level roadmap + worker composed loop")
    parser.add_argument("--program", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--min-open", type=int, default=2)
    parser.add_argument("--roadmap-timeout-sec", type=int, default=300)
    parser.add_argument("--max-retries", type=int, default=2)
    args = parser.parse_args()

    run_program(
        program_file=args.program,
        queue_file=args.queue,
        artifacts=args.artifacts,
        min_open=args.min_open,
        timeout_s=args.roadmap_timeout_sec,
        max_retries=args.max_retries,
    )


if __name__ == "__main__":
    main()
