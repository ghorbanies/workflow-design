# Human flows — workflows with no code at all

Approval chains run through spreadsheets, shared drives, forms, and hallways long before
anyone builds software for them. Every rule in this skill still applies — the only thing
that changes is where the transition log lives.

## Contents
- The log is a sheet
- Append-only without a database
- The one fatal spreadsheet mistake
- Gates on paper
- Metrics without SQL (and the CSV bridge to dynamics.py)
- When to graduate to software

## The log is a sheet

Use the same schema as [modeling.md](modeling.md), as columns:

| item_id | from_state | to_state | gate | decided_by | actor_kind | reason_code | at |
|---|---|---|---|---|---|---|---|
| 1042 | submitted | approved | manager | R. Chen | human | | 2026-03-04 10:12 |
| 1042 | approved | fulfilled | | ops-rota | human | | 2026-03-05 09:30 |

One row per move, ever. `decided_by` is a name or a rota, not "the team".
`actor_kind` still matters: a rule applied mechanically ("under $50 auto-clears") is
`system` even when a person's hand types the row — the question is whether judgment was
exercised, not whether fingers were involved.

Keep **two sheets**: `items` (one row per item, current state only) and `log` (append-only).
This is the two-table rule from modeling.md, and it is *more* important without code,
because nothing else preserves history.

## Append-only without a database

No database means no constraints, so the discipline has to be structural:

- **Feed the log from a form**, not by typing into the sheet. Each form submission is one
  transition row with an automatic timestamp; the sheet itself stays protected/read-only.
  This gets you append-only, a real `at` column, and a closed list for `to_state` and
  `reason_code` (form dropdowns are your enum).
- If forms are not available, protect the log sheet and give edit rights to as few people
  as possible — and accept that the log is now only as trustworthy as they are careful.
- Never sort the log sheet in place. Sort a copy or a filter view; an in-place sort that
  gets interrupted is how histories scramble.

## The one fatal spreadsheet mistake

**Editing the status cell on the item row and calling that the process.** That is a
current-value-only system: every question in [dynamics.md](dynamics.md) — where do items
stall, who decided, how often does the second reviewer reverse the first — becomes
unanswerable, permanently, and nobody notices until they ask.

The tell: a spreadsheet with colors and no dates. If the process matters enough to track,
it matters enough to log moves, not states.

Paper version of the same rule: the routing slip stapled to the file, with one dated,
signed line per station, **is** the transition log. Keep the slips.

## Gates on paper

A gate needs the same four things (entry condition · who may decide · outcomes · where a
rejection lands) — written at the top of the log sheet or on the form, because there is
no code to be the arbiter. Two additions specific to human flows:

- **Rejections need a destination person, not just a state.** "Rejected" in software puts
  the item in a queue; on paper, an item nobody re-owns is gone. Every rejection row names
  who now holds it.
- **The escape hatch rule still applies**: the "other/exception" path must be defined
  (usually "goes to the flow owner"), or exceptions become undocumented side flows —
  which, in human systems, is where most items eventually travel.

## Metrics without SQL

Every metric in [dynamics.md](dynamics.md) is a pivot table over the log sheet:
auto-pass rate = count of `actor_kind ≠ human` per gate; queue age = today minus max `at`
per item not in a terminal state; reversal rate = a filter join on the two gates' rows.

Or skip the pivots — the CSV bridge is two commands:

```bash
sqlite3 flow.db ".mode csv" ".import log.csv transitions"
python scripts/dynamics.py --db flow.db --terminal closed,rejected
```

The preflight will immediately tell you what the sheet's hygiene really is (unparsable
dates, missing actor kinds) — refusals on a spreadsheet-born log are normal the first
time, and each one is a specific instruction back to the form design.

## When to graduate to software

Move when any of these is true — they are the signals that discipline is losing:

- The log needs a constraint people keep violating (states outside the list, missing
  reasons) — forms have run out of enforcement power.
- More than one gate decision per day is being made from the sheet — queue visibility
  (who sees their waiting items) is now the bottleneck.
- Two people need to write the log at the same time.
- Anyone has asked for an SLA ([slas.md](slas.md)) — timers need code.

Graduation is cheap **if the sheet had the right columns**: the log imports directly into
the transitions table, history intact. That is the payoff of running the paper flow on
the real schema from day one — the software system starts with its past already loaded.
