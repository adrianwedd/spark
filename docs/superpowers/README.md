# Specs and Plans — Decision Fossils

> **These documents are evidence, not operational truth.**
>
> Everything under `specs/` and `plans/` records what was *decided* and *why*,
> on the date in its filename. None of it is maintained afterwards. A spec
> describes the system as it was intended at the moment of writing; the code
> moved on, and the spec did not.
>
> **Do not implement from a document here, and do not cite one as current
> behaviour.** Read it to understand *why* something is the way it is, then
> verify the *what* against the code and against
> [the canonical docs](../architecture/overview.md).

---

## How to use these

| You want to know | Read |
|---|---|
| What the system does today | [docs/architecture/overview.md](../architecture/overview.md) |
| Why it does that | the spec here, plus the "Why it looks like this" section of the canonical doc |
| What was tried and rejected | the spec's alternatives section |

Specs whose subject is still live are cross-linked from the canonical doc that
owns the topic. Those links point *backwards* — from current truth to its
rationale — never the other way round.

## Contents

- **`specs/`** — designs produced by the brainstorming workflow, written
  before implementation. `YYYY-MM-DD-<topic>-design.md`.
- **`plans/`** — step-by-step implementation plans, including their staging
  and commit commands. Those commands were correct for the tree that existed
  then; **do not run them.**

## Known-superseded highlights

These are frequently mistaken for current truth:

| Fossil | Superseded by |
|---|---|
| `specs/2026-08-01-px-brain-design.md` | shipped and changed — [resident-brain](../architecture/resident-brain.md) |
| `specs/2026-08-17-brain-handshake-validation-design.md` | shipped — readiness is now a proven round trip |
| `plans/*` staging commands | [git-workflow](../git-workflow.md) — stage exact owned paths |
| any test count in any plan | [testing](../testing.md) |

Other historical material — superseded session notes and doc reviews — is in
[docs/historical/](../historical/).
