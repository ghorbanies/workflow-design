# Evaluations

These scenarios are the source of truth for whether this skill works — not the authors'
impression of it. Each one tests a different failure mode: wrong triggering, wrong tool,
tool used without its discipline, or advice given from memory instead of from the
references.

## How to run

There is no built-in runner. Each scenario is run against a **fresh Claude instance** with
the skill installed and *no other context* — the point is to test what a stranger gets,
not what the authors get after a long session.

1. Start a fresh session with the skill available.
2. Give it the `query` verbatim, with the listed `files` present in the working directory.
3. Score each line of `expected_behavior` as pass/fail. No partial credit.
4. A scenario passes only if every line passes.

Fixtures live in `fixtures/`. `fixtures/broken.flow.json` is a deliberately defective flow
definition; generate the demo transition log with:

```bash
python ../scripts/dynamics.py --demo fixtures/demo.sqlite
```

Re-run all scenarios after any change to SKILL.md, the references, or the tools' interfaces
— and before any release. Track results in the release notes, not in this file.

## Scoring notes

- "Runs X" means the tool was actually executed and its real output used — quoting the
  tool's documentation without running it is a fail.
- "Refuses" means the response explicitly declines to produce the number/claim and says
  why — hedging while still producing it is a fail.
- Expected behaviors are written to be checkable by a reviewer who has not read this
  skill; if a line feels judgment-heavy, tighten the line rather than the judgment.
