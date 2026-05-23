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
- `status` prints a compact JSON status snapshot and optional artifact errors.
- `run-worker` and `run-program` can opt into v0.4 git worktree execution with
  `--execution-mode worktree`.

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

v0.4 adds an optional local git worktree executor around the same v0.3
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

`worktree.json` records branch name, worktree path, base ref, commit SHA when
present, validation output, dirty status evidence, and final task state. Artifact
integrity rejects `DONE` worktree tasks without a valid commit SHA and rejects
`FAILED` worktree tasks that report a success commit.

The program loop writes:

- `roadmap_events.ndjson`
- `roadmap_status.md`
- `program_metrics.json`

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

It exits non-zero if any assertion or checked-in fixture comparison fails.

## Completion Gate for v0.4

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

git diff --check
```
