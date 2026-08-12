# AI gates — human review over agent output

The fastest-growing workflow shape: an AI agent drafts, classifies, or proposes an action,
and a human gate decides whether it executes. Everything in this skill applies directly —
and the metrics get sharper teeth, because the central question of an AI-reviewed flow is
exactly the one the transition log answers: **is the human review real, or theater?**

## Contents
- Modeling: the agent is an actor, never a decider
- The trust boundary
- Rubber-stamp detection
- Sampling honestly
- Rejection reasons are your improvement loop
- A prompt change is a flow change

## Modeling: the agent is an actor, never a decider

Extend `actor_kind` with `agent` (alongside `human`, `system`, `timeout`). Then two rules:

- **Agent transitions are production steps**: `draft_created`, `classified_urgent`,
  `action_proposed` — recorded with `actor_kind='agent'` plus the model/prompt version
  (see below). An agent-written state never uses a human-decision name: it is
  `agent_classified`, not `verified` — the naming rule from
  [modeling.md](modeling.md) §3, and in AI flows it is the whole audit story.
- **Gates stay human.** A gate outcome not named `auto*` must be recorded by a human.
  `conformance.py` enforces this as-is: an `agent` actor on a human outcome is an
  `ACTOR-KIND-MISMATCH` — which is precisely the incident you want to catch, an agent
  quietly approving its own work.

If policy allows the agent to skip review for some class (high confidence, low stakes),
that is an **`auto_pass` outcome with its own reason code** (`confidence_above_threshold`),
never the human outcome's name. The threshold itself is a routing decision: store the
confidence value and the threshold *on the transition row* at decision time (modeling
rule 4) — otherwise next month's threshold change silently rewrites which historical items
"would have" auto-passed.

## The trust boundary

**The agent never writes the log's human fields.** Not `decided_by`, not a human outcome,
not a backdated `at`. If the agent's tooling can write arbitrary rows, the log stops being
evidence — for exactly the reason a fabricated audit trail is worse than a missing one
([evolving.md](evolving.md) §4). Practically: the agent gets an API that can only append
`actor_kind='agent'` rows; human rows come only from the human-facing surface.

## Rubber-stamp detection

The auto-pass rate from [dynamics.md](dynamics.md) reads differently here: the gate is
formally human, so what rubber-stamping produces is not `system` rows — it is human rows
with **no time in them**. Two measurements, both free from the log:

- **Decision latency distribution per reviewer**: time between the item entering the gate's
  station and the human decision row. A reviewer "reviewing" agent output at a median of
  four seconds is not reviewing; the p10 is the tell (fast tail = batch-clicking).
- **Reversal asymmetry**: if the human gate approves 99.5% of agent output, either the
  agent is genuinely that good — or the gate is theater. The tiebreaker is downstream:
  sample approved items and re-review blind, or watch the loop-back/complaint rate after
  execution. A 99.5% approval rate with a nonzero downstream defect rate is the signature
  of rubber-stamping.

Both readings exist, as always: low approval with high latency can mean a bad agent — or a
gate asked to review things it lacks context to judge. Pair the ratio with the reason
distribution before concluding.

## Sampling honestly

At volume, 100% human review dies quietly: the queue grows, someone "catches up", and the
log records a heroic afternoon of 900 approvals. Model the real policy instead:

- Decide the sampling rule explicitly (all high-stakes + N% of the rest).
- Sampled-out items take **`auto_pass` with reason `sampled_out`** — they are honestly
  marked as unreviewed, countable, and auditable.
- The sampling rate is then a *stored, versioned policy*, and "what fraction of executed
  agent actions did a human actually see" is a one-line query instead of a guess.

The alternative — pretending 100% review while actually skimming — makes every number
about the gate a lie, and it is the default outcome if sampling is not designed.

## Rejection reasons are your improvement loop

Enumerated rejection reasons at an AI gate are not bureaucracy; they are the agent's
backlog, for free. `hallucinated_fact`, `wrong_tone`, `missed_constraint`,
`unsafe_action` — the reason distribution *is* the prompt-engineering priority list, and
its trend after each agent change is the only honest measure of whether the change
helped. Free-text-only rejections throw this away (modeling.md §6).

## A prompt change is a flow change

A new prompt, model, or tool list changes what flows through every downstream gate — it is
a **definition change with items in flight**, and all of [evolving.md](evolving.md)
applies:

- Stamp `agent_version` (prompt hash, model id, or release tag) on every agent row.
  Metrics that cross a version boundary without splitting on it average two different
  agents into one meaningless number.
- **Capture the gate metrics before the change** — approval rate, reason distribution,
  decision latency. This is the phase-3 baseline rule, and with AI changes it is acutely
  time-boxed: the old agent stops existing at deploy.
- Roll out dual-run when stakes allow: route a slice to the new version, compare gate
  metrics per version. The human gate is your eval harness — it is already scoring every
  output; version-splitting the log is what turns those scores into an A/B result.
