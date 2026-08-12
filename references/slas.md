# SLAs and escalation — promises about time

An SLA is a promise that time-in-station stays under a bound. Most SLA fields are lies of
the display-versus-execution kind ([modeling.md](modeling.md) §8): a `due_at` column that
is shown in red when passed, and enforced by nothing. This file is about making time
promises that execute.

## Contents
- An SLA needs an execution point
- Deadlines are stored, not recomputed
- The escalation ladder
- Escalation must change state, or it is invisible
- Set bounds from measured p90, not aspiration
- Clock traps: calendar time, business hours, holds

## An SLA needs an execution point

For every time bound, name the line that acts on it. The honest options:

1. **A timer fires a transition** — `actor_kind='timeout'`, moving the item (to
   `escalated`, `expired`, or back to a queue). The strongest form: the promise is a state
   machine edge.
2. **A timer fires a notification** — weaker, but real, *if* the notification is logged
   (see below).
3. **Nothing fires** — then the SLA is display-only, and must be labeled as such wherever
   it appears. An unenforced deadline that looks enforced teaches everyone to ignore all
   deadlines, including the real ones.

No timer infrastructure at all (a spreadsheet flow — [human-flows.md](human-flows.md))?
Then the execution point is a *ritual*: a named person reviews the queue-age report on a
named schedule. Write the ritual down next to the SLA; a promise with no reviewer is
option 3 wearing a calendar invite.

## Deadlines are stored, not recomputed

`due_at` is computed **once**, when the item enters the station, from the policy in force
at that moment — then stored on the item. Recomputing it at read time from the current
policy table means every policy change silently rewrites promises already made to people
already waiting (the routing-key rule of modeling.md §4, applied to time). Tightening the
SLA from 48h to 24h must not make yesterday's items retroactively late.

## The escalation ladder

Escalation is a ladder, and each rung is an event row:

| Rung | What happens | Recorded as |
|---|---|---|
| Reminder | The owner is nudged | notification log row |
| Escalation | A *different* actor is brought in | transition (or logged notification to the escalation target) |
| Auto-action | The system moves the item | `timeout` transition |

Three design rules learned the hard way:

- **A reminder to the same person who is already ignoring the queue is rung zero.** Real
  escalation changes *who* is looking, not how often the same person is pinged.
- **Cap the ladder with an auto-action or a named human endpoint.** A ladder that tops out
  at "remind the manager again" means the flow's worst case is infinite politeness.
- **Each rung fires once.** Re-firing reminders on a schedule without logging them makes
  "how many times did we nudge before acting" — the number that tells you the ladder is
  broken — unanswerable.

## Escalation must change state, or it is invisible

If escalation only sends email, the item still sits in the same queue looking the same as
its neighbors, and phase-3 metrics cannot see that anything happened. Two honest designs:

- **Escalation is a transition** (to `escalated`, or an urgency-axis change per
  modeling.md §5) — the queue reorders itself, and time-in-escalation is measurable.
- **Escalation is a logged notification** — the item's state is untouched, but the
  notification log carries `(item, rung, target, at)`, so the ladder's history is queryable.

Pick one deliberately. The common accident — email plus nothing — is how items acquire a
folklore of "I escalated that weeks ago" with no evidence either way.

## Set bounds from measured p90, not aspiration

An SLA someone wished for ("everything within 24h") against a station whose measured p90
is 60h produces a permanent breach backlog, and permanent breach teaches the team that
breach is normal — which destroys the alarm value of every other SLA too.

Sequence: measure first ([dynamics.md](dynamics.md) §3) → set the bound near current p90
→ ratchet it down as the flow actually improves. The SLA's job is to catch *regressions
and abandonment*, not to encode a wish. The queue-age metric (§4) is the SLA's
counterpart for items with no bound at all: the oldest waiting item is the promise nobody
made but everyone assumes.

## Clock traps

- **Calendar vs business hours**: a 24h SLA in calendar time breaches every weekend. If
  the policy is business-hours, the *stored* `due_at` must be computed with the business
  calendar — do not store calendar time and "adjust when reading" (two truths, and the
  display-vs-execution split again). And record which calendar was used; calendars change.
- **Mixed timezones in the log** make every duration wrong before SLAs even start —
  `dynamics.py` preflight refuses exactly this; fix it first.
- **On-hold and the SLA clock**: does `on_hold` ([modeling.md](modeling.md) §10) pause the
  clock? Decide explicitly per hold reason (`awaiting_customer` usually pauses;
  `legal_review` usually does not — the customer cannot see whose fault the wait is).
  Pausing means storing the pause spans; they are already in the log as the `on_hold`
  station's time, so the paused clock is derivable — but only if the policy says so in
  writing.
