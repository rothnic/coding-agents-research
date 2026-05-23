#!/usr/bin/env python3
"""Tiny local Symphony-like event-loop proof of concept.

No cloud dependencies. Uses local JSON files as a beads-like queue substrate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
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
WORKTREE_METADATA_ARTIFACT = "worktree.json"
WORKTREE_PROCESSED_BY = "local-agent-loop-worktree-v0"


@dataclass
class Config:
    max_retries: int = 2


@dataclass
class GitResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "args": self.args,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass
class WorktreeConfig:
    repo: Path
    worktrees_dir: Path
    base_ref: str = "HEAD"
    branch_prefix: str = "codex/local-agent-loop"


class ValidationError(RuntimeError):
    """Raised when local loop JSON inputs are invalid."""


class WorktreeExecutionError(RuntimeError):
    """Raised when a worktree-backed task cannot be executed safely."""


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


def run_git(cwd: Path, args: list[str], check: bool = True) -> GitResult:
    command = ["git", "-C", str(cwd), *args]
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result = GitResult(
        args=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise WorktreeExecutionError(f"{' '.join(command)}: {stderr}")
    return result


def slugify_ref_component(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return slug or "task"


def worktree_task_key(task_id: str) -> str:
    slug = slugify_ref_component(task_id)[:48].rstrip(".-") or "task"
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def worktree_branch_name(task_id: str, cfg: WorktreeConfig) -> str:
    return f"{cfg.branch_prefix.rstrip('/')}/{worktree_task_key(task_id)}"


def worktree_path_for_task(task_id: str, cfg: WorktreeConfig) -> Path:
    return cfg.worktrees_dir / worktree_task_key(task_id)


def build_worktree_config(
    execution_mode: str,
    repo: Path | None,
    worktrees_dir: Path | None,
    base_ref: str,
    branch_prefix: str,
) -> WorktreeConfig | None:
    if execution_mode == "state-machine":
        return None
    if execution_mode != "worktree":
        raise ValidationError(f"unsupported execution mode {execution_mode!r}")
    if repo is None:
        raise ValidationError("--worktree-repo is required for worktree execution")
    if worktrees_dir is None:
        raise ValidationError("--worktrees-dir is required for worktree execution")
    return WorktreeConfig(
        repo=repo.resolve(),
        worktrees_dir=worktrees_dir.resolve(),
        base_ref=base_ref,
        branch_prefix=branch_prefix,
    )


def git_ref_exists(repo: Path, branch: str) -> bool:
    result = run_git(
        repo,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
    )
    return result.returncode == 0


def git_status_lines(repo: Path) -> list[str]:
    result = run_git(repo, ["status", "--porcelain"], check=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


def validate_relative_repo_path(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute() or ".." in path.parts:
        raise WorktreeExecutionError(f"unsafe worktree change path {path_text!r}")
    if not path.parts:
        raise WorktreeExecutionError("worktree change path must not be empty")
    return path


def worktree_change_path(task: dict[str, Any]) -> Path:
    raw = task.get("worktree_change_path")
    if raw is None:
        raw = f"agent-loop-results/{worktree_task_key(task['id'])}.md"
    if not isinstance(raw, str) or not raw.strip():
        raise WorktreeExecutionError("worktree_change_path must be a non-empty string")
    return validate_relative_repo_path(raw)


def worktree_change_content(task: dict[str, Any]) -> str:
    explicit = task.get("worktree_change_content")
    if explicit is not None:
        if not isinstance(explicit, str):
            raise WorktreeExecutionError("worktree_change_content must be a string")
        return explicit if explicit.endswith("\n") else explicit + "\n"
    return (
        "# Local Agent Loop Worktree Result\n\n"
        f"- Task: `{task['id']}`\n"
        f"- Title: {task.get('title', 'untitled')}\n"
        f"- Schema: `{SCHEMA_VERSION}`\n"
    )


def read_ndjson_objects(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except JSONDecodeError as exc:
                raise WorktreeExecutionError(
                    f"{path}:{line_number}: invalid NDJSON event: {exc.msg}"
                ) from exc
            if not isinstance(event, dict):
                raise WorktreeExecutionError(f"{path}:{line_number}: expected event object")
            events.append(event)
    return events


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


def write_worktree_plan(task: dict[str, Any], task_dir: Path, cfg: WorktreeConfig) -> None:
    plan = task_dir / "plan.md"
    if plan.exists():
        return
    branch = worktree_branch_name(task["id"], cfg)
    worktree_path = worktree_path_for_task(task["id"], cfg)
    change_path = worktree_change_path(task)
    plan.write_text(
        "# Plan\n\n"
        "1. Create or reuse the deterministic task branch and worktree.\n"
        "2. Apply the deterministic fixture change.\n"
        "3. Run local validation checks.\n"
        "4. Commit successful work and write execution metadata.\n\n"
        f"- Branch: `{branch}`\n"
        f"- Worktree: `{worktree_path}`\n"
        f"- Change path: `{change_path.as_posix()}`\n",
        encoding="utf-8",
    )


def initial_worktree_metadata(task: dict[str, Any], cfg: WorktreeConfig) -> dict[str, Any]:
    task_id = task["id"]
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "repo_path": str(cfg.repo),
        "base_ref": cfg.base_ref,
        "branch_name": worktree_branch_name(task_id, cfg),
        "worktree_path": str(worktree_path_for_task(task_id, cfg)),
        "commit_sha": None,
        "commit_reused": False,
        "branch_preexisted": None,
        "worktree_preexisted": None,
        "dirty_status_before": [],
        "dirty_status_after": [],
        "change_path": None,
        "validation_output": None,
        "final_task_state": task.get("state"),
    }


def ensure_task_worktree(task: dict[str, Any], cfg: WorktreeConfig) -> dict[str, Any]:
    repo = cfg.repo
    if not repo.exists():
        raise WorktreeExecutionError(f"{repo}: worktree repository does not exist")
    result = run_git(repo, ["rev-parse", "--show-toplevel"], check=True)
    repo_root = Path(result.stdout.strip())
    branch = worktree_branch_name(task["id"], cfg)
    worktree_path = worktree_path_for_task(task["id"], cfg)
    base_sha = run_git(repo_root, ["rev-parse", "--verify", cfg.base_ref], check=True).stdout.strip()
    branch_preexisted = git_ref_exists(repo_root, branch)
    worktree_preexisted = worktree_path.exists()

    cfg.worktrees_dir.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        if not (worktree_path / ".git").exists():
            raise WorktreeExecutionError(
                f"{worktree_path}: expected git worktree at deterministic path"
            )
        current_branch = run_git(
            worktree_path,
            ["rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
        ).stdout.strip()
        if current_branch != branch:
            raise WorktreeExecutionError(
                f"{worktree_path}: checked out {current_branch!r}, expected {branch!r}"
            )
    elif branch_preexisted:
        run_git(repo_root, ["worktree", "add", str(worktree_path), branch], check=True)
    else:
        run_git(
            repo_root,
            ["worktree", "add", "-b", branch, str(worktree_path), cfg.base_ref],
            check=True,
        )

    head_sha = run_git(worktree_path, ["rev-parse", "HEAD"], check=True).stdout.strip()
    return {
        "repo_root": str(repo_root),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "branch_name": branch,
        "worktree_path": str(worktree_path),
        "branch_preexisted": branch_preexisted,
        "worktree_preexisted": worktree_preexisted,
    }


def verify_recoverable_task_commit(
    task: dict[str, Any],
    worktree_path: Path,
    base_sha: str,
) -> str | None:
    head_sha = run_git(worktree_path, ["rev-parse", "HEAD"], check=True).stdout.strip()
    if head_sha == base_sha:
        return None

    ancestor = run_git(
        worktree_path,
        ["merge-base", "--is-ancestor", base_sha, head_sha],
        check=False,
    )
    if ancestor.returncode != 0:
        raise WorktreeExecutionError(
            "preexisting task branch is not based on the configured base ref"
        )

    ahead_count = int(
        run_git(worktree_path, ["rev-list", "--count", f"{base_sha}..{head_sha}"], check=True)
        .stdout.strip()
        or "0"
    )
    if ahead_count != 1:
        raise WorktreeExecutionError(
            "preexisting task branch has ambiguous history; expected exactly one task commit"
        )

    change_path = worktree_change_path(task)
    changed_paths = [
        line.strip()
        for line in run_git(
            worktree_path,
            ["diff", "--name-only", f"{base_sha}..{head_sha}"],
            check=True,
        ).stdout.splitlines()
        if line.strip()
    ]
    if changed_paths != [change_path.as_posix()]:
        raise WorktreeExecutionError(
            "preexisting task branch changes do not match the deterministic task path"
        )

    expected_content = worktree_change_content(task)
    actual_path = worktree_path / change_path
    if not actual_path.exists() or actual_path.read_text(encoding="utf-8") != expected_content:
        raise WorktreeExecutionError(
            "preexisting task branch content does not match the deterministic fixture"
        )

    return head_sha


def apply_worktree_change(task: dict[str, Any], worktree_path: Path) -> tuple[Path, str]:
    change_path = worktree_change_path(task)
    content = worktree_change_content(task)
    absolute_path = worktree_path / change_path
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    if not absolute_path.exists() or absolute_path.read_text(encoding="utf-8") != content:
        absolute_path.write_text(content, encoding="utf-8")
    return change_path, content


def validate_worktree_change(
    task: dict[str, Any],
    worktree_path: Path,
    change_path: Path,
    expected_content: str,
) -> dict[str, Any]:
    actual_path = worktree_path / change_path
    exists = actual_path.exists()
    actual_content = actual_path.read_text(encoding="utf-8") if exists else ""
    content_matches = exists and actual_content == expected_content
    simulated_failure = bool(task.get("simulate_worktree_validation_fail"))
    required_checks = task.get("required_checks") or ["fixture-change"]
    checks = []
    for check in required_checks:
        passed = content_matches and not simulated_failure
        checks.append(
            {
                "name": check,
                "passed": passed,
                "output": (
                    "deterministic fixture change verified"
                    if passed
                    else "deterministic fixture validation failed"
                ),
            }
        )
    passed = bool(checks) and all(check["passed"] for check in checks)
    message = "All worktree checks passed" if passed else "Worktree validation failed"
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task["id"],
        "passed": passed,
        "message": message,
        "required_checks": required_checks,
        "checks": checks,
        "change_path": change_path.as_posix(),
        "timestamp": utc_now(),
    }


def record_worktree_validation_failure(
    task: dict[str, Any],
    task_dir: Path,
    metadata: dict[str, Any],
    message: str,
    checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    validation_result = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task["id"],
        "passed": False,
        "message": message,
        "required_checks": task.get("required_checks") or ["fixture-change"],
        "checks": checks or [],
        "timestamp": utc_now(),
    }
    metadata["validation_output"] = validation_result
    metadata["final_task_state"] = "FAILED"
    write_json(task_dir / "validation.json", validation_result)
    write_json(task_dir / WORKTREE_METADATA_ARTIFACT, metadata)
    return validation_result


def commit_worktree_change(
    task: dict[str, Any],
    worktree_path: Path,
    change_path: Path,
    base_sha: str,
) -> tuple[str, bool, GitResult | None]:
    status = git_status_lines(worktree_path)
    if status:
        run_git(worktree_path, ["add", "--", change_path.as_posix()], check=True)
        commit_result = run_git(
            worktree_path,
            ["commit", "-m", f"local-agent-loop: {task['id']}"],
            check=True,
        )
        commit_sha = run_git(worktree_path, ["rev-parse", "HEAD"], check=True).stdout.strip()
        return commit_sha, False, commit_result

    head_sha = run_git(worktree_path, ["rev-parse", "HEAD"], check=True).stdout.strip()
    if head_sha == base_sha:
        raise WorktreeExecutionError(
            "deterministic change produced no diff and no existing task commit"
        )
    verify_recoverable_task_commit(task, worktree_path, base_sha)
    return head_sha, True, None


def write_worktree_result(
    task: dict[str, Any],
    task_dir: Path,
    metadata: dict[str, Any],
) -> None:
    commit_sha = metadata.get("commit_sha") or "none"
    validation = metadata.get("validation_output") or {}
    (task_dir / "result.md").write_text(
        "# Worktree Task Result\n\n"
        f"- Task: `{task['id']}`\n"
        f"- Final state: `{task['state']}`\n"
        f"- Branch: `{metadata.get('branch_name')}`\n"
        f"- Worktree: `{metadata.get('worktree_path')}`\n"
        f"- Commit: `{commit_sha}`\n"
        f"- Validation passed: `{validation.get('passed')}`\n"
        f"- Updated: `{utc_now()}`\n",
        encoding="utf-8",
    )


def recover_worktree_task_from_terminal_artifacts(task: dict[str, Any], task_dir: Path) -> bool:
    events = read_ndjson_objects(task_dir / "events.ndjson")
    if not events or events[-1].get("to_state") not in TERMINAL_STATES:
        return False

    final_state = str(events[-1]["to_state"])
    task["state"] = final_state
    task["processed_by"] = WORKTREE_PROCESSED_BY
    task["processed_ts"] = utc_now()

    metadata_path = task_dir / WORKTREE_METADATA_ARTIFACT
    if metadata_path.exists():
        metadata = read_json(metadata_path)
        if isinstance(metadata, dict):
            for metadata_field, task_field in (
                ("branch_name", "worktree_branch"),
                ("worktree_path", "worktree_path"),
                ("base_ref", "worktree_base_ref"),
                ("change_path", "worktree_change_path"),
                ("commit_sha", "worktree_commit_sha"),
            ):
                value = metadata.get(metadata_field)
                if isinstance(value, str) and value:
                    task[task_field] = value
            if final_state == "FAILED":
                task.pop("worktree_commit_sha", None)
    return True


def execute_worktree_task(
    task: dict[str, Any],
    artifacts_root: Path,
    cfg: WorktreeConfig,
) -> dict[str, Any]:
    task_id = task["id"]
    task_dir = artifacts_root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    events_file = task_dir / "events.ndjson"
    metadata = initial_worktree_metadata(task, cfg)

    if recover_worktree_task_from_terminal_artifacts(task, task_dir):
        return task

    if task["state"] == "OPEN":
        transition(task, "CLAIMED", "Worker claimed worktree task", events_file)
    if task["state"] == "CLAIMED":
        write_worktree_plan(task, task_dir, cfg)
        transition(task, "PLANNED", "Worktree plan created", events_file)
    if task["state"] == "PLANNED":
        transition(task, "EXECUTING", "Worktree execution started", events_file)
    if task["state"] == "EXECUTING":
        transition(task, "VALIDATING", "Worktree change ready for validation", events_file)

    if task["state"] == "VALIDATING":
        try:
            ensured = ensure_task_worktree(task, cfg)
            metadata.update(ensured)
            worktree_path = Path(ensured["worktree_path"])
            dirty_before = git_status_lines(worktree_path)
            metadata["dirty_status_before"] = dirty_before
            if dirty_before:
                record_worktree_validation_failure(
                    task,
                    task_dir,
                    metadata,
                    "Worktree is dirty before execution; refusing to mutate",
                    [
                        {
                            "name": "dirty-worktree-guard",
                            "passed": False,
                            "output": "\n".join(dirty_before),
                        }
                    ],
                )
                transition(task, "FAILED", "Dirty worktree guard failed", events_file)
            else:
                verify_recoverable_task_commit(task, worktree_path, ensured["base_sha"])
                change_path, expected_content = apply_worktree_change(task, worktree_path)
                metadata["change_path"] = change_path.as_posix()
                validation_result = validate_worktree_change(
                    task,
                    worktree_path,
                    change_path,
                    expected_content,
                )
                metadata["validation_output"] = validation_result
                write_json(task_dir / "validation.json", validation_result)
                if validation_result["passed"]:
                    try:
                        commit_sha, reused, commit_result = commit_worktree_change(
                            task,
                            worktree_path,
                            change_path,
                            ensured["base_sha"],
                        )
                    except WorktreeExecutionError as exc:
                        record_worktree_validation_failure(
                            task,
                            task_dir,
                            metadata,
                            str(exc),
                            [
                                {
                                    "name": "commit",
                                    "passed": False,
                                    "output": str(exc),
                                }
                            ],
                        )
                        transition(task, "FAILED", "Worktree commit failed", events_file)
                    else:
                        metadata["commit_sha"] = commit_sha
                        metadata["commit_reused"] = reused
                        metadata["commit_result"] = (
                            commit_result.as_dict() if commit_result is not None else None
                        )
                        task["worktree_branch"] = metadata["branch_name"]
                        task["worktree_path"] = metadata["worktree_path"]
                        task["worktree_base_ref"] = cfg.base_ref
                        task["worktree_change_path"] = change_path.as_posix()
                        task["worktree_commit_sha"] = commit_sha
                        transition(
                            task,
                            "DONE",
                            "Worktree validation passed and committed",
                            events_file,
                        )
                else:
                    transition(task, "FAILED", "Worktree validation failed", events_file)
        except (ValidationError, WorktreeExecutionError) as exc:
            record_worktree_validation_failure(task, task_dir, metadata, str(exc))
            transition(task, "FAILED", "Worktree execution failed before mutation", events_file)

    if task["state"] in TERMINAL_STATES:
        task["processed_by"] = WORKTREE_PROCESSED_BY
        task["processed_ts"] = utc_now()
        metadata["final_task_state"] = task["state"]
        if metadata["commit_sha"] is None:
            task.pop("worktree_commit_sha", None)
        metadata_path = Path(metadata["worktree_path"])
        metadata["dirty_status_after"] = (
            git_status_lines(metadata_path)
            if (metadata_path / ".git").exists()
            else []
        )
        write_json(task_dir / WORKTREE_METADATA_ARTIFACT, metadata)

    write_worktree_result(task, task_dir, metadata)
    return task


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
    worktree_config: WorktreeConfig | None = None,
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
        "execution_mode": "worktree" if worktree_config is not None else "state-machine",
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
            if worktree_config is not None:
                tasks[i] = execute_worktree_task(task, artifacts_root, worktree_config)
            else:
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
    parser.add_argument(
        "--execution-mode",
        choices=("state-machine", "worktree"),
        default="state-machine",
        help="Use the default state machine worker or git worktree-backed execution",
    )
    parser.add_argument("--worktree-repo", type=Path)
    parser.add_argument("--worktrees-dir", type=Path)
    parser.add_argument("--worktree-base-ref", default="HEAD")
    parser.add_argument("--worktree-branch-prefix", default="codex/local-agent-loop")
    args = parser.parse_args(argv)

    try:
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
    except (ValidationError, WorktreeExecutionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
