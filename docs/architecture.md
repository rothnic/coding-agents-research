# Repository Architecture

This repo is optimized for **many small, isolated research efforts** that stay organized over time.

## Directory Layout

- `docs/research/` — project analyses (one folder per target project)
- `docs/guides/` — distilled best practices (cross-project)
- `docs/specs/` — implementation-ready specs derived from guides
- `worktrees/` — optional local checkouts/branches for experiments
- `scripts/` — helper scripts for setup, extraction, and validation
- `templates/` — reusable markdown templates for consistency

## Naming Rules

- Research folders: `<org>-<project>` (e.g., `openai-symphony`)
- Spec folders: `<theme>-<version>` (e.g., `local-agent-loop-v0`)
- Use kebab-case for all file and folder names.

## Documentation Flow

1. **Research**: capture facts, structure, and key mechanisms from source project.
2. **Distill**: create concise guides across recurring patterns.
3. **Specify**: produce local-first, implementation-ready specs.
4. **Prototype**: build tiny end-to-end proof in iterative branches.

## Quality Bar

Every research package should include:

- `summary.md` (ultra concise overview)
- `structure.md` (how system is organized)
- `loops.md` (core execution/control loop)
- `prototype-min.md` (smallest functional local-first variant)
- `next-steps.md` (actionable implementation backlog)

