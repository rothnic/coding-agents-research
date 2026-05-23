# OpenAI Symphony — Comprehensive Analysis + Tiny Local Reproduction Spec

## 1) Purpose

This document deepens the current Symphony research set with:

1. A **detailed system analysis** of Symphony-like orchestration concerns.
2. A **tiny, hyper-concise local-only reproduction spec** using:
   - `beads` as the task substrate,
   - an event/loop-based orchestrator,
   - no cloud dependencies.

It intentionally remains design-only (no prototype implementation yet).

---

## 2) System Analysis (Symphony-Like Orchestration)

## 2.1 Design Intent (What the system is trying to optimize)

A Symphony-style system is best understood as an **agentic production pipeline** with four simultaneous goals:

- **Throughput**: complete many tasks concurrently.
- **Correctness**: enforce validation before completion.
- **Auditability**: preserve traceable decisions and artifacts.
- **Recoverability**: survive failure without losing state.

This creates a tension triangle:

- More autonomy increases throughput but can reduce reliability.
- More guardrails increase reliability but can reduce speed.
- More logging increases debuggability but can add overhead.

A robust design therefore externalizes state transitions, standardizes artifacts, and applies deterministic gates.

## 2.2 Architectural Decomposition

A minimal Symphony-like architecture can be decomposed into nine cooperating domains:

1. **Intake/Queue Domain**
   - Receives tasks and assigns unique IDs.
   - Supports prioritization and selection rules.

2. **Planning Domain**
   - Converts task intent into executable step plans.
   - Declares required validations and stop conditions.

3. **Execution Domain**
   - Runs edits/actions in isolated work context.
   - Captures command outputs and patch diffs.

4. **Validation Domain**
   - Evaluates required checks (tests/lints/builds).
   - Emits pass/fail and structured failure reasons.

5. **Decision Domain**
   - Chooses next transition: retry, escalate, or complete.
   - Applies policy limits (max retries, timeout budgets).

6. **Artifact Domain**
   - Stores plans, logs, diffs, result summaries.
   - Links artifacts to task IDs and timestamps.

7. **State Machine Domain**
   - Encodes legal transitions and terminal states.
   - Prevents ambiguous in-between states.

8. **Concurrency Domain**
   - Manages worker parallelism and locking semantics.
   - Avoids duplicate claim/execution of same task.

9. **Human Oversight Domain**
   - Defines checkpoints requiring explicit approval.
   - Enables intervention and policy overrides.

## 2.3 Core Contract: Task as a State Machine

A Symphony-like loop is most reliable when task lifecycle is explicit:

`OPEN -> CLAIMED -> PLANNED -> EXECUTING -> VALIDATING -> (DONE | BLOCKED | FAILED)`

Optional loops:

- `VALIDATING -> EXECUTING` (fix-and-retry)
- `EXECUTING -> BLOCKED` (dependency uncertainty)
- `BLOCKED -> CLAIMED` (after human unblock)

Key invariant: **every transition writes an artifact** (event + metadata).

## 2.4 Invariants Required for Production-Like Behavior

1. **Single active owner per task** at a time.
2. **Idempotent claims** (restarts must not duplicate work).
3. **Deterministic validation gates** (same checks, same criteria).
4. **Append-only event history** (postmortem-safe).
5. **Bounded retries** with explicit terminal failure.
6. **Artifact completeness** before terminal states.

## 2.5 Failure Taxonomy and Recovery Strategy

Failures should be tagged, not just logged:

- `PLAN_ERROR`: unclear requirement; replan/human clarify.
- `EXEC_ERROR`: edit/script/runtime failure; retry with patch.
- `VALIDATION_FAIL`: tests/lints fail; iterate until retry budget.
- `ENV_ERROR`: missing local dependency/tooling.
- `POLICY_BLOCK`: action requires human approval/policy bypass.

Recovery policy (minimal):

- `retry_count < N`: retry phase with context.
- `retry_count == N`: mark `FAILED` with reason bundle.
- `BLOCKED` class errors: pause + request human action.

## 2.6 Concurrency Model Considerations

For local-first reproduction, safe concurrency requires:

- **Claim lock** at queue layer (`beads` issue assignment/tag lock).
- **Workspace isolation** via one worktree per task.
- **Per-task event stream** to avoid cross-task log mixing.

Potential race scenarios:

- Double-claim due to stale read.
- Completed task still visible as open.
- Worker crash leaving a stale claim.

Mitigations:

- Heartbeat timestamp + claim TTL.
- Reaper step to release expired claims.
- Transition precondition checks before write.

## 2.7 Artifact Schema (Minimum Viable)

Per task `T`, maintain:

- `artifacts/T/plan.md`
- `artifacts/T/events.ndjson`
- `artifacts/T/validation.json`
- `artifacts/T/result.md`

Event record minimum fields:

- `ts` (UTC timestamp)
- `task_id`
- `from_state`
- `to_state`
- `actor` (worker/human)
- `reason`
- `artifact_refs[]`

## 2.8 Why This Mirrors Symphony Behavior (Without Cloud)

Even without hosted services, this model preserves the essential distributed-work properties:

- queue-based intake,
- autonomous worker loop,
- strict state transitions,
- validation-gated progression,
- durable audit trail.

Cloud components mainly add elasticity/managed infra; they do not change the conceptual control loop.

---

## 3) Tiny Hyper-Concise Local Reproduction Spec (No Implementation)

## 3.1 Objective

Demonstrate Symphony-like distributed execution semantics on one machine using `beads` + local event loop.

## 3.2 Non-Goals

- No cloud APIs/services.
- No remote queue brokers.
- No multi-host orchestration in v0.

## 3.3 Components

1. **Task Queue (`beads`)**
   - Source of truth for task status.

2. **Loop Runner (single binary/script)**
   - Polls queue, claims tasks, drives state machine.

3. **Worker Executor**
   - Runs plan/edit/test phases inside isolated worktree.

4. **Artifact Writer**
   - Emits append-only events and outcome files.

## 3.4 Event/Loop Model

- Poll interval: fixed (`POLL_MS`).
- For each tick:
  1. fetch `OPEN` tasks,
  2. attempt atomic claim,
  3. run phase machine,
  4. persist events after each transition.

Event types (minimal):

- `TASK_CLAIMED`
- `PLAN_WRITTEN`
- `EXEC_STARTED`
- `EXEC_FINISHED`
- `VALIDATION_PASSED`
- `VALIDATION_FAILED`
- `TASK_DONE`
- `TASK_BLOCKED`
- `TASK_FAILED`

## 3.5 State Transition Table (v0)

- `OPEN -> CLAIMED`
- `CLAIMED -> PLANNED`
- `PLANNED -> EXECUTING`
- `EXECUTING -> VALIDATING`
- `VALIDATING -> DONE` (pass)
- `VALIDATING -> EXECUTING` (fail + retries left)
- `VALIDATING -> FAILED` (fail + retries exhausted)
- `ANY_ACTIVE -> BLOCKED` (external dependency)

## 3.6 Acceptance Criteria

The reproduction is considered successful if one task can be shown to:

1. Move through legal states only.
2. Produce all required artifacts.
3. Enforce validation gate before `DONE`.
4. Recover from one induced validation failure via retry.
5. Finish with auditable result summary.

## 3.7 Minimal Config

```yaml
max_workers: 2
poll_ms: 1500
max_retries: 2
claim_ttl_sec: 900
required_checks:
  - test
  - lint
```

## 3.8 Security/Safety for Local-Only Mode

- Allowlist executable commands per project.
- Redact secrets from logs.
- Enforce workspace path boundary.
- Record exact command + exit code for each check.

## 3.9 Evolution Path (After v0)

- Add pluggable policy engine for transition rules.
- Add priority scheduling + starvation prevention.
- Add multi-process workers on same host.
- Later: swap queue backend while preserving event contract.

---

## 4) One-Screen "Tiny Spec" (Ultra-Condensed)

**Name:** `local-symphony-loop-v0`

**Goal:** Execute queued tasks with deterministic state transitions, validation gating, and full audit artifacts.

**Stack:** `beads` (queue/state), local git worktrees (isolation), event-loop runner (control), markdown/json artifacts (traceability).

**States:** `OPEN, CLAIMED, PLANNED, EXECUTING, VALIDATING, DONE, BLOCKED, FAILED`.

**Loop:** poll -> claim -> plan -> execute -> validate -> {done | retry | fail | block}.

**Rules:** single owner, append-only events, bounded retries, required checks before done.

**Artifacts:** `plan.md`, `events.ndjson`, `validation.json`, `result.md` per task.

**No-cloud guarantee:** all components run locally; no external control plane required.
