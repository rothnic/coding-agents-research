#!/usr/bin/env python3
"""Higher-level planner/replanner loop composed with the worker loop.

Adds v0.3 durability: schema validation, deterministic recovery, artifact
integrity checks, idempotent task synthesis, and an operator CLI.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent_loop_v0 import (
    REQUIRED_TASK_ARTIFACTS,
    SCHEMA_VERSION,
    TERMINAL_STATES,
    TASK_STATES,
    WORKTREE_METADATA_ARTIFACT,
    WORKTREE_PROCESSED_BY,
    ValidationError,
    WorktreeConfig,
    WorktreeExecutionError,
    build_worktree_config,
    format_validation_errors,
    parse_iso8601,
    queue_counts,
    read_json,
    run,
    run_git,
    validate_iso8601_field,
    validate_queue_document,
    validate_string_list,
    write_json,
)


TERMINAL = TERMINAL_STATES
UNBLOCKED = {"OPEN", "CLAIMED", "PLANNED", "EXECUTING", "VALIDATING"}
PROGRAM_STATES = {
    "ROADMAP_REVIEWING",
    "TASK_SYNTHESIZING",
    "UNBLOCKING",
    "HANDING_OFF",
    "IDLE",
}
LEGAL_PROGRAM_TRANSITIONS = {
    "IDLE": {"ROADMAP_REVIEWING"},
    "ROADMAP_REVIEWING": {"TASK_SYNTHESIZING", "UNBLOCKING", "HANDING_OFF"},
    "TASK_SYNTHESIZING": {"UNBLOCKING", "HANDING_OFF"},
    "UNBLOCKING": {"HANDING_OFF"},
    "HANDING_OFF": {"IDLE"},
}
BLOCKED_REASON_ALIASES = {
    "": "default",
    "blocked": "default",
    "default": "default",
    "manual": "default",
    "manual_review": "default",
    "external": "waiting_on_external",
    "waiting": "waiting_on_external",
    "waiting_on_external": "waiting_on_external",
    "clarification": "needs_clarification",
    "needs_clarification": "needs_clarification",
    "dependency": "missing_dependency",
    "missing_dependency": "missing_dependency",
}
UNBLOCK_ACTION_MATRIX = {
    "waiting_on_external": {
        "pass": "request_sync_and_reopen",
        "fail": "wait_for_dependency_completion",
    },
    "needs_clarification": {
        "pass": "create_clarification_task_and_reopen",
        "fail": "wait_for_clarification_dependencies",
    },
    "missing_dependency": {
        "pass": "create_dependency_task_and_reopen",
        "fail": "wait_for_dependency_completion",
    },
    "default": {
        "pass": "manual_review_then_reopen",
        "fail": "wait_for_manual_review_dependencies",
    },
}
NO_DEADLINE = "9999-12-31T23:59:59+00:00"
NO_DEADLINE_DT = datetime.max.replace(tzinfo=timezone.utc)
PROGRAM_TIMESTAMP_FIELDS = (
    "last_roadmap_review_ts",
    "last_run_started_ts",
    "last_run_completed_ts",
    "last_recovery_ts",
)
BACKLOG_TIMESTAMP_FIELDS = ("deadline_ts",)
PROGRAM_ARTIFACTS = ("roadmap_events.ndjson", "roadmap_status.md", "program_metrics.json")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def count_tasks(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "open_count": 0,
        "unblocked_count": 0,
        "blocked_count": 0,
        "done_count": 0,
        "failed_count": 0,
        "terminal_count": 0,
        "total_count": len(tasks),
    }
    for task in tasks:
        state = task.get("state")
        if state == "OPEN":
            counts["open_count"] += 1
        if state in UNBLOCKED:
            counts["unblocked_count"] += 1
        if state == "BLOCKED":
            counts["blocked_count"] += 1
        if state == "DONE":
            counts["done_count"] += 1
        if state == "FAILED":
            counts["failed_count"] += 1
        if state in TERMINAL:
            counts["terminal_count"] += 1
    return counts


def validate_backlog_item(
    item: Any,
    label: str,
    seen_ids: set[str],
    errors: list[str],
) -> None:
    if not isinstance(item, dict):
        errors.append(f"{label}: expected object, got {type(item).__name__}")
        return

    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        errors.append(f"{label}.id: expected non-empty string")
    elif item_id in seen_ids:
        errors.append(f"{label}.id: duplicate roadmap item id {item_id!r}")
    else:
        seen_ids.add(item_id)

    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append(f"{label}.title: expected non-empty string")

    if "priority" in item and (
        not isinstance(item["priority"], int) or isinstance(item["priority"], bool)
    ):
        errors.append(f"{label}.priority: expected integer when present")

    if "required_checks" in item:
        validate_string_list(item["required_checks"], f"{label}.required_checks", errors)

    if "depends_on" in item:
        dependencies = validate_string_list(item["depends_on"], f"{label}.depends_on", errors)
        if isinstance(item_id, str) and item_id in dependencies:
            errors.append(f"{label}.depends_on: roadmap item cannot depend on itself")

    for field in BACKLOG_TIMESTAMP_FIELDS:
        if field in item:
            validate_iso8601_field(item[field], f"{label}.{field}", errors)


def validate_program_document(program: Any, label: str = "program") -> list[str]:
    errors: list[str] = []
    if not isinstance(program, dict):
        return [f"{label}: expected object, got {type(program).__name__}"]

    version = program.get("schema_version")
    if version != SCHEMA_VERSION:
        errors.append(
            f"{label}.schema_version: unsupported schema version {version!r}; "
            f"expected {SCHEMA_VERSION!r}"
        )

    program_id = program.get("program_id")
    if not isinstance(program_id, str) or not program_id.strip():
        errors.append(f"{label}.program_id: expected non-empty string")

    if "goal" in program and not isinstance(program["goal"], str):
        errors.append(f"{label}.goal: expected string when present")

    state = program.get("program_state")
    if state not in PROGRAM_STATES:
        errors.append(
            f"{label}.program_state: invalid state {state!r}; "
            f"expected one of {sorted(PROGRAM_STATES)}"
        )

    backlog = program.get("roadmap_backlog")
    if not isinstance(backlog, list):
        errors.append(f"{label}.roadmap_backlog: expected list")
    else:
        seen_ids: set[str] = set()
        for index, item in enumerate(backlog):
            validate_backlog_item(item, f"{label}.roadmap_backlog[{index}]", seen_ids, errors)

    if "review_log" in program and not isinstance(program["review_log"], list):
        errors.append(f"{label}.review_log: expected list when present")

    for field in PROGRAM_TIMESTAMP_FIELDS:
        if field in program:
            validate_iso8601_field(program[field], f"{label}.{field}", errors)

    return errors


def require_valid_inputs(program: Any, queue: Any, program_label: str, queue_label: str) -> None:
    errors = [
        *validate_program_document(program, label=program_label),
        *validate_queue_document(queue, label=queue_label),
    ]
    if errors:
        raise ValidationError(format_validation_errors("Input validation failed", errors))


def load_validated_inputs(
    program_file: Path,
    queue_file: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    program = read_json(program_file)
    queue = read_json(queue_file)
    require_valid_inputs(program, queue, str(program_file), str(queue_file))
    return program, queue


def write_program_event(artifacts: Path, event: dict[str, Any]) -> None:
    artifacts.mkdir(parents=True, exist_ok=True)
    out = artifacts / "roadmap_events.ndjson"
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


@dataclass
class ProgramRun:
    program: dict[str, Any]
    artifacts: Path
    run_started_ts: str = field(default_factory=utc_now)
    dry_run: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.state = str(self.program.get("program_state") or "IDLE")
        self.program["program_state"] = self.state

    def emit(self, event_type: str, **payload: Any) -> dict[str, Any]:
        from_state = payload.pop("from_program_state", self.state)
        to_state = payload.pop("to_program_state", self.state)
        event = {
            "schema_version": SCHEMA_VERSION,
            "ts": utc_now(),
            "run_started_ts": self.run_started_ts,
            "program_id": self.program.get("program_id", "unknown"),
            "type": event_type,
            "from_program_state": from_state,
            "to_program_state": to_state,
            **payload,
        }
        self.events.append(event)
        if not self.dry_run:
            write_program_event(self.artifacts, event)
        return event

    def transition(self, to_state: str, reason: str) -> bool:
        from_state = self.state
        if to_state not in PROGRAM_STATES:
            self.emit(
                "PROGRAM_TRANSITION_REJECTED",
                from_program_state=from_state,
                to_program_state=to_state,
                accepted=False,
                reason=f"Unknown program state: {to_state}",
            )
            return False
        if to_state not in LEGAL_PROGRAM_TRANSITIONS.get(from_state, set()):
            self.emit(
                "PROGRAM_TRANSITION_REJECTED",
                from_program_state=from_state,
                to_program_state=to_state,
                accepted=False,
                reason=reason,
                legal_next_states=sorted(LEGAL_PROGRAM_TRANSITIONS.get(from_state, set())),
            )
            return False

        self.state = to_state
        self.program["program_state"] = to_state
        self.emit(
            "PROGRAM_STATE_TRANSITION",
            from_program_state=from_state,
            to_program_state=to_state,
            accepted=True,
            reason=reason,
        )
        return True


def require_transition(runner: ProgramRun, to_state: str, reason: str) -> None:
    if not runner.transition(to_state, reason):
        raise RuntimeError(f"Illegal program transition: {runner.state} -> {to_state}")


def normalized_blocked_reason(task: dict[str, Any]) -> str:
    raw = task.get("blocked_reason_code") or task.get("blocked_reason") or "default"
    slug = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    code = BLOCKED_REASON_ALIASES.get(slug, "default")
    task["blocked_reason_code"] = code
    return code


def choose_unblock_action(reason_code: str, dependency_check: str) -> str:
    matrix = UNBLOCK_ACTION_MATRIX.get(reason_code, UNBLOCK_ACTION_MATRIX["default"])
    return matrix[dependency_check]


def dependency_check(task: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    depends_on = list(task.get("depends_on") or [])
    missing = [dep for dep in depends_on if dep not in by_id]
    unmet = [
        dep
        for dep in depends_on
        if dep in by_id and by_id[dep].get("state") != "DONE"
    ]
    override = bool(task.get("dependency_override") or task.get("override_dependencies"))
    passed = (not missing and not unmet) or override
    return {
        "dependency_check": "pass" if passed else "fail",
        "dependency_override": override,
        "depends_on": depends_on,
        "missing_dependencies": missing,
        "unmet_dependencies": unmet,
    }


def deadline_key(item: dict[str, Any]) -> tuple[datetime, str]:
    raw = item.get("deadline_ts")
    if not raw:
        return (NO_DEADLINE_DT, NO_DEADLINE)
    text = str(raw)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return (NO_DEADLINE_DT, text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed.astimezone(timezone.utc), text)


def synthesize_tasks(
    program: dict[str, Any],
    queue: dict[str, Any],
    min_open: int,
    counts: dict[str, int],
) -> list[dict[str, Any]]:
    backlog = list(program.get("roadmap_backlog") or [])
    queue_tasks = queue.setdefault("tasks", [])
    existing_ids = {
        task.get("id")
        for task in queue_tasks
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    ordered = sorted(
        enumerate(backlog),
        key=lambda pair: (-int(pair[1].get("priority") or 0), deadline_key(pair[1]), pair[0]),
    )
    selected_indexes: set[int] = set()
    generated: list[dict[str, Any]] = []
    open_count = counts["open_count"]

    for original_index, item in ordered:
        item_id = item["id"]
        if item_id in existing_ids:
            selected_indexes.add(original_index)
            continue
        if open_count >= min_open:
            break
        task = {
            "id": item_id,
            "title": item["title"],
            "state": "OPEN",
            "retries": 0,
            "required_checks": item.get("required_checks") or ["test", "lint"],
            "priority": int(item.get("priority") or 0),
        }
        if item.get("deadline_ts"):
            task["deadline_ts"] = item["deadline_ts"]
        if item.get("depends_on"):
            task["depends_on"] = list(item.get("depends_on") or [])
        queue_tasks.append(task)
        existing_ids.add(item_id)
        selected_indexes.add(original_index)
        generated.append(task)
        open_count += 1

    program["roadmap_backlog"] = [
        item for index, item in enumerate(backlog) if index not in selected_indexes
    ]
    return generated


def recover_interrupted_program_state(runner: ProgramRun) -> dict[str, Any] | None:
    if runner.state == "IDLE":
        return None
    previous_state = runner.state
    runner.state = "IDLE"
    runner.program["program_state"] = "IDLE"
    runner.program["last_recovery_ts"] = utc_now()
    recovery = {
        "recovered_from": previous_state,
        "recovered_to": "IDLE",
        "reason": "Recovered interrupted program state before starting a new review",
    }
    runner.emit(
        "PROGRAM_STATE_RECOVERY",
        from_program_state=previous_state,
        to_program_state="IDLE",
        accepted=True,
        reason=recovery["reason"],
    )
    return recovery


def review_blocked_tasks(queue: dict[str, Any], runner: ProgramRun) -> list[dict[str, Any]]:
    tasks = queue.get("tasks", [])
    by_id = {task["id"]: task for task in tasks}
    decisions: list[dict[str, Any]] = []

    for task in tasks:
        if task.get("state") != "BLOCKED":
            continue
        reason_code = normalized_blocked_reason(task)
        dep = dependency_check(task, by_id)
        action = choose_unblock_action(reason_code, dep["dependency_check"])
        reopened = dep["dependency_check"] == "pass"
        if reopened:
            task["state"] = "OPEN"
            task["unblocked_by"] = "program-loop"
            task["unblock_policy"] = action
            task["unblocked_ts"] = utc_now()

        decision = {
            "task_id": task.get("id"),
            "blocked_reason_code": reason_code,
            "action": action,
            "reopened": reopened,
            **dep,
        }
        decisions.append(decision)
        runner.emit("UNBLOCK_DECISION", decision=decision)

    return decisions


def parse_last_review_elapsed(program: dict[str, Any], timeout_s: int) -> int:
    last = program.get("last_roadmap_review_ts")
    if not last:
        return timeout_s + 1
    try:
        parsed = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - parsed).total_seconds())
    except ValueError:
        return timeout_s + 1


def build_reason_flags(
    program: dict[str, Any],
    queue: dict[str, Any],
    min_open: int,
    timeout_s: int,
) -> tuple[dict[str, bool], dict[str, int], int]:
    counts = count_tasks(queue.get("tasks", []))
    elapsed = parse_last_review_elapsed(program, timeout_s)
    reason_flags = {
        "open_below_threshold": counts["open_count"] < min_open,
        "no_unblocked_tasks": counts["unblocked_count"] == 0,
        "timeout_elapsed": elapsed >= timeout_s,
    }
    return reason_flags, counts, elapsed


def maybe_replan(
    program: dict[str, Any],
    queue: dict[str, Any],
    min_open: int,
    timeout_s: int,
    runner: ProgramRun,
) -> dict[str, Any] | None:
    reason_flags, counts_before, elapsed = build_reason_flags(program, queue, min_open, timeout_s)
    if not any(reason_flags.values()):
        return None

    review_ts = utc_now()
    review_entry: dict[str, Any] = {
        "ts": review_ts,
        "reason": reason_flags,
        "elapsed_since_last_review_sec": elapsed,
        "counts_before": counts_before,
        "generated_tasks": 0,
        "generated_task_ids": [],
        "unblock_decisions": [],
        "reopened_task_ids": [],
    }

    require_transition(runner, "TASK_SYNTHESIZING", "Roadmap review requires task synthesis")
    generated = synthesize_tasks(program, queue, min_open=min_open, counts=counts_before)
    review_entry["generated_tasks"] = len(generated)
    review_entry["generated_task_ids"] = [task["id"] for task in generated]
    runner.emit(
        "TASK_SYNTHESIS",
        generated_task_ids=review_entry["generated_task_ids"],
        ordering_policy="priority_desc_deadline_asc_fifo",
    )

    if reason_flags["no_unblocked_tasks"]:
        require_transition(runner, "UNBLOCKING", "No unblocked tasks are available")
        decisions = review_blocked_tasks(queue, runner)
        review_entry["unblock_decisions"] = decisions
        review_entry["reopened_task_ids"] = [
            decision["task_id"] for decision in decisions if decision["reopened"]
        ]

    program["last_roadmap_review_ts"] = review_ts
    program.setdefault("review_log", []).append(review_entry)
    runner.emit("ROADMAP_REVIEW", review=review_entry)
    return review_entry


def transition_to_handoff(runner: ProgramRun) -> None:
    if runner.state == "ROADMAP_REVIEWING":
        require_transition(runner, "HANDING_OFF", "Roadmap review did not require replanning")
    elif runner.state == "TASK_SYNTHESIZING":
        require_transition(runner, "HANDING_OFF", "Task synthesis complete")
    elif runner.state == "UNBLOCKING":
        require_transition(runner, "HANDING_OFF", "Unblocking review complete")


def task_sort_key(task: dict[str, Any]) -> tuple[int, int, tuple[datetime, str], str]:
    state_rank = 0 if task.get("state") in UNBLOCKED else 1
    return (
        state_rank,
        -int(task.get("priority") or 0),
        deadline_key(task),
        str(task.get("id", "")),
    )


def next_three_tasks(program: dict[str, Any], queue: dict[str, Any]) -> list[dict[str, Any]]:
    queue_tasks = [
        task
        for task in queue.get("tasks", [])
        if task.get("state") not in TERMINAL
    ]
    if queue_tasks:
        return sorted(queue_tasks, key=task_sort_key)[:3]

    backlog = []
    for item in (program.get("roadmap_backlog") or [])[:3]:
        backlog.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "state": "BACKLOG",
                "priority": item.get("priority", 0),
                "deadline_ts": item.get("deadline_ts"),
            }
        )
    return backlog


def build_metrics(
    program: dict[str, Any],
    queue: dict[str, Any],
    review_entry: dict[str, Any] | None,
    run_started_ts: str,
) -> dict[str, Any]:
    counts = count_tasks(queue.get("tasks", []))
    total = counts["total_count"] or 1
    generated = review_entry.get("generated_tasks", 0) if review_entry else 0
    reopened = len(review_entry.get("reopened_task_ids", [])) if review_entry else 0
    return {
        "schema_version": SCHEMA_VERSION,
        "program_id": program.get("program_id", "unknown"),
        "program_state": program.get("program_state", "unknown"),
        "run_started_ts": run_started_ts,
        "updated_ts": utc_now(),
        "roadmap_backlog_count": len(program.get("roadmap_backlog") or []),
        "review_log_count": len(program.get("review_log") or []),
        "open_count": counts["open_count"],
        "blocked_count": counts["blocked_count"],
        "unblocked_count": counts["unblocked_count"],
        "generated_count": generated,
        "reopened_count": reopened,
        "done_count": counts["done_count"],
        "failed_count": counts["failed_count"],
        "terminal_count": counts["terminal_count"],
        "total_count": counts["total_count"],
        "done_rate": counts["done_count"] / total,
        "failed_rate": counts["failed_count"] / total,
        "blocked_rate": counts["blocked_count"] / total,
        "unblocked_rate": counts["unblocked_count"] / total,
    }


def write_status_markdown(
    program: dict[str, Any],
    queue: dict[str, Any],
    metrics: dict[str, Any],
    artifacts: Path,
) -> None:
    blockers = [
        task
        for task in queue.get("tasks", [])
        if task.get("state") == "BLOCKED"
    ]
    next_tasks = next_three_tasks(program, queue)

    lines = [
        "# Roadmap Status",
        "",
        f"- Program: `{program.get('program_id', 'unknown')}`",
        f"- Schema: `{SCHEMA_VERSION}`",
        f"- State: `{program.get('program_state', 'unknown')}`",
        f"- Objective: {program.get('goal', 'unspecified')}",
        f"- Updated: `{metrics['updated_ts']}`",
        "",
        "## Progress",
        "",
        f"- Done: `{metrics['done_count']}`",
        f"- Failed: `{metrics['failed_count']}`",
        f"- Open: `{metrics['open_count']}`",
        f"- Blocked: `{metrics['blocked_count']}`",
        f"- Generated this run: `{metrics['generated_count']}`",
        "",
        "## Blockers",
        "",
    ]
    if blockers:
        for task in blockers:
            reason = task.get("blocked_reason_code") or task.get("blocked_reason", "default")
            deps = ", ".join(task.get("depends_on", [])) or "none"
            lines.append(f"- `{task.get('id')}`: {reason}; depends on {deps}")
    else:
        lines.append("- None")

    lines.extend(["", "## Next 3 Tasks", ""])
    if next_tasks:
        for task in next_tasks:
            priority = int(task.get("priority") or 0)
            deadline = task.get("deadline_ts") or "none"
            lines.append(
                f"- `{task.get('id')}` ({task.get('state')}): "
                f"{task.get('title', 'untitled')} - priority {priority}, deadline {deadline}"
            )
    else:
        lines.append("- None")

    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "roadmap_status.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_outcome_reports(
    program: dict[str, Any],
    queue: dict[str, Any],
    artifacts: Path,
    review_entry: dict[str, Any] | None,
    run_started_ts: str,
) -> dict[str, Any]:
    metrics = build_metrics(program, queue, review_entry, run_started_ts)
    write_json(artifacts / "program_metrics.json", metrics)
    write_status_markdown(program, queue, metrics, artifacts)
    return metrics


def read_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValidationError(
                    f"{path}:{line_number}: invalid NDJSON event: {exc.msg}"
                ) from exc
            if not isinstance(event, dict):
                raise ValidationError(f"{path}:{line_number}: expected event object")
            events.append(event)
    return events


def read_artifact_object(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        data = read_json(path)
    except ValidationError as exc:
        errors.append(str(exc))
        return None
    if not isinstance(data, dict):
        errors.append(f"{path}: expected object, got {type(data).__name__}")
        return None
    return data


def validate_event_metadata(
    event: dict[str, Any],
    label: str,
    errors: list[str],
    timestamp_fields: tuple[str, ...],
) -> None:
    if event.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"{label}: unsupported schema version {event.get('schema_version')!r}"
        )
    for field in timestamp_fields:
        if field not in event:
            errors.append(f"{label}: missing timestamp field {field!r}")
        else:
            validate_iso8601_field(event[field], f"{label}.{field}", errors)


def is_commit_sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in "0123456789abcdef" for char in value)
    )


def verify_worktree_metadata(
    task: dict[str, Any],
    task_dir: Path,
    errors: list[str],
) -> None:
    task_id = task["id"]
    metadata_path = task_dir / WORKTREE_METADATA_ARTIFACT
    if not metadata_path.exists():
        errors.append(f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: missing required artifact")
        return
    if metadata_path.stat().st_size == 0:
        errors.append(f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: artifact is empty")
        return

    metadata = read_artifact_object(metadata_path, errors)
    if metadata is None:
        return
    if metadata.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: unsupported schema version "
            f"{metadata.get('schema_version')!r}"
        )
    if metadata.get("task_id") != task_id:
        errors.append(f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: task_id mismatch")
    if not isinstance(metadata.get("branch_name"), str) or not metadata["branch_name"].strip():
        errors.append(f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: missing branch_name")
    elif task.get("worktree_branch") and metadata["branch_name"] != task.get("worktree_branch"):
        errors.append(f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: branch_name mismatch")
    if not isinstance(metadata.get("worktree_path"), str) or not metadata["worktree_path"].strip():
        errors.append(f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: missing worktree_path")
    elif task.get("worktree_path") and metadata["worktree_path"] != task.get("worktree_path"):
        errors.append(f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: worktree_path mismatch")
    if task.get("worktree_base_ref") and metadata.get("base_ref") != task.get("worktree_base_ref"):
        errors.append(f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: base_ref mismatch")
    if task.get("worktree_change_path") and metadata.get("change_path") != task.get("worktree_change_path"):
        errors.append(f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: change_path mismatch")
    if metadata.get("final_task_state") != task.get("state"):
        errors.append(
            f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: final task state mismatch"
        )
    validation_output = metadata.get("validation_output")
    if not isinstance(validation_output, dict):
        errors.append(
            f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: missing validation_output"
        )
    else:
        if validation_output.get("task_id") != task_id:
            errors.append(
                f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: validation task_id mismatch"
            )
        if task.get("state") == "DONE" and validation_output.get("passed") is not True:
            errors.append(
                f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: DONE task validation failed"
            )
        if task.get("state") == "FAILED" and validation_output.get("passed") is not False:
            errors.append(
                f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: FAILED task has passing validation"
            )

    commit_sha = metadata.get("commit_sha")
    if task.get("state") == "DONE":
        if not is_commit_sha(commit_sha):
            errors.append(
                f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: DONE task missing commit SHA"
            )
        if task.get("worktree_commit_sha") != commit_sha:
            errors.append(
                f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: commit SHA mismatch"
            )
        worktree_path_value = metadata.get("worktree_path")
        if not isinstance(worktree_path_value, str) or not worktree_path_value.strip():
            errors.append(
                f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: worktree path is invalid"
            )
        else:
            worktree_path = Path(worktree_path_value)
            if not (worktree_path / ".git").exists():
                errors.append(
                    f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: worktree path is not a git worktree"
                )
                return
            try:
                actual_branch = run_git(
                    worktree_path,
                    ["rev-parse", "--abbrev-ref", "HEAD"],
                    check=True,
                ).stdout.strip()
                actual_head = run_git(
                    worktree_path,
                    ["rev-parse", "HEAD"],
                    check=True,
                ).stdout.strip()
            except WorktreeExecutionError as exc:
                errors.append(
                    f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: git metadata check failed: {exc}"
                )
            else:
                if actual_branch != metadata.get("branch_name"):
                    errors.append(
                        f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: actual branch mismatch"
                    )
                if is_commit_sha(commit_sha) and actual_head != commit_sha:
                    errors.append(
                        f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: actual HEAD mismatch"
                    )
    if task.get("state") == "FAILED":
        if commit_sha is not None:
            errors.append(
                f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: FAILED task has commit SHA"
            )
        if task.get("worktree_commit_sha"):
            errors.append(f"artifacts/{task_id}: FAILED task has worktree_commit_sha")


def verify_task_artifacts(task: dict[str, Any], artifacts: Path) -> list[str]:
    errors: list[str] = []
    task_id = task["id"]
    task_dir = artifacts / task_id
    if not task_dir.is_dir():
        return [f"artifacts/{task_id}: missing processed task artifact directory"]

    for name in REQUIRED_TASK_ARTIFACTS:
        path = task_dir / name
        if not path.exists():
            errors.append(f"artifacts/{task_id}/{name}: missing required artifact")
        elif path.stat().st_size == 0:
            errors.append(f"artifacts/{task_id}/{name}: artifact is empty")

    validation_path = task_dir / "validation.json"
    if validation_path.exists():
        validation = read_artifact_object(validation_path, errors)
        if validation is not None:
            if validation.get("schema_version") != SCHEMA_VERSION:
                errors.append(
                    f"artifacts/{task_id}/validation.json: unsupported schema version "
                    f"{validation.get('schema_version')!r}"
                )
            if validation.get("task_id") != task_id:
                errors.append(f"artifacts/{task_id}/validation.json: task_id mismatch")
            if task["state"] == "DONE" and validation.get("passed") is not True:
                errors.append(f"artifacts/{task_id}/validation.json: DONE task did not pass")
            if task["state"] == "FAILED" and validation.get("passed") is not False:
                errors.append(f"artifacts/{task_id}/validation.json: FAILED task did not fail")

    if task.get("processed_by") == WORKTREE_PROCESSED_BY:
        verify_worktree_metadata(task, task_dir, errors)

    events_path = task_dir / "events.ndjson"
    if events_path.exists():
        try:
            events = read_ndjson(events_path)
        except ValidationError as exc:
            errors.append(str(exc))
            events = []
        if events:
            previous_to_state: str | None = None
            if events[-1].get("to_state") != task["state"]:
                errors.append(
                    f"artifacts/{task_id}/events.ndjson: last event does not match "
                    f"task state {task['state']!r}"
                )
            for index, event in enumerate(events):
                event_label = f"artifacts/{task_id}/events.ndjson[{index}]"
                validate_event_metadata(event, event_label, errors, ("ts",))
                if event.get("task_id") != task_id:
                    errors.append(f"{event_label}: task_id mismatch")
                if event.get("from_state") not in TASK_STATES:
                    errors.append(f"{event_label}: invalid from_state")
                if event.get("to_state") not in TASK_STATES:
                    errors.append(f"{event_label}: invalid to_state")
                if previous_to_state is not None and event.get("from_state") != previous_to_state:
                    errors.append(
                        f"{event_label}: transition chain break; from_state "
                        f"{event.get('from_state')!r} does not match previous "
                        f"to_state {previous_to_state!r}"
                    )
                previous_to_state = event.get("to_state")

    return errors


def verify_artifact_integrity(
    program: dict[str, Any],
    queue: dict[str, Any],
    artifacts: Path,
) -> list[str]:
    errors: list[str] = []
    for name in PROGRAM_ARTIFACTS:
        path = artifacts / name
        if not path.exists():
            errors.append(f"{path}: missing program-level artifact")
        elif path.stat().st_size == 0:
            errors.append(f"{path}: artifact is empty")

    metrics: dict[str, Any] | None = None
    metrics_path = artifacts / "program_metrics.json"
    if metrics_path.exists():
        metrics = read_artifact_object(metrics_path, errors)
        if metrics is not None:
            counts = count_tasks(queue.get("tasks", []))
            expected_metrics = {
                "schema_version": SCHEMA_VERSION,
                "program_id": program.get("program_id"),
                "program_state": program.get("program_state"),
                "roadmap_backlog_count": len(program.get("roadmap_backlog") or []),
                "review_log_count": len(program.get("review_log") or []),
                "open_count": counts["open_count"],
                "blocked_count": counts["blocked_count"],
                "unblocked_count": counts["unblocked_count"],
                "done_count": counts["done_count"],
                "failed_count": counts["failed_count"],
                "terminal_count": counts["terminal_count"],
                "total_count": counts["total_count"],
            }
            for field, expected in expected_metrics.items():
                if metrics.get(field) != expected:
                    errors.append(
                        f"{metrics_path}: {field}={metrics.get(field)!r}, "
                        f"expected {expected!r}"
                    )

    status_path = artifacts / "roadmap_status.md"
    if status_path.exists():
        status = status_path.read_text(encoding="utf-8")
        program_id = program.get("program_id", "unknown")
        if f"- Program: `{program_id}`" not in status:
            errors.append(f"{status_path}: program id does not match final state")
        if f"- State: `{program.get('program_state', 'unknown')}`" not in status:
            errors.append(f"{status_path}: program state does not match final state")

    events_path = artifacts / "roadmap_events.ndjson"
    if events_path.exists():
        try:
            events = read_ndjson(events_path)
        except ValidationError as exc:
            errors.append(str(exc))
            events = []
        for index, event in enumerate(events):
            event_label = f"{events_path}[{index}]"
            validate_event_metadata(event, event_label, errors, ("ts", "run_started_ts"))
            if event.get("program_id") != program.get("program_id"):
                errors.append(f"{event_label}: program_id mismatch")
            if event.get("from_program_state") not in PROGRAM_STATES:
                errors.append(f"{event_label}: invalid from_program_state")
            if event.get("to_program_state") not in PROGRAM_STATES:
                errors.append(f"{event_label}: invalid to_program_state")

    for task in queue.get("tasks", []):
        if task.get("state") in TERMINAL and task.get("processed_by") in {
            "local-agent-loop-v0",
            WORKTREE_PROCESSED_BY,
        }:
            errors.extend(verify_task_artifacts(task, artifacts))

    return errors


def summarize_status(
    program: dict[str, Any],
    queue: dict[str, Any],
    artifacts: Path | None = None,
) -> dict[str, Any]:
    tasks = queue.get("tasks", [])
    summary = {
        "schema_version": SCHEMA_VERSION,
        "program_id": program.get("program_id"),
        "program_state": program.get("program_state"),
        "roadmap_backlog_count": len(program.get("roadmap_backlog") or []),
        "review_log_count": len(program.get("review_log") or []),
        "queue_counts": queue_counts(tasks),
        "task_ids": [task.get("id") for task in tasks],
    }
    if artifacts is not None:
        summary["artifact_integrity_errors"] = verify_artifact_integrity(
            program, queue, artifacts
        )
    return summary


def build_worker_dry_run_summary(
    queue: dict[str, Any],
    queue_file: Path,
    artifacts: Path,
    execution_mode: str = "state-machine",
) -> dict[str, Any]:
    tasks = queue.get("tasks", [])
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry-run",
        "execution_mode": execution_mode,
        "queue_file": str(queue_file),
        "artifacts": str(artifacts),
        "runnable_task_ids": [
            task["id"]
            for task in tasks
            if task.get("state") in UNBLOCKED
        ],
        "counts_before": queue_counts(tasks),
        "counts_after": queue_counts(tasks),
    }


def run_program(
    program_file: Path,
    queue_file: Path,
    artifacts: Path,
    min_open: int,
    timeout_s: int,
    max_retries: int,
    dry_run: bool = False,
    review_only: bool = False,
    worktree_config: WorktreeConfig | None = None,
) -> dict[str, Any]:
    run_started_ts = utc_now()
    loaded_program, loaded_queue = load_validated_inputs(program_file, queue_file)
    program = copy.deepcopy(loaded_program) if dry_run else loaded_program
    queue = copy.deepcopy(loaded_queue) if dry_run else loaded_queue
    runner = ProgramRun(
        program=program,
        artifacts=artifacts,
        run_started_ts=run_started_ts,
        dry_run=dry_run,
    )
    recovery = recover_interrupted_program_state(runner)

    require_transition(runner, "ROADMAP_REVIEWING", "Evaluate roadmap review triggers")
    review_entry = maybe_replan(
        program,
        queue,
        min_open=min_open,
        timeout_s=timeout_s,
        runner=runner,
    )
    transition_to_handoff(runner)

    program["last_run_started_ts"] = run_started_ts
    require_valid_inputs(program, queue, "program after review", "queue after review")
    if not dry_run:
        write_json(queue_file, queue)
        write_json(program_file, program)

    worker_summary: dict[str, Any] | None = None
    if not review_only:
        if dry_run:
            worker_summary = build_worker_dry_run_summary(
                queue,
                queue_file,
                artifacts,
                "worktree" if worktree_config is not None else "state-machine",
            )
        else:
            worker_summary = run(
                queue_file=queue_file,
                artifacts_root=artifacts,
                max_retries=max_retries,
                worktree_config=worktree_config,
            )

    if not dry_run and not review_only:
        queue = read_json(queue_file)
    require_valid_inputs(program, queue, "program before completion", "queue before completion")
    require_transition(runner, "IDLE", "Worker loop complete")
    program["last_run_completed_ts"] = utc_now()
    require_valid_inputs(program, queue, "program after completion", "queue after completion")

    metrics: dict[str, Any] | None = None
    integrity_errors: list[str] = []
    if not dry_run:
        write_json(program_file, program)
        metrics = write_outcome_reports(program, queue, artifacts, review_entry, run_started_ts)
        integrity_errors = verify_artifact_integrity(program, queue, artifacts)
        if integrity_errors:
            raise ValidationError(
                format_validation_errors("Artifact integrity check failed", integrity_errors)
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry-run" if dry_run else "run",
        "execution_mode": "worktree" if worktree_config is not None else "state-machine",
        "review_only": review_only,
        "program_file": str(program_file),
        "queue_file": str(queue_file),
        "artifacts": str(artifacts),
        "recovery": recovery,
        "review_generated_task_ids": (
            review_entry.get("generated_task_ids", []) if review_entry else []
        ),
        "review_reopened_task_ids": (
            review_entry.get("reopened_task_ids", []) if review_entry else []
        ),
        "worker_summary": worker_summary,
        "metrics": metrics,
        "artifact_integrity_errors": integrity_errors,
    }


def add_program_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--program", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--min-open", type=int, default=2)
    parser.add_argument("--roadmap-timeout-sec", type=int, default=300)
    parser.add_argument("--max-retries", type=int, default=2)


def add_worktree_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--execution-mode",
        choices=("state-machine", "worktree"),
        default="state-machine",
        help="Use the default state-machine worker or git worktree-backed execution",
    )
    parser.add_argument("--worktree-repo", type=Path)
    parser.add_argument("--worktrees-dir", type=Path)
    parser.add_argument("--worktree-base-ref", default="HEAD")
    parser.add_argument("--worktree-branch-prefix", default="codex/local-agent-loop")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate local-agent-loop-v0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate program and queue JSON")
    validate_parser.add_argument("--program", type=Path, required=True)
    validate_parser.add_argument("--queue", type=Path, required=True)
    validate_parser.add_argument("--artifacts", type=Path)
    validate_parser.add_argument(
        "--check-artifacts",
        action="store_true",
        help="Also verify artifact integrity against final state",
    )

    worker_parser = subparsers.add_parser("run-worker", help="Run only the task worker loop")
    worker_parser.add_argument("--queue", type=Path, required=True)
    worker_parser.add_argument("--artifacts", type=Path, required=True)
    worker_parser.add_argument("--max-retries", type=int, default=2)
    worker_parser.add_argument("--dry-run", action="store_true")
    add_worktree_args(worker_parser)

    program_parser = subparsers.add_parser(
        "run-program",
        help="Run roadmap review and worker handoff",
    )
    add_program_args(program_parser)
    program_parser.add_argument("--dry-run", action="store_true")
    program_parser.add_argument(
        "--review-only",
        action="store_true",
        help="Run roadmap review and reporting without invoking the worker",
    )
    add_worktree_args(program_parser)

    status_parser = subparsers.add_parser("status", help="Print compact program status")
    status_parser.add_argument("--program", type=Path, required=True)
    status_parser.add_argument("--queue", type=Path, required=True)
    status_parser.add_argument("--artifacts", type=Path)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].startswith("--"):
        argv.insert(0, "run-program")
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "validate":
            if args.check_artifacts and args.artifacts is None:
                raise ValidationError("--check-artifacts requires --artifacts")
            program, queue = load_validated_inputs(args.program, args.queue)
            summary = summarize_status(
                program,
                queue,
                args.artifacts if args.check_artifacts else None,
            )
            if args.check_artifacts and summary["artifact_integrity_errors"]:
                raise ValidationError(
                    format_validation_errors(
                        "Artifact integrity check failed",
                        summary["artifact_integrity_errors"],
                    )
                )
            summary["valid"] = True
        elif args.command == "run-worker":
            worktree_config = build_worktree_config(
                args.execution_mode,
                args.worktree_repo,
                args.worktrees_dir,
                args.worktree_base_ref,
                args.worktree_branch_prefix,
            )
            summary = run(
                queue_file=args.queue,
                artifacts_root=args.artifacts,
                max_retries=args.max_retries,
                dry_run=args.dry_run,
                worktree_config=worktree_config,
            )
        elif args.command == "run-program":
            worktree_config = build_worktree_config(
                args.execution_mode,
                args.worktree_repo,
                args.worktrees_dir,
                args.worktree_base_ref,
                args.worktree_branch_prefix,
            )
            summary = run_program(
                program_file=args.program,
                queue_file=args.queue,
                artifacts=args.artifacts,
                min_open=args.min_open,
                timeout_s=args.roadmap_timeout_sec,
                max_retries=args.max_retries,
                dry_run=args.dry_run,
                review_only=args.review_only,
                worktree_config=worktree_config,
            )
        elif args.command == "status":
            program, queue = load_validated_inputs(args.program, args.queue)
            summary = summarize_status(program, queue, args.artifacts)
        else:
            parser.error(f"unknown command {args.command!r}")
            return 2
    except (ValidationError, WorktreeExecutionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
