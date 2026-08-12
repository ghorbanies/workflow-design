# Phase 3 — Workflow dynamics

## Contents
- The starting three
- 1. Gate auto-pass rate · 2. Second-approver reversal rate · 3. Time in station (p50/p90)
- 4. Queue age, not queue length · 5. Loop-back (rework) rate · 6. Never-terminal (the leak)
- 6b. Variants — the paths items actually take
- 7. Rejection reason distribution · 8. Decision concentration
- 9. Funnel conversion by station · 10. Conformance against the model
- How to read any workflow ratio (every ratio has two failure directions)
- The rule that keeps this from becoming a dashboard

Numbers that tell you where a flow is stuck, computed from the transition log, with nobody
filing a report. This is the phase almost everyone skips, and it is the cheapest one — if
phase 1 was done, the data is already there.

All queries below run against the two tables in [modeling.md](modeling.md):
`items(id, tenant_id, state, route_key, urgency, created_at, updated_at)` and
`transitions(item_id, from_state, to_state, gate, decided_by, actor_kind, reason_code, at)`.
They are plain SQL; adapt names, not logic.

---

## The starting three

If you build nothing else, build these. They answer "is this flow real, and where does it
hurt" for almost any workflow:

1. **Gate auto-pass rate** — is this gate a decision or a formality?
2. **p90 time-in-station** — which station is the bottleneck?
3. **Never-terminal rate** — how much of what enters silently leaks out?

---

## 1. Gate auto-pass rate

*What fraction of decisions at this gate were made by a human?*

```sql
SELECT gate,
       COUNT(*)                                                        AS decisions,
       SUM(CASE WHEN actor_kind <> 'human' THEN 1 ELSE 0 END)          AS automatic,
       ROUND(100.0 * SUM(CASE WHEN actor_kind <> 'human' THEN 1 ELSE 0 END)
             / COUNT(*), 1)                                            AS auto_pct
FROM transitions
WHERE gate IS NOT NULL
GROUP BY gate
ORDER BY auto_pct DESC;
```

**Reading.** High auto-pass means the gate is theater: it costs latency and gives no
assurance. Either delete it, or find out why humans stopped using it (usually the queue is
invisible, or the decision has no consequence).

**Trap.** This is uncomputable if automatic transitions were named after human decisions —
the phase-1 rule. If `auto_pct` comes back 0 for a gate you know auto-passes, you do not have
a great gate; you have a naming bug.

---

## 2. Second-approver reversal rate

*Of the items the first station passed, how many did the second station reject?*

```sql
WITH passed_first AS (
  SELECT item_id, MIN(at) AS at1
  FROM transitions
  WHERE gate = 'gate_1' AND to_state = 'passed_1'
  GROUP BY item_id
),
second AS (
  SELECT t.item_id, t.to_state
  FROM transitions t
  JOIN passed_first p ON p.item_id = t.item_id AND t.at > p.at1
  WHERE t.gate = 'gate_2'
)
SELECT COUNT(*)                                                     AS reviewed,
       SUM(CASE WHEN to_state = 'rejected' THEN 1 ELSE 0 END)       AS reversed,
       ROUND(100.0 * SUM(CASE WHEN to_state = 'rejected' THEN 1 ELSE 0 END)
             / COUNT(*), 1)                                         AS reversal_pct
FROM second;
```

**Reading.** High reversal means the **first** station's instructions do not work — people are
passing things that should not pass. The fix is upstream (clearer criteria, a checklist, a
required field), not "tell the second reviewer to be faster."

**Trap — both directions.** A *low* reversal rate has two readings: the first station is
excellent, or the second gate is rubber-stamping. Tiebreaker: cross-check with §1 (is gate 2
mostly automatic?) and with the reason-code distribution (does gate 2 ever produce a rejection
reason at all?).

---

## 3. Time in station (p50 / p90)

*How long do items wait at each station?*

```sql
WITH spans AS (
  SELECT t.item_id,
         t.to_state AS station,
         t.at       AS entered,
         (SELECT MIN(n.at) FROM transitions n
           WHERE n.item_id = t.item_id AND n.at > t.at) AS left_at
  FROM transitions t
)
SELECT station,
       COUNT(*) AS n,
       AVG( (julianday(COALESCE(left_at, CURRENT_TIMESTAMP)) - julianday(entered)) * 24 )
         AS avg_hours
FROM spans
GROUP BY station
ORDER BY avg_hours DESC;
```

`julianday` is SQLite; use `EXTRACT(EPOCH FROM …)/3600` on Postgres,
`TIMESTAMPDIFF(HOUR, …)` on MySQL.

**Use percentiles, not the average.** Workflow waits are long-tailed; the mean hides the tail
that people actually complain about. Where `PERCENTILE_CONT` exists:

```sql
SELECT station,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY hours) AS p50,
       PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY hours) AS p90
FROM span_hours GROUP BY station;
```

Where it does not, take the ordered row at offset `0.9 * n`:

```sql
SELECT hours FROM span_hours WHERE station = ?
ORDER BY hours LIMIT 1 OFFSET (SELECT CAST(COUNT(*) * 0.9 AS INT) FROM span_hours WHERE station = ?);
```

**Reading.** The p90 station is the bottleneck. A station with a good p50 and a terrible p90 is
not slow — it is *unreliable*, which is a different fix (missing owner, no escalation path)
than "add capacity".

**Trap.** Items still sitting in a station have no `left_at`. Excluding them (`WHERE left_at IS
NOT NULL`) is survivor bias in its purest form: the worst-stuck items are exactly the ones
excluded, and the bottleneck disappears from the report. Count them as open with duration to
`now`, as above.

---

## 4. Queue age, not queue length

*How long has the oldest waiting item been waiting?*

```sql
SELECT state,
       COUNT(*)                                                       AS waiting,
       MIN(updated_at)                                                AS oldest_entered
FROM items
WHERE state IN ('awaiting_docs','awaiting_review','awaiting_fulfillment')
GROUP BY state;
```

**Reading.** Length tells you how busy the station is; **age** tells you who is being
abandoned. A queue of 3 items where the oldest is six weeks old is a worse failure than a
queue of 40 that clears daily — and the length metric alone ranks them backwards.

---

## 5. Loop-back (rework) rate

*How often does an item go back to a station it already passed?*

```sql
SELECT to_state AS station,
       COUNT(*) AS revisits
FROM transitions t
WHERE EXISTS (
  SELECT 1 FROM transitions p
  WHERE p.item_id = t.item_id AND p.to_state = t.to_state AND p.at < t.at
)
GROUP BY to_state
ORDER BY revisits DESC;
```

**Reading.** Rework is the flow's silent capacity tax: it never appears in throughput but
consumes the same people. A station with high loop-back usually has an upstream input-quality
problem, or the two stations disagree about the standard.

---

## 6. Never-terminal rate (the leak)

*What entered and never reached any end state?*

```sql
SELECT COUNT(*) AS stuck
FROM items
WHERE state NOT IN ('closed','rejected','cancelled')
  AND updated_at < DATE('now', '-30 day');
```

**Reading.** These items are invisible to every operational dashboard — they are not in
anyone's active queue and not in the completed count. In most flows this number is larger than
anyone expects, and every item in it is a person who never got an answer.

---

## 6b. Variants — the paths items actually take

*What routes do items really follow through the flow?*

```sql
-- one signature per item: its ordered sequence of states
SELECT group_concat(to_state, ' > ') AS path, COUNT(*) OVER () AS _
FROM (SELECT item_id, to_state FROM transitions ORDER BY item_id, at)
GROUP BY item_id;
```

(SQLite's `group_concat` does not guarantee order everywhere; `dynamics.py` builds the
signature in code from a timestamp-ordered scan, which is the portable form. On Postgres
use `string_agg(to_state, ' > ' ORDER BY at)`.)

Group the signatures and count. The output is a short head and a long tail:

**Reading.** The top two or three variants *are* the process as it exists — often different
from the process as drawn. The tail is where the interesting failures live: every rare
variant is either a legitimate exception, an ad-hoc workaround someone invented, or a bug.
A tail that keeps growing means the flow's rules are losing to reality.

**Trap.** Do not "clean up" rare variants by deleting their items from the analysis; count
them and read a sample. The workaround people invented is usually a requirement nobody
wrote down.

## 7. Rejection reason distribution

```sql
SELECT gate, reason_code, COUNT(*) AS n
FROM transitions
WHERE to_state = 'rejected'
GROUP BY gate, reason_code
ORDER BY gate, n DESC;
```

**Reading.** The top reason at a gate is a design brief: usually it can be prevented upstream
by a required field, a validation, or a clearer instruction. A gate whose rejections are 80%
one reason is a gate that should be partly automated.

**Trap.** This works only if reasons are enumerated (phase 1). With free text you get a
histogram of typing habits. If reasons are currently free text, the cheapest fix is to
enumerate the top handful discovered by reading a sample, plus `other` — and then watch
`other`: if it stays above ~15%, the enumeration is wrong, not the users.

---

## 8. Decision concentration

```sql
SELECT gate, decided_by, COUNT(*) AS decisions,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY gate), 1) AS pct
FROM transitions
WHERE gate IS NOT NULL AND actor_kind = 'human'
GROUP BY gate, decided_by
ORDER BY gate, decisions DESC;
```

**Reading.** One actor making most decisions at a gate is a single point of failure —
the flow stops when they are away — and often means the capability was granted to several
people but the *queue* is only visible to one.

---

## 9. Funnel conversion by station

```sql
SELECT to_state AS station, COUNT(DISTINCT item_id) AS reached
FROM transitions
GROUP BY to_state
ORDER BY reached DESC;
```

Compare consecutive stations to get the pass-through rate of each step. Read against §6:
a station with a low pass-through is either rejecting a lot (visible) or leaking (invisible),
and those need opposite responses.

---

## 10. Conformance against the model

Everything above reads the log alone. If the flow also has a declared model (the JSON that
`flowcheck.py` lints), hold the two against each other — that is the question none of the
metrics can answer: **does the system do what the model says?**

```bash
python scripts/conformance.py --db app.sqlite --flow my-flow.json
```

Both directions matter:

- **log → model**: off-model transitions, broken per-item chains (`from_state` not equal to
  the previous `to_state`), exits from terminal states, human-only outcomes recorded with a
  system actor, undeclared rejection reasons. Any of these means per-item history cannot be
  trusted — fix that *before* believing any number in this file.
- **model → log**: declared edges and gates never observed. Untested policy or dead model;
  either way nobody knows whether that path works.

The summary number is **trace fitness**: the share of items whose entire history conforms.
Fitness below 100% is not a style problem — the non-conforming items are exactly the ones
your metrics silently misrepresent.

**Run it before trusting a dashboard, and after any phase-4 change.** A model that is never
held against the log is documentation, and documentation rots.

## How to read any workflow ratio

**Every ratio has two failure directions.** Before acting on a number, name both readings and
the tiebreaker:

| Metric | High means | Low means | Tiebreaker |
|---|---|---|---|
| Auto-pass rate | Gate is theater | Gate is a real decision point | Does rejecting ever happen there? |
| Reversal rate | Upstream instructions fail | Upstream is good **or** downstream rubber-stamps | Auto-pass rate of the second gate |
| Time in station | Bottleneck | Healthy **or** items skip it | Funnel count for that station |
| Rework rate | Quality problem upstream | Healthy **or** rework is unrecorded | Do loop-backs write transition rows? |
| Queue length | Busy | Healthy **or** abandoned | Queue *age* |

The common shape: a "good" number is frequently produced by the thing not happening at all.
Always pair a ratio with a volume.

---

## The rule that keeps this from becoming a dashboard

**No metric without an action.** For each number, write one line: *what would I do differently
if this were high, and what if it were low?* If both answers are "nothing", do not build it.
An unactioned tile is maintenance cost with a chart on top, and it dilutes the two or three
numbers that do drive decisions.

Second rule: **measure before you change the flow.** A baseline that is not captured before
a redesign cannot be captured afterwards — the comparison is gone forever, and every claim
about the improvement becomes an opinion. If a change to the flow is coming, snapshot these
numbers first; it costs one query run.
