# Local Agent Loop v0 — Proof of Concept

This folder includes a runnable local-only proof of concept for a Symphony-like
orchestration loop **plus** a composed higher-level roadmap workflow.

## Two Composable Loops

1. **Worker loop** (`scripts/local_agent_loop_v0.py`)
   - Executes task-level state transitions and validation.
2. **Program loop** (`scripts/local_program_loop_v0.py`)
   - Owns higher-level objectives/roadmap,
   - reviews progress and objective changes,
   - generates near-term detailed tasks,
   - attempts unblocking when no runnable tasks exist,
   - then invokes the worker loop.

## Trigger Rules for Program Review

The program loop replans when **any** is true:

- open task count is below threshold (`min_open`),
- no unblocked/runnable tasks exist,
- roadmap review timeout has elapsed.

This directly models: “not only when no tasks exist, but also when no tasks are unblocked.”

## Files

- Worker runner: `scripts/local_agent_loop_v0.py`
- Program runner: `scripts/local_program_loop_v0.py`
- Regression harness: `scripts/test_local_agent_loop_v0.py`
- Program roadmap sample: `docs/specs/local-agent-loop-v0/examples/tasks/program.json`
- Queue sample: `docs/specs/local-agent-loop-v0/examples/tasks/queue.json`
- Regression fixture: `docs/specs/local-agent-loop-v0/fixtures/regression/expected-summary.json`
- Artifact output root (generated): `docs/specs/local-agent-loop-v0/examples/artifacts/`

## Worker State Machine

`OPEN -> CLAIMED -> PLANNED -> EXECUTING -> VALIDATING -> (DONE | FAILED | BLOCKED)`

Retry path:

`VALIDATING -> EXECUTING -> VALIDATING` (bounded by `max_retries`)

## CLI Surface

The operator entrypoint is `scripts/local_program_loop_v0.py`:

- `validate` checks program and queue schema before any mutation.
- `run-worker` executes only the task worker loop.
- `run-program` validates inputs, runs roadmap review, hands off to the worker,
  writes program artifacts, and verifies artifact integrity.
- `integrate` runs the v0.6 integration phase over completed worktree tasks.
- `status` prints a compact JSON status snapshot and optional artifact errors.
- `run-worker` and `run-program` can opt into v0.4 git worktree execution with
  `--execution-mode worktree`. v0.5 keeps that mode explicit and adds
  policy-driven local action adapters for worktree tasks.
- `run-program` can opt into v0.6 integration with `--integration-mode`.
  Integration is off by default; `local-merge` must be selected explicitly.

The worker script still supports direct task execution through
`scripts/local_agent_loop_v0.py`, but the program CLI is the preferred
operator surface because it validates both files and checks program-level
artifacts.

## Run Worker Loop Only

```bash
tmp_dir="$(mktemp -d)"
cp docs/specs/local-agent-loop-v0/examples/tasks/queue.json "$tmp_dir/queue.json"

python3 scripts/local_program_loop_v0.py run-worker \
  --queue "$tmp_dir/queue.json" \
  --artifacts "$tmp_dir/artifacts" \
  --max-retries 2
```

## Validate, Execute, and Verify

This single command validates input schemas, runs the composed workflow, and
fails if required program or processed-task artifacts are missing or inconsistent:

```bash
tmp_dir="$(mktemp -d)"
cp docs/specs/local-agent-loop-v0/examples/tasks/program.json "$tmp_dir/program.json"
cp docs/specs/local-agent-loop-v0/examples/tasks/queue.json "$tmp_dir/queue.json"

python3 scripts/local_program_loop_v0.py run-program \
  --program "$tmp_dir/program.json" \
  --queue "$tmp_dir/queue.json" \
  --artifacts "$tmp_dir/artifacts" \
  --min-open 2 \
  --roadmap-timeout-sec 60 \
  --max-retries 2
```

Inspect the final state:

```bash
python3 scripts/local_program_loop_v0.py status \
  --program "$tmp_dir/program.json" \
  --queue "$tmp_dir/queue.json" \
  --artifacts "$tmp_dir/artifacts"
```

## Worktree-Backed Execution

v0.4 added an optional local git worktree executor around the same v0.3
program/queue schema. The default command path remains the dependency-light
state-machine worker; worktree mode is only enabled when explicitly requested.

This smoke example creates a clean disposable target repository, copies the
sample program and queue into a temp state directory, and runs one command that
validates inputs, creates deterministic task worktrees, applies fixture changes,
runs local checks, commits successful tasks, updates state, and verifies
artifacts:

```bash
tmp_dir="$(mktemp -d)"
target_repo="$tmp_dir/target-repo"
mkdir -p "$target_repo"
git -C "$target_repo" init
git -C "$target_repo" config user.name "Local Agent Loop"
git -C "$target_repo" config user.email "loop@example.invalid"
printf '# Fixture Repository\n' > "$target_repo/README.md"
git -C "$target_repo" add README.md
git -C "$target_repo" commit -m "Initial fixture"
git -C "$target_repo" branch -M main

cp docs/specs/local-agent-loop-v0/examples/tasks/program.json "$tmp_dir/program.json"
cp docs/specs/local-agent-loop-v0/examples/tasks/queue.json "$tmp_dir/queue.json"

python3 scripts/local_program_loop_v0.py run-program \
  --program "$tmp_dir/program.json" \
  --queue "$tmp_dir/queue.json" \
  --artifacts "$tmp_dir/artifacts" \
  --min-open 2 \
  --roadmap-timeout-sec 60 \
  --max-retries 2 \
  --execution-mode worktree \
  --worktree-repo "$target_repo" \
  --worktrees-dir "$tmp_dir/worktrees" \
  --worktree-base-ref main
```

Re-running the same command reuses the same task branches and worktrees, skips
terminal tasks, and does not create duplicate task commits.

## v0.5 Action Adapter Contract

v0.5 lets a queue task choose a small built-in local action adapter with
`local_action`. If `local_action` is omitted, worktree mode uses the v0.4
deterministic file fixture adapter.

Supported adapters:

- `deterministic-file-change`: writes one safe relative file. Inputs are
  `path` and `content`; legacy `worktree_change_path` and
  `worktree_change_content` still work.
- `command-backed-patch-fixture`: runs one bounded local command that must be
  exactly present in `local_action.inputs.allowed_commands`, then verifies one
  expected relative path and expected content before validation.

Validation commands are also declarative and allowlisted:

- `validation_commands[].command` must be an argv array, not a shell string.
- `validation_command_allowlist` must include the exact argv array for every
  validation command.
- `timeout_sec` must be greater than `0` and no more than `30`.
- Command stdout, stderr, exit code, timeout status, argv, and stable output
  artifact path are captured under each task's `command-outputs/` directory.

The worker rejects unknown adapters, unsafe paths, missing validation evidence
for command-backed actions, non-argv command strings, non-allowlisted commands,
dirty worktrees before mutation, and unexpected changed paths. Command-backed
execution is local-only, uses `subprocess` without a shell, and records failure
diagnostics without a success commit.

## Command-Backed Adapter Smoke

This example creates a clean disposable target repository and a one-task queue.
The single `run-program` command executes a named action adapter, validates via
an allowlisted local command, commits success, and verifies artifacts:

```bash
adapter_tmp="$(mktemp -d)"
target_repo="$adapter_tmp/target-repo"
mkdir -p "$target_repo"
git -C "$target_repo" init
git -C "$target_repo" config user.name "Local Agent Loop"
git -C "$target_repo" config user.email "loop@example.invalid"
printf '# Fixture Repository\n' > "$target_repo/README.md"
git -C "$target_repo" add README.md
git -C "$target_repo" commit -m "Initial fixture"
git -C "$target_repo" branch -M main

cat > "$adapter_tmp/program.json" <<'JSON'
{
  "schema_version": "local-agent-loop-v0.3",
  "program_id": "adapter-smoke",
  "goal": "Run a command-backed local action adapter",
  "program_state": "IDLE",
  "last_roadmap_review_ts": "2026-01-01T00:00:00+00:00",
  "roadmap_backlog": [],
  "review_log": []
}
JSON

cat > "$adapter_tmp/queue.json" <<'JSON'
{
  "schema_version": "local-agent-loop-v0.3",
  "tasks": [
    {
      "id": "task-command-backed",
      "title": "Command-backed adapter smoke",
      "state": "OPEN",
      "retries": 0,
      "required_checks": ["command-fixture"],
      "local_action": {
        "adapter": "command-backed-patch-fixture",
        "inputs": {
          "name": "write-command-fixture",
          "command": [
            "python3",
            "-c",
            "from pathlib import Path; p=Path('agent-loop-results/command-backed-fixture.md'); p.parent.mkdir(parents=True, exist_ok=True); p.write_text('# Command Backed Fixture\\n\\nTask: task-command-backed\\n', encoding='utf-8')"
          ],
          "allowed_commands": [
            [
              "python3",
              "-c",
              "from pathlib import Path; p=Path('agent-loop-results/command-backed-fixture.md'); p.parent.mkdir(parents=True, exist_ok=True); p.write_text('# Command Backed Fixture\\n\\nTask: task-command-backed\\n', encoding='utf-8')"
            ]
          ],
          "timeout_sec": 5,
          "expected_path": "agent-loop-results/command-backed-fixture.md",
          "expected_content": "# Command Backed Fixture\n\nTask: task-command-backed\n"
        }
      },
      "validation_commands": [
        {
          "name": "verify-command-fixture",
          "command": [
            "python3",
            "-c",
            "from pathlib import Path; data=Path('agent-loop-results/command-backed-fixture.md').read_text(encoding='utf-8'); assert data == '# Command Backed Fixture\\n\\nTask: task-command-backed\\n'; print('validated command-backed fixture')"
          ],
          "timeout_sec": 5
        }
      ],
      "validation_command_allowlist": [
        [
          "python3",
          "-c",
          "from pathlib import Path; data=Path('agent-loop-results/command-backed-fixture.md').read_text(encoding='utf-8'); assert data == '# Command Backed Fixture\\n\\nTask: task-command-backed\\n'; print('validated command-backed fixture')"
        ]
      ]
    }
  ]
}
JSON

cp "$adapter_tmp/program.json" "$adapter_tmp/program.pristine.json"
cp "$adapter_tmp/queue.json" "$adapter_tmp/queue.pristine.json"

python3 scripts/local_program_loop_v0.py run-program \
  --program "$adapter_tmp/program.json" \
  --queue "$adapter_tmp/queue.json" \
  --artifacts "$adapter_tmp/artifacts" \
  --min-open 1 \
  --roadmap-timeout-sec 60 \
  --max-retries 2 \
  --execution-mode worktree \
  --worktree-repo "$target_repo" \
  --worktrees-dir "$adapter_tmp/worktrees" \
  --worktree-base-ref main

python3 scripts/local_program_loop_v0.py validate \
  --program "$adapter_tmp/program.json" \
  --queue "$adapter_tmp/queue.json" \
  --artifacts "$adapter_tmp/artifacts" \
  --check-artifacts
```

## v0.6 Integration Contract

v0.6 adds an explicit integration phase for completed worktree tasks. The
phase only considers worktree-backed tasks with `DONE` execution state, valid
adapter metadata, passing validation evidence, and a recorded task commit SHA.
It never runs by default.

Integration modes:

- `report-only`: writes merge-readiness artifacts without changing the target
  repo base branch.
- `local-merge`: merges eligible task branches into the target repo base branch
  and must be selected explicitly with `--integration-mode local-merge`.

The local integration policy is built from CLI flags and, optionally, an
`--integration-policy` JSON file. Policy fields are:

- `schema_version`: `local-agent-loop-v0.3`
- `target_base_ref`: base branch/ref to inspect or merge, usually `main`
- `allowed_branch_prefixes`: task branch prefixes eligible for integration
- `require_clean_target`: require a clean target repo before integration
- `allow_fast_forward`: permit fast-forward integration
- `allow_merge_commit`: permit non-fast-forward merge commits

The integration phase rejects or blocks tasks when execution artifacts are
missing or tampered, validation failed, the task branch is ambiguous, the target
base moved unexpectedly, merge conflicts occur, or the task is not `DONE`.

For each considered task, v0.6 writes
`<artifacts>/<task-id>/integration.json`. The program-level integration
artifacts are `integration_report.json` and `integration_events.ndjson`.
These artifacts record merge mode, base ref, base SHAs, task branch, task
commit SHA, merge or fast-forward SHA, conflict diagnostics, skipped reasons,
and final integration state.

Run report-only integration over the completed command-backed smoke state:

```bash
python3 scripts/local_program_loop_v0.py integrate \
  --program "$adapter_tmp/program.json" \
  --queue "$adapter_tmp/queue.json" \
  --artifacts "$adapter_tmp/artifacts" \
  --worktree-repo "$target_repo" \
  --worktree-base-ref main \
  --integration-mode report-only
```

Run a clean command-backed task and integrate it into `main` in one explicit
local-merge command. This example assumes `program.json` and `queue.json` use
the command-backed payload from the previous smoke section:

```bash
merge_tmp="$(mktemp -d)"
merge_repo="$merge_tmp/target-repo"
mkdir -p "$merge_repo"
git -C "$merge_repo" init
git -C "$merge_repo" config user.name "Local Agent Loop"
git -C "$merge_repo" config user.email "loop@example.invalid"
printf '# Fixture Repository\n' > "$merge_repo/README.md"
git -C "$merge_repo" add README.md
git -C "$merge_repo" commit -m "Initial fixture"
git -C "$merge_repo" branch -M main
cp "$adapter_tmp/program.pristine.json" "$merge_tmp/program.json"
cp "$adapter_tmp/queue.pristine.json" "$merge_tmp/queue.json"

python3 scripts/local_program_loop_v0.py run-program \
  --program "$merge_tmp/program.json" \
  --queue "$merge_tmp/queue.json" \
  --artifacts "$merge_tmp/artifacts" \
  --min-open 1 \
  --roadmap-timeout-sec 60 \
  --max-retries 2 \
  --execution-mode worktree \
  --worktree-repo "$merge_repo" \
  --worktrees-dir "$merge_tmp/worktrees" \
  --worktree-base-ref main \
  --integration-mode local-merge

python3 scripts/local_program_loop_v0.py validate \
  --program "$merge_tmp/program.json" \
  --queue "$merge_tmp/queue.json" \
  --artifacts "$merge_tmp/artifacts" \
  --check-artifacts
```

## Review-Only and Dry Run

Review-only mutates temp copies by running roadmap review and reporting without
invoking the worker:

```bash
tmp_dir="$(mktemp -d)"
cp docs/specs/local-agent-loop-v0/examples/tasks/program.json "$tmp_dir/program.json"
cp docs/specs/local-agent-loop-v0/examples/tasks/queue.json "$tmp_dir/queue.json"

python3 scripts/local_program_loop_v0.py run-program \
  --program "$tmp_dir/program.json" \
  --queue "$tmp_dir/queue.json" \
  --artifacts "$tmp_dir/artifacts" \
  --review-only
```

Dry run validates and reports the review plan without writing queue, program, or
artifact files:

```bash
tmp_dir="$(mktemp -d)"
cp docs/specs/local-agent-loop-v0/examples/tasks/program.json "$tmp_dir/program.json"
cp docs/specs/local-agent-loop-v0/examples/tasks/queue.json "$tmp_dir/queue.json"

python3 scripts/local_program_loop_v0.py run-program \
  --program "$tmp_dir/program.json" \
  --queue "$tmp_dir/queue.json" \
  --artifacts "$tmp_dir/artifacts-dry-run" \
  --dry-run
```

## Schema Contract

`program.json` and `queue.json` must declare:

```json
{
  "schema_version": "local-agent-loop-v0.3"
}
```

Validation fails before mutation for malformed JSON, unsupported schema versions,
duplicate task or backlog ids, invalid task or program states, malformed
dependency lists, self-dependencies, invalid retry counts, and invalid ISO
timestamps. Missing dependency targets are allowed because blocked work may be
waiting on external or future tasks; the unblocking policy keeps those tasks
blocked until dependencies are satisfied or explicitly overridden.

## Durable v0.3 Behavior

- Reruns skip terminal tasks and do not append duplicate task events.
- Task synthesis removes backlog items whose ids already exist in the queue,
  preventing duplicate generated tasks after interrupted runs.
- Persisted non-`IDLE` program states are recovered to `IDLE` with a
  `PROGRAM_STATE_RECOVERY` event before the next review begins.
- Invalid inputs fail fast before queue, program, or artifact files are mutated.
- Program events and task events are append-only NDJSON streams.
- Processed `DONE` and `FAILED` tasks are marked with `processed_by` and
  `processed_ts`; artifact integrity checks apply to those processed tasks.

## Durable v0.4 Worktree Behavior

- Worktree-backed tasks use deterministic local names:
  `codex/local-agent-loop/<slug>-<hash>` branches and
  `<worktrees-dir>/<slug>-<hash>` worktree paths. The hash is derived from the
  full task id so task ids that slug to the same text still get isolated
  branches and worktrees.
- Existing branches and worktrees are reused when they match the deterministic
  task identity.
- A clean worktree receives a deterministic fixture file at
  `agent-loop-results/<slug>-<hash>.md`.
- Successful validation commits the change with `local-agent-loop: <task-id>`
  and records the commit SHA on the task and in `worktree.json`.
- If the deterministic change was already committed but the queue was
  interrupted before finalization, rerun recovery reuses the existing commit
  instead of creating another one.
- Dirty or ambiguous worktrees fail before mutation and write validation
  evidence without a success commit.
- Failed validation leaves the task `FAILED`, records diagnostics, and does not
  record a commit SHA.

## Durable v0.5 Adapter Behavior

- Worktree metadata records `action_adapter`, normalized `action_inputs`,
  expected change paths, validation commands, command output artifact references,
  timeout status, validation output, and commit SHA.
- Command output artifacts are stable JSON files under
  `<artifacts>/<task-id>/command-outputs/` and include captured stdout, stderr,
  exit code, timeout status, argv, and artifact path.
- Command-backed tasks require allowlisted validation command evidence before
  local mutation.
- Failed validation commands and timed-out commands produce `FAILED` task state,
  keep the task branch uncommitted, and preserve diagnostics.
- Reruns skip terminal tasks and do not duplicate branches, worktrees, commits,
  task events, synthesized tasks, or command output artifacts.

## Durable v0.6 Integration Behavior

- Integration is a separate phase from task execution. Task execution state stays
  in `task.state`; merge readiness and merge outcome stay in
  `task.integration_state`.
- `report-only` writes `READY`, `SKIPPED`, `FAILED`, or `BLOCKED` integration
  artifacts without changing the target repo base branch.
- `local-merge` requires an explicit CLI opt-in, a clean target repo, a named
  base ref, valid worktree metadata, passing validation evidence, a single task
  commit, and an allowed task branch prefix.
- Successful local merges record `MERGED` plus the merge commit SHA or
  fast-forward SHA. If the task commit is already contained in the base branch,
  reruns reuse the existing integration artifact and do not create another
  merge.
- Merge conflicts are recorded as `BLOCKED` with conflict diagnostics, the merge
  is aborted, and task execution artifacts remain intact.
- Artifact integrity rejects integrated tasks missing worktree adapter metadata,
  validation evidence, task commit SHA, integration metadata, or merge SHA.

## Expected Behavior

- Program loop can generate additional near-term tasks from roadmap backlog.
- Program loop can reopen blocked work when dependencies are satisfied.
- Program loop leaves blocked work blocked when dependencies are missing or unmet.
- Worker loop resumes partially completed active tasks and writes per-task artifacts.
- Re-running the composed workflow on the same completed inputs does not duplicate
  generated tasks or corrupt task event logs.

## Artifact Contract

For each processed task `<task_id>`, the worker writes:

- `plan.md`
- `events.ndjson`
- `validation.json`
- `result.md`

For each worktree-backed processed task, the worker also writes:

- `worktree.json`
- `command-outputs/*.json` when action or validation commands run
- `integration.json` when the task is considered by the integration phase

`worktree.json` records branch name, worktree path, base ref, action adapter,
action inputs, expected change paths, validation commands, command output
references, timeout status, commit SHA when present, validation output, dirty
status evidence, and final task state. Artifact integrity rejects `DONE`
worktree tasks without adapter metadata, validation command evidence for
command-backed actions, command output artifacts, or a valid commit SHA. It also
rejects `FAILED` worktree tasks that report a success commit.

The program loop writes:

- `roadmap_events.ndjson`
- `roadmap_status.md`
- `program_metrics.json`
- `integration_report.json` when integration runs
- `integration_events.ndjson` when integration runs

`program_metrics.json` must match the final queue and program state for counts,
program id, program state, backlog count, review log count, and schema version.
All files are local and require no cloud services.

## Program Review Log Fields

Each roadmap review appends an entry in `program.json.review_log` with:

- `reason.open_below_threshold`
- `reason.no_unblocked_tasks`
- `reason.timeout_elapsed`
- `generated_tasks`
- `generated_task_ids`
- `unblock_decisions`
- `reopened_task_ids`

## Unblocking Policy

When no unblocked tasks exist, the program loop reviews every blocked task,
reopens each dependency-ready task, and records the policy used:

- `waiting_on_external` -> `request_sync_and_reopen`
- `needs_clarification` -> `create_clarification_task_and_reopen`
- `missing_dependency` -> `create_dependency_task_and_reopen`
- fallback -> `manual_review_then_reopen`

Blocked tasks support `depends_on: ["task-id"]`. A blocked task is only reopened
when every dependency is `DONE`, unless the task explicitly sets
`dependency_override` or `override_dependencies`.

## Regression Harness

```bash
python3 scripts/test_local_agent_loop_v0.py
```

The harness includes v0.2 behavior scenarios for backlog refill, all tasks
blocked, dependency-unblock partial success, validation retry, illegal transition
guarding, and deterministic scheduling. It also includes v0.3 scenarios for
malformed input, duplicate ids, interrupted run recovery, rerun idempotency,
dry-run behavior, schema version mismatch, and artifact integrity. v0.4 adds
worktree scenarios for successful execution, validation failure with no commit,
dirty worktree rejection, interrupted existing-commit recovery, rerun
idempotency, missing worktree metadata detection, branch/worktree reuse,
terminal artifact recovery, ambiguous branch rejection, task-id collision
isolation, metadata tamper detection, and relative worktree-dir CLI handling.
v0.5 adds adapter scenarios for command-backed success, validation command
failure, command timeout, adapter reruns, missing command output artifacts,
missing `DONE` metadata, unknown adapter rejection, ambiguous command rejection,
missing validation evidence, and unsafe action path rejection. v0.6 adds
integration scenarios for report-only readiness, local-merge idempotency,
merge conflicts, target-base drift, not-`DONE` rejection, ambiguous task
branches, dirty target repos, policy contract branch allowlists, and integrated
artifact integrity.

It exits non-zero if any assertion or checked-in fixture comparison fails.

## Completion Gate for v0.6

```bash
PYTHONPYCACHEPREFIX="$(mktemp -d)" python3 -m py_compile \
  scripts/local_agent_loop_v0.py \
  scripts/local_program_loop_v0.py \
  scripts/test_local_agent_loop_v0.py

PYTHONPYCACHEPREFIX="$(mktemp -d)" python3 scripts/test_local_agent_loop_v0.py

tmp_dir="$(mktemp -d)"
cp docs/specs/local-agent-loop-v0/examples/tasks/program.json "$tmp_dir/program.json"
cp docs/specs/local-agent-loop-v0/examples/tasks/queue.json "$tmp_dir/queue.json"
python3 scripts/local_program_loop_v0.py run-program \
  --program "$tmp_dir/program.json" \
  --queue "$tmp_dir/queue.json" \
  --artifacts "$tmp_dir/artifacts" \
  --min-open 2 \
  --roadmap-timeout-sec 60 \
  --max-retries 2
python3 scripts/local_program_loop_v0.py status \
  --program "$tmp_dir/program.json" \
  --queue "$tmp_dir/queue.json" \
  --artifacts "$tmp_dir/artifacts"

worktree_tmp="$(mktemp -d)"
target_repo="$worktree_tmp/target-repo"
mkdir -p "$target_repo"
git -C "$target_repo" init
git -C "$target_repo" config user.name "Local Agent Loop"
git -C "$target_repo" config user.email "loop@example.invalid"
printf '# Fixture Repository\n' > "$target_repo/README.md"
git -C "$target_repo" add README.md
git -C "$target_repo" commit -m "Initial fixture"
git -C "$target_repo" branch -M main
cp docs/specs/local-agent-loop-v0/examples/tasks/program.json "$worktree_tmp/program.json"
cp docs/specs/local-agent-loop-v0/examples/tasks/queue.json "$worktree_tmp/queue.json"
python3 scripts/local_program_loop_v0.py run-program \
  --program "$worktree_tmp/program.json" \
  --queue "$worktree_tmp/queue.json" \
  --artifacts "$worktree_tmp/artifacts" \
  --min-open 2 \
  --roadmap-timeout-sec 60 \
  --max-retries 2 \
  --execution-mode worktree \
  --worktree-repo "$target_repo" \
  --worktrees-dir "$worktree_tmp/worktrees" \
  --worktree-base-ref main
python3 scripts/local_program_loop_v0.py validate \
  --program "$worktree_tmp/program.json" \
  --queue "$worktree_tmp/queue.json" \
  --artifacts "$worktree_tmp/artifacts" \
  --check-artifacts

adapter_tmp="$(mktemp -d)"
target_repo="$adapter_tmp/target-repo"
mkdir -p "$target_repo"
git -C "$target_repo" init
git -C "$target_repo" config user.name "Local Agent Loop"
git -C "$target_repo" config user.email "loop@example.invalid"
printf '# Fixture Repository\n' > "$target_repo/README.md"
git -C "$target_repo" add README.md
git -C "$target_repo" commit -m "Initial fixture"
git -C "$target_repo" branch -M main
python3 - <<'PY' "$adapter_tmp/program.json" "$adapter_tmp/queue.json"
import json
import sys

program_path, queue_path = sys.argv[1:3]
path = "agent-loop-results/command-backed-fixture.md"
content = "# Command Backed Fixture\n\nTask: task-command-backed\n"
action = (
    "from pathlib import Path; "
    f"p=Path({path!r}); "
    "p.parent.mkdir(parents=True, exist_ok=True); "
    f"p.write_text({content!r}, encoding='utf-8')"
)
validation = (
    "from pathlib import Path; "
    f"data=Path({path!r}).read_text(encoding='utf-8'); "
    f"assert data == {content!r}; "
    "print('validated command-backed fixture')"
)
action_argv = ["python3", "-c", action]
validation_argv = ["python3", "-c", validation]
program = {
    "schema_version": "local-agent-loop-v0.3",
    "program_id": "adapter-smoke",
    "goal": "Run a command-backed local action adapter",
    "program_state": "IDLE",
    "last_roadmap_review_ts": "2026-01-01T00:00:00+00:00",
    "roadmap_backlog": [],
    "review_log": [],
}
queue = {
    "schema_version": "local-agent-loop-v0.3",
    "tasks": [
        {
            "id": "task-command-backed",
            "title": "Command-backed adapter smoke",
            "state": "OPEN",
            "retries": 0,
            "required_checks": ["command-fixture"],
            "local_action": {
                "adapter": "command-backed-patch-fixture",
                "inputs": {
                    "name": "write-command-fixture",
                    "command": action_argv,
                    "allowed_commands": [action_argv],
                    "timeout_sec": 5,
                    "expected_path": path,
                    "expected_content": content,
                },
            },
            "validation_commands": [
                {
                    "name": "verify-command-fixture",
                    "command": validation_argv,
                    "timeout_sec": 5,
                }
            ],
            "validation_command_allowlist": [validation_argv],
        }
    ],
}
open(program_path, "w", encoding="utf-8").write(json.dumps(program, indent=2) + "\n")
open(queue_path, "w", encoding="utf-8").write(json.dumps(queue, indent=2) + "\n")
PY
cp "$adapter_tmp/program.json" "$adapter_tmp/program.pristine.json"
cp "$adapter_tmp/queue.json" "$adapter_tmp/queue.pristine.json"
python3 scripts/local_program_loop_v0.py run-program \
  --program "$adapter_tmp/program.json" \
  --queue "$adapter_tmp/queue.json" \
  --artifacts "$adapter_tmp/artifacts" \
  --min-open 1 \
  --roadmap-timeout-sec 60 \
  --max-retries 2 \
  --execution-mode worktree \
  --worktree-repo "$target_repo" \
  --worktrees-dir "$adapter_tmp/worktrees" \
  --worktree-base-ref main
python3 scripts/local_program_loop_v0.py validate \
  --program "$adapter_tmp/program.json" \
  --queue "$adapter_tmp/queue.json" \
  --artifacts "$adapter_tmp/artifacts" \
  --check-artifacts

python3 scripts/local_program_loop_v0.py integrate \
  --program "$adapter_tmp/program.json" \
  --queue "$adapter_tmp/queue.json" \
  --artifacts "$adapter_tmp/artifacts" \
  --worktree-repo "$target_repo" \
  --worktree-base-ref main \
  --integration-mode report-only

merge_tmp="$(mktemp -d)"
merge_repo="$merge_tmp/target-repo"
mkdir -p "$merge_repo"
git -C "$merge_repo" init
git -C "$merge_repo" config user.name "Local Agent Loop"
git -C "$merge_repo" config user.email "loop@example.invalid"
printf '# Fixture Repository\n' > "$merge_repo/README.md"
git -C "$merge_repo" add README.md
git -C "$merge_repo" commit -m "Initial fixture"
git -C "$merge_repo" branch -M main
cp "$adapter_tmp/program.pristine.json" "$merge_tmp/program.json"
cp "$adapter_tmp/queue.pristine.json" "$merge_tmp/queue.json"
python3 scripts/local_program_loop_v0.py run-program \
  --program "$merge_tmp/program.json" \
  --queue "$merge_tmp/queue.json" \
  --artifacts "$merge_tmp/artifacts" \
  --min-open 1 \
  --roadmap-timeout-sec 60 \
  --max-retries 2 \
  --execution-mode worktree \
  --worktree-repo "$merge_repo" \
  --worktrees-dir "$merge_tmp/worktrees" \
  --worktree-base-ref main \
  --integration-mode local-merge
python3 scripts/local_program_loop_v0.py validate \
  --program "$merge_tmp/program.json" \
  --queue "$merge_tmp/queue.json" \
  --artifacts "$merge_tmp/artifacts" \
  --check-artifacts

git diff --check
```
