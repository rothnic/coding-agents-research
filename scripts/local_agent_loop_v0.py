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
COMMAND_OUTPUTS_DIR = "command-outputs"
DETERMINISTIC_FILE_ADAPTER = "deterministic-file-change"
COMMAND_BACKED_PATCH_ADAPTER = "command-backed-patch-fixture"
SUPPORTED_WORKTREE_ACTION_ADAPTERS = {
    DETERMINISTIC_FILE_ADAPTER,
    COMMAND_BACKED_PATCH_ADAPTER,
}
MAX_COMMAND_TIMEOUT_SEC = 30.0


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


@dataclass(frozen=True)
class CommandSpec:
    name: str
    argv: list[str]
    timeout_sec: float


@dataclass(frozen=True)
class WorktreeAction:
    adapter: str
    inputs: dict[str, Any]
    expected_paths: list[Path]
    expected_contents: dict[str, str]
    action_command: CommandSpec | None = None


class ValidationError(RuntimeError):
    """Raised when local loop JSON inputs are invalid."""


class WorktreeExecutionError(RuntimeError):
    """Raised when a worktree-backed task cannot be executed safely."""


class WorktreeCommandError(WorktreeExecutionError):
    """Raised when a bounded local command fails after output capture."""

    def __init__(self, message: str, outputs: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.outputs = outputs


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
        encoding="utf-8",
        errors="replace",
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
    if ".git" in path.parts:
        raise WorktreeExecutionError(f"unsafe worktree change path {path_text!r}")
    if not path.parts:
        raise WorktreeExecutionError("worktree change path must not be empty")
    return path


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorktreeExecutionError(f"{label}: expected object")
    return value


def normalize_timeout(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorktreeExecutionError(f"{label}: expected numeric timeout")
    timeout = float(value)
    if timeout <= 0 or timeout > MAX_COMMAND_TIMEOUT_SEC:
        raise WorktreeExecutionError(
            f"{label}: timeout must be > 0 and <= {MAX_COMMAND_TIMEOUT_SEC:g} seconds"
        )
    return timeout


def normalize_command_argv(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise WorktreeExecutionError(f"{label}: expected non-empty argv list")
    argv: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise WorktreeExecutionError(f"{label}[{index}]: expected non-empty string")
        argv.append(item)
    executable = Path(argv[0]).name
    if executable in {"sh", "bash", "zsh", "fish"}:
        raise WorktreeExecutionError(f"{label}: shell commands are not supported")
    return argv


def normalize_command_allowlist(value: Any, label: str) -> list[list[str]]:
    if not isinstance(value, list) or not value:
        raise WorktreeExecutionError(f"{label}: expected non-empty list of argv lists")
    return [
        normalize_command_argv(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    ]


def command_is_allowlisted(argv: list[str], allowlist: list[list[str]]) -> bool:
    return any(argv == allowed for allowed in allowlist)


def command_from_mapping(
    mapping: dict[str, Any],
    label: str,
    allowlist: list[list[str]],
) -> CommandSpec:
    if "command" in mapping and "argv" in mapping and mapping["command"] != mapping["argv"]:
        raise WorktreeExecutionError(f"{label}: command and argv disagree")
    raw_argv = mapping.get("argv", mapping.get("command"))
    argv = normalize_command_argv(raw_argv, f"{label}.argv")
    if not command_is_allowlisted(argv, allowlist):
        raise WorktreeExecutionError(f"{label}.argv: command is not allowlisted")
    name = mapping.get("name")
    if not isinstance(name, str) or not name.strip():
        raise WorktreeExecutionError(f"{label}.name: expected non-empty string")
    timeout = normalize_timeout(mapping.get("timeout_sec", 10), f"{label}.timeout_sec")
    forbidden_keys = {"shell", "cwd", "env"}
    present_forbidden = sorted(forbidden_keys & set(mapping))
    if present_forbidden:
        raise WorktreeExecutionError(
            f"{label}: unsupported command fields {present_forbidden}"
        )
    return CommandSpec(name=name, argv=argv, timeout_sec=timeout)


def task_action_contract(task: dict[str, Any]) -> dict[str, Any] | None:
    has_local_action = "local_action" in task
    has_action = "action" in task
    if has_local_action and has_action and task["local_action"] != task["action"]:
        raise WorktreeExecutionError("ambiguous action contract: local_action and action differ")
    raw = task.get("local_action") if has_local_action else task.get("action")
    if raw is None:
        return None
    return require_object(raw, "local_action")


def deterministic_action_from_inputs(task: dict[str, Any], inputs: dict[str, Any]) -> WorktreeAction:
    path_value = inputs.get("path", task.get("worktree_change_path"))
    if path_value is None:
        path = worktree_change_path(task)
    elif not isinstance(path_value, str) or not path_value.strip():
        raise WorktreeExecutionError("local_action.inputs.path: expected non-empty string")
    else:
        path = validate_relative_repo_path(path_value)

    content_value = inputs.get("content", task.get("worktree_change_content"))
    if content_value is None:
        content = worktree_change_content({**task, "worktree_change_path": path.as_posix()})
    elif not isinstance(content_value, str):
        raise WorktreeExecutionError("local_action.inputs.content: expected string")
    else:
        content = content_value if content_value.endswith("\n") else content_value + "\n"

    return WorktreeAction(
        adapter=DETERMINISTIC_FILE_ADAPTER,
        inputs={"path": path.as_posix(), "content": content},
        expected_paths=[path],
        expected_contents={path.as_posix(): content},
    )


def command_backed_action_from_inputs(inputs: dict[str, Any]) -> WorktreeAction:
    allowlist = normalize_command_allowlist(
        inputs.get("allowed_commands"),
        "local_action.inputs.allowed_commands",
    )
    command_mapping = {
        "name": inputs.get("name", "patch-fixture"),
        "timeout_sec": inputs.get("timeout_sec", 10),
    }
    if "command" in inputs:
        command_mapping["command"] = inputs["command"]
    if "argv" in inputs:
        command_mapping["argv"] = inputs["argv"]
    command = command_from_mapping(
        command_mapping,
        "local_action.inputs.command",
        allowlist,
    )
    raw_expected_path = inputs.get("expected_path")
    if not isinstance(raw_expected_path, str) or not raw_expected_path.strip():
        raise WorktreeExecutionError(
            "local_action.inputs.expected_path: expected non-empty string"
        )
    expected_path = validate_relative_repo_path(raw_expected_path)
    expected_content = inputs.get("expected_content")
    if not isinstance(expected_content, str):
        raise WorktreeExecutionError("local_action.inputs.expected_content: expected string")
    if not expected_content.endswith("\n"):
        expected_content += "\n"
    return WorktreeAction(
        adapter=COMMAND_BACKED_PATCH_ADAPTER,
        inputs={
            "name": command.name,
            "command": command.argv,
            "timeout_sec": command.timeout_sec,
            "expected_path": expected_path.as_posix(),
            "expected_content": expected_content,
        },
        expected_paths=[expected_path],
        expected_contents={expected_path.as_posix(): expected_content},
        action_command=command,
    )


def resolve_worktree_action(task: dict[str, Any]) -> WorktreeAction:
    raw = task_action_contract(task)
    if raw is None:
        return deterministic_action_from_inputs(task, {})
    adapter = raw.get("adapter")
    if not isinstance(adapter, str) or not adapter.strip():
        raise WorktreeExecutionError("local_action.adapter: expected non-empty string")
    if adapter not in SUPPORTED_WORKTREE_ACTION_ADAPTERS:
        raise WorktreeExecutionError(f"unknown worktree action adapter {adapter!r}")
    inputs = raw.get("inputs", {})
    if not isinstance(inputs, dict):
        raise WorktreeExecutionError("local_action.inputs: expected object")
    if adapter == DETERMINISTIC_FILE_ADAPTER:
        return deterministic_action_from_inputs(task, inputs)
    return command_backed_action_from_inputs(inputs)


def validation_command_allowlist(task: dict[str, Any]) -> list[list[str]]:
    raw = task.get("validation_command_allowlist", task.get("allowed_validation_commands"))
    if raw is None:
        return []
    return normalize_command_allowlist(raw, "validation_command_allowlist")


def resolve_validation_commands(task: dict[str, Any]) -> list[CommandSpec]:
    raw = task.get("validation_commands")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WorktreeExecutionError("validation_commands: expected list")
    allowlist = validation_command_allowlist(task)
    if not allowlist:
        raise WorktreeExecutionError(
            "validation_commands require validation_command_allowlist"
        )
    commands: list[CommandSpec] = []
    seen_names: set[str] = set()
    for index, item in enumerate(raw):
        mapping = require_object(item, f"validation_commands[{index}]")
        command = command_from_mapping(mapping, f"validation_commands[{index}]", allowlist)
        if command.name in seen_names:
            raise WorktreeExecutionError(
                f"validation_commands[{index}].name: duplicate name {command.name!r}"
            )
        seen_names.add(command.name)
        commands.append(command)
    return commands


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


def write_worktree_plan(
    task: dict[str, Any],
    task_dir: Path,
    cfg: WorktreeConfig,
    action: WorktreeAction,
    validation_commands: list[CommandSpec],
) -> None:
    plan = task_dir / "plan.md"
    if plan.exists():
        return
    branch = worktree_branch_name(task["id"], cfg)
    worktree_path = worktree_path_for_task(task["id"], cfg)
    change_paths = ", ".join(path.as_posix() for path in action.expected_paths)
    validation_names = ", ".join(command.name for command in validation_commands) or "built-in"
    plan.write_text(
        "# Plan\n\n"
        "1. Create or reuse the deterministic task branch and worktree.\n"
        "2. Resolve the declarative local action adapter.\n"
        "3. Apply the local fixture change.\n"
        "4. Run local validation checks.\n"
        "5. Capture command evidence when configured.\n"
        "6. Commit successful work and write execution metadata.\n\n"
        f"- Branch: `{branch}`\n"
        f"- Worktree: `{worktree_path}`\n"
        f"- Action adapter: `{action.adapter}`\n"
        f"- Change path(s): `{change_paths}`\n"
        f"- Validation command(s): `{validation_names}`\n",
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
        "action_adapter": None,
        "action_inputs": None,
        "expected_change_paths": [],
        "action_command_outputs": [],
        "validation_commands": [],
        "validation_command_outputs": [],
        "command_timed_out": False,
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
    action: WorktreeAction,
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

    expected_paths = [path.as_posix() for path in action.expected_paths]
    changed_paths = [
        line.strip()
        for line in run_git(
            worktree_path,
            ["diff", "--name-only", f"{base_sha}..{head_sha}"],
            check=True,
        ).stdout.splitlines()
        if line.strip()
    ]
    if changed_paths != expected_paths:
        raise WorktreeExecutionError(
            "preexisting task branch changes do not match the deterministic task path"
        )

    for path_text, expected_content in action.expected_contents.items():
        actual_path = worktree_path / path_text
        if not actual_path.exists() or actual_path.read_text(encoding="utf-8") != expected_content:
            raise WorktreeExecutionError(
                "preexisting task branch content does not match the action contract"
            )

    return head_sha


def git_pending_paths(worktree_path: Path) -> list[str]:
    diff_paths = [
        line.strip()
        for line in run_git(worktree_path, ["diff", "--name-only"], check=True).stdout.splitlines()
        if line.strip()
    ]
    untracked_paths = [
        line.strip()
        for line in run_git(
            worktree_path,
            ["ls-files", "--others", "--exclude-standard"],
            check=True,
        ).stdout.splitlines()
        if line.strip()
    ]
    return sorted({*diff_paths, *untracked_paths})


def ensure_only_expected_paths_changed(worktree_path: Path, expected_paths: list[Path]) -> None:
    actual = git_pending_paths(worktree_path)
    expected = sorted(path.as_posix() for path in expected_paths)
    if actual != expected:
        raise WorktreeExecutionError(
            f"action changed unexpected paths; got {actual!r}, expected {expected!r}"
        )


def command_artifact_relative_path(kind: str, index: int, name: str) -> Path:
    safe_name = slugify_ref_component(name).lower()[:48] or "command"
    return Path(COMMAND_OUTPUTS_DIR) / f"{kind}-{index:03d}-{safe_name}.json"


def text_from_timeout_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_local_command(
    task: dict[str, Any],
    worktree_path: Path,
    task_dir: Path,
    command: CommandSpec,
    kind: str,
    index: int,
) -> dict[str, Any]:
    relative_artifact = command_artifact_relative_path(kind, index, command.name)
    output_path = task_dir / relative_artifact
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            command.argv,
            cwd=worktree_path,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=command.timeout_sec,
        )
        output = {
            "schema_version": SCHEMA_VERSION,
            "task_id": task["id"],
            "kind": kind,
            "name": command.name,
            "argv": command.argv,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
            "timeout_sec": command.timeout_sec,
            "output_artifact": relative_artifact.as_posix(),
        }
    except subprocess.TimeoutExpired as exc:
        output = {
            "schema_version": SCHEMA_VERSION,
            "task_id": task["id"],
            "kind": kind,
            "name": command.name,
            "argv": command.argv,
            "returncode": None,
            "stdout": text_from_timeout_value(exc.stdout),
            "stderr": text_from_timeout_value(exc.stderr),
            "timed_out": True,
            "timeout_sec": command.timeout_sec,
            "output_artifact": relative_artifact.as_posix(),
        }
    write_json(output_path, output)
    return output


def apply_worktree_action(
    task: dict[str, Any],
    worktree_path: Path,
    task_dir: Path,
    action: WorktreeAction,
) -> list[dict[str, Any]]:
    command_outputs: list[dict[str, Any]] = []
    if action.adapter == DETERMINISTIC_FILE_ADAPTER:
        for path_text, content in action.expected_contents.items():
            absolute_path = worktree_path / path_text
            absolute_path.parent.mkdir(parents=True, exist_ok=True)
            if not absolute_path.exists() or absolute_path.read_text(encoding="utf-8") != content:
                absolute_path.write_text(content, encoding="utf-8")
    elif action.adapter == COMMAND_BACKED_PATCH_ADAPTER:
        if action.action_command is None:
            raise WorktreeExecutionError("command-backed adapter missing action command")
        output = run_local_command(
            task,
            worktree_path,
            task_dir,
            action.action_command,
            "action",
            0,
        )
        command_outputs.append(output)
        if output["timed_out"]:
            raise WorktreeCommandError("action command timed out", command_outputs)
        if output["returncode"] != 0:
            raise WorktreeCommandError(
                f"action command failed with exit code {output['returncode']}",
                command_outputs,
            )
    else:
        raise WorktreeExecutionError(f"unknown worktree action adapter {action.adapter!r}")
    try:
        ensure_only_expected_paths_changed(worktree_path, action.expected_paths)
    except WorktreeExecutionError as exc:
        if command_outputs:
            raise WorktreeCommandError(str(exc), command_outputs) from exc
        raise
    return command_outputs


def validate_worktree_action(
    task: dict[str, Any],
    worktree_path: Path,
    task_dir: Path,
    action: WorktreeAction,
    validation_commands: list[CommandSpec],
) -> dict[str, Any]:
    simulated_failure = bool(task.get("simulate_worktree_validation_fail"))
    required_checks = task.get("required_checks") or ["fixture-change"]
    checks = []
    for path_text, expected_content in action.expected_contents.items():
        actual_path = worktree_path / path_text
        exists = actual_path.exists()
        actual_content = actual_path.read_text(encoding="utf-8") if exists else ""
        content_matches = exists and actual_content == expected_content
        passed = content_matches and not simulated_failure
        checks.append(
            {
                "name": f"expected-content:{path_text}",
                "passed": passed,
                "output": (
                    "action output content verified"
                    if passed
                    else "action output content validation failed"
                ),
            }
        )

    validation_outputs = [
        run_local_command(task, worktree_path, task_dir, command, "validation", index)
        for index, command in enumerate(validation_commands)
    ]
    for output in validation_outputs:
        passed = output["returncode"] == 0 and not output["timed_out"]
        checks.append(
            {
                "name": output["name"],
                "passed": passed,
                "output": (
                    "validation command passed"
                    if passed
                    else "validation command failed"
                ),
                "exit_code": output["returncode"],
                "timed_out": output["timed_out"],
                "output_artifact": output["output_artifact"],
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
        "action_adapter": action.adapter,
        "checks": checks,
        "change_paths": [path.as_posix() for path in action.expected_paths],
        "validation_commands": [
            {
                "name": command.name,
                "argv": command.argv,
                "timeout_sec": command.timeout_sec,
            }
            for command in validation_commands
        ],
        "validation_command_outputs": validation_outputs,
        "timestamp": utc_now(),
    }


def record_worktree_validation_failure(
    task: dict[str, Any],
    task_dir: Path,
    metadata: dict[str, Any],
    message: str,
    checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    plan = task_dir / "plan.md"
    if not plan.exists():
        plan.write_text(
            "# Plan\n\n"
            "1. Validate the worktree action contract.\n"
            "2. Stop before local mutation when the contract is unsafe or incomplete.\n"
            "3. Record diagnostics for operator review.\n",
            encoding="utf-8",
        )
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
    change_paths: list[Path],
    base_sha: str,
    preexisting_commit_sha: str | None = None,
) -> tuple[str, bool, GitResult | None]:
    status = git_status_lines(worktree_path)
    if status:
        ensure_only_expected_paths_changed(worktree_path, change_paths)
        run_git(
            worktree_path,
            ["add", "--", *[path.as_posix() for path in change_paths]],
            check=True,
        )
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
    if preexisting_commit_sha is not None:
        if head_sha != preexisting_commit_sha:
            raise WorktreeExecutionError(
                "preexisting task commit changed before commit finalization"
            )
        return head_sha, True, None
    action = resolve_worktree_action(task)
    verify_recoverable_task_commit(task, worktree_path, base_sha, action)
    return head_sha, True, None


def write_worktree_result(
    task: dict[str, Any],
    task_dir: Path,
    metadata: dict[str, Any],
) -> None:
    commit_sha = metadata.get("commit_sha") or "none"
    validation = metadata.get("validation_output") or {}
    validation_outputs = metadata.get("validation_command_outputs") or []
    timed_out = metadata.get("command_timed_out")
    (task_dir / "result.md").write_text(
        "# Worktree Task Result\n\n"
        f"- Task: `{task['id']}`\n"
        f"- Final state: `{task['state']}`\n"
        f"- Action adapter: `{metadata.get('action_adapter')}`\n"
        f"- Branch: `{metadata.get('branch_name')}`\n"
        f"- Worktree: `{metadata.get('worktree_path')}`\n"
        f"- Commit: `{commit_sha}`\n"
        f"- Validation passed: `{validation.get('passed')}`\n"
        f"- Validation command outputs: `{len(validation_outputs)}`\n"
        f"- Command timed out: `{timed_out}`\n"
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
                ("action_adapter", "worktree_action_adapter"),
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

    raw_action_for_metadata = task.get("local_action", task.get("action"))
    if isinstance(raw_action_for_metadata, dict):
        raw_adapter = raw_action_for_metadata.get("adapter")
        raw_inputs = raw_action_for_metadata.get("inputs")
        if isinstance(raw_adapter, str) and raw_adapter.strip():
            metadata["action_adapter"] = raw_adapter
        if isinstance(raw_inputs, dict):
            metadata["action_inputs"] = raw_inputs

    try:
        action = resolve_worktree_action(task)
        validation_commands = resolve_validation_commands(task)
        if action.adapter == COMMAND_BACKED_PATCH_ADAPTER and not validation_commands:
            raise WorktreeExecutionError(
                "command-backed-patch-fixture requires validation_commands"
            )
        metadata["action_adapter"] = action.adapter
        metadata["action_inputs"] = action.inputs
        metadata["expected_change_paths"] = [
            path.as_posix() for path in action.expected_paths
        ]
        metadata["validation_commands"] = [
            {
                "name": command.name,
                "argv": command.argv,
                "timeout_sec": command.timeout_sec,
            }
            for command in validation_commands
        ]
    except (ValidationError, WorktreeExecutionError) as exc:
        record_worktree_validation_failure(task, task_dir, metadata, str(exc))
        transition(task, "FAILED", "Worktree action contract rejected", events_file)
    else:
        if task["state"] == "OPEN":
            transition(task, "CLAIMED", "Worker claimed worktree task", events_file)
        if task["state"] == "CLAIMED":
            write_worktree_plan(task, task_dir, cfg, action, validation_commands)
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
                    preexisting_commit_sha = verify_recoverable_task_commit(
                        task,
                        worktree_path,
                        ensured["base_sha"],
                        action,
                    )
                    try:
                        action_outputs = (
                            []
                            if preexisting_commit_sha is not None
                            else apply_worktree_action(
                                task,
                                worktree_path,
                                task_dir,
                                action,
                            )
                        )
                    except WorktreeCommandError as exc:
                        metadata["action_command_outputs"] = exc.outputs
                        metadata["command_timed_out"] = any(
                            output.get("timed_out") for output in exc.outputs
                        )
                        record_worktree_validation_failure(
                            task,
                            task_dir,
                            metadata,
                            str(exc),
                            [
                                {
                                    "name": "action-command",
                                    "passed": False,
                                    "output": str(exc),
                                }
                            ],
                        )
                        transition(task, "FAILED", "Worktree action command failed", events_file)
                    except WorktreeExecutionError as exc:
                        record_worktree_validation_failure(
                            task,
                            task_dir,
                            metadata,
                            str(exc),
                            [
                                {
                                    "name": "action-contract",
                                    "passed": False,
                                    "output": str(exc),
                                }
                            ],
                        )
                        transition(task, "FAILED", "Worktree action failed", events_file)
                    else:
                        metadata["action_command_outputs"] = action_outputs
                        metadata["command_timed_out"] = any(
                            output.get("timed_out")
                            for output in action_outputs
                        )
                        metadata["change_path"] = action.expected_paths[0].as_posix()
                        validation_result = validate_worktree_action(
                            task,
                            worktree_path,
                            task_dir,
                            action,
                            validation_commands,
                        )
                        metadata["validation_output"] = validation_result
                        metadata["validation_command_outputs"] = validation_result[
                            "validation_command_outputs"
                        ]
                        metadata["command_timed_out"] = metadata["command_timed_out"] or any(
                            output.get("timed_out")
                            for output in metadata["validation_command_outputs"]
                        )
                        write_json(task_dir / "validation.json", validation_result)
                        if validation_result["passed"]:
                            try:
                                commit_sha, reused, commit_result = commit_worktree_change(
                                    task,
                                    worktree_path,
                                    action.expected_paths,
                                    ensured["base_sha"],
                                    preexisting_commit_sha,
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
                                task["worktree_change_path"] = action.expected_paths[
                                    0
                                ].as_posix()
                                task["worktree_action_adapter"] = action.adapter
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
        try:
            metadata["dirty_status_after"] = (
                git_status_lines(metadata_path)
                if (metadata_path / ".git").exists()
                else []
            )
        except WorktreeExecutionError as exc:
            metadata["dirty_status_after"] = [f"git status failed: {exc}"]
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
