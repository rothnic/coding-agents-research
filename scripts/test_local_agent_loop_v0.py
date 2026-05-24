#!/usr/bin/env python3
"""CLI-level regression harness for local-agent-loop-v0."""
from __future__ import annotations

import argparse
import difflib
import json
import shutil
import subprocess
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
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                events.append(json.loads(line))
    return events


def run_cmd(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def init_fixture_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "init"], cwd=path)
    run_cmd(["git", "config", "user.name", "Local Agent Loop"], cwd=path)
    run_cmd(["git", "config", "user.email", "loop@example.invalid"], cwd=path)
    (path / "README.md").write_text("# Fixture Repository\n", encoding="utf-8")
    run_cmd(["git", "add", "README.md"], cwd=path)
    run_cmd(["git", "commit", "-m", "Initial fixture"], cwd=path)
    run_cmd(["git", "branch", "-M", "main"], cwd=path)


def git_stdout(repo: Path, args: list[str]) -> str:
    return run_cmd(["git", *args], cwd=repo).stdout.strip()


def program_doc(**fields: Any) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, **fields}


def queue_doc(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "tasks": tasks}


def worktree_program_doc(program_id: str) -> dict[str, Any]:
    return program_doc(
        program_id=program_id,
        goal="Execute a deterministic local worktree task",
        program_state="IDLE",
        last_roadmap_review_ts=local_program_loop_v0.utc_now(),
        roadmap_backlog=[],
        review_log=[],
    )


def worktree_task(task_id: str, **fields: Any) -> dict[str, Any]:
    task = {
        "id": task_id,
        "title": f"Worktree task {task_id}",
        "state": "OPEN",
        "retries": 0,
        "required_checks": ["fixture-change"],
    }
    task.update(fields)
    return task


def command_backed_task(
    task_id: str,
    *,
    validation_mode: str = "success",
    include_validation: bool = True,
    ambiguous_validation_command: bool = False,
) -> dict[str, Any]:
    path = f"agent-loop-results/{local_agent_loop_v0.worktree_task_key(task_id)}-command.md"
    content = f"# Command Backed Fixture\n\nTask: {task_id}\n"
    action_py = (
        "from pathlib import Path; "
        f"p=Path({path!r}); "
        "p.parent.mkdir(parents=True, exist_ok=True); "
        f"p.write_text({content!r}, encoding='utf-8')"
    )
    action_argv = ["python3", "-c", action_py]
    if validation_mode == "success":
        validation_py = (
            "from pathlib import Path; "
            f"data=Path({path!r}).read_text(encoding='utf-8'); "
            f"assert data == {content!r}; "
            "print('validated command-backed fixture')"
        )
        validation_timeout = 5
    elif validation_mode == "failure":
        validation_py = "import sys; print('validation failed', file=sys.stderr); sys.exit(7)"
        validation_timeout = 5
    elif validation_mode == "timeout":
        validation_py = "import time; time.sleep(2); print('late validation')"
        validation_timeout = 0.1
    else:
        raise AssertionError(f"unknown validation mode {validation_mode!r}")
    validation_argv = ["python3", "-c", validation_py]
    task = worktree_task(
        task_id,
        required_checks=["command-fixture"],
        local_action={
            "adapter": local_agent_loop_v0.COMMAND_BACKED_PATCH_ADAPTER,
            "inputs": {
                "name": "write-command-fixture",
                "command": action_argv,
                "allowed_commands": [action_argv],
                "timeout_sec": 5,
                "expected_path": path,
                "expected_content": content,
            },
        },
    )
    if include_validation:
        task["validation_commands"] = [
            {
                "name": "verify-command-fixture",
                "command": (
                    " ".join(validation_argv)
                    if ambiguous_validation_command
                    else validation_argv
                ),
                "timeout_sec": validation_timeout,
            }
        ]
        task["validation_command_allowlist"] = [validation_argv]
    return task


def worktree_config(repo: Path, worktrees_dir: Path) -> local_agent_loop_v0.WorktreeConfig:
    return local_agent_loop_v0.WorktreeConfig(
        repo=repo,
        worktrees_dir=worktrees_dir,
        base_ref="main",
        branch_prefix="codex/local-agent-loop",
    )


def worktree_branch(task_id: str, cfg: local_agent_loop_v0.WorktreeConfig) -> str:
    return local_agent_loop_v0.worktree_branch_name(task_id, cfg)


def worktree_path(task_id: str, cfg: local_agent_loop_v0.WorktreeConfig) -> Path:
    return local_agent_loop_v0.worktree_path_for_task(task_id, cfg)


def run_worktree_program(
    tmp_dir: Path,
    task: dict[str, Any],
    repo: Path,
    worktrees_dir: Path,
) -> tuple[dict[str, Any], Path, Path, Path]:
    program_file = tmp_dir / "program.json"
    queue_file = tmp_dir / "queue.json"
    artifacts = tmp_dir / "artifacts"
    write_json(program_file, worktree_program_doc(f"program-{task['id']}"))
    write_json(queue_file, queue_doc([task]))
    local_program_loop_v0.run_program(
        program_file=program_file,
        queue_file=queue_file,
        artifacts=artifacts,
        min_open=1,
        timeout_s=300,
        max_retries=2,
        worktree_config=worktree_config(repo, worktrees_dir),
    )
    return read_json(queue_file), program_file, queue_file, artifacts


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


def scenario_worktree_success() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="worktree-success-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            worktree_task("task-worktree-success"),
            repo,
            worktrees,
        )
        task = queue_out["tasks"][0]
        cfg = worktree_config(repo, worktrees)
        branch = worktree_branch("task-worktree-success", cfg)
        metadata = read_json(artifacts / "task-worktree-success/worktree.json")
        validation = read_json(artifacts / "task-worktree-success/validation.json")
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        result = {
            "task_state": task["state"],
            "processed_by": task["processed_by"],
            "commit_sha_valid": local_program_loop_v0.is_commit_sha(
                task.get("worktree_commit_sha")
            ),
            "metadata_commit_matches_task": (
                metadata["commit_sha"] == task.get("worktree_commit_sha")
            ),
            "validation_passed": validation["passed"],
            "branch_exists": branch in git_stdout(repo, ["branch", "--list", branch]),
            "commit_count_on_branch": int(git_stdout(repo, ["rev-list", "--count", branch])),
            "change_file_exists": (
                worktree_path("task-worktree-success", cfg)
                / local_agent_loop_v0.worktree_change_path(task).as_posix()
            ).exists(),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "DONE", "worktree task did not finish DONE")
        require(result["processed_by"] == local_agent_loop_v0.WORKTREE_PROCESSED_BY, "wrong worker")
        require(result["commit_sha_valid"], "DONE worktree task missing valid commit SHA")
        require(result["metadata_commit_matches_task"], "metadata commit did not match task")
        require(result["validation_passed"], "worktree validation failed")
        require(result["branch_exists"], "worktree branch was not created")
        require(result["commit_count_on_branch"] == 2, "worktree branch has wrong commit count")
        require(result["change_file_exists"], "deterministic change file missing")
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_worktree_validation_failure_no_commit() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="worktree-validation-fail-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            worktree_task("task-worktree-fail", simulate_worktree_validation_fail=True),
            repo,
            worktrees,
        )
        task = queue_out["tasks"][0]
        cfg = worktree_config(repo, worktrees)
        branch = worktree_branch("task-worktree-fail", cfg)
        metadata = read_json(artifacts / "task-worktree-fail/worktree.json")
        validation = read_json(artifacts / "task-worktree-fail/validation.json")
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        result = {
            "task_state": task["state"],
            "validation_passed": validation["passed"],
            "commit_sha": task.get("worktree_commit_sha"),
            "metadata_commit_sha": metadata["commit_sha"],
            "branch_commit_count": int(git_stdout(repo, ["rev-list", "--count", branch])),
            "dirty_after_detected": bool(metadata["dirty_status_after"]),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "FAILED", "failed validation task did not become FAILED")
        require(not result["validation_passed"], "validation failure reported pass")
        require(result["commit_sha"] is None, "FAILED task recorded a commit SHA")
        require(result["metadata_commit_sha"] is None, "FAILED metadata recorded a commit SHA")
        require(result["branch_commit_count"] == 1, "validation failure created a commit")
        require(result["dirty_after_detected"], "failed validation did not preserve diagnostics")
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_worktree_dirty_guard() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="worktree-dirty-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        cfg = worktree_config(repo, worktrees)
        branch = worktree_branch("task-worktree-dirty", cfg)
        path = worktree_path("task-worktree-dirty", cfg)
        run_cmd(["git", "worktree", "add", "-b", branch, str(path), "main"], cwd=repo)
        (path / "DIRTY.txt").write_text("uncommitted\n", encoding="utf-8")

        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            worktree_task("task-worktree-dirty"),
            repo,
            worktrees,
        )
        task = queue_out["tasks"][0]
        metadata = read_json(artifacts / "task-worktree-dirty/worktree.json")
        validation = read_json(artifacts / "task-worktree-dirty/validation.json")
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        result = {
            "task_state": task["state"],
            "validation_message": validation["message"],
            "commit_sha": task.get("worktree_commit_sha"),
            "dirty_status_before_count": len(metadata["dirty_status_before"]),
            "change_file_created": (
                path / local_agent_loop_v0.worktree_change_path(task).as_posix()
            ).exists(),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "FAILED", "dirty worktree was not rejected")
        require("dirty before execution" in result["validation_message"], "dirty guard unclear")
        require(result["commit_sha"] is None, "dirty guard recorded a commit")
        require(result["dirty_status_before_count"] > 0, "dirty status was not recorded")
        require(not result["change_file_created"], "dirty guard mutated the worktree")
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_worktree_interrupted_existing_commit_recovery() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="worktree-existing-commit-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        state = tmp_dir / "state"
        init_fixture_repo(repo)
        program_file = state / "program.json"
        queue_file = state / "queue.json"
        artifacts = state / "artifacts"
        write_json(program_file, worktree_program_doc("program-existing-commit"))
        write_json(queue_file, queue_doc([worktree_task("task-worktree-recover")]))
        cfg = worktree_config(repo, worktrees)

        local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=1,
            timeout_s=300,
            max_retries=2,
            worktree_config=cfg,
        )
        first_queue = read_json(queue_file)
        first_commit = first_queue["tasks"][0]["worktree_commit_sha"]
        shutil.rmtree(artifacts / "task-worktree-recover")
        write_json(queue_file, queue_doc([worktree_task("task-worktree-recover")]))

        local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=1,
            timeout_s=300,
            max_retries=2,
            worktree_config=cfg,
        )
        queue_out = read_json(queue_file)
        task = queue_out["tasks"][0]
        metadata = read_json(artifacts / "task-worktree-recover/worktree.json")
        branch = worktree_branch("task-worktree-recover", cfg)
        result = {
            "task_state": task["state"],
            "same_commit_reused": task["worktree_commit_sha"] == first_commit,
            "metadata_commit_reused": metadata["commit_reused"],
            "branch_preexisted": metadata["branch_preexisted"],
            "worktree_preexisted": metadata["worktree_preexisted"],
            "commit_count_on_branch": int(git_stdout(repo, ["rev-list", "--count", branch])),
        }
        require(result["task_state"] == "DONE", "existing commit recovery did not finish DONE")
        require(result["same_commit_reused"], "existing commit recovery created a new commit")
        require(result["metadata_commit_reused"], "metadata did not report commit reuse")
        require(result["branch_preexisted"], "recovery did not reuse existing branch")
        require(result["worktree_preexisted"], "recovery did not reuse existing worktree")
        require(result["commit_count_on_branch"] == 2, "existing commit recovery duplicated commit")
        return result


def scenario_worktree_rerun_idempotency() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="worktree-rerun-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        state = tmp_dir / "state"
        init_fixture_repo(repo)
        program_file = state / "program.json"
        queue_file = state / "queue.json"
        artifacts = state / "artifacts"
        write_json(program_file, worktree_program_doc("program-rerun"))
        write_json(queue_file, queue_doc([worktree_task("task-worktree-once")]))
        cfg = worktree_config(repo, worktrees)

        local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=1,
            timeout_s=300,
            max_retries=2,
            worktree_config=cfg,
        )
        branch = worktree_branch("task-worktree-once", cfg)
        commit_count_after_first = int(git_stdout(repo, ["rev-list", "--count", branch]))
        events_after_first = read_ndjson(artifacts / "task-worktree-once/events.ndjson")
        first_queue = read_json(queue_file)

        local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=1,
            timeout_s=300,
            max_retries=2,
            worktree_config=cfg,
        )
        commit_count_after_second = int(git_stdout(repo, ["rev-list", "--count", branch]))
        events_after_second = read_ndjson(artifacts / "task-worktree-once/events.ndjson")
        second_queue = read_json(queue_file)
        task_ids = [task["id"] for task in second_queue["tasks"]]
        first_commit_sha = first_queue["tasks"][0]["worktree_commit_sha"]
        second_commit_sha = second_queue["tasks"][0]["worktree_commit_sha"]
        result = {
            "task_ids": task_ids,
            "unique_task_id_count": len(set(task_ids)),
            "commit_sha_valid": local_program_loop_v0.is_commit_sha(second_commit_sha),
            "same_commit_sha": first_commit_sha == second_commit_sha,
            "commit_count_after_first": commit_count_after_first,
            "commit_count_after_second": commit_count_after_second,
            "task_event_count_after_first": len(events_after_first),
            "task_event_count_after_second": len(events_after_second),
            "worktree_dir_count": len([path for path in worktrees.iterdir() if path.is_dir()]),
        }
        require(result["unique_task_id_count"] == len(task_ids), "rerun duplicated task ids")
        require(result["commit_sha_valid"], "rerun task missing commit")
        require(result["same_commit_sha"], "rerun changed commit")
        require(
            result["commit_count_after_second"] == result["commit_count_after_first"],
            "rerun created duplicate commit",
        )
        require(
            result["task_event_count_after_second"] == result["task_event_count_after_first"],
            "rerun appended terminal task events",
        )
        require(result["worktree_dir_count"] == 1, "rerun created duplicate worktrees")
        return result


def scenario_worktree_missing_artifact_detection() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="worktree-missing-artifact-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            worktree_task("task-worktree-missing-artifact"),
            repo,
            worktrees,
        )
        (artifacts / "task-worktree-missing-artifact/worktree.json").unlink()
        errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        result = {
            "error_count": len(errors),
            "missing_worktree_metadata_detected": any("worktree.json" in err for err in errors),
        }
        require(result["error_count"] > 0, "missing worktree artifact passed integrity")
        require(
            result["missing_worktree_metadata_detected"],
            "missing worktree metadata was not detected",
        )
        return result


def scenario_worktree_branch_and_worktree_reuse() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="worktree-reuse-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        cfg = worktree_config(repo, worktrees)
        branch = worktree_branch("task-worktree-reuse", cfg)
        path = worktree_path("task-worktree-reuse", cfg)
        run_cmd(["git", "worktree", "add", "-b", branch, str(path), "main"], cwd=repo)

        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            worktree_task("task-worktree-reuse"),
            repo,
            worktrees,
        )
        task = queue_out["tasks"][0]
        metadata = read_json(artifacts / "task-worktree-reuse/worktree.json")
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        result = {
            "task_state": task["state"],
            "branch_preexisted": metadata["branch_preexisted"],
            "worktree_preexisted": metadata["worktree_preexisted"],
            "commit_sha_valid": local_program_loop_v0.is_commit_sha(
                task.get("worktree_commit_sha")
            ),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "DONE", "preexisting worktree task did not finish")
        require(result["branch_preexisted"], "preexisting branch was not reused")
        require(result["worktree_preexisted"], "preexisting worktree was not reused")
        require(result["commit_sha_valid"], "reuse scenario missing commit")
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_worktree_terminal_artifact_recovery() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="worktree-terminal-artifact-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        state = tmp_dir / "state"
        init_fixture_repo(repo)
        program_file = state / "program.json"
        queue_file = state / "queue.json"
        artifacts = state / "artifacts"
        write_json(program_file, worktree_program_doc("program-terminal-artifact"))
        write_json(queue_file, queue_doc([worktree_task("task-worktree-terminal")]))
        cfg = worktree_config(repo, worktrees)

        local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=1,
            timeout_s=300,
            max_retries=2,
            worktree_config=cfg,
        )
        first_queue = read_json(queue_file)
        first_commit = first_queue["tasks"][0]["worktree_commit_sha"]
        events_after_first = read_ndjson(artifacts / "task-worktree-terminal/events.ndjson")
        write_json(queue_file, queue_doc([worktree_task("task-worktree-terminal")]))

        local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=1,
            timeout_s=300,
            max_retries=2,
            worktree_config=cfg,
        )
        queue_out = read_json(queue_file)
        events_after_second = read_ndjson(artifacts / "task-worktree-terminal/events.ndjson")
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        result = {
            "task_state": queue_out["tasks"][0]["state"],
            "same_commit_recovered": queue_out["tasks"][0]["worktree_commit_sha"] == first_commit,
            "event_count_after_first": len(events_after_first),
            "event_count_after_second": len(events_after_second),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "DONE", "terminal artifact recovery did not finish DONE")
        require(result["same_commit_recovered"], "terminal artifact recovery changed commit")
        require(
            result["event_count_after_second"] == result["event_count_after_first"],
            "terminal artifact recovery appended duplicate events",
        )
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_worktree_ambiguous_branch_rejected() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="worktree-ambiguous-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        cfg = worktree_config(repo, worktrees)
        branch = worktree_branch("task-worktree-ambiguous", cfg)
        path = worktree_path("task-worktree-ambiguous", cfg)
        run_cmd(["git", "worktree", "add", "-b", branch, str(path), "main"], cwd=repo)
        (path / "UNRELATED.md").write_text("unrelated\n", encoding="utf-8")
        run_cmd(["git", "add", "UNRELATED.md"], cwd=path)
        run_cmd(["git", "commit", "-m", "Unrelated task history"], cwd=path)

        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            worktree_task("task-worktree-ambiguous"),
            repo,
            worktrees,
        )
        task = queue_out["tasks"][0]
        validation = read_json(artifacts / "task-worktree-ambiguous/validation.json")
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        result = {
            "task_state": task["state"],
            "commit_sha": task.get("worktree_commit_sha"),
            "validation_message": validation["message"],
            "branch_commit_count": int(git_stdout(repo, ["rev-list", "--count", branch])),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "FAILED", "ambiguous branch was not rejected")
        require(result["commit_sha"] is None, "ambiguous branch recorded a commit")
        require(
            "preexisting task branch" in result["validation_message"],
            "ambiguous branch diagnostic was unclear",
        )
        require(result["branch_commit_count"] == 2, "ambiguous branch was mutated")
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_worktree_task_id_collision_isolated() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="worktree-collision-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        state = tmp_dir / "state"
        init_fixture_repo(repo)
        program_file = state / "program.json"
        queue_file = state / "queue.json"
        artifacts = state / "artifacts"
        tasks = [
            worktree_task("task/a"),
            worktree_task("task a"),
        ]
        write_json(program_file, worktree_program_doc("program-collision"))
        write_json(queue_file, queue_doc(tasks))
        cfg = worktree_config(repo, worktrees)
        local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=2,
            timeout_s=300,
            max_retries=2,
            worktree_config=cfg,
        )
        queue_out = read_json(queue_file)
        branches = [task["worktree_branch"] for task in queue_out["tasks"]]
        paths = [task["worktree_path"] for task in queue_out["tasks"]]
        commits = [task["worktree_commit_sha"] for task in queue_out["tasks"]]
        result = {
            "states": [task["state"] for task in queue_out["tasks"]],
            "unique_branch_count": len(set(branches)),
            "unique_worktree_path_count": len(set(paths)),
            "commit_sha_count": sum(local_program_loop_v0.is_commit_sha(commit) for commit in commits),
        }
        require(result["states"] == ["DONE", "DONE"], "collision tasks did not complete")
        require(result["unique_branch_count"] == 2, "collision tasks shared a branch")
        require(result["unique_worktree_path_count"] == 2, "collision tasks shared a worktree")
        require(result["commit_sha_count"] == 2, "collision tasks missing commits")
        return result


def scenario_worktree_metadata_tamper_detection() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="worktree-metadata-tamper-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            worktree_task("task-worktree-tamper"),
            repo,
            worktrees,
        )
        metadata_path = artifacts / "task-worktree-tamper/worktree.json"
        metadata = read_json(metadata_path)
        metadata["branch_name"] = "codex/local-agent-loop/wrong"
        metadata["worktree_path"] = str(tmp_dir / "wrong-worktree")
        write_json(metadata_path, metadata)
        errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        result = {
            "error_count": len(errors),
            "branch_mismatch_detected": any("branch_name mismatch" in err for err in errors),
            "path_mismatch_detected": any("worktree_path mismatch" in err for err in errors),
        }
        require(result["error_count"] > 0, "tampered worktree metadata passed")
        require(result["branch_mismatch_detected"], "branch tamper was not detected")
        require(result["path_mismatch_detected"], "path tamper was not detected")
        return result


def scenario_worktree_relative_dir_cli() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="worktree-relative-cli-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        init_fixture_repo(repo)
        program_file = tmp_dir / "program.json"
        queue_file = tmp_dir / "queue.json"
        artifacts = tmp_dir / "artifacts"
        write_json(program_file, worktree_program_doc("program-relative-cli"))
        write_json(queue_file, queue_doc([worktree_task("task-worktree-relative")]))
        run_cmd(
            [
                "python3",
                str(ROOT / "scripts/local_program_loop_v0.py"),
                "run-program",
                "--program",
                str(program_file),
                "--queue",
                str(queue_file),
                "--artifacts",
                str(artifacts),
                "--execution-mode",
                "worktree",
                "--worktree-repo",
                "repo",
                "--worktrees-dir",
                ".worktrees",
                "--worktree-base-ref",
                "main",
            ],
            cwd=tmp_dir,
        )
        queue_out = read_json(queue_file)
        result = {
            "task_state": queue_out["tasks"][0]["state"],
            "relative_worktree_root_created": (tmp_dir / ".worktrees").is_dir(),
            "worktree_path_is_absolute": Path(queue_out["tasks"][0]["worktree_path"]).is_absolute(),
        }
        require(result["task_state"] == "DONE", "relative worktrees-dir CLI run failed")
        require(result["relative_worktree_root_created"], "relative worktree dir was not resolved")
        require(result["worktree_path_is_absolute"], "recorded worktree path was not absolute")
        return result


def scenario_command_backed_adapter_success() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="command-backed-success-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        task_id = "task-command-backed-success"
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            command_backed_task(task_id),
            repo,
            worktrees,
        )
        task = queue_out["tasks"][0]
        metadata = read_json(artifacts / f"{task_id}/worktree.json")
        validation = read_json(artifacts / f"{task_id}/validation.json")
        validation_output = metadata["validation_command_outputs"][0]
        output_artifact = artifacts / task_id / validation_output["output_artifact"]
        branch = worktree_branch(task_id, worktree_config(repo, worktrees))
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        result = {
            "task_state": task["state"],
            "adapter": metadata["action_adapter"],
            "commit_sha_valid": local_program_loop_v0.is_commit_sha(
                task.get("worktree_commit_sha")
            ),
            "validation_passed": validation["passed"],
            "action_output_count": len(metadata["action_command_outputs"]),
            "validation_output_count": len(metadata["validation_command_outputs"]),
            "validation_stdout_captured": (
                "validated command-backed fixture" in validation_output["stdout"]
            ),
            "output_artifact_exists": output_artifact.exists(),
            "commit_count_on_branch": int(git_stdout(repo, ["rev-list", "--count", branch])),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "DONE", "command-backed task did not finish DONE")
        require(
            result["adapter"] == local_agent_loop_v0.COMMAND_BACKED_PATCH_ADAPTER,
            "metadata recorded wrong adapter",
        )
        require(result["commit_sha_valid"], "command-backed task missing commit")
        require(result["validation_passed"], "command-backed validation failed")
        require(result["action_output_count"] == 1, "action command output not captured")
        require(result["validation_output_count"] == 1, "validation command output not captured")
        require(result["validation_stdout_captured"], "validation stdout missing")
        require(result["output_artifact_exists"], "validation output artifact missing")
        require(result["commit_count_on_branch"] == 2, "command-backed branch commit count wrong")
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_command_backed_validation_failure_no_commit() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="command-backed-validation-fail-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        task_id = "task-command-backed-validation-fail"
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            command_backed_task(task_id, validation_mode="failure"),
            repo,
            worktrees,
        )
        task = queue_out["tasks"][0]
        metadata = read_json(artifacts / f"{task_id}/worktree.json")
        validation_output = metadata["validation_command_outputs"][0]
        branch = worktree_branch(task_id, worktree_config(repo, worktrees))
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        result = {
            "task_state": task["state"],
            "commit_sha": task.get("worktree_commit_sha"),
            "metadata_commit_sha": metadata["commit_sha"],
            "validation_exit_code": validation_output["returncode"],
            "validation_stderr_captured": "validation failed" in validation_output["stderr"],
            "branch_commit_count": int(git_stdout(repo, ["rev-list", "--count", branch])),
            "dirty_after_detected": bool(metadata["dirty_status_after"]),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "FAILED", "validation failure did not fail task")
        require(result["commit_sha"] is None, "validation failure recorded task commit")
        require(result["metadata_commit_sha"] is None, "validation failure recorded metadata commit")
        require(result["validation_exit_code"] == 7, "validation exit code was not captured")
        require(result["validation_stderr_captured"], "validation stderr was not captured")
        require(result["branch_commit_count"] == 1, "validation failure created commit")
        require(result["dirty_after_detected"], "validation failure lost dirty diagnostics")
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_command_backed_validation_timeout_no_commit() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="command-backed-timeout-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        task_id = "task-command-backed-timeout"
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            command_backed_task(task_id, validation_mode="timeout"),
            repo,
            worktrees,
        )
        task = queue_out["tasks"][0]
        metadata = read_json(artifacts / f"{task_id}/worktree.json")
        validation_output = metadata["validation_command_outputs"][0]
        branch = worktree_branch(task_id, worktree_config(repo, worktrees))
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        result = {
            "task_state": task["state"],
            "commit_sha": task.get("worktree_commit_sha"),
            "metadata_commit_sha": metadata["commit_sha"],
            "command_timed_out": metadata["command_timed_out"],
            "validation_timed_out": validation_output["timed_out"],
            "validation_returncode": validation_output["returncode"],
            "branch_commit_count": int(git_stdout(repo, ["rev-list", "--count", branch])),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "FAILED", "timeout did not fail task")
        require(result["commit_sha"] is None, "timeout recorded task commit")
        require(result["metadata_commit_sha"] is None, "timeout recorded metadata commit")
        require(result["command_timed_out"], "metadata did not record timeout")
        require(result["validation_timed_out"], "validation output did not record timeout")
        require(result["validation_returncode"] is None, "timeout returncode should be null")
        require(result["branch_commit_count"] == 1, "timeout created commit")
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_command_backed_rerun_idempotency() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="command-backed-rerun-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        state = tmp_dir / "state"
        init_fixture_repo(repo)
        task_id = "task-command-backed-once"
        program_file = state / "program.json"
        queue_file = state / "queue.json"
        artifacts = state / "artifacts"
        write_json(program_file, worktree_program_doc("program-command-rerun"))
        write_json(queue_file, queue_doc([command_backed_task(task_id)]))
        cfg = worktree_config(repo, worktrees)
        local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=1,
            timeout_s=300,
            max_retries=2,
            worktree_config=cfg,
        )
        first_queue = read_json(queue_file)
        first_metadata = read_json(artifacts / f"{task_id}/worktree.json")
        events_after_first = read_ndjson(artifacts / f"{task_id}/events.ndjson")
        branch = worktree_branch(task_id, cfg)
        commit_count_after_first = int(git_stdout(repo, ["rev-list", "--count", branch]))
        local_program_loop_v0.run_program(
            program_file=program_file,
            queue_file=queue_file,
            artifacts=artifacts,
            min_open=1,
            timeout_s=300,
            max_retries=2,
            worktree_config=cfg,
        )
        second_queue = read_json(queue_file)
        second_metadata = read_json(artifacts / f"{task_id}/worktree.json")
        events_after_second = read_ndjson(artifacts / f"{task_id}/events.ndjson")
        result = {
            "same_commit_sha": (
                first_queue["tasks"][0]["worktree_commit_sha"]
                == second_queue["tasks"][0]["worktree_commit_sha"]
            ),
            "commit_count_after_first": commit_count_after_first,
            "commit_count_after_second": int(git_stdout(repo, ["rev-list", "--count", branch])),
            "event_count_after_first": len(events_after_first),
            "event_count_after_second": len(events_after_second),
            "worktree_dir_count": len([path for path in worktrees.iterdir() if path.is_dir()]),
            "action_output_count_stable": (
                len(first_metadata["action_command_outputs"])
                == len(second_metadata["action_command_outputs"])
            ),
            "validation_output_count_stable": (
                len(first_metadata["validation_command_outputs"])
                == len(second_metadata["validation_command_outputs"])
            ),
        }
        require(result["same_commit_sha"], "rerun changed command-backed commit")
        require(
            result["commit_count_after_second"] == result["commit_count_after_first"],
            "rerun created duplicate command-backed commit",
        )
        require(
            result["event_count_after_second"] == result["event_count_after_first"],
            "rerun duplicated command-backed events",
        )
        require(result["worktree_dir_count"] == 1, "rerun created duplicate worktrees")
        require(result["action_output_count_stable"], "rerun duplicated action outputs")
        require(result["validation_output_count_stable"], "rerun duplicated validation outputs")
        return result


def scenario_command_backed_missing_output_artifact_detection() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="command-backed-missing-output-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        task_id = "task-command-backed-missing-output"
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            command_backed_task(task_id),
            repo,
            worktrees,
        )
        metadata_path = artifacts / f"{task_id}/worktree.json"
        metadata = read_json(metadata_path)
        output_artifact = artifacts / task_id / metadata["validation_command_outputs"][0][
            "output_artifact"
        ]
        output_artifact.unlink()
        errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        result = {
            "error_count": len(errors),
            "missing_command_output_detected": any(
                "missing command output artifact" in err for err in errors
            ),
        }
        require(result["error_count"] > 0, "missing command output artifact passed")
        require(result["missing_command_output_detected"], "missing output artifact undetected")
        return result


def scenario_command_backed_done_metadata_integrity() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="command-backed-integrity-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        task_id = "task-command-backed-integrity"
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            command_backed_task(task_id),
            repo,
            worktrees,
        )
        metadata_path = artifacts / f"{task_id}/worktree.json"
        original_metadata = read_json(metadata_path)
        clean_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )

        metadata = dict(original_metadata)
        metadata.pop("action_adapter")
        write_json(metadata_path, metadata)
        missing_adapter_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )

        metadata = dict(original_metadata)
        metadata["validation_command_outputs"] = []
        write_json(metadata_path, metadata)
        missing_evidence_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )

        metadata = dict(original_metadata)
        metadata["commit_sha"] = None
        write_json(metadata_path, metadata)
        missing_commit_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )

        result = {
            "clean_error_count": len(clean_errors),
            "missing_adapter_detected": any(
                "missing action_adapter" in err for err in missing_adapter_errors
            ),
            "missing_evidence_detected": any(
                "missing validation command evidence" in err
                for err in missing_evidence_errors
            ),
            "missing_commit_detected": any(
                "DONE task missing commit SHA" in err for err in missing_commit_errors
            ),
        }
        require(result["clean_error_count"] == 0, f"clean artifacts failed: {clean_errors}")
        require(result["missing_adapter_detected"], "missing action adapter passed integrity")
        require(result["missing_evidence_detected"], "missing validation evidence passed")
        require(result["missing_commit_detected"], "missing commit SHA passed integrity")
        return result


def scenario_unknown_adapter_rejected() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="unknown-adapter-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        task_id = "task-unknown-adapter"
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            worktree_task(
                task_id,
                local_action={"adapter": "unknown-adapter", "inputs": {}},
            ),
            repo,
            worktrees,
        )
        task = queue_out["tasks"][0]
        validation = read_json(artifacts / f"{task_id}/validation.json")
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        branch = worktree_branch(task_id, worktree_config(repo, worktrees))
        result = {
            "task_state": task["state"],
            "commit_sha": task.get("worktree_commit_sha"),
            "validation_message": validation["message"],
            "branch_exists": bool(git_stdout(repo, ["branch", "--list", branch])),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "FAILED", "unknown adapter was not rejected")
        require(result["commit_sha"] is None, "unknown adapter recorded commit")
        require("unknown worktree action adapter" in result["validation_message"], "bad diagnostic")
        require(not result["branch_exists"], "unknown adapter created branch")
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_ambiguous_validation_command_rejected() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ambiguous-command-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        task_id = "task-ambiguous-validation-command"
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            command_backed_task(task_id, ambiguous_validation_command=True),
            repo,
            worktrees,
        )
        task = queue_out["tasks"][0]
        validation = read_json(artifacts / f"{task_id}/validation.json")
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        branch = worktree_branch(task_id, worktree_config(repo, worktrees))
        result = {
            "task_state": task["state"],
            "validation_message": validation["message"],
            "branch_exists": bool(git_stdout(repo, ["branch", "--list", branch])),
            "commit_sha": task.get("worktree_commit_sha"),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "FAILED", "ambiguous command did not fail task")
        require("expected non-empty argv list" in result["validation_message"], "bad diagnostic")
        require(not result["branch_exists"], "ambiguous command created branch")
        require(result["commit_sha"] is None, "ambiguous command recorded commit")
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_missing_validation_evidence_rejected() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="missing-validation-evidence-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        task_id = "task-missing-validation-evidence"
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            command_backed_task(task_id, include_validation=False),
            repo,
            worktrees,
        )
        task = queue_out["tasks"][0]
        validation = read_json(artifacts / f"{task_id}/validation.json")
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        branch = worktree_branch(task_id, worktree_config(repo, worktrees))
        result = {
            "task_state": task["state"],
            "validation_message": validation["message"],
            "branch_exists": bool(git_stdout(repo, ["branch", "--list", branch])),
            "commit_sha": task.get("worktree_commit_sha"),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "FAILED", "missing validation did not fail task")
        require("requires validation_commands" in result["validation_message"], "bad diagnostic")
        require(not result["branch_exists"], "missing validation created branch")
        require(result["commit_sha"] is None, "missing validation recorded commit")
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_unsafe_action_path_rejected() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="unsafe-action-path-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        task_id = "task-unsafe-action-path"
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            worktree_task(
                task_id,
                local_action={
                    "adapter": local_agent_loop_v0.DETERMINISTIC_FILE_ADAPTER,
                    "inputs": {
                        "path": "../escape.md",
                        "content": "escape\n",
                    },
                },
            ),
            repo,
            worktrees,
        )
        task = queue_out["tasks"][0]
        validation = read_json(artifacts / f"{task_id}/validation.json")
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        branch = worktree_branch(task_id, worktree_config(repo, worktrees))
        result = {
            "task_state": task["state"],
            "validation_message": validation["message"],
            "branch_exists": bool(git_stdout(repo, ["branch", "--list", branch])),
            "commit_sha": task.get("worktree_commit_sha"),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "FAILED", "unsafe path did not fail task")
        require("unsafe worktree change path" in result["validation_message"], "bad diagnostic")
        require(not result["branch_exists"], "unsafe path created branch")
        require(result["commit_sha"] is None, "unsafe path recorded commit")
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_git_control_path_rejected() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="git-control-path-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        task_id = "task-git-control-path"
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            worktree_task(
                task_id,
                local_action={
                    "adapter": local_agent_loop_v0.DETERMINISTIC_FILE_ADAPTER,
                    "inputs": {
                        "path": ".git",
                        "content": "corrupt\n",
                    },
                },
            ),
            repo,
            worktrees,
        )
        task = queue_out["tasks"][0]
        validation = read_json(artifacts / f"{task_id}/validation.json")
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        branch = worktree_branch(task_id, worktree_config(repo, worktrees))
        result = {
            "task_state": task["state"],
            "validation_message": validation["message"],
            "branch_exists": bool(git_stdout(repo, ["branch", "--list", branch])),
            "commit_sha": task.get("worktree_commit_sha"),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "FAILED", "git control path did not fail task")
        require("unsafe worktree change path" in result["validation_message"], "bad diagnostic")
        require(not result["branch_exists"], "git control path created branch")
        require(result["commit_sha"] is None, "git control path recorded commit")
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_ambiguous_action_command_rejected() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ambiguous-action-command-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        task_id = "task-ambiguous-action-command"
        task = command_backed_task(task_id)
        task["local_action"]["inputs"]["argv"] = [
            "python3",
            "-c",
            "print('different action')",
        ]
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            task,
            repo,
            worktrees,
        )
        task_out = queue_out["tasks"][0]
        validation = read_json(artifacts / f"{task_id}/validation.json")
        integrity_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        branch = worktree_branch(task_id, worktree_config(repo, worktrees))
        result = {
            "task_state": task_out["state"],
            "validation_message": validation["message"],
            "branch_exists": bool(git_stdout(repo, ["branch", "--list", branch])),
            "commit_sha": task_out.get("worktree_commit_sha"),
            "integrity_error_count": len(integrity_errors),
        }
        require(result["task_state"] == "FAILED", "ambiguous action command passed")
        require("command and argv disagree" in result["validation_message"], "bad diagnostic")
        require(not result["branch_exists"], "ambiguous action command created branch")
        require(result["commit_sha"] is None, "ambiguous action command recorded commit")
        require(result["integrity_error_count"] == 0, f"integrity failed: {integrity_errors}")
        return result


def scenario_validation_output_spoof_detection() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="validation-output-spoof-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        task_id = "task-validation-output-spoof"
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            command_backed_task(task_id),
            repo,
            worktrees,
        )
        metadata_path = artifacts / f"{task_id}/worktree.json"
        metadata = read_json(metadata_path)
        metadata["validation_command_outputs"] = metadata["action_command_outputs"]
        write_json(metadata_path, metadata)
        errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        result = {
            "error_count": len(errors),
            "kind_mismatch_detected": any(
                "expected validation command output" in err for err in errors
            ),
            "declared_command_mismatch_detected": any(
                "validation_command_outputs does not match declared commands" in err
                for err in errors
            ),
        }
        require(result["error_count"] > 0, "spoofed validation output passed")
        require(result["kind_mismatch_detected"], "validation kind spoof was not detected")
        require(
            result["declared_command_mismatch_detected"],
            "validation command spoof was not detected",
        )
        return result


def scenario_deterministic_validation_evidence_integrity() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="deterministic-validation-evidence-") as tmp:
        tmp_dir = Path(tmp)
        repo = tmp_dir / "repo"
        worktrees = tmp_dir / "worktrees"
        init_fixture_repo(repo)
        task_id = "task-deterministic-validation-evidence"
        validation_argv = ["python3", "-c", "print('deterministic validation')"]
        task = worktree_task(
            task_id,
            validation_commands=[
                {
                    "name": "deterministic-validation",
                    "command": validation_argv,
                    "timeout_sec": 5,
                }
            ],
            validation_command_allowlist=[validation_argv],
        )
        queue_out, program_file, _, artifacts = run_worktree_program(
            tmp_dir / "state",
            task,
            repo,
            worktrees,
        )
        metadata_path = artifacts / f"{task_id}/worktree.json"
        metadata = read_json(metadata_path)
        clean_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        metadata["validation_command_outputs"] = []
        write_json(metadata_path, metadata)
        missing_evidence_errors = local_program_loop_v0.verify_artifact_integrity(
            read_json(program_file),
            queue_out,
            artifacts,
        )
        result = {
            "clean_error_count": len(clean_errors),
            "missing_evidence_detected": any(
                "missing validation command evidence" in err
                for err in missing_evidence_errors
            ),
        }
        require(result["clean_error_count"] == 0, f"clean artifacts failed: {clean_errors}")
        require(result["missing_evidence_detected"], "deterministic evidence gap passed")
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
        "worktree_success": scenario_worktree_success,
        "worktree_validation_failure_no_commit": scenario_worktree_validation_failure_no_commit,
        "worktree_dirty_guard": scenario_worktree_dirty_guard,
        "worktree_interrupted_existing_commit_recovery": (
            scenario_worktree_interrupted_existing_commit_recovery
        ),
        "worktree_rerun_idempotency": scenario_worktree_rerun_idempotency,
        "worktree_missing_artifact_detection": scenario_worktree_missing_artifact_detection,
        "worktree_branch_and_worktree_reuse": scenario_worktree_branch_and_worktree_reuse,
        "worktree_terminal_artifact_recovery": scenario_worktree_terminal_artifact_recovery,
        "worktree_ambiguous_branch_rejected": scenario_worktree_ambiguous_branch_rejected,
        "worktree_task_id_collision_isolated": scenario_worktree_task_id_collision_isolated,
        "worktree_metadata_tamper_detection": scenario_worktree_metadata_tamper_detection,
        "worktree_relative_dir_cli": scenario_worktree_relative_dir_cli,
        "command_backed_adapter_success": scenario_command_backed_adapter_success,
        "command_backed_validation_failure_no_commit": (
            scenario_command_backed_validation_failure_no_commit
        ),
        "command_backed_validation_timeout_no_commit": (
            scenario_command_backed_validation_timeout_no_commit
        ),
        "command_backed_rerun_idempotency": scenario_command_backed_rerun_idempotency,
        "command_backed_missing_output_artifact_detection": (
            scenario_command_backed_missing_output_artifact_detection
        ),
        "command_backed_done_metadata_integrity": scenario_command_backed_done_metadata_integrity,
        "unknown_adapter_rejected": scenario_unknown_adapter_rejected,
        "ambiguous_validation_command_rejected": scenario_ambiguous_validation_command_rejected,
        "missing_validation_evidence_rejected": scenario_missing_validation_evidence_rejected,
        "unsafe_action_path_rejected": scenario_unsafe_action_path_rejected,
        "git_control_path_rejected": scenario_git_control_path_rejected,
        "ambiguous_action_command_rejected": scenario_ambiguous_action_command_rejected,
        "validation_output_spoof_detection": scenario_validation_output_spoof_detection,
        "deterministic_validation_evidence_integrity": (
            scenario_deterministic_validation_evidence_integrity
        ),
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
