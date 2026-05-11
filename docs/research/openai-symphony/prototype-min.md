# OpenAI Symphony — Minimal Local-First Prototype

## Goal

Build the smallest end-to-end coding-agent loop that is still functional.

## Constraints

- No required cloud services.
- Single machine, local repos, local issue tracker.
- Human-readable markdown artifacts.

## Proposed Toy Architecture

- **Issue source**: beads project/issues as task queue.
- **Orchestrator**: simple loop runner script.
- **Workspace**: git worktree per task.
- **Agent step**: plan -> edit -> test -> summarize.
- **Validation**: required command set per task.
- **Output**: commit + run log + concise result report.

## End-to-End Flow

1. Pick next open issue.
2. Create task worktree.
3. Generate tiny plan artifact.
4. Apply code/doc changes.
5. Run required checks.
6. Commit results.
7. Write completion summary linked to issue.

## First Implementation Slice

- One script to bootstrap task workspace.
- One script to run canonical loop phases.
- One markdown template for results.
- One example task proving full loop works.

