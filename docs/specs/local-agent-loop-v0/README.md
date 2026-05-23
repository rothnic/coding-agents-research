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
