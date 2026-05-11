# OpenAI Symphony — Loop Notes

## Core Loop to Extract

1. Ingest task
2. Plan
3. Execute in isolated workspace
4. Validate
5. Record outcome
6. Iterate or finish

## Minimum Invariants

- Every step writes a trace artifact.
- Validation gates merge/progress.
- Failures produce explicit retry or stop decisions.

