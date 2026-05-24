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
    COMMAND_BACKED_PATCH_ADAPTER,
    COMMAND_OUTPUTS_DIR,
    REQUIRED_TASK_ARTIFACTS,
    SCHEMA_VERSION,
    SUPPORTED_WORKTREE_ACTION_ADAPTERS,
    TERMINAL_STATES,
    TASK_STATES,
    WORKTREE_METADATA_ARTIFACT,
    WORKTREE_PROCESSED_BY,
    ValidationError,
    WorktreeConfig,
    WorktreeExecutionError,
    build_worktree_config,
    format_validation_errors,
    git_status_lines,
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
INTEGRATION_ARTIFACT = "integration.json"
INTEGRATION_REPORT_ARTIFACT = "integration_report.json"
INTEGRATION_EVENTS_ARTIFACT = "integration_events.ndjson"
INTEGRATION_MODES = {"off", "report-only", "local-merge"}
ACTIVE_INTEGRATION_MODES = INTEGRATION_MODES - {"off"}
INTEGRATION_STATES = {"NOT_REQUESTED", "READY", "MERGED", "SKIPPED", "FAILED", "BLOCKED"}
TERMINAL_INTEGRATION_STATES = {"MERGED", "SKIPPED", "FAILED", "BLOCKED"}


@dataclass(frozen=True)
class IntegrationPolicy:
    mode: str
    repo: Path
    target_base_ref: str
    allowed_branch_prefixes: tuple[str, ...]
    require_clean_target: bool = True
    allow_fast_forward: bool = True
    allow_merge_commit: bool = True


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
    integrations = integration_counts(queue.get("tasks", []))
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
        "integration_counts": integrations,
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
        "## Integration",
        "",
        f"- Candidates: `{metrics['integration_counts']['total_integration_candidates']}`",
        f"- Ready: `{metrics['integration_counts']['ready_count']}`",
        f"- Merged: `{metrics['integration_counts']['merged_count']}`",
        f"- Skipped: `{metrics['integration_counts']['skipped_count']}`",
        f"- Failed: `{metrics['integration_counts']['failed_count']}`",
        f"- Blocked: `{metrics['integration_counts']['blocked_count']}`",
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


def normalize_policy_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{label}: expected boolean")
    return value


def normalize_policy_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}: expected non-empty string")
    return value


def normalize_branch_prefixes(value: Any, label: str) -> tuple[str, ...]:
    prefixes = validate_string_list(value, label, [])
    if not prefixes:
        raise ValidationError(f"{label}: expected non-empty list[str]")
    return tuple(prefix.rstrip("/") + "/" for prefix in prefixes)


def load_integration_policy_overrides(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    policy = read_json(path)
    if not isinstance(policy, dict):
        raise ValidationError(f"{path}: expected integration policy object")
    version = policy.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValidationError(
            f"{path}.schema_version: unsupported schema version {version!r}; "
            f"expected {SCHEMA_VERSION!r}"
        )
    return policy


def build_integration_policy(
    mode: str,
    repo: Path | None,
    base_ref: str,
    branch_prefix: str,
    policy_path: Path | None = None,
) -> IntegrationPolicy | None:
    if mode not in INTEGRATION_MODES:
        raise ValidationError(f"unsupported integration mode {mode!r}")
    if mode == "off":
        return None
    if repo is None:
        raise ValidationError("--worktree-repo is required for integration")

    overrides = load_integration_policy_overrides(policy_path)
    target_base_ref = normalize_policy_string(
        overrides.get("target_base_ref", base_ref),
        "integration_policy.target_base_ref",
    )
    if mode == "local-merge" and target_base_ref == "HEAD":
        raise ValidationError(
            "local-merge integration requires a named target base branch"
        )

    default_prefix = branch_prefix.rstrip("/") + "/"
    allowed_prefixes = normalize_branch_prefixes(
        overrides.get("allowed_branch_prefixes", [default_prefix]),
        "integration_policy.allowed_branch_prefixes",
    )
    require_clean = normalize_policy_bool(
        overrides.get("require_clean_target", True),
        "integration_policy.require_clean_target",
    )
    allow_fast_forward = normalize_policy_bool(
        overrides.get("allow_fast_forward", True),
        "integration_policy.allow_fast_forward",
    )
    allow_merge_commit = normalize_policy_bool(
        overrides.get("allow_merge_commit", True),
        "integration_policy.allow_merge_commit",
    )
    if mode == "local-merge" and not (allow_fast_forward or allow_merge_commit):
        raise ValidationError(
            "local-merge integration policy must allow fast-forward or merge commits"
        )

    return IntegrationPolicy(
        mode=mode,
        repo=repo.resolve(),
        target_base_ref=target_base_ref,
        allowed_branch_prefixes=allowed_prefixes,
        require_clean_target=require_clean,
        allow_fast_forward=allow_fast_forward,
        allow_merge_commit=allow_merge_commit,
    )


def integration_counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "total_integration_candidates": 0,
        "not_requested_count": 0,
        "ready_count": 0,
        "merged_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
        "blocked_count": 0,
    }
    for task in tasks:
        is_candidate = (
            task.get("processed_by") == WORKTREE_PROCESSED_BY
            or bool(task.get("worktree_branch"))
            or bool(task.get("worktree_path"))
            or isinstance(task.get("local_action"), dict)
            or isinstance(task.get("action"), dict)
        )
        if not is_candidate:
            continue
        counts["total_integration_candidates"] += 1
        state = task.get("integration_state") or "NOT_REQUESTED"
        if state == "READY":
            counts["ready_count"] += 1
        elif state == "MERGED":
            counts["merged_count"] += 1
        elif state == "SKIPPED":
            counts["skipped_count"] += 1
        elif state == "FAILED":
            counts["failed_count"] += 1
        elif state == "BLOCKED":
            counts["blocked_count"] += 1
        else:
            counts["not_requested_count"] += 1
    return counts


def integration_task_candidates(queue: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        task
        for task in queue.get("tasks", [])
        if (
            task.get("processed_by") == WORKTREE_PROCESSED_BY
            or bool(task.get("worktree_branch"))
            or bool(task.get("worktree_path"))
            or isinstance(task.get("local_action"), dict)
            or isinstance(task.get("action"), dict)
        )
    ]


def git_repo_root(repo: Path) -> Path:
    result = run_git(repo, ["rev-parse", "--show-toplevel"], check=True)
    return Path(result.stdout.strip())


def git_is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return run_git(
        repo,
        ["merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
    ).returncode == 0


def current_ref_sha(repo: Path, ref: str) -> str:
    return run_git(repo, ["rev-parse", "--verify", ref], check=True).stdout.strip()


def branch_exists(repo: Path, branch: str) -> bool:
    return run_git(
        repo,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
    ).returncode == 0


def task_branch_allowed(branch: str, policy: IntegrationPolicy) -> bool:
    return any(branch.startswith(prefix) for prefix in policy.allowed_branch_prefixes)


def read_task_worktree_metadata(task: dict[str, Any], artifacts: Path) -> dict[str, Any] | None:
    task_id = task["id"]
    metadata_path = artifacts / task_id / WORKTREE_METADATA_ARTIFACT
    if not metadata_path.exists() or metadata_path.stat().st_size == 0:
        return None
    try:
        metadata = read_json(metadata_path)
    except ValidationError:
        return None
    return metadata if isinstance(metadata, dict) else None


def integration_record(
    task: dict[str, Any],
    policy: IntegrationPolicy,
    target_base_sha_before: str,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task.get("id"),
        "integration_mode": policy.mode,
        "base_ref": policy.target_base_ref,
        "base_sha_before": target_base_sha_before,
        "base_sha_after": target_base_sha_before,
        "task_branch": (
            task.get("worktree_branch")
            or (metadata or {}).get("branch_name")
        ),
        "task_commit_sha": (
            task.get("worktree_commit_sha")
            or (metadata or {}).get("commit_sha")
        ),
        "merge_commit_sha": None,
        "fast_forward": False,
        "conflict_diagnostics": [],
        "skipped_reasons": [],
        "final_integration_state": "NOT_REQUESTED",
    }


def finish_integration_record(
    record: dict[str, Any],
    state: str,
    base_sha_after: str,
    reasons: list[str] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    record["final_integration_state"] = state
    record["base_sha_after"] = base_sha_after
    if reasons:
        record["skipped_reasons"] = reasons
    if diagnostics:
        record["conflict_diagnostics"] = diagnostics
    return record


def record_task_integration(task: dict[str, Any], record: dict[str, Any]) -> None:
    state = str(record["final_integration_state"])
    task["integration_state"] = state
    task["integration_mode"] = record["integration_mode"]
    task["integration_base_ref"] = record["base_ref"]
    if isinstance(record.get("task_branch"), str):
        task["integration_task_branch"] = record["task_branch"]
    if is_commit_sha(record.get("task_commit_sha")):
        task["integration_task_commit_sha"] = record["task_commit_sha"]
    if is_commit_sha(record.get("merge_commit_sha")):
        task["integration_sha"] = record["merge_commit_sha"]
    elif state != "MERGED":
        task.pop("integration_sha", None)


def write_task_integration_artifact(
    artifacts: Path,
    task_id: str,
    record: dict[str, Any],
) -> None:
    write_json(artifacts / task_id / INTEGRATION_ARTIFACT, record)


def reusable_terminal_integration_record(
    artifacts: Path,
    task: dict[str, Any],
    policy: IntegrationPolicy,
    repo_root: Path,
    target_base_sha_before: str,
) -> dict[str, Any] | None:
    integration_state = task.get("integration_state")
    if integration_state not in TERMINAL_INTEGRATION_STATES:
        return None
    integration_path = artifacts / task["id"] / INTEGRATION_ARTIFACT
    if not integration_path.exists():
        return None
    existing = read_artifact_object(integration_path, [])
    if existing is None:
        return None
    task_commit = existing.get("task_commit_sha")
    merge_sha = existing.get("merge_commit_sha")
    if (
        existing.get("integration_mode") != policy.mode
        or existing.get("final_integration_state") != integration_state
        or existing.get("base_ref") != policy.target_base_ref
        or task_commit != task.get("worktree_commit_sha")
    ):
        return None
    if integration_state != "MERGED":
        return existing
    if (
        not is_commit_sha(task_commit)
        or not is_commit_sha(merge_sha)
        or not git_is_ancestor(repo_root, task_commit, target_base_sha_before)
    ):
        return None
    return existing


def integration_event_key(event: dict[str, Any]) -> tuple[Any, ...]:
    return (
        event.get("task_id"),
        event.get("integration_mode"),
        event.get("task_commit_sha"),
        event.get("merge_commit_sha"),
        event.get("final_integration_state"),
    )


def append_integration_event_once(artifacts: Path, record: dict[str, Any]) -> bool:
    events_path = artifacts / INTEGRATION_EVENTS_ARTIFACT
    existing = read_ndjson(events_path) if events_path.exists() else []
    event = {
        "schema_version": SCHEMA_VERSION,
        "ts": utc_now(),
        "type": "TASK_INTEGRATION",
        "task_id": record.get("task_id"),
        "integration_mode": record.get("integration_mode"),
        "base_ref": record.get("base_ref"),
        "task_branch": record.get("task_branch"),
        "task_commit_sha": record.get("task_commit_sha"),
        "merge_commit_sha": record.get("merge_commit_sha"),
        "final_integration_state": record.get("final_integration_state"),
        "skipped_reasons": record.get("skipped_reasons", []),
    }
    key = integration_event_key(event)
    if any(integration_event_key(existing_event) == key for existing_event in existing):
        return False
    artifacts.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")
    return True


def classify_integration_candidate(
    task: dict[str, Any],
    artifacts: Path,
    policy: IntegrationPolicy,
    repo_root: Path,
    target_base_sha_before: str,
    target_dirty_status: list[str],
) -> tuple[dict[str, Any], bool]:
    metadata = read_task_worktree_metadata(task, artifacts)
    record = integration_record(task, policy, target_base_sha_before, metadata)
    reasons: list[str] = []
    state = "FAILED"

    if task.get("state") != "DONE":
        reasons.append("task is not DONE")
        validation = read_artifact_object(artifacts / task["id"] / "validation.json", [])
        if validation and validation.get("passed") is False:
            reasons.append("validation failed")
        state = "SKIPPED"
    elif task.get("processed_by") != WORKTREE_PROCESSED_BY:
        reasons.append("task was not processed by the worktree executor")
        state = "SKIPPED"

    if reasons:
        current_base = current_ref_sha(repo_root, policy.target_base_ref)
        return finish_integration_record(record, state, current_base, reasons), False

    task_errors = verify_task_artifacts(task, artifacts)
    if task_errors:
        current_base = current_ref_sha(repo_root, policy.target_base_ref)
        reasons = [f"artifact integrity failed: {error}" for error in task_errors]
        if any("actual HEAD mismatch" in error for error in task_errors):
            reasons.insert(0, "task branch is ambiguous")
        return finish_integration_record(
            record,
            "FAILED",
            current_base,
            reasons,
        ), False

    if metadata is None:
        current_base = current_ref_sha(repo_root, policy.target_base_ref)
        return finish_integration_record(
            record,
            "FAILED",
            current_base,
            [f"missing {WORKTREE_METADATA_ARTIFACT}"],
        ), False

    validation = metadata.get("validation_output")
    if not isinstance(validation, dict) or validation.get("passed") is not True:
        current_base = current_ref_sha(repo_root, policy.target_base_ref)
        return finish_integration_record(
            record,
            "FAILED",
            current_base,
            ["validation failed"],
        ), False

    branch = record.get("task_branch")
    commit_sha = record.get("task_commit_sha")
    if not isinstance(branch, str) or not branch.strip():
        reasons.append("missing task branch")
    elif not task_branch_allowed(branch, policy):
        reasons.append("task branch is not allowed by integration policy")
    elif not branch_exists(repo_root, branch):
        reasons.append("task branch does not exist")

    if not is_commit_sha(commit_sha):
        reasons.append("missing task commit SHA")
    base_sha = metadata.get("base_sha")
    if not is_commit_sha(base_sha):
        reasons.append("missing task base SHA")

    if not reasons and isinstance(branch, str) and is_commit_sha(commit_sha):
        branch_head = current_ref_sha(repo_root, branch)
        if branch_head != commit_sha:
            reasons.append("task branch is ambiguous: branch head does not match task commit")

    if not reasons and is_commit_sha(base_sha) and is_commit_sha(commit_sha):
        if not git_is_ancestor(repo_root, base_sha, commit_sha):
            reasons.append("task branch is ambiguous: task commit is not based on recorded base")
        else:
            ahead_count = int(
                run_git(
                    repo_root,
                    ["rev-list", "--count", f"{base_sha}..{commit_sha}"],
                    check=True,
                ).stdout.strip()
                or "0"
            )
            if ahead_count != 1:
                reasons.append(
                    "task branch is ambiguous: expected exactly one task commit"
                )

    already_contains_task = (
        is_commit_sha(commit_sha)
        and git_is_ancestor(repo_root, str(commit_sha), target_base_sha_before)
    )
    if (
        not reasons
        and is_commit_sha(base_sha)
        and target_base_sha_before != base_sha
        and not already_contains_task
    ):
        return finish_integration_record(
            record,
            "BLOCKED",
            current_ref_sha(repo_root, policy.target_base_ref),
            ["target base moved unexpectedly"],
        ), False

    if not reasons and policy.require_clean_target and target_dirty_status:
        return finish_integration_record(
            record,
            "BLOCKED",
            current_ref_sha(repo_root, policy.target_base_ref),
            ["target repo is not clean"],
            [{"status": target_dirty_status}],
        ), False

    if reasons:
        return finish_integration_record(
            record,
            "FAILED",
            current_ref_sha(repo_root, policy.target_base_ref),
            reasons,
        ), False

    return record, True


def merge_integration_candidate(
    task: dict[str, Any],
    record: dict[str, Any],
    policy: IntegrationPolicy,
    repo_root: Path,
) -> dict[str, Any]:
    branch = str(record["task_branch"])
    commit_sha = str(record["task_commit_sha"])
    current_base = current_ref_sha(repo_root, policy.target_base_ref)
    if git_is_ancestor(repo_root, commit_sha, current_base):
        record["merge_commit_sha"] = current_base
        return finish_integration_record(record, "MERGED", current_base)

    if policy.allow_fast_forward and git_is_ancestor(repo_root, current_base, commit_sha):
        result = run_git(repo_root, ["merge", "--ff-only", branch], check=False)
        if result.returncode == 0:
            merged_sha = current_ref_sha(repo_root, policy.target_base_ref)
            record["merge_commit_sha"] = merged_sha
            record["fast_forward"] = True
            return finish_integration_record(record, "MERGED", merged_sha)
        diagnostics = [{"merge_command": result.as_dict()}]
        return finish_integration_record(
            record,
            "BLOCKED",
            current_ref_sha(repo_root, policy.target_base_ref),
            ["fast-forward merge failed"],
            diagnostics,
        )

    if not policy.allow_merge_commit:
        return finish_integration_record(
            record,
            "BLOCKED",
            current_base,
            ["merge commit is not allowed by integration policy"],
        )

    result = run_git(repo_root, ["merge", "--no-ff", "--no-edit", branch], check=False)
    if result.returncode == 0:
        merged_sha = current_ref_sha(repo_root, policy.target_base_ref)
        record["merge_commit_sha"] = merged_sha
        record["fast_forward"] = False
        return finish_integration_record(record, "MERGED", merged_sha)

    status = git_status_lines(repo_root)
    unmerged_paths = [
        line.strip()
        for line in run_git(
            repo_root,
            ["diff", "--name-only", "--diff-filter=U"],
            check=False,
        ).stdout.splitlines()
        if line.strip()
    ]
    abort_result = run_git(repo_root, ["merge", "--abort"], check=False)
    diagnostics = [
        {
            "merge_command": result.as_dict(),
            "status": status,
            "unmerged_paths": unmerged_paths,
            "abort_command": abort_result.as_dict(),
        }
    ]
    return finish_integration_record(
        record,
        "BLOCKED",
        current_ref_sha(repo_root, policy.target_base_ref),
        ["merge conflict"],
        diagnostics,
    )


def write_integration_report(
    artifacts: Path,
    policy: IntegrationPolicy,
    target_base_sha_before: str,
    target_base_sha_after: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    counts = {
        "ready_count": sum(1 for item in records if item["final_integration_state"] == "READY"),
        "merged_count": sum(1 for item in records if item["final_integration_state"] == "MERGED"),
        "skipped_count": sum(
            1 for item in records if item["final_integration_state"] == "SKIPPED"
        ),
        "failed_count": sum(1 for item in records if item["final_integration_state"] == "FAILED"),
        "blocked_count": sum(
            1 for item in records if item["final_integration_state"] == "BLOCKED"
        ),
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "integration_mode": policy.mode,
        "base_ref": policy.target_base_ref,
        "base_sha_before": target_base_sha_before,
        "base_sha_after": target_base_sha_after,
        "candidate_count": len(records),
        **counts,
        "task_results": [
            {
                "task_id": record["task_id"],
                "final_integration_state": record["final_integration_state"],
                "task_branch": record.get("task_branch"),
                "task_commit_sha": record.get("task_commit_sha"),
                "merge_commit_sha": record.get("merge_commit_sha"),
                "skipped_reasons": record.get("skipped_reasons", []),
                "conflict_diagnostics": record.get("conflict_diagnostics", []),
            }
            for record in records
        ],
    }
    write_json(artifacts / INTEGRATION_REPORT_ARTIFACT, report)
    return report


def integrate_completed_tasks(
    queue: dict[str, Any],
    queue_file: Path,
    artifacts: Path,
    policy: IntegrationPolicy,
) -> dict[str, Any]:
    repo_root = git_repo_root(policy.repo)
    if policy.mode == "local-merge":
        dirty_before_switch = git_status_lines(repo_root)
        if dirty_before_switch and policy.require_clean_target:
            target_dirty_status = dirty_before_switch
        else:
            run_git(repo_root, ["switch", policy.target_base_ref], check=True)
            target_dirty_status = git_status_lines(repo_root)
    else:
        target_dirty_status = git_status_lines(repo_root)

    target_base_sha_before = current_ref_sha(repo_root, policy.target_base_ref)
    records: list[dict[str, Any]] = []
    for task in integration_task_candidates(queue):
        reusable_record = reusable_terminal_integration_record(
            artifacts,
            task,
            policy,
            repo_root,
            target_base_sha_before,
        )
        if reusable_record is not None:
            records.append(reusable_record)
            continue

        record, eligible = classify_integration_candidate(
            task,
            artifacts,
            policy,
            repo_root,
            target_base_sha_before,
            target_dirty_status,
        )
        if eligible and policy.mode == "report-only":
            record = finish_integration_record(
                record,
                "READY",
                current_ref_sha(repo_root, policy.target_base_ref),
            )
        elif eligible and policy.mode == "local-merge":
            record = merge_integration_candidate(task, record, policy, repo_root)

        record_task_integration(task, record)
        write_task_integration_artifact(artifacts, task["id"], record)
        append_integration_event_once(artifacts, record)
        records.append(record)

    target_base_sha_after = current_ref_sha(repo_root, policy.target_base_ref)
    report = write_integration_report(
        artifacts,
        policy,
        target_base_sha_before,
        target_base_sha_after,
        records,
    )
    write_json(queue_file, queue)
    return {
        "schema_version": SCHEMA_VERSION,
        "integration_mode": policy.mode,
        "base_ref": policy.target_base_ref,
        "base_sha_before": target_base_sha_before,
        "base_sha_after": target_base_sha_after,
        "target_dirty_status": target_dirty_status,
        "report": report,
        "records": records,
    }


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


def command_identity(command: Any, argv_field: str = "argv") -> tuple[str, tuple[str, ...], float] | None:
    if not isinstance(command, dict):
        return None
    name = command.get("name")
    argv = command.get(argv_field)
    timeout = command.get("timeout_sec")
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(argv, list)
        or not all(isinstance(part, str) for part in argv)
        or isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
    ):
        return None
    return (name, tuple(argv), float(timeout))


def expected_command_identities(
    metadata: dict[str, Any],
    field: str,
) -> list[tuple[str, tuple[str, ...], float]]:
    if field == "validation_command_outputs":
        commands = metadata.get("validation_commands")
        if not isinstance(commands, list):
            return []
        return [
            identity
            for command in commands
            if (identity := command_identity(command, "argv")) is not None
        ]
    if field == "action_command_outputs":
        action_inputs = metadata.get("action_inputs")
        identity = command_identity(action_inputs, "command")
        return [identity] if identity is not None else []
    return []


def verify_command_output_artifacts(
    task_id: str,
    task_dir: Path,
    metadata: dict[str, Any],
    errors: list[str],
) -> None:
    expected_kinds = {
        "action_command_outputs": "action",
        "validation_command_outputs": "validation",
    }
    for field, expected_kind in expected_kinds.items():
        outputs = metadata.get(field)
        if outputs is None:
            errors.append(f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: missing {field}")
            continue
        if not isinstance(outputs, list):
            errors.append(
                f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: {field} must be a list"
            )
            continue
        for index, output in enumerate(outputs):
            label = f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}.{field}[{index}]"
            if not isinstance(output, dict):
                errors.append(f"{label}: expected object")
                continue
            artifact = output.get("output_artifact")
            if not isinstance(artifact, str) or not artifact.strip():
                errors.append(f"{label}: missing output_artifact")
                continue
            artifact_path = Path(artifact)
            if artifact_path.is_absolute() or ".." in artifact_path.parts:
                errors.append(f"{label}: unsafe output_artifact")
                continue
            if not artifact_path.parts or artifact_path.parts[0] != COMMAND_OUTPUTS_DIR:
                errors.append(f"{label}: output_artifact must be under {COMMAND_OUTPUTS_DIR}")
                continue
            output_path = task_dir / artifact_path
            if not output_path.exists():
                errors.append(f"artifacts/{task_id}/{artifact}: missing command output artifact")
                continue
            if output_path.stat().st_size == 0:
                errors.append(f"artifacts/{task_id}/{artifact}: command output artifact is empty")
                continue
            output_doc = read_artifact_object(output_path, errors)
            if output_doc is None:
                continue
            for mirrored_field in (
                "kind",
                "name",
                "argv",
                "returncode",
                "timed_out",
                "timeout_sec",
                "output_artifact",
                "stdout",
                "stderr",
            ):
                if output_doc.get(mirrored_field) != output.get(mirrored_field):
                    errors.append(
                        f"artifacts/{task_id}/{artifact}: {mirrored_field} "
                        "does not match metadata"
                    )
            if output_doc.get("schema_version") != SCHEMA_VERSION:
                errors.append(f"artifacts/{task_id}/{artifact}: unsupported schema version")
            if output_doc.get("task_id") != task_id:
                errors.append(f"artifacts/{task_id}/{artifact}: task_id mismatch")
            if output_doc.get("output_artifact") != artifact:
                errors.append(f"artifacts/{task_id}/{artifact}: output_artifact mismatch")
            if output_doc.get("kind") != expected_kind:
                errors.append(
                    f"artifacts/{task_id}/{artifact}: expected {expected_kind} command output"
                )
            for string_field in ("name", "kind", "stdout", "stderr"):
                if not isinstance(output_doc.get(string_field), str):
                    errors.append(f"artifacts/{task_id}/{artifact}: {string_field} must be string")
            if not isinstance(output_doc.get("argv"), list):
                errors.append(f"artifacts/{task_id}/{artifact}: argv must be list")
            if output_doc.get("returncode") is not None and not isinstance(
                output_doc.get("returncode"), int
            ):
                errors.append(f"artifacts/{task_id}/{artifact}: returncode must be int or null")
            if not isinstance(output_doc.get("timed_out"), bool):
                errors.append(f"artifacts/{task_id}/{artifact}: timed_out must be boolean")
        expected = expected_command_identities(metadata, field)
        actual = [
            identity
            for output in outputs
            if (identity := command_identity(output, "argv")) is not None
        ]
        if expected and actual != expected and (
            outputs or metadata.get("final_task_state") == "DONE"
        ):
            errors.append(
                f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: "
                f"{field} does not match declared commands"
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
    action_adapter = metadata.get("action_adapter")
    if not isinstance(action_adapter, str) or not action_adapter.strip():
        if task.get("state") == "DONE":
            errors.append(
                f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: missing action_adapter"
            )
    elif action_adapter not in SUPPORTED_WORKTREE_ACTION_ADAPTERS and task.get("state") == "DONE":
        errors.append(
            f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: unknown action_adapter"
        )
    elif task.get("worktree_action_adapter") and action_adapter != task.get(
        "worktree_action_adapter"
    ):
        errors.append(
            f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: action_adapter mismatch"
        )
    if task.get("state") == "DONE" and not isinstance(metadata.get("action_inputs"), dict):
        errors.append(f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: missing action_inputs")
    expected_change_paths = metadata.get("expected_change_paths")
    if task.get("state") == "DONE" and (
        not isinstance(expected_change_paths, list) or not expected_change_paths
    ):
        errors.append(
            f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: missing expected_change_paths"
        )
    elif isinstance(expected_change_paths, list) and not all(
        isinstance(path, str) and path.strip() for path in expected_change_paths
    ):
        errors.append(
            f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: invalid expected_change_paths"
        )
    has_validation_commands = (
        isinstance(metadata.get("validation_commands"), list)
        and bool(metadata["validation_commands"])
    )
    validation_outputs = metadata.get("validation_command_outputs")
    has_validation_outputs = isinstance(validation_outputs, list) and bool(validation_outputs)
    if task.get("state") == "DONE":
        if action_adapter == COMMAND_BACKED_PATCH_ADAPTER and not has_validation_commands:
            errors.append(
                f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: "
                "command-backed adapter missing validation_commands"
            )
        if (has_validation_commands or action_adapter == COMMAND_BACKED_PATCH_ADAPTER) and (
            not has_validation_outputs
        ):
            errors.append(
                f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: "
                "missing validation command evidence"
            )
    verify_command_output_artifacts(task_id, task_dir, metadata, errors)
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
        if validation_output.get("action_adapter") and validation_output.get(
            "action_adapter"
        ) != action_adapter:
            errors.append(
                f"artifacts/{task_id}/{WORKTREE_METADATA_ARTIFACT}: "
                "validation action_adapter mismatch"
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


def verify_integration_artifact(
    task: dict[str, Any],
    task_dir: Path,
    errors: list[str],
) -> None:
    task_id = task["id"]
    integration_state = task.get("integration_state")
    integration_path = task_dir / INTEGRATION_ARTIFACT
    if integration_state is None and not integration_path.exists():
        return

    if integration_state not in INTEGRATION_STATES:
        errors.append(f"artifacts/{task_id}: invalid integration_state")
    if not integration_path.exists():
        errors.append(f"artifacts/{task_id}/{INTEGRATION_ARTIFACT}: missing integration metadata")
        return
    if integration_path.stat().st_size == 0:
        errors.append(f"artifacts/{task_id}/{INTEGRATION_ARTIFACT}: artifact is empty")
        return

    integration = read_artifact_object(integration_path, errors)
    if integration is None:
        return
    label = f"artifacts/{task_id}/{INTEGRATION_ARTIFACT}"
    if integration.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{label}: unsupported schema version")
    if integration.get("task_id") != task_id:
        errors.append(f"{label}: task_id mismatch")
    if integration.get("integration_mode") not in ACTIVE_INTEGRATION_MODES:
        errors.append(f"{label}: invalid integration mode")
    if integration.get("integration_mode") != task.get("integration_mode"):
        errors.append(f"{label}: integration mode mismatch")
    if integration.get("final_integration_state") != integration_state:
        errors.append(f"{label}: final integration state mismatch")
    if integration.get("base_ref") != task.get("integration_base_ref"):
        errors.append(f"{label}: base ref mismatch")
    if integration.get("task_branch") != task.get("integration_task_branch"):
        errors.append(f"{label}: task branch mismatch")
    if integration.get("task_commit_sha") != task.get("integration_task_commit_sha"):
        errors.append(f"{label}: task commit mismatch")

    if integration_state in {"READY", "MERGED"}:
        if task.get("state") != "DONE":
            errors.append(f"{label}: integrated task is not DONE")
        if task.get("processed_by") != WORKTREE_PROCESSED_BY:
            errors.append(f"{label}: integrated task was not worktree processed")
        if not is_commit_sha(task.get("worktree_commit_sha")):
            errors.append(f"{label}: integrated task missing task commit SHA")

    if integration_state == "MERGED":
        merge_sha = integration.get("merge_commit_sha")
        if not is_commit_sha(merge_sha):
            errors.append(f"{label}: MERGED integration missing merge SHA")
        if task.get("integration_sha") != merge_sha:
            errors.append(f"{label}: merge SHA mismatch")
    elif "integration_sha" in task:
        errors.append(f"{label}: non-MERGED task has integration_sha")


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
    verify_integration_artifact(task, task_dir, errors)

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


def verify_integration_report_artifacts(
    queue: dict[str, Any],
    artifacts: Path,
    errors: list[str],
) -> None:
    tasks = queue.get("tasks", [])
    has_integration = any(task.get("integration_state") for task in tasks)
    require_events = has_integration
    report_path = artifacts / INTEGRATION_REPORT_ARTIFACT
    events_path = artifacts / INTEGRATION_EVENTS_ARTIFACT
    if not has_integration and not report_path.exists() and not events_path.exists():
        return

    if not report_path.exists():
        errors.append(f"{report_path}: missing integration report")
    elif report_path.stat().st_size == 0:
        errors.append(f"{report_path}: artifact is empty")
    else:
        report = read_artifact_object(report_path, errors)
        if report is not None:
            if report.get("schema_version") != SCHEMA_VERSION:
                errors.append(f"{report_path}: unsupported schema version")
            if report.get("integration_mode") not in ACTIVE_INTEGRATION_MODES:
                errors.append(f"{report_path}: invalid integration mode")
            expected = integration_counts(tasks)
            if report.get("candidate_count") != expected["total_integration_candidates"]:
                errors.append(
                    f"{report_path}: candidate_count={report.get('candidate_count')!r}, "
                    f"expected {expected['total_integration_candidates']!r}"
                )
            for report_field, expected_field in (
                ("ready_count", "ready_count"),
                ("merged_count", "merged_count"),
                ("skipped_count", "skipped_count"),
                ("failed_count", "failed_count"),
                ("blocked_count", "blocked_count"),
            ):
                if report.get(report_field) != expected[expected_field]:
                    errors.append(
                        f"{report_path}: {report_field}={report.get(report_field)!r}, "
                        f"expected {expected[expected_field]!r}"
                    )
            task_results = report.get("task_results")
            if not isinstance(task_results, list):
                errors.append(f"{report_path}: task_results must be list")
            if report.get("candidate_count"):
                require_events = True
            if isinstance(task_results, list):
                candidates = integration_task_candidates(queue)
                if len(task_results) != len(candidates):
                    errors.append(
                        f"{report_path}: task_results length={len(task_results)!r}, "
                        f"expected {len(candidates)!r}"
                    )
                expected_by_id = {
                    task.get("id"): task
                    for task in candidates
                    if isinstance(task.get("id"), str)
                }
                seen_task_ids: set[str] = set()
                for index, result in enumerate(task_results):
                    result_label = f"{report_path}.task_results[{index}]"
                    if not isinstance(result, dict):
                        errors.append(f"{result_label}: expected object")
                        continue
                    task_id = result.get("task_id")
                    if not isinstance(task_id, str) or task_id not in expected_by_id:
                        errors.append(f"{result_label}: unexpected task_id")
                        continue
                    if task_id in seen_task_ids:
                        errors.append(f"{result_label}: duplicate task_id")
                    seen_task_ids.add(task_id)
                    task = expected_by_id[task_id]
                    field_pairs = (
                        ("final_integration_state", "integration_state"),
                        ("task_branch", "integration_task_branch"),
                        ("task_commit_sha", "integration_task_commit_sha"),
                        ("merge_commit_sha", "integration_sha"),
                    )
                    for result_field, task_field in field_pairs:
                        expected_value = task.get(task_field)
                        actual_value = result.get(result_field)
                        if result_field == "merge_commit_sha" and expected_value is None:
                            if actual_value is not None:
                                errors.append(f"{result_label}: merge_commit_sha mismatch")
                            continue
                        if actual_value != expected_value:
                            errors.append(f"{result_label}: {result_field} mismatch")

    if not require_events and not events_path.exists():
        return

    if not events_path.exists():
        errors.append(f"{events_path}: missing integration events")
    elif events_path.stat().st_size == 0:
        errors.append(f"{events_path}: artifact is empty")
    else:
        try:
            events = read_ndjson(events_path)
        except ValidationError as exc:
            errors.append(str(exc))
            events = []
        seen_keys: set[tuple[Any, ...]] = set()
        for index, event in enumerate(events):
            label = f"{events_path}[{index}]"
            validate_event_metadata(event, label, errors, ("ts",))
            if event.get("type") != "TASK_INTEGRATION":
                errors.append(f"{label}: invalid event type")
            if event.get("integration_mode") not in ACTIVE_INTEGRATION_MODES:
                errors.append(f"{label}: invalid integration mode")
            if event.get("final_integration_state") not in INTEGRATION_STATES:
                errors.append(f"{label}: invalid final integration state")
            key = integration_event_key(event)
            if key in seen_keys:
                errors.append(f"{label}: duplicate integration event")
            seen_keys.add(key)


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
            if "integration_counts" in metrics and metrics.get(
                "integration_counts"
            ) != integration_counts(queue.get("tasks", [])):
                errors.append(f"{metrics_path}: integration_counts does not match final state")

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
        elif task.get("integration_state"):
            task_dir = artifacts / task["id"]
            if not task_dir.is_dir():
                errors.append(f"artifacts/{task['id']}: missing integration task directory")
            else:
                verify_integration_artifact(task, task_dir, errors)

    verify_integration_report_artifacts(queue, artifacts, errors)

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
        "task_execution_counts": queue_counts(tasks),
        "queue_counts": queue_counts(tasks),
        "integration_counts": integration_counts(tasks),
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
    integration_policy: IntegrationPolicy | None = None,
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
    integration_summary: dict[str, Any] | None = None
    if not dry_run:
        write_json(program_file, program)
        metrics = write_outcome_reports(program, queue, artifacts, review_entry, run_started_ts)
        integrity_errors = verify_artifact_integrity(program, queue, artifacts)
        if integrity_errors:
            raise ValidationError(
                format_validation_errors("Artifact integrity check failed", integrity_errors)
            )
        if integration_policy is not None:
            if worktree_config is None:
                raise ValidationError("integration requires --execution-mode worktree")
            integration_summary = integrate_completed_tasks(
                queue=queue,
                queue_file=queue_file,
                artifacts=artifacts,
                policy=integration_policy,
            )
            queue = read_json(queue_file)
            metrics = write_outcome_reports(program, queue, artifacts, review_entry, run_started_ts)
            integrity_errors = verify_artifact_integrity(program, queue, artifacts)
            if integrity_errors:
                raise ValidationError(
                    format_validation_errors(
                        "Artifact integrity check failed",
                        integrity_errors,
                    )
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
        "integration_summary": integration_summary,
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


def add_integration_args(
    parser: argparse.ArgumentParser,
    *,
    default_mode: str,
    include_off: bool,
) -> None:
    choices = sorted(INTEGRATION_MODES if include_off else ACTIVE_INTEGRATION_MODES)
    parser.add_argument(
        "--integration-mode",
        choices=choices,
        default=default_mode,
        help=(
            "Run the v0.6 integration phase. local-merge mutates the target "
            "base branch and must be selected explicitly."
        ),
    )
    parser.add_argument(
        "--integration-policy",
        type=Path,
        help="Optional local integration policy JSON contract",
    )


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
    add_integration_args(program_parser, default_mode="off", include_off=True)

    integration_parser = subparsers.add_parser(
        "integrate",
        help="Run the v0.6 integration phase for completed worktree tasks",
    )
    integration_parser.add_argument("--program", type=Path, required=True)
    integration_parser.add_argument("--queue", type=Path, required=True)
    integration_parser.add_argument("--artifacts", type=Path, required=True)
    integration_parser.add_argument("--worktree-repo", type=Path, required=True)
    integration_parser.add_argument("--worktree-base-ref", default="main")
    integration_parser.add_argument(
        "--worktree-branch-prefix",
        default="codex/local-agent-loop",
    )
    add_integration_args(integration_parser, default_mode="report-only", include_off=False)

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
            integration_policy = build_integration_policy(
                args.integration_mode,
                args.worktree_repo,
                args.worktree_base_ref,
                args.worktree_branch_prefix,
                args.integration_policy,
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
                integration_policy=integration_policy,
            )
        elif args.command == "integrate":
            program, queue = load_validated_inputs(args.program, args.queue)
            policy = build_integration_policy(
                args.integration_mode,
                args.worktree_repo,
                args.worktree_base_ref,
                args.worktree_branch_prefix,
                args.integration_policy,
            )
            if policy is None:
                raise ValidationError("integrate requires an active integration mode")
            summary = integrate_completed_tasks(
                queue=queue,
                queue_file=args.queue,
                artifacts=args.artifacts,
                policy=policy,
            )
            metrics = write_outcome_reports(
                program,
                read_json(args.queue),
                args.artifacts,
                None,
                utc_now(),
            )
            summary["metrics"] = metrics
            integrity_errors = verify_artifact_integrity(
                program,
                read_json(args.queue),
                args.artifacts,
            )
            summary["artifact_integrity_errors"] = integrity_errors
            if integrity_errors:
                raise ValidationError(
                    format_validation_errors(
                        "Artifact integrity check failed",
                        integrity_errors,
                    )
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
