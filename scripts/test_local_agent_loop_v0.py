#!/usr/bin/env python3
"""CLI-level regression harness for local-agent-loop-v0."""
from __future__ import annotations

import argparse
import difflib
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import local_agent_loop_v0
import local_program_loop_v0


ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = ROOT / "docs/specs/local-agent-loop-v0"
EXPECTED_SUMMARY = SPEC_DIR / "fixtures/regression/expected-summary.json"
SCHEMA_VERSION = local_agent_loop_v0.SCHEMA_VERSION
REQUIRED_METRIC_FIELDS = {
    "schema_version",
    "program_state",
    "open_count",
    "blocked_count",
    "unblocked_count",
    "generated_count",
    "done_count",
    "failed_count",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def program_doc(**fields: Any) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, **fields}


def queue_doc(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "tasks": tasks}


def iso_delta_seconds(start: str, end: str) -> float:
    return (
        datetime.fromisoformat(end.replace("Z", "+00:00"))
        - datetime.fromisoformat(start.replace("Z", "+00:00"))
    ).total_seconds()


def stable_decision(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": decision["task_id"],
        "dependency_check": decision["dependency_check"],
        "reopened": decision["reopened"],
        "action": decision["action"],
        "missing_dependencies": decision["missing_dependencies"],
        "unmet_dependencies": decision["unmet_dependencies"],
        "blocked_reason_code": decision["blocked_reason_code"],
    }


def stable_program_event(event: dict[str, Any]) -> dict[str, Any]:
    stable = {
        "type": event["type"],
        "from_program_state": event["from_program_state"],
        "to_program_state": event["to_program_state"],
    }
    if "accepted" in event:
        stable["accepted"] = event["accepted"]
    if "reason" in event:
        stable["reason"] = event["reason"]
    if "generated_task_ids" in event:
        stable["generated_task_ids"] = event["generated_task_ids"]
    if "decision" in event:
        stable["decision"] = stable_decision(event["decision"])
    return stable


def summarize_composed_run(
    name: str,
    program: dict[str, Any],
    queue: dict[str, Any],
    min_open: int,
    timeout_s: int = 0,
    max_retries: int = 2,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"{name}-") as tmp:
        tmp_dir = Path(tmp)
        program_file = tmp_dir / "program.json"
        queue_file = tmp_dir / "queue.json"
        artifacts = tmp_dir / "artifacts"
        write_json(program_file, program)
        write_json(queue_file, queue)

        local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=min_open,
            timeout_s=timeout_s,
            max_retries=max_retries,
        )

        program_out = read_json(program_file)
        queue_out = read_json(queue_file)
        events = read_ndjson(artifacts / "roadmap_events.ndjson")
        metrics = read_json(artifacts / "program_metrics.json")
        review = program_out.get("review_log", [{}])[-1]
        metric_fields = set(metrics)
        task_dirs = sorted(
            path.name
            for path in artifacts.iterdir()
            if path.is_dir()
        )
        status = (artifacts / "roadmap_status.md").read_text(encoding="utf-8")

        return {
            "generated_order": review.get("generated_task_ids", []),
            "reopened_task_ids": review.get("reopened_task_ids", []),
            "unblock_decisions": [
                stable_decision(decision)
                for decision in review.get("unblock_decisions", [])
            ],
            "queue_states": {
                task["id"]: task["state"]
                for task in queue_out.get("tasks", [])
            },
            "program_state": program_out["program_state"],
            "program_events": [stable_program_event(event) for event in events],
            "state_transition_count": sum(
                1 for event in events if event["type"] == "PROGRAM_STATE_TRANSITION"
            ),
            "all_program_events_have_state_fields": all(
                "from_program_state" in event and "to_program_state" in event
                for event in events
            ),
            "metric_fields_present": REQUIRED_METRIC_FIELDS <= metric_fields,
            "metrics_fresh_under_5s": (
                iso_delta_seconds(metrics["run_started_ts"], metrics["updated_ts"]) < 5
            ),
            "status_sections_present": [
                header in status
                for header in ["## Progress", "## Blockers", "## Next 3 Tasks"]
            ],
            "task_artifacts": task_dirs,
            "generated_count": metrics["generated_count"],
            "blocked_count": metrics["blocked_count"],
            "done_count": metrics["done_count"],
            "failed_count": metrics["failed_count"],
        }


def backlog_refill_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    program = program_doc(**{
        "program_id": "regression-backlog-refill",
        "goal": "Keep a deterministic near-term queue",
        "program_state": "IDLE",
        "last_roadmap_review_ts": "2026-01-01T00:00:00+00:00",
        "roadmap_backlog": [
            {
                "id": "task-medium",
                "title": "Medium priority task",
                "priority": 3,
                "deadline_ts": "2026-05-25T00:00:00+00:00",
            },
            {
                "id": "task-high-late",
                "title": "High priority later deadline",
                "priority": 9,
                "deadline_ts": "2026-05-24T01:00:00-05:00",
            },
            {
                "id": "task-high-early",
                "title": "High priority earlier deadline",
                "priority": 9,
                "deadline_ts": "2026-05-24T08:00:00+03:00",
            },
        ],
        "review_log": [],
    })
    return program, queue_doc([])


def all_blocked_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    program = program_doc(**{
        "program_id": "regression-all-blocked",
        "goal": "Reopen eligible blocked work",
        "program_state": "IDLE",
        "last_roadmap_review_ts": "2026-01-01T00:00:00+00:00",
        "roadmap_backlog": [],
        "review_log": [],
    })
    queue = queue_doc(
        [
            {"id": "task-done", "title": "Completed dependency", "state": "DONE"},
            {
                "id": "task-ready-1",
                "title": "External wait resolved",
                "state": "BLOCKED",
                "blocked_reason": "waiting_on_external",
                "depends_on": ["task-done"],
            },
            {
                "id": "task-ready-2",
                "title": "Clarification ready",
                "state": "BLOCKED",
                "blocked_reason": "needs_clarification",
                "depends_on": [],
            },
        ]
    )
    return program, queue


def dependency_partial_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    program = program_doc(**{
        "program_id": "regression-dependency-partial",
        "goal": "Respect dependency graph before reopening",
        "program_state": "IDLE",
        "last_roadmap_review_ts": "2026-01-01T00:00:00+00:00",
        "roadmap_backlog": [],
        "review_log": [],
    })
    queue = queue_doc(
        [
            {"id": "task-prereq-done", "title": "Finished prerequisite", "state": "DONE"},
            {
                "id": "task-ready",
                "title": "Ready blocked task",
                "state": "BLOCKED",
                "blocked_reason": "missing_dependency",
                "depends_on": ["task-prereq-done"],
            },
            {
                "id": "task-missing",
                "title": "Missing prerequisite",
                "state": "BLOCKED",
                "blocked_reason": "waiting_on_external",
                "depends_on": ["task-not-found"],
            },
            {
                "id": "task-chain",
                "title": "Chained prerequisite",
                "state": "BLOCKED",
                "blocked_reason": "needs_clarification",
                "depends_on": ["task-missing"],
            },
        ]
    )
    return program, queue


def require_composed_invariants(summary: dict[str, Any]) -> None:
    require(summary["state_transition_count"] >= 1, "program run recorded no transitions")
    require(
        summary["all_program_events_have_state_fields"],
        "program event missing state fields",
    )
    require(summary["metric_fields_present"], "program metrics missing required field")
    require(summary["metrics_fresh_under_5s"], "program metrics were not fresh")
    require(all(summary["status_sections_present"]), "roadmap_status.md missing section")

    failed_dependency_reopens = [
        decision
        for decision in summary["unblock_decisions"]
        if decision["dependency_check"] == "fail" and decision["reopened"]
    ]
    require(
        not failed_dependency_reopens,
        f"reopened tasks with unmet dependencies: {failed_dependency_reopens}",
    )

    reopened_from_decisions = [
        decision["task_id"]
        for decision in summary["unblock_decisions"]
        if decision["reopened"]
    ]
    require(
        summary["reopened_task_ids"] == reopened_from_decisions,
        "reopened task ids do not match unblock decisions",
    )


def scenario_backlog_refill() -> dict[str, Any]:
    program, queue = backlog_refill_inputs()
    summary = summarize_composed_run("backlog-refill", program, queue, min_open=2)
    require_composed_invariants(summary)
    require(
        summary["generated_order"] == ["task-high-early", "task-high-late"],
        f"unexpected generated order: {summary['generated_order']}",
    )
    return summary


def scenario_all_blocked() -> dict[str, Any]:
    program, queue = all_blocked_inputs()
    summary = summarize_composed_run("all-blocked", program, queue, min_open=0)
    require_composed_invariants(summary)
    require(
        summary["reopened_task_ids"] == ["task-ready-1", "task-ready-2"],
        f"unexpected reopened tasks: {summary['reopened_task_ids']}",
    )
    return summary


def scenario_dependency_partial() -> dict[str, Any]:
    program, queue = dependency_partial_inputs()
    summary = summarize_composed_run("dependency-partial", program, queue, min_open=0)
    require_composed_invariants(summary)
    require(summary["reopened_task_ids"] == ["task-ready"], "unexpected reopened task")
    require(
        summary["queue_states"]["task-missing"] == "BLOCKED",
        "task with missing dependency was reopened",
    )
    require(
        summary["queue_states"]["task-chain"] == "BLOCKED",
        "task with unmet dependency was reopened",
    )
    return summary


def scenario_validation_retry() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="validation-retry-") as tmp:
        tmp_dir = Path(tmp)
        queue_file = tmp_dir / "queue.json"
        artifacts = tmp_dir / "artifacts"
        write_json(
            queue_file,
            queue_doc(
                [
                    {
                        "id": "task-retry",
                        "title": "Validation retry path",
                        "state": "OPEN",
                        "retries": 0,
                        "simulate_validation_fail_once": True,
                    }
                ]
            ),
        )

        local_agent_loop_v0.run(queue_file=queue_file, artifacts_root=artifacts, max_retries=2)
        queue_out = read_json(queue_file)
        task = queue_out["tasks"][0]
        validation = read_json(artifacts / "task-retry/validation.json")
        events = read_ndjson(artifacts / "task-retry/events.ndjson")
        summary = {
            "final_state": task["state"],
            "retries": task["retries"],
            "validation_attempt": validation["attempt"],
            "validation_passed": validation["passed"],
            "event_transitions": [
                f"{event['from_state']}->{event['to_state']}"
                for event in events
            ],
        }
        require(summary["final_state"] == "DONE", "retry scenario did not finish DONE")
        require(summary["retries"] == 1, "retry scenario used wrong retry count")
        require(summary["validation_attempt"] == 2, "retry scenario did not retry")
        require(summary["validation_passed"], "retry scenario validation did not pass")
        return summary


def scenario_transition_guard() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="transition-guard-") as tmp:
        artifacts = Path(tmp) / "artifacts"
        program = {
            "program_id": "regression-transition-guard",
            "goal": "Reject illegal transition injection",
            "program_state": "IDLE",
        }
        runner = local_program_loop_v0.ProgramRun(program=program, artifacts=artifacts)
        accepted = runner.transition("UNBLOCKING", "Injected illegal transition for test")
        events = read_ndjson(artifacts / "roadmap_events.ndjson")
        summary = {
            "accepted": accepted,
            "program_state_after": program["program_state"],
            "events": [stable_program_event(event) for event in events],
        }
        require(not summary["accepted"], "illegal transition injection was accepted")
        require(
            summary["program_state_after"] == "IDLE",
            "illegal transition changed program state",
        )
        require(
            summary["events"][0]["type"] == "PROGRAM_TRANSITION_REJECTED",
            "illegal transition did not emit rejection event",
        )
        return summary


def scenario_interrupted_run_recovery() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="interrupted-recovery-") as tmp:
        tmp_dir = Path(tmp)
        program_file = tmp_dir / "program.json"
        queue_file = tmp_dir / "queue.json"
        artifacts = tmp_dir / "artifacts"
        write_json(
            program_file,
            program_doc(
                program_id="regression-interrupted-recovery",
                goal="Recover persisted handoff after an interrupted run",
                program_state="HANDING_OFF",
                last_roadmap_review_ts="2026-01-01T00:00:00+00:00",
                roadmap_backlog=[
                    {"id": "task-existing", "title": "Already synthesized"},
                    {"id": "task-new", "title": "New task after recovery"},
                ],
                review_log=[],
            ),
        )
        write_json(
            queue_file,
            queue_doc(
                [
                    {
                        "id": "task-existing",
                        "title": "Already synthesized",
                        "state": "OPEN",
                    }
                ]
            ),
        )

        run_summary = local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=1,
            timeout_s=0,
            max_retries=2,
        )

        program_out = read_json(program_file)
        queue_out = read_json(queue_file)
        events = read_ndjson(artifacts / "roadmap_events.ndjson")
        task_ids = [task["id"] for task in queue_out["tasks"]]
        summary = {
            "recovered_from": run_summary["recovery"]["recovered_from"],
            "program_state": program_out["program_state"],
            "task_ids": task_ids,
            "unique_task_id_count": len(set(task_ids)),
            "queue_states": {task["id"]: task["state"] for task in queue_out["tasks"]},
            "events": [stable_program_event(event) for event in events],
        }
        require(summary["recovered_from"] == "HANDING_OFF", "handoff state was not recovered")
        require(summary["program_state"] == "IDLE", "program did not return to IDLE")
        require(
            summary["unique_task_id_count"] == len(task_ids),
            "recovery duplicated existing synthesized task",
        )
        require(
            summary["queue_states"]["task-existing"] == "DONE",
            "existing task did not resume through worker",
        )
        require(
            summary["events"][0]["type"] == "PROGRAM_STATE_RECOVERY",
            "recovery event was not recorded first",
        )
        return summary


def scenario_malformed_input_rejected_before_mutation() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="malformed-input-") as tmp:
        tmp_dir = Path(tmp)
        program_file = tmp_dir / "program.json"
        queue_file = tmp_dir / "queue.json"
        artifacts = tmp_dir / "artifacts"
        program = program_doc(
            program_id="regression-malformed-input",
            goal="Reject malformed input before mutation",
            program_state="IDLE",
            last_roadmap_review_ts="not-a-timestamp",
            roadmap_backlog=[
                {
                    "id": "task-backlog",
                    "title": "Bad backlog dependency",
                    "depends_on": None,
                }
            ],
            review_log=[],
        )
        queue = queue_doc(
            [
                {
                    "id": "task-bad",
                    "title": "Bad task",
                    "state": "NOT_A_STATE",
                    "depends_on": "task-other",
                    "deadline_ts": "bad timestamp",
                }
            ]
        )
        write_json(program_file, program)
        write_json(queue_file, queue)
        before_program = program_file.read_text(encoding="utf-8")
        before_queue = queue_file.read_text(encoding="utf-8")

        message = ""
        try:
            local_program_loop_v0.run_program(
                program_file=program_file,
                queue_file=queue_file,
                artifacts=artifacts,
                min_open=1,
                timeout_s=0,
                max_retries=2,
            )
        except local_agent_loop_v0.ValidationError as exc:
            message = str(exc)

        summary = {
            "rejected": bool(message),
            "mentions_state": "invalid state" in message,
            "mentions_timestamp": "invalid ISO timestamp" in message,
            "mentions_dependency": "depends_on" in message,
            "program_unchanged": program_file.read_text(encoding="utf-8") == before_program,
            "queue_unchanged": queue_file.read_text(encoding="utf-8") == before_queue,
            "artifacts_created": artifacts.exists(),
        }
        require(summary["rejected"], "malformed inputs were accepted")
        require(summary["mentions_state"], "malformed error did not mention bad state")
        require(summary["mentions_timestamp"], "malformed error did not mention bad timestamp")
        require(summary["mentions_dependency"], "malformed error did not mention bad dependency")
        require(summary["program_unchanged"], "program mutated after validation failure")
        require(summary["queue_unchanged"], "queue mutated after validation failure")
        require(not summary["artifacts_created"], "artifacts were created after validation failure")
        return summary


def scenario_duplicate_ids_rejected() -> dict[str, Any]:
    program = program_doc(
        program_id="regression-duplicate-ids",
        goal="Reject duplicate ids",
        program_state="IDLE",
        last_roadmap_review_ts="2026-01-01T00:00:00+00:00",
        roadmap_backlog=[
            {"id": "task-dup-backlog", "title": "First"},
            {"id": "task-dup-backlog", "title": "Second"},
        ],
        review_log=[],
    )
    queue = queue_doc(
        [
            {"id": "task-dup", "title": "First", "state": "OPEN"},
            {"id": "task-dup", "title": "Second", "state": "BLOCKED"},
        ]
    )
    errors = local_program_loop_v0.validate_program_document(program)
    errors.extend(local_agent_loop_v0.validate_queue_document(queue))
    summary = {
        "error_count": len(errors),
        "program_duplicate_detected": any("duplicate roadmap item id" in err for err in errors),
        "queue_duplicate_detected": any("duplicate task id" in err for err in errors),
    }
    require(summary["program_duplicate_detected"], "duplicate backlog id was not rejected")
    require(summary["queue_duplicate_detected"], "duplicate queue task id was not rejected")
    return summary


def scenario_schema_version_mismatch() -> dict[str, Any]:
    program = program_doc(
        program_id="regression-schema-mismatch",
        goal="Reject unsupported schema versions",
        program_state="IDLE",
        last_roadmap_review_ts="2026-01-01T00:00:00+00:00",
        roadmap_backlog=[],
        review_log=[],
    )
    queue = queue_doc([])
    program["schema_version"] = "local-agent-loop-v9"
    queue["schema_version"] = "local-agent-loop-v0.1"
    errors = local_program_loop_v0.validate_program_document(program)
    errors.extend(local_agent_loop_v0.validate_queue_document(queue))
    summary = {
        "error_count": len(errors),
        "unsupported_program_version": any(
            "program.schema_version" in err and "unsupported" in err for err in errors
        ),
        "unsupported_queue_version": any(
            "queue.schema_version" in err and "unsupported" in err for err in errors
        ),
    }
    require(summary["unsupported_program_version"], "program schema mismatch was accepted")
    require(summary["unsupported_queue_version"], "queue schema mismatch was accepted")
    return summary


def scenario_v0_2_null_inputs_rejected() -> dict[str, Any]:
    null_field_program = program_doc(
        program_id="regression-null-hardening-rejected",
        goal="Reject v0.2 null optional fields under v0.3 schema",
        program_state="IDLE",
        last_roadmap_review_ts="2026-01-01T00:00:00Z",
        roadmap_backlog=[
            {
                "id": "task-null-backlog",
                "title": "Null backlog task",
                "priority": None,
                "deadline_ts": "2026-05-24T00:00:00Z",
                "depends_on": None,
                "required_checks": None,
            }
        ],
        review_log=[],
    )
    null_field_queue = queue_doc(
        [
            {
                "id": "task-null-deps",
                "title": "Null dependency list",
                "state": "BLOCKED",
                "blocked_reason": "manual",
                "depends_on": None,
            }
        ]
    )
    null_backlog_program = program_doc(
        program_id="regression-null-backlog-rejected",
        goal="Reject null backlog under v0.3 schema",
        program_state="IDLE",
        last_roadmap_review_ts=local_program_loop_v0.utc_now(),
        roadmap_backlog=None,
        review_log=[],
    )
    null_backlog_queue = queue_doc(
        [{"id": "task-already-done", "title": "Already complete", "state": "DONE"}]
    )
    errors = [
        *local_program_loop_v0.validate_program_document(null_field_program),
        *local_agent_loop_v0.validate_queue_document(null_field_queue),
        *local_program_loop_v0.validate_program_document(null_backlog_program),
        *local_agent_loop_v0.validate_queue_document(null_backlog_queue),
    ]
    summary = {
        "error_count": len(errors),
        "null_priority_detected": any("priority" in err for err in errors),
        "null_dependency_detected": any("depends_on" in err for err in errors),
        "null_required_checks_detected": any("required_checks" in err for err in errors),
        "null_backlog_detected": any("roadmap_backlog" in err for err in errors),
    }
    require(summary["null_priority_detected"], "null priority was not rejected")
    require(summary["null_dependency_detected"], "null dependency list was not rejected")
    require(summary["null_required_checks_detected"], "null checks list was not rejected")
    require(summary["null_backlog_detected"], "null backlog was not rejected")
    return summary


def scenario_dry_run_behavior() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="dry-run-") as tmp:
        tmp_dir = Path(tmp)
        program_file = tmp_dir / "program.json"
        queue_file = tmp_dir / "queue.json"
        artifacts = tmp_dir / "artifacts"
        program, queue = backlog_refill_inputs()
        write_json(program_file, program)
        write_json(queue_file, queue)
        before_program = program_file.read_text(encoding="utf-8")
        before_queue = queue_file.read_text(encoding="utf-8")

        summary = local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=2,
            timeout_s=0,
            max_retries=2,
            dry_run=True,
        )
        result = {
            "mode": summary["mode"],
            "generated_task_ids": summary["review_generated_task_ids"],
            "dry_run_runnable_task_ids": summary["worker_summary"]["runnable_task_ids"],
            "program_unchanged": program_file.read_text(encoding="utf-8") == before_program,
            "queue_unchanged": queue_file.read_text(encoding="utf-8") == before_queue,
            "artifacts_created": artifacts.exists(),
        }
        require(result["mode"] == "dry-run", "dry run summary did not report dry-run mode")
        require(result["generated_task_ids"], "dry run did not report planned generated work")
        require(
            result["dry_run_runnable_task_ids"] == result["generated_task_ids"],
            "dry run worker plan did not reflect reviewed in-memory queue",
        )
        require(result["program_unchanged"], "dry run mutated program file")
        require(result["queue_unchanged"], "dry run mutated queue file")
        require(not result["artifacts_created"], "dry run wrote artifacts")
        return result


def scenario_rerun_idempotency() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="rerun-idempotency-") as tmp:
        tmp_dir = Path(tmp)
        program_file = tmp_dir / "program.json"
        queue_file = tmp_dir / "queue.json"
        artifacts = tmp_dir / "artifacts"
        program = program_doc(
            program_id="regression-rerun-idempotency",
            goal="Avoid duplicate synthesis across repeated runs",
            program_state="IDLE",
            last_roadmap_review_ts="2026-01-01T00:00:00+00:00",
            roadmap_backlog=[{"id": "task-once", "title": "Generate once"}],
            review_log=[],
        )
        write_json(program_file, program)
        write_json(queue_file, queue_doc([]))

        first = local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=1,
            timeout_s=0,
            max_retries=2,
        )
        events_after_first = read_ndjson(artifacts / "task-once/events.ndjson")
        second = local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=1,
            timeout_s=0,
            max_retries=2,
        )
        queue_out = read_json(queue_file)
        events_after_second = read_ndjson(artifacts / "task-once/events.ndjson")
        task_ids = [task["id"] for task in queue_out["tasks"]]
        result = {
            "first_generated": first["review_generated_task_ids"],
            "second_generated": second["review_generated_task_ids"],
            "task_ids": task_ids,
            "unique_task_id_count": len(set(task_ids)),
            "task_event_count_after_first": len(events_after_first),
            "task_event_count_after_second": len(events_after_second),
        }
        require(result["first_generated"] == ["task-once"], "first run did not generate task")
        require(result["second_generated"] == [], "rerun generated duplicate task")
        require(
            result["unique_task_id_count"] == len(task_ids),
            "rerun duplicated queue task ids",
        )
        require(
            result["task_event_count_after_second"] == result["task_event_count_after_first"],
            "rerun duplicated terminal task events",
        )
        return result


def scenario_artifact_integrity() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="artifact-integrity-") as tmp:
        tmp_dir = Path(tmp)
        program_file = tmp_dir / "program.json"
        queue_file = tmp_dir / "queue.json"
        artifacts = tmp_dir / "artifacts"
        program = program_doc(
            program_id="regression-artifact-integrity",
            goal="Verify processed task artifacts",
            program_state="IDLE",
            last_roadmap_review_ts="2026-01-01T00:00:00+00:00",
            roadmap_backlog=[{"id": "task-integrity", "title": "Integrity task"}],
            review_log=[],
        )
        write_json(program_file, program)
        write_json(queue_file, queue_doc([]))
        local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=1,
            timeout_s=0,
            max_retries=2,
        )
        program_out = read_json(program_file)
        queue_out = read_json(queue_file)
        clean_errors = local_program_loop_v0.verify_artifact_integrity(
            program_out, queue_out, artifacts
        )
        metrics_path = artifacts / "program_metrics.json"
        original_metrics = metrics_path.read_text(encoding="utf-8")
        metrics_path.write_text("[]\n", encoding="utf-8")
        metric_shape_errors = local_program_loop_v0.verify_artifact_integrity(
            program_out, queue_out, artifacts
        )
        metrics_path.write_text(original_metrics, encoding="utf-8")

        validation_path = artifacts / "task-integrity/validation.json"
        original_validation = validation_path.read_text(encoding="utf-8")
        validation_path.write_text("[]\n", encoding="utf-8")
        validation_shape_errors = local_program_loop_v0.verify_artifact_integrity(
            program_out, queue_out, artifacts
        )
        validation_path.write_text(original_validation, encoding="utf-8")

        event_path = artifacts / "task-integrity/events.ndjson"
        events = read_ndjson(event_path)
        events[0]["schema_version"] = "bogus"
        event_path.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        event_schema_errors = local_program_loop_v0.verify_artifact_integrity(
            program_out, queue_out, artifacts
        )

        event_path.write_text(
            "\n".join(
                json.dumps({**event, "schema_version": SCHEMA_VERSION})
                for event in events
            )
            + "\n",
            encoding="utf-8",
        )
        validation_path.unlink()
        corrupted_errors = local_program_loop_v0.verify_artifact_integrity(
            program_out, queue_out, artifacts
        )
        result = {
            "clean_error_count": len(clean_errors),
            "metric_shape_detected": any("expected object" in err for err in metric_shape_errors),
            "validation_shape_detected": any(
                "expected object" in err for err in validation_shape_errors
            ),
            "event_schema_detected": any(
                "unsupported schema version" in err for err in event_schema_errors
            ),
            "corrupted_error_count": len(corrupted_errors),
            "missing_validation_detected": any(
                "validation.json" in err for err in corrupted_errors
            ),
        }
        require(result["clean_error_count"] == 0, f"clean artifacts failed: {clean_errors}")
        require(result["metric_shape_detected"], "metric object-shape corruption was not detected")
        require(
            result["validation_shape_detected"],
            "validation object-shape corruption was not detected",
        )
        require(result["event_schema_detected"], "event schema corruption was not detected")
        require(result["corrupted_error_count"] > 0, "corrupted artifacts passed")
        require(result["missing_validation_detected"], "missing validation was not detected")
        return result


def scenario_determinism() -> dict[str, Any]:
    orders = []
    for _ in range(20):
        program, queue = backlog_refill_inputs()
        summary = summarize_composed_run("determinism", program, queue, min_open=2)
        orders.append(summary["generated_order"])
    first = orders[0]
    mismatches = sum(1 for order in orders if order != first)
    summary = {
        "runs": len(orders),
        "expected_order": first,
        "mismatch_count": mismatches,
    }
    require(summary["runs"] == 20, "determinism scenario did not run 20 times")
    require(summary["mismatch_count"] == 0, "deterministic scheduling mismatch")
    return summary


def build_summary() -> dict[str, Any]:
    scenarios: dict[str, Callable[[], dict[str, Any]]] = {
        "backlog_refill": scenario_backlog_refill,
        "all_tasks_blocked": scenario_all_blocked,
        "dependency_unblock_partial_success": scenario_dependency_partial,
        "validation_fail_then_retry": scenario_validation_retry,
        "illegal_transition_guard": scenario_transition_guard,
        "interrupted_run_recovery": scenario_interrupted_run_recovery,
        "malformed_input_rejected_before_mutation": scenario_malformed_input_rejected_before_mutation,
        "duplicate_ids_rejected": scenario_duplicate_ids_rejected,
        "schema_version_mismatch": scenario_schema_version_mismatch,
        "v0_2_null_inputs_rejected": scenario_v0_2_null_inputs_rejected,
        "dry_run_behavior": scenario_dry_run_behavior,
        "rerun_idempotency": scenario_rerun_idempotency,
        "artifact_integrity": scenario_artifact_integrity,
        "deterministic_scheduling_20_runs": scenario_determinism,
    }
    results = {name: fn() for name, fn in scenarios.items()}
    return {
        "scenario_count": len(results),
        "scenario_pass_count": len(results),
        "scenarios": results,
    }


def render(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local-agent-loop-v0 regression scenarios")
    parser.add_argument(
        "--update-fixtures",
        action="store_true",
        help="Update checked-in expected summary fixture",
    )
    args = parser.parse_args()

    actual = build_summary()
    actual_text = render(actual)
    if args.update_fixtures:
        EXPECTED_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
        EXPECTED_SUMMARY.write_text(actual_text, encoding="utf-8")
        print(f"updated {EXPECTED_SUMMARY}")
        return 0

    expected_text = EXPECTED_SUMMARY.read_text(encoding="utf-8")
    if actual_text != expected_text:
        diff = difflib.unified_diff(
            expected_text.splitlines(keepends=True),
            actual_text.splitlines(keepends=True),
            fromfile=str(EXPECTED_SUMMARY),
            tofile="actual",
        )
        print("Regression fixture mismatch:")
        print("".join(diff))
        return 1

    print(
        "local-agent-loop-v0 regression scenarios passed: "
        f"{actual['scenario_pass_count']}/{actual['scenario_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
