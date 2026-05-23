#!/usr/bin/env python3
"""Tiny local Symphony-like event-loop proof of concept.

No cloud dependencies. Uses local JSON files as a beads-like queue substrate.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTIVE_STATES = {"CLAIMED", "PLANNED", "EXECUTING", "VALIDATING"}


@dataclass
class Config:
    max_retries: int = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def append_event(events_file: Path, task_id: str, from_state: str, to_state: str, reason: str) -> None:
    event = {
        "ts": utc_now(),
        "task_id": task_id,
        "from_state": from_state,
        "to_state": to_state,
        "actor": "loop-runner",
        "reason": reason,
    }
    with events_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def transition(task: dict[str, Any], to_state: str, reason: str, events_file: Path) -> None:
    from_state = task["state"]
    task["state"] = to_state
    append_event(events_file, task["id"], from_state, to_state, reason)


def validation_check(task: dict[str, Any]) -> tuple[bool, str]:
    # deterministic induced failure for demonstration
    fail_once = task.get("simulate_validation_fail_once", False)
    has_failed_once = task.get("_failed_once", False)
    if fail_once and not has_failed_once:
        task["_failed_once"] = True
        return False, "Simulated first validation failure"
    return True, "All required checks passed"


def write_artifacts(task: dict[str, Any], task_dir: Path, validation_result: dict[str, Any] | None = None) -> None:
    plan = task_dir / "plan.md"
    if not plan.exists():
        plan.write_text(
            "# Plan\n\n"
            "1. Parse task intent.\n"
            "2. Execute minimal local action(s).\n"
            "3. Run required validation gate(s).\n"
            "4. Produce result summary.\n"
        )
    if validation_result is not None:
        write_json(task_dir / "validation.json", validation_result)


def execute_task(task: dict[str, Any], artifacts_root: Path, cfg: Config) -> dict[str, Any]:
    task_id = task["id"]
    task_dir = artifacts_root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    events_file = task_dir / "events.ndjson"

    if task["state"] == "OPEN":
        transition(task, "CLAIMED", "Worker claimed task", events_file)
    if task["state"] == "CLAIMED":
        transition(task, "PLANNED", "Plan created", events_file)
        write_artifacts(task, task_dir)
    if task["state"] == "PLANNED":
        transition(task, "EXECUTING", "Execution started", events_file)
    if task["state"] == "EXECUTING":
        transition(task, "VALIDATING", "Execution finished; entering validation", events_file)

    if task["state"] == "VALIDATING":
        passed, message = validation_check(task)
        validation_result = {
            "task_id": task_id,
            "passed": passed,
            "message": message,
            "required_checks": task.get("required_checks", ["test", "lint"]),
            "attempt": task.get("retries", 0) + 1,
            "timestamp": utc_now(),
        }
        write_artifacts(task, task_dir, validation_result)

        if passed:
            transition(task, "DONE", "Validation passed", events_file)
        else:
            retries = task.get("retries", 0) + 1
            task["retries"] = retries
            if retries <= cfg.max_retries:
                transition(task, "EXECUTING", f"Validation failed; retry {retries}", events_file)
                transition(task, "VALIDATING", "Retry execution finished; re-validating", events_file)
                passed_retry, message_retry = validation_check(task)
                validation_result["attempt"] = retries + 1
                validation_result["passed"] = passed_retry
                validation_result["message"] = message_retry
                write_artifacts(task, task_dir, validation_result)
                if passed_retry:
                    transition(task, "DONE", "Validation passed after retry", events_file)
                else:
                    transition(task, "FAILED", "Validation failed after retry budget", events_file)
            else:
                transition(task, "FAILED", "Validation failed; retry budget exhausted", events_file)

    (task_dir / "result.md").write_text(
        f"# Task Result\n\n"
        f"- Task: `{task_id}`\n"
        f"- Final state: `{task['state']}`\n"
        f"- Retries used: `{task.get('retries', 0)}`\n"
        f"- Updated: `{utc_now()}`\n"
    )
    return task


def run(queue_file: Path, artifacts_root: Path, max_retries: int) -> None:
    queue = read_json(queue_file)
    tasks = queue.get("tasks", [])
    cfg = Config(max_retries=max_retries)

    for i, task in enumerate(tasks):
        if task.get("state") in {"DONE", "FAILED", "BLOCKED"}:
            continue
        if task.get("state") == "OPEN" or task.get("state") in ACTIVE_STATES:
            tasks[i] = execute_task(task, artifacts_root, cfg)

    queue["tasks"] = tasks
    write_json(queue_file, queue)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local-agent-loop-v0 proof of concept")
    parser.add_argument("--queue", type=Path, required=True, help="Path to queue JSON")
    parser.add_argument("--artifacts", type=Path, required=True, help="Artifact output directory")
    parser.add_argument("--max-retries", type=int, default=2)
    args = parser.parse_args()
    run(args.queue, args.artifacts, args.max_retries)


if __name__ == "__main__":
    main()
