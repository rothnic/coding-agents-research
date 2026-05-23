#!/usr/bin/env python3
"""Higher-level planner/replanner loop composed with the worker loop.

Adds v0.2 program guardrails: dependency-aware unblocking, explicit program
states, deterministic scheduling, and high-level reporting artifacts.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent_loop_v0 import read_json, run, write_json


TERMINAL = {"DONE", "FAILED"}
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def count_tasks(tasks: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "open_count": sum(1 for t in tasks if t.get("state") == "OPEN"),
        "unblocked_count": sum(1 for t in tasks if t.get("state") in UNBLOCKED),
        "blocked_count": sum(1 for t in tasks if t.get("state") == "BLOCKED"),
        "done_count": sum(1 for t in tasks if t.get("state") == "DONE"),
        "failed_count": sum(1 for t in tasks if t.get("state") == "FAILED"),
        "terminal_count": sum(1 for t in tasks if t.get("state") in TERMINAL),
        "total_count": len(tasks),
    }


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

    def __post_init__(self) -> None:
        self.state = str(self.program.get("program_state") or "IDLE")
        self.program["program_state"] = self.state

    def emit(self, event_type: str, **payload: Any) -> dict[str, Any]:
        from_state = payload.pop("from_program_state", self.state)
        to_state = payload.pop("to_program_state", self.state)
        event = {
            "ts": utc_now(),
            "run_started_ts": self.run_started_ts,
            "program_id": self.program.get("program_id", "unknown"),
            "type": event_type,
            "from_program_state": from_state,
            "to_program_state": to_state,
            **payload,
        }
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
    depends_on = list(task.get("depends_on", []))
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
    backlog = list(program.get("roadmap_backlog", []))
    ordered = sorted(
        enumerate(backlog),
        key=lambda pair: (-int(pair[1].get("priority", 0)), deadline_key(pair[1]), pair[0]),
    )
    selected_indexes: set[int] = set()
    generated: list[dict[str, Any]] = []
    open_count = counts["open_count"]

    for original_index, item in ordered:
        if open_count >= min_open:
            break
        task = {
            "id": item["id"],
            "title": item["title"],
            "state": "OPEN",
            "retries": 0,
            "required_checks": item.get("required_checks", ["test", "lint"]),
            "priority": int(item.get("priority", 0)),
        }
        if item.get("deadline_ts"):
            task["deadline_ts"] = item["deadline_ts"]
        if item.get("depends_on"):
            task["depends_on"] = list(item.get("depends_on", []))
        queue.setdefault("tasks", []).append(task)
        selected_indexes.add(original_index)
        generated.append(task)
        open_count += 1

    program["roadmap_backlog"] = [
        item for index, item in enumerate(backlog) if index not in selected_indexes
    ]
    return generated


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
        return int((datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds())
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


def task_sort_key(task: dict[str, Any]) -> tuple[int, int, str, str]:
    state_rank = 0 if task.get("state") in UNBLOCKED else 1
    return (
        state_rank,
        -int(task.get("priority", 0)),
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
    for item in program.get("roadmap_backlog", [])[:3]:
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
        "program_id": program.get("program_id", "unknown"),
        "run_started_ts": run_started_ts,
        "updated_ts": utc_now(),
        "open_count": counts["open_count"],
        "blocked_count": counts["blocked_count"],
        "unblocked_count": counts["unblocked_count"],
        "generated_count": generated,
        "reopened_count": reopened,
        "done_count": counts["done_count"],
        "failed_count": counts["failed_count"],
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
            priority = int(task.get("priority", 0))
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


def run_program(
    program_file: Path,
    queue_file: Path,
    artifacts: Path,
    min_open: int,
    timeout_s: int,
    max_retries: int,
) -> None:
    run_started_ts = utc_now()
    program = read_json(program_file)
    queue = read_json(queue_file)
    runner = ProgramRun(program=program, artifacts=artifacts, run_started_ts=run_started_ts)

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
    write_json(queue_file, queue)
    write_json(program_file, program)

    run(queue_file=queue_file, artifacts_root=artifacts, max_retries=max_retries)

    queue = read_json(queue_file)
    require_transition(runner, "IDLE", "Worker loop complete")
    program["last_run_completed_ts"] = utc_now()
    write_json(program_file, program)
    write_outcome_reports(program, queue, artifacts, review_entry, run_started_ts)


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
