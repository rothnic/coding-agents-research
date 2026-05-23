#!/usr/bin/env python3
"""Higher-level planner/replanner loop composed with local_agent_loop_v0 worker loop.

Adds program-level artifacts and reason-aware unblocking policies.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent_loop_v0 import read_json, run, write_json


TERMINAL = {"DONE", "FAILED"}
UNBLOCKED = {"OPEN", "CLAIMED", "PLANNED", "EXECUTING", "VALIDATING"}
UNBLOCK_POLICIES = {
    "waiting_on_external": "request_sync_and_reopen",
    "needs_clarification": "create_clarification_task_and_reopen",
    "missing_dependency": "create_dependency_task_and_reopen",
    "default": "manual_review_then_reopen",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def count_tasks(tasks: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "open": sum(1 for t in tasks if t.get("state") == "OPEN"),
        "unblocked": sum(1 for t in tasks if t.get("state") in UNBLOCKED),
        "blocked": sum(1 for t in tasks if t.get("state") == "BLOCKED"),
        "terminal": sum(1 for t in tasks if t.get("state") in TERMINAL),
    }


def write_program_event(artifacts: Path, event: dict[str, Any]) -> None:
    artifacts.mkdir(parents=True, exist_ok=True)
    out = artifacts / "roadmap_events.ndjson"
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def choose_unblock_policy(task: dict[str, Any]) -> str:
    reason = task.get("blocked_reason", "default")
    return UNBLOCK_POLICIES.get(reason, UNBLOCK_POLICIES["default"])


def maybe_replan(program: dict[str, Any], queue: dict[str, Any], min_open: int, timeout_s: int) -> dict[str, Any] | None:
    tasks = queue.get("tasks", [])
    c = count_tasks(tasks)
    now = datetime.now(timezone.utc)
    last = program.get("last_roadmap_review_ts")
    elapsed = timeout_s + 1
    if last:
        elapsed = int((now - datetime.fromisoformat(last)).total_seconds())

    reason_flags = {
        "open_below_threshold": c["open"] < min_open,
        "no_unblocked_tasks": c["unblocked"] == 0,
        "timeout_elapsed": elapsed >= timeout_s,
    }
    needs_review = any(reason_flags.values())
    if not needs_review:
        return None

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
    unblocked_task_id = None
    unblock_policy = None
    if reason_flags["no_unblocked_tasks"]:
        for task in queue.get("tasks", []):
            if task.get("state") == "BLOCKED":
                unblock_policy = choose_unblock_policy(task)
                task["state"] = "OPEN"
                task["unblocked_by"] = "program-loop"
                task["unblock_policy"] = unblock_policy
                unblocked_task_id = task.get("id")
                break

    program["last_roadmap_review_ts"] = utc_now()
    review_entry = {
        "ts": program["last_roadmap_review_ts"],
        "reason": reason_flags,
        "generated_tasks": generated,
        "unblocked_task_id": unblocked_task_id,
        "unblock_policy": unblock_policy,
    }
    program.setdefault("review_log", []).append(review_entry)
    return review_entry


def run_program(program_file: Path, queue_file: Path, artifacts: Path, min_open: int, timeout_s: int, max_retries: int) -> None:
    program = read_json(program_file)
    queue = read_json(queue_file)

    review_entry = maybe_replan(program, queue, min_open=min_open, timeout_s=timeout_s)
    if review_entry is not None:
        write_json(queue_file, queue)
        write_json(program_file, program)
        write_program_event(
            artifacts,
            {
                "ts": program["last_roadmap_review_ts"],
                "program_id": program.get("program_id", "unknown"),
                "type": "ROADMAP_REVIEW",
                "review": review_entry,
            },
        )

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
