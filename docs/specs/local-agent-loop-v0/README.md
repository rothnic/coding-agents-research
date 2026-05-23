# Local Agent Loop v0 — Proof of Concept

This folder includes a runnable local-only proof of concept for a Symphony-like orchestration loop **plus** a composed higher-level roadmap workflow.

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
- Program roadmap sample: `docs/specs/local-agent-loop-v0/examples/tasks/program.json`
- Queue sample: `docs/specs/local-agent-loop-v0/examples/tasks/queue.json`
- Artifact output root (generated): `docs/specs/local-agent-loop-v0/examples/artifacts/`

## Worker State Machine

`OPEN -> CLAIMED -> PLANNED -> EXECUTING -> VALIDATING -> (DONE | FAILED | BLOCKED)`

Retry path:

`VALIDATING -> EXECUTING -> VALIDATING` (bounded by `max_retries`)

## Run Worker Loop Only

```bash
python scripts/local_agent_loop_v0.py \
  --queue docs/specs/local-agent-loop-v0/examples/tasks/queue.json \
  --artifacts docs/specs/local-agent-loop-v0/examples/artifacts \
  --max-retries 2
```

## Run Composed Program + Worker Workflow

```bash
python scripts/local_program_loop_v0.py \
  --program docs/specs/local-agent-loop-v0/examples/tasks/program.json \
  --queue docs/specs/local-agent-loop-v0/examples/tasks/queue.json \
  --artifacts docs/specs/local-agent-loop-v0/examples/artifacts \
  --min-open 2 \
  --roadmap-timeout-sec 60 \
  --max-retries 2
```

## Expected Behavior

- Program loop can generate additional near-term tasks from roadmap backlog.
- Program loop can reopen blocked work when no unblocked tasks remain.
- Worker loop processes runnable tasks and writes per-task artifacts.

## Artifact Contract

For each task `<task_id>`, the worker writes:

- `plan.md`
- `events.ndjson`
- `validation.json`
- `result.md`

All files are local and require no cloud services.


## Program Review Log Fields

Each roadmap review appends an entry in `program.json.review_log` with:

- `reason.open_below_threshold`
- `reason.no_unblocked_tasks`
- `reason.timeout_elapsed`
- `generated_tasks`
- `unblocked_task_id` (if an unblock action was taken)


## Program-Level Artifacts

The program loop now also writes a higher-level event stream:

- `roadmap_events.ndjson` (under the same artifacts root)

Each event contains the roadmap review decision payload for auditing high-level planning/replanning behavior.

## Unblocking Policy

When no unblocked tasks exist, the program loop reopens one blocked task and records the policy used:

- `waiting_on_external` -> `request_sync_and_reopen`
- `needs_clarification` -> `create_clarification_task_and_reopen`
- `missing_dependency` -> `create_dependency_task_and_reopen`
- fallback -> `manual_review_then_reopen`

## Next Logical Functionality (v0.2 Plan) with Discrete Success Criteria

This is the recommended next increment focused on user-facing reliability and measurable completion.

### 1) Dependency-Aware Unblocking Graph

**What to add**
- Represent task dependencies explicitly (`depends_on: [task-id...]`).
- Prevent reopening blocked tasks unless dependencies are resolved or explicitly overridden.
- Add blocked-cause normalization (`blocked_reason_code`) and a deterministic unblock action matrix.

**Success criteria**
- Given a queue with 3 blocked tasks and different dependency chains, the program loop:
  - reopens only tasks whose prerequisites are `DONE`,
  - leaves unresolved tasks blocked,
  - emits one unblock decision event per reviewed blocked task.
- `roadmap_events.ndjson` includes `dependency_check: pass|fail` for each unblock attempt.

**Measurable checks**
- `>= 1` and `<= N` tasks reopened exactly as predicted by dependency graph fixture.
- `0` reopened tasks with unmet dependencies (strict).

### 2) Program State Machine + Transition Guardrails

**What to add**
- Introduce explicit program states:
  - `ROADMAP_REVIEWING`, `TASK_SYNTHESIZING`, `UNBLOCKING`, `HANDING_OFF`, `IDLE`.
- Enforce legal transitions and record transition failures as events.

**Success criteria**
- Every program-loop run records at least one program-state transition.
- Illegal transition injection test is rejected and logged with reason.

**Measurable checks**
- `100%` of program events include `from_program_state` + `to_program_state`.
- `0` silent transition failures.

### 3) Priority and Scheduling Policy

**What to add**
- Add `priority` and optional `deadline_ts` to roadmap backlog items.
- Generate near-term tasks by deterministic ordering (priority desc, earliest deadline, FIFO tie-break).

**Success criteria**
- In a mixed-priority fixture, generated tasks are always emitted in expected order.
- Repeated runs with identical input produce identical generated order.

**Measurable checks**
- Ordering test pass rate: `100%` across at least 20 repeated runs.
- Determinism mismatch count: `0`.

### 4) High-Level Outcome Reporting

**What to add**
- New artifact: `roadmap_status.md` summarizing objective progress, blockers, and next 3 tasks.
- Add compact JSON snapshot `program_metrics.json` with counts and rates.

**Success criteria**
- After each composed run, both artifacts are updated once.
- Metrics include: `open_count`, `blocked_count`, `unblocked_count`, `generated_count`, `done_count`, `failed_count`.

**Measurable checks**
- Artifact freshness: timestamp delta between run start and artifact write `< 5s`.
- Missing required metric fields: `0`.

### 5) Regression Test Harness (CLI-Level)

**What to add**
- Add a lightweight local test script that executes canonical scenarios:
  - backlog refill,
  - all tasks blocked,
  - dependency-unblock partial success,
  - validation fail then retry.

**Success criteria**
- All scenarios run via one command and return non-zero on any assertion failure.
- Outputs are diff-stable against checked-in fixtures.

**Measurable checks**
- Scenario pass count = total scenario count.
- Fixture diff violations: `0`.

## Definition of Done for v0.2

v0.2 is complete only when:
- all five functionality areas above are implemented,
- all measurable checks pass in CI/local repeated runs,
- README examples are updated and reproducible from clean checkout,
- at least one end-to-end run demonstrates:
  - roadmap review,
  - deterministic task synthesis,
  - dependency-aware unblocking,
  - worker execution,
  - auditable program + task artifacts.
