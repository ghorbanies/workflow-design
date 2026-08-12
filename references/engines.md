# Engine or hand-rolled? — choosing the substrate

This skill's two-table pattern (items + append-only transitions) is a deliberate default,
not a prejudice. Workflow engines exist for good reasons; so does not using one. The
choice is decidable from the flow's properties, not from taste.

## Contents
- The distinction that decides it
- Decision table
- If you hand-roll
- If you use an engine
- The hybrid that usually wins
- Signals you chose wrong

## The distinction that decides it

**Engines orchestrate code. This skill's flows orchestrate decisions.**

If the steps between states are *service calls* — charge the card, provision the account,
call the third-party API, retry on failure — the hard problems are machine-pace ones:
retries, idempotency, partial failure, exactly/at-least-once. That is what engine
runtimes (the Temporal / Step Functions / Airflow / n8n class) are built for, and
hand-rolling them is how homemade distributed-systems bugs are born.

If the steps between states are *human decisions* — review, approve, reject, assign — the
hard problems are the ones in this skill: honest states, enforced gates, a trustworthy
log, metrics. Items wait hours-to-weeks; throughput is decisions per day, not calls per
second. An engine adds an infrastructure dependency, a second source of truth, and a
learning curve — and still doesn't solve the actual hard problems, because they are
modeling problems, not execution problems.

Most real systems contain both kinds of step. That is the hybrid, below.

## Decision table

Score the flow; the column with more checks wins.

| Property | Hand-rolled (two tables) | Engine |
|---|---|---|
| Steps are mostly | human decisions | service calls |
| Item pace | minutes to weeks | ms to minutes |
| Retries/compensation needed | rare, manual is fine | core requirement |
| Timers | a few SLAs (cron is enough) | many, dynamic, per-item |
| State must be queryable by ops/analytics | constantly | occasionally |
| Cross-service transactions (sagas) | no | yes |
| Team's strength | SQL + the app language | comfortable running infra |
| Audit needs | log IS the product | engine history is an implementation detail |

Two overrides that beat the table:

- **If the org already runs an engine well**, the marginal cost of one more workflow on it
  is low — consistency beats purity.
- **If the flow is the product's core audit artifact** (approvals, compliance), keep the
  domain log in your own database *regardless* of what executes the steps.

## If you hand-roll

The two tables from [modeling.md](modeling.md), plus the three things engines would have
given you — build only these, not a framework:

1. **One writer path**: every transition goes through a single function that validates the
   edge against the model, writes the row, and updates the item — the execution point for
   everything this skill checks. (This is also what `conformance.py` will hold to account.)
2. **A timer runner**: one scheduled job scanning for due `timeout` transitions
   ([slas.md](slas.md)). Resist per-feature cron jobs; one runner, table-driven.
3. **Idempotent transition writes**: the guard "current state must equal the edge's
   from-state" inside the writer, so a double-submit becomes a no-op error, not a
   duplicate row. This single check is most of what "workflow engine" means at human pace.

## If you use an engine

The rules of this skill do not go away — they relocate:

- **Keep your own transition log anyway.** Engine execution history is retention-limited,
  vendor-shaped, and gone when you migrate. The domain log (who approved what, when, why)
  outlives the substrate; write it from the workflow's steps as first-class writes.
- **The model file stays the source of truth** for states/gates/reasons, and
  `conformance.py` runs against *your* log exactly as before — the engine is an
  implementation detail behind it.
- **Human gates in engine workflows** (signals, task tokens, callbacks) still need
  capability checks, enumerated rejection reasons, and honest naming — engines enforce
  none of these; they just wait efficiently.
- Watch for the engine's *own* invisible exceptions: dev-console "terminate & restart"
  operations are transitions too, and if they bypass your log, your history has holes
  shaped exactly like your worst incidents.

## The hybrid that usually wins

Human-gated flow in the two tables; machine legs delegated:

```
[submitted] --human gate--> [approved] --engine workflow: provision+bill+notify--> [fulfilled]
```

The gate decision writes the domain log and kicks off the engine job; the job's completion
(or terminal failure) writes the next domain transition. The domain log stays the state of
record; the engine handles what it is good at — and each side's failure modes stay in the
tool built for them.

## Signals you chose wrong

- Hand-rolled, and you are building retry queues, backoff logic, or a DAG scheduler →
  those steps wanted an engine.
- Engine, and analysts export execution histories to answer "where do items stall" →
  the domain log is missing; you bought an engine and lost your metrics.
- Either substrate, and a "quick manual fix" writes states without transition rows →
  the writer path has a back door; every guarantee in this skill is now optional.
