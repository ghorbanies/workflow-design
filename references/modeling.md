# Phase 1 — Modeling a workflow

## Contents
1. The two tables (items + append-only transition log)
2. Timestamps (UTC everywhere; the mixed-table trap)
3. Naming states honestly (automatic ≠ human)
4. Store the routing key; do not recompute it
5. Two axes, not one enum
6. Gates (entry, capability, outcomes, rejection destination)
7. Terminal states and the demand record (no DELETE-as-exit)
8. Display versus execution
9. Ordering and counts belong to the data layer
10. Suspension — "on hold" without forking every state
11. When the state count explodes (axes, substatus, prefix families)
12. Modeling checklist

Goal: a shape that survives the second requirement change and makes phase 3 free.

Running example (deliberately generic): items are **submitted**, checked for **completeness**,
then **approved** by a reviewer, then **fulfilled**, and finally **closed**. Two gates:
completeness and approval. Substitute your own nouns; nothing below depends on the domain.

---

## 1. The two tables

Almost every workflow needs exactly two: the item and its transition log.

```sql
CREATE TABLE items (
  id            INTEGER PRIMARY KEY,
  tenant_id     INTEGER NOT NULL,          -- scope column from day one, even single-tenant
  state         TEXT    NOT NULL,          -- ONE current value
  route_key     TEXT,                      -- resolved at transition time, never recomputed
  urgency       TEXT    NOT NULL,          -- a second axis, not folded into state
  created_at    TEXT    NOT NULL,          -- UTC, always
  updated_at    TEXT    NOT NULL
);

CREATE TABLE transitions (
  id            INTEGER PRIMARY KEY,
  item_id       INTEGER NOT NULL,
  from_state    TEXT,                      -- NULL on creation
  to_state      TEXT    NOT NULL,
  gate          TEXT,                      -- which gate produced this, NULL for plain moves
  decided_by    INTEGER,                   -- actor id; NULL only when actor_kind='system'
  actor_kind    TEXT    NOT NULL,          -- 'human' | 'system' | 'timeout'
  reason_code   TEXT,                      -- enumerated, not free text
  note          TEXT,                      -- free text lives HERE, next to the code
  at            TEXT    NOT NULL           -- UTC
);
CREATE INDEX ix_tr_item ON transitions(item_id, at);
CREATE INDEX ix_tr_gate ON transitions(gate, at);
```

Everything phase 3 computes comes out of `transitions`. If a column there is optional in
practice, a metric dies. `actor_kind` and `at` are never optional.

### Why `state` and not booleans

Two fields (`state` and `is_approved`) means two truths. The screen reads one, the guard
reads the other, and a month later one path updates only one of them. There is no way to tell
which is right; both look correct in isolation. One current value, one place that writes it.

### Why the transition log is append-only

An `UPDATE` on history is a lie with no fingerprint. Corrections are new rows
(`to_state` back to the previous value, `reason_code='correction'`), which is also how you
get the rework metric for free.

---

## 2. Timestamps

Store UTC everywhere and convert at the edges. A mixed table — some rows written in server
local time, some in UTC — cannot be repaired later without knowing which is which, and the
deviation is invisible until a duration is computed across the boundary.

The moment you discover a mixed table, the cheap fix is usually *not* migrating live rows
(migrating production data carries more risk than the skew). Record the deviation, note its
direction, and let it age out. But record it — otherwise someone eventually asks why one
item's clock ran three hours long and there is no answer.

---

## 3. Naming states honestly

| Wrong | Right | Why |
|---|---|---|
| `approved` set by a scheduler | `auto_passed` | Otherwise "what fraction of this gate is human?" is uncomputable |
| `approved` set by timeout | `expired_pass` | A timeout is a policy, not a decision |
| `deleted` for finished work | `closed` / `done` | Terminal states keep the row; the queue is a view |
| `pending` for two different waits | `awaiting_docs`, `awaiting_review` | One label over two waits merges two bottlenecks into one number |

The test: read the state name out loud as a sentence with the actor. "The system approved it"
is a lie you will later find in an audit export.

---

## 4. Store the routing key; do not recompute it

Routing usually looks up something in a table: category → team, brand → line, region → owner.

```
# WRONG — computed at read time
def owner_of(item):  return LOOKUP[item.category]

# RIGHT — resolved once, at the transition
item.route_key = resolve_route(item.category)   # written with the transition row
```

Recomputing means the next edit to `LOOKUP` rewrites the past: every historical item appears
to have been routed to wherever the *current* table says. Reports become unreproducible and
nobody notices, because nothing errors.

Corollary, stated generally: **anything persisted must not be re-derived on read.** If both
exist, they will disagree.

The same reasoning constrains the *input*: if a routing key is derived from free-typed text,
three spellings become three routes and the routing breaks silently. Route on a closed list.
Give the list an explicit escape hatch (`other`) whose behavior is defined — typically
"notify everyone" rather than "notify nobody", so an item never ends up with no owner.

---

## 5. Two axes, not one enum

Two labels that change on different clocks belong in different columns:

- **Ownership / type / category** — set at intake, almost never changes.
- **Urgency / priority** — changes repeatedly during the item's life.

Folding them into one enum (`urgent_customer`, `normal_customer`, `urgent_internal`, …) means
a change to one axis is indistinguishable from a correction to the other, and the enum grows
multiplicatively. Split them, and the state machine gets smaller while expressing more.

Test for this: ask "can these two change independently, at different times, by different
people?" If yes, they are two fields.

---

## 6. Gates

A gate is not a boolean column. Write down four things for each:

1. **Entry condition** — which state(s) can reach this gate.
2. **Who may decide** — a capability, not a person, and enforced server-side.
3. **Allowed outcomes** — pass, reject, and any third option (`needs_info`) named explicitly.
4. **Where a rejection lands** — a real state with a real queue. A rejection with no
   destination is how items vanish from the flow without appearing in any metric.

Additional rules:

- **Rejection needs an enumerated `reason_code`.** Free text alone kills the rejection-reason
  distribution, which is the metric that tells you *why* a station fails. Keep free text too —
  next to the code, not instead of it.
- **Gate decisions are recorded even when the gate auto-passes.** Auto-passes that write no
  row make the gate look busier with humans than it is.
- **Do not hide a control because the actor lacks permission** unless the server also refuses.
  Hiding alone creates two truths about permissions. Server refusal is the truth; the UI may
  mirror it, never replace it.

---

## 7. Terminal states and the demand record

The most common data-modeling mistake in workflow apps: "remove it from my queue" implemented
as `DELETE`.

The queue is a **view** (`WHERE state IN (…)`). The table is the **demand record** — how many
items arrived, how many made it through, where the rest stopped. Deleting rows destroys the
conversion rate, which is usually the number the workflow exists to move.

Every "clear this from my list" action maps to a terminal state, never a delete.

---

## 8. Display versus execution

For every stored value, name the line of code that acts on it. If none exists, the value is
**display-only** and must be marked so in the schema comment and in the UI copy.

A field that is stored, shown, and never enforced is a promise the system does not keep:
a limit that does not limit, an expiry that does not expire, a banner that reports a state no
guard consults. The failure mode is always the same — the interface tells the user something
the engine does not believe.

Related, and worth its own line: **an invisible exception in a guard is a future lying
banner.** If some class of actor bypasses a rule, that exception must be a state the interface
reads from the same source the guard reads. Never "the guard has a special case and the UI
computes its own version."

---

## 9. Ordering and counts

Ordering is a property of the flow, not of the screen: "unreturned items first, oldest first;
then urgent, oldest first; then the rest." Put it in the query. A UI sort silently disagrees
with any other consumer — export, API, second screen.

Counts derive from the same rows the list is built from. A separate `COUNT(*)` with its own
`WHERE` clause is one edit away from disagreeing with the list it is labeling, and both look
right when read on their own.

---

## 10. Suspension — "on hold" without forking every state

Sooner or later an item must leave the flow temporarily: waiting on the customer, paused
for legal, blocked on a part. Two wrong shapes appear immediately:

- **A hold-variant of every state** (`review_on_hold`, `fulfillment_on_hold`, …) — the
  state count doubles, and every new station silently needs a hold twin nobody remembers
  to add.
- **A boolean `is_on_hold` next to `state`** — two competing truths again; queues and
  guards will disagree about whether a held item is "in" the station.

The right shape borrows the *history state* idea from statecharts: **one `on_hold` state,
plus a stored return pointer.**

```sql
ALTER TABLE items ADD COLUMN resume_state TEXT;   -- set on entering on_hold, else NULL
```

Entering hold: a normal transition to `on_hold` with `resume_state = ` the state the item
left (stored, not recomputed — rule 4 applies to this pointer too). Resuming: a transition
back to `resume_state`, which then reverts to NULL. Both moves are ordinary transition
rows with actor and reason, so phase 3 gets hold-time and hold-frequency for free —
`on_hold` shows up as a station, which is exactly what it is.

Rules that keep it honest:

- A hold **reason code is mandatory** (`awaiting_customer`, `legal_review`, …) — "on hold"
  with no reason is where items go to be forgotten. The queue-age metric on `on_hold` is
  the alarm for that.
- If different holds need different permissions or SLAs, that is *still* one `on_hold`
  state with different reason codes — split into separate states only when the **allowed
  outcomes** differ, not when the reasons do.
- Deadline the hold where policy allows: a `timeout` transition out of `on_hold`
  (to `expired` or back to `resume_state`) so a hold cannot become an unbounded waiting room.

## 11. When the state count explodes

A flat state list starts to smell around a dozen states. Before inventing a framework,
check three things in order:

1. **Are two independent axes folded together?** (`urgent_customer_review`,
   `normal_internal_review`, …) — rule 5. Splitting axes shrinks the cross-product back
   to a sum. This is the statechart notion of *parallel states*, done with columns.
2. **Are several states really one station with a substatus?** If a group of states share
   the same allowed outcomes, same permissions, and same queue — and differ only in a
   label — they are one state plus a `substatus` column (or a reason code). The test is
   the same as for hold: **split states only when the allowed outcomes differ.**
3. **Are there real phases?** If the flow has stages that each contain their own
   mini-flow (intake → processing → delivery, each with internal steps), name the phase
   with a **prefix family** (`processing_assigned`, `processing_in_progress`,
   `processing_qa`) and keep transition rules phase-local. That is hierarchy done with
   naming — you get the statechart benefit (rules scoped per phase, phase-level metrics
   by prefix) without a new engine. A phase-level metric is then just
   `GROUP BY substr(state, 1, instr(state, '_') - 1)`.

What does **not** work: nesting flow definitions inside each other. One flat list per
flow, disciplined naming, and columns for the extra axes — every tool in this skill
(and every query you will ever write) stays simple.

## 12. Modeling checklist

- [ ] One current-state column; no competing boolean.
- [ ] Append-only transition log with `to_state`, `actor_kind`, `decided_by`, `at`.
- [ ] Automatic and human transitions have different state names.
- [ ] All timestamps UTC; conversion only at the edges.
- [ ] Routing key stored at transition time, never recomputed at read time.
- [ ] Routing input is a closed list with a defined `other` behavior.
- [ ] Independently-changing labels are separate columns.
- [ ] Every gate: entry condition · capability · outcomes · rejection destination.
- [ ] Rejections carry an enumerated reason code.
- [ ] "Remove from queue" is a terminal state, never a delete.
- [ ] Every stored value has an execution point or is marked display-only.
- [ ] No guard has an exception the interface cannot see.
- [ ] Ordering and counts both come from the data layer, from the same rows.
- [ ] Scope/tenant column present from the first migration, even if unused today.
