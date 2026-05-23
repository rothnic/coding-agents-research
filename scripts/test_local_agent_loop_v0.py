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
REQUIRED_METRIC_FIELDS = {
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


def iso_delta_seconds(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


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
    program = {
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
    }
    return program, {"tasks": []}


def all_blocked_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    program = {
        "program_id": "regression-all-blocked",
        "goal": "Reopen eligible blocked work",
        "program_state": "IDLE",
        "last_roadmap_review_ts": "2026-01-01T00:00:00+00:00",
        "roadmap_backlog": [],
        "review_log": [],
    }
    queue = {
        "tasks": [
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
    }
    return program, queue


def dependency_partial_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    program = {
        "program_id": "regression-dependency-partial",
        "goal": "Respect dependency graph before reopening",
        "program_state": "IDLE",
        "last_roadmap_review_ts": "2026-01-01T00:00:00+00:00",
        "roadmap_backlog": [],
        "review_log": [],
    }
    queue = {
        "tasks": [
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
    }
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
            {
                "tasks": [
                    {
                        "id": "task-retry",
                        "title": "Validation retry path",
                        "state": "OPEN",
                        "retries": 0,
                        "simulate_validation_fail_once": True,
                    }
                ]
            },
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


def scenario_composed_transition_enforcement() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="composed-transition-") as tmp:
        tmp_dir = Path(tmp)
        program_file = tmp_dir / "program.json"
        queue_file = tmp_dir / "queue.json"
        artifacts = tmp_dir / "artifacts"
        write_json(
            program_file,
            {
                "program_id": "regression-composed-transition",
                "goal": "Reject invalid persisted state before handoff",
                "program_state": "HANDING_OFF",
                "last_roadmap_review_ts": "2026-01-01T00:00:00+00:00",
                "roadmap_backlog": [
                    {"id": "task-should-not-run", "title": "Should not run"}
                ],
                "review_log": [],
            },
        )
        write_json(queue_file, {"tasks": []})

        rejected = False
        try:
            local_program_loop_v0.run_program(
                program_file=program_file,
                queue_file=queue_file,
                artifacts=artifacts,
                min_open=1,
                timeout_s=0,
                max_retries=2,
            )
        except RuntimeError as exc:
            rejected = "Illegal program transition" in str(exc)

        queue_out = read_json(queue_file)
        events = read_ndjson(artifacts / "roadmap_events.ndjson")
        summary = {
            "rejected": rejected,
            "queue_states": {
                task["id"]: task["state"]
                for task in queue_out.get("tasks", [])
            },
            "events": [stable_program_event(event) for event in events],
        }
        require(summary["rejected"], "composed illegal transition was not rejected")
        require(summary["queue_states"] == {}, "queue mutated after transition rejection")
        require(
            summary["events"][0]["type"] == "PROGRAM_TRANSITION_REJECTED",
            "transition rejection event was not recorded",
        )
        return summary


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
        "composed_transition_enforcement": scenario_composed_transition_enforcement,
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
