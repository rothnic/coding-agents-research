#!/usr/bin/env python3
"""Tiny local Symphony-like event-loop proof of concept.

No cloud dependencies. Uses local JSON files as a beads-like queue substrate.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "local-agent-loop-v0.3"
ACTIVE_STATES = {"CLAIMED", "PLANNED", "EXECUTING", "VALIDATING"}
TERMINAL_STATES = {"DONE", "FAILED"}
TASK_STATES = {"OPEN", "BLOCKED", *ACTIVE_STATES, *TERMINAL_STATES}
REQUIRED_TASK_ARTIFACTS = ("plan.md", "events.ndjson", "validation.json", "result.md")
TASK_TIMESTAMP_FIELDS = ("deadline_ts", "unblocked_ts", "processed_ts")


@dataclass
class Config:
    max_retries: int = 2


class ValidationError(RuntimeError):
    """Raised when local loop JSON inputs are invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"{path}: file not found") from exc
    except JSONDecodeError as exc:
        raise ValidationError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def parse_iso8601(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_iso8601_field(value: Any, label: str, errors: list[str]) -> None:
    try:
        parse_iso8601(value)
    except (TypeError, ValueError) as exc:
        errors.append(f"{label}: invalid ISO timestamp: {exc}")


def validate_schema_version(document: Any, label: str, errors: list[str]) -> None:
    if not isinstance(document, dict):
        return
    version = document.get("schema_version")
    if version != SCHEMA_VERSION:
        errors.append(
            f"{label}.schema_version: unsupported schema version {version!r}; "
            f"expected {SCHEMA_VERSION!r}"
        )


def validate_string_list(value: Any, label: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{label}: expected list[str], got {type(value).__name__}")
        return []

    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_label = f"{label}[{index}]"
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{item_label}: expected non-empty string")
            continue
        if item in seen:
            errors.append(f"{item_label}: duplicate value {item!r}")
        seen.add(item)
        result.append(item)
    return result


def validate_task_document(
    task: Any,
    label: str,
    seen_ids: set[str],
    errors: list[str],
) -> str | None:
    if not isinstance(task, dict):
        errors.append(f"{label}: expected object, got {type(task).__name__}")
        return None

    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        errors.append(f"{label}.id: expected non-empty string")
        task_id = None
    elif task_id in seen_ids:
        errors.append(f"{label}.id: duplicate task id {task_id!r}")
    else:
        seen_ids.add(task_id)

    title = task.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        errors.append(f"{label}.title: expected non-empty string when present")

    state = task.get("state")
    if state not in TASK_STATES:
        errors.append(
            f"{label}.state: invalid state {state!r}; expected one of {sorted(TASK_STATES)}"
        )

    retries = task.get("retries", 0)
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        errors.append(f"{label}.retries: expected non-negative integer")

    if "required_checks" in task:
        validate_string_list(task["required_checks"], f"{label}.required_checks", errors)

    if "depends_on" in task:
        dependencies = validate_string_list(task["depends_on"], f"{label}.depends_on", errors)
        if task_id is not None and task_id in dependencies:
            errors.append(f"{label}.depends_on: task cannot depend on itself")

    for field in TASK_TIMESTAMP_FIELDS:
        if field in task:
            validate_iso8601_field(task[field], f"{label}.{field}", errors)

    if "simulate_validation_fail_once" in task and not isinstance(
        task["simulate_validation_fail_once"], bool
    ):
        errors.append(f"{label}.simulate_validation_fail_once: expected boolean")

    return task_id if isinstance(task_id, str) else None


def validate_queue_document(queue: Any, label: str = "queue") -> list[str]:
    errors: list[str] = []
    if not isinstance(queue, dict):
        return [f"{label}: expected object, got {type(queue).__name__}"]

    validate_schema_version(queue, label, errors)
    tasks = queue.get("tasks")
    if not isinstance(tasks, list):
        errors.append(f"{label}.tasks: expected list")
        return errors

    seen_ids: set[str] = set()
    for index, task in enumerate(tasks):
        validate_task_document(task, f"{label}.tasks[{index}]", seen_ids, errors)

    return errors


def format_validation_errors(title: str, errors: list[str]) -> str:
    return title + ":\n" + "\n".join(f"- {error}" for error in errors)


def require_valid_queue(queue: Any, label: str = "queue") -> None:
    errors = validate_queue_document(queue, label=label)
    if errors:
        raise ValidationError(format_validation_errors("Queue validation failed", errors))


def append_event(events_file: Path, task_id: str, from_state: str, to_state: str, reason: str) -> None:
    event = {
        "schema_version": SCHEMA_VERSION,
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
            "schema_version": SCHEMA_VERSION,
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

    if task["state"] in TERMINAL_STATES:
        task["processed_by"] = "local-agent-loop-v0"
        task["processed_ts"] = utc_now()

    (task_dir / "result.md").write_text(
        f"# Task Result\n\n"
        f"- Task: `{task_id}`\n"
        f"- Final state: `{task['state']}`\n"
        f"- Retries used: `{task.get('retries', 0)}`\n"
        f"- Updated: `{utc_now()}`\n"
    )
    return task


def queue_counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total_count": len(tasks),
        "open_count": sum(1 for task in tasks if task.get("state") == "OPEN"),
        "active_count": sum(1 for task in tasks if task.get("state") in ACTIVE_STATES),
        "blocked_count": sum(1 for task in tasks if task.get("state") == "BLOCKED"),
        "done_count": sum(1 for task in tasks if task.get("state") == "DONE"),
        "failed_count": sum(1 for task in tasks if task.get("state") == "FAILED"),
    }


def run(
    queue_file: Path,
    artifacts_root: Path,
    max_retries: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    queue = read_json(queue_file)
    require_valid_queue(queue, label=str(queue_file))
    tasks = queue.get("tasks", [])
    cfg = Config(max_retries=max_retries)
    runnable_ids = [
        task["id"]
        for task in tasks
        if task.get("state") == "OPEN" or task.get("state") in ACTIVE_STATES
    ]
    summary = {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry-run" if dry_run else "run",
        "queue_file": str(queue_file),
        "artifacts": str(artifacts_root),
        "runnable_task_ids": runnable_ids,
        "counts_before": queue_counts(tasks),
    }
    if dry_run:
        summary["counts_after"] = summary["counts_before"]
        return summary

    for i, task in enumerate(tasks):
        if task.get("state") in {"DONE", "FAILED", "BLOCKED"}:
            continue
        if task.get("state") == "OPEN" or task.get("state") in ACTIVE_STATES:
            tasks[i] = execute_task(task, artifacts_root, cfg)

    queue["tasks"] = tasks
    require_valid_queue(queue, label=f"{queue_file} after run")
    write_json(queue_file, queue)
    summary["counts_after"] = queue_counts(tasks)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local-agent-loop-v0 worker")
    parser.add_argument("--queue", type=Path, required=True, help="Path to queue JSON")
    parser.add_argument("--artifacts", type=Path, required=True, help="Artifact output directory")
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true", help="Validate and summarize without writing")
    args = parser.parse_args(argv)

    try:
        summary = run(
            queue_file=args.queue,
            artifacts_root=args.artifacts,
            max_retries=args.max_retries,
            dry_run=args.dry_run,
        )
    except ValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
