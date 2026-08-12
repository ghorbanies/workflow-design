# Phase 4 — Changing a workflow that already has items in it

## Contents
1. Version the definition, and stamp it on the item
2. Never rename a state in place
3. In-flight items: drain / map / dual-run
4. Backfilled rows must be marked (never fabricate a decision)
5. Adding a gate (grandfather or send back)
6. Removing a gate (history stays)
7. Changing a routing or lookup table
8. Schema changes: expand, migrate, contract
9. A migration whose errors are swallowed fails silently
10. Measure before, and change one thing at a time
- Checklist for changing a live flow

Phases 1–3 assume you are building or reading a flow. This one is about the moment that
actually breaks things: the definition changes while items are mid-flight. A new gate, a
renamed state, a different routing table, a split station.

The failure is never the change itself. It is that **two truths now exist** — the flow
items entered under, and the flow they are being judged by — and nobody wrote down which
applies to whom.

---

## 1. Version the definition, and stamp it on the item

```sql
ALTER TABLE items ADD COLUMN flow_version INTEGER NOT NULL DEFAULT 1;
```

An item carries the version it entered under. This costs one column and buys three things:
history that still reads correctly, a migration you can run in batches, and charts that can
be split at the cutover instead of averaging two different processes together.

Also write one marker row into the transition log at cutover
(`gate='__flow_version', to_state='v2', actor_kind='system'`). Anyone reading a chart a year
later can see the line where the definition changed. **A metric series that crosses a
definition change without a visible break is a lie of omission** — the step in the graph
looks like a result, and it is an artifact.

---

## 2. Never rename a state in place

Renaming rewrites history: every past row now claims the item was in a state that did not
exist at the time. It is the same defect as recomputing a stored routing key, with a worse
blast radius.

Instead: add the new state, move items explicitly, and leave the old name in the log
forever. Queries that group by state get a mapping table, not an `UPDATE`.

```sql
-- read-side mapping, not a rewrite
CREATE TABLE state_aliases (old TEXT PRIMARY KEY, new TEXT NOT NULL);
INSERT INTO state_aliases VALUES ('awaiting_qa', 'awaiting_review');
```

The same rule covers gate names, reason codes, and capabilities. Old values keep meaning
what they meant.

---

## 3. In-flight items: pick one of three, and say which

| Strategy | What it means | Use when |
|---|---|---|
| **Drain** | Stop admitting to the old flow; let existing items finish under v1 | The flow is short (hours/days) and both versions can run |
| **Map** | Move every in-flight item to a v2 state by an explicit old→new table | The flow is long, or the old version cannot be supported |
| **Dual-run** | Both definitions live; `flow_version` decides which rules apply | The change is risky and you want a reversible cutover |

There is no fourth option called "it'll sort itself out". Whichever you pick, write it in the
project's decision log with the date, because the metrics for that period are only
interpretable if you know which one was in force.

A mapped move is a **real transition**, recorded like any other:

```
to_state='awaiting_review', actor_kind='system', reason_code='flow_migration_v2'
```

Never silently `UPDATE items SET state=…` without a transition row. An item that changed
state with no event is invisible to every metric in phase 3, and to anyone trying to
reconstruct what happened.

---

## 4. Backfilled rows must be marked as backfilled

If you write history to fill a gap, mark it: `actor_kind='system'`,
`reason_code='backfill'`. Unmarked backfill contaminates every downstream ratio — the
auto-pass rate, the concentration, the reversal rate — and the contamination is undetectable
later because the rows look exactly like real ones.

And the hard limit: **never backfill a decision.** A gate approval that nobody made must not
appear in the log as if someone made it. If the historical decision is unknown, the honest
row says so (`actor_kind='system'`, `reason_code='unknown_pre_migration'`) or there is no
row at all. Losing a data point is recoverable; a fabricated audit trail is not.

---

## 5. Adding a gate

The question nobody asks in the design meeting: **what happens to items already past that
point?**

- **Grandfather them:** record an explicit `auto_pass` with `reason_code='pre_existing_item'`.
  They show up in the auto-pass rate — correctly, because no human decided.
- **Send them back:** a real transition backwards, which people will notice, so it needs a
  message attached and a queue that can absorb the spike.

The wrong answer is leaving them in a state the new definition says is impossible. Those
items become permanently unroutable, and they are usually discovered by a customer.

Run [`../scripts/flowcheck.py`](../scripts/flowcheck.py) on the *new* definition before
deploying: an added gate is the most common source of `DEAD-END` and `UNREACHABLE-STATE`.

---

## 6. Removing a gate

Keep every historical row. Do not delete the gate's name from the log, do not `NULL` its
column, do not "clean up" its reason codes. Metrics over the past must keep working, and the
reason you removed the gate (usually a 95% auto-pass rate) is a finding worth being able to
re-derive.

Removing a gate also removes a state. See rule 2: the state stays in history, and only new
items stop entering it.

---

## 7. Changing a routing or lookup table

Because the resolved key was stored at transition time (phase 1), history is already safe.
Two additions:

- Give the mapping an **effective-from** date, so "why did this go to team A in March"
  has an answer.
- Items in flight keep the key they were routed with, unless someone explicitly re-routes
  them — which is, again, a transition row with an actor.

---

## 8. Schema changes: expand, migrate, contract

Never one deploy. Five steps, each independently reversible:

1. **Expand** — add the new column/table. Nothing reads it.
2. **Write both** — new writes populate old and new. Old readers unaffected.
3. **Backfill** — in batches, marked as backfill, with the error handling in rule 9.
4. **Read new** — switch readers over. Old column still written, so rollback is one deploy.
5. **Contract** — stop writing, then drop, after a period long enough to notice.

The step people skip is 4-before-5. Dropping the old column in the same deploy that switches
readers means a rollback is a data-loss event.

---

## 9. A migration whose errors are swallowed fails silently

```python
try:
    migrate(batch)
except Exception:
    pass          # <- the entire migration is now a no-op with a success message
```

Prove a migration worked by **running it and reading the result** — row counts before and
after, a sample of migrated rows, and an explicit count of failures. Not by reading the code,
and not by the absence of an error message.

Batch it, make it resumable, and make re-running it safe (idempotent): the second run should
report zero rows changed, and that report is your proof the first run finished.

---

## 10. Measure before, and change one thing at a time

Snapshot the phase-3 numbers **before** the change
([`../scripts/dynamics.py`](../scripts/dynamics.py), one command). A baseline that is not
captured beforehand cannot be captured afterwards, and every claim about the improvement
becomes an opinion.

Then the discipline that makes the comparison mean anything: **do not change the flow and the
metric definition in the same release.** If both move, the difference tells you nothing about
either. Change the flow, measure, and only then refine how you measure.

---

## Checklist for changing a live flow

- [ ] `flow_version` on the item; marker row in the log at cutover.
- [ ] No state, gate, or reason code renamed in place; aliases handled on read.
- [ ] In-flight strategy chosen explicitly (drain / map / dual-run) and written down.
- [ ] Every migration move is a transition row with an actor and a reason code.
- [ ] Backfilled rows marked; no fabricated decisions.
- [ ] Items already past a new gate are grandfathered or sent back — not left unroutable.
- [ ] Removed gates keep their history intact.
- [ ] Schema change split into expand → write both → backfill → read new → contract.
- [ ] Migration proven by running it and reading counts, not by reading the code.
- [ ] `flowcheck.py` clean on the new definition.
- [ ] Phase-3 baseline captured before the change; metric definitions unchanged during it.
