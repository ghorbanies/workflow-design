# workflow-design

A [Claude skill](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
for designing, hardening, and measuring **workflows** — approval chains, review pipelines,
intake / triage / fulfillment flows, ticketing, order processing, and any state machine
with human gates. It covers the state machine and audit trail design, the testing
discipline that catches hollow test suites, process-mining-style metrics (bottlenecks,
SLA breaches, rubber-stamp approvals), zero-downtime workflow migration, human-in-the-loop
review of AI agent output, and the build-vs-workflow-engine decision.

```bash
npx skills add ghorbanies/workflow-design
```

## Why this skill

Every company has a queue whose end nobody has seen. A request gets submitted, goes for
approval, and then — approved, rejected, or the third thing nobody talks about: **it just
stays there.** Not refused, not granted; forgotten. Six months later a customer calls
asking what happened, and nobody knows, because that item lives on no dashboard — it is
not in anyone's work queue and not in the completed count.

This skill exists for that item, and for everything else that goes wrong in flows where
items move between stations and people decide. Four acts, each earned from a real failure:

- **Before you build** — lint the flow design. A perfectly reasonable-looking expense
  approval draft came back with eight errors, including the quiet one: *the manager only
  had an approve button.* A manager who disagreed had no legal move, so disagreement would
  have happened in email — and the item would vanish from the system. Worse: an automatic
  timeout was named `approved`, so an auditor would read "a human approved this" about
  money nobody ever looked at.
- **When you stop trusting your tests** — the red-proof runner removes each guard from
  your code and demands the suite go red. If nothing fails, that guard has no guardian.
  Best credential: run on itself, it found its own safety net untested — the one thing
  nobody wrote a test for, because it was "obviously correct."
- **When management asks where things get stuck** — one command over the log you already
  have: which station is actually slow, how many items entered and never reached any end,
  and the favorite — *which approval step is theater.* A gate that waves 99% through
  means either your process is that good or someone is just clicking. And a promise: a
  number the data cannot support is **refused, not printed** — a confident wrong number
  is worse than none.
- **When you change a live flow** — you add an approval step and 2,000 orders are
  mid-flight. What happens to the ones already past that point? Without an answer, they
  end up in a state the new definition says is impossible — usually discovered by an
  angry customer.

And the act that belongs to this decade: **a human approving AI output.** "Is the review
actually doing anything?" has a measurable answer, not an opinion — if a reviewer's
median decision time is a few seconds, they are not reviewing; they are clicking.

In one line: **this skill keeps your workflow from lying to you** — at design time, in
the test suite, on the dashboard, and mid-migration.

Most workflow advice stops at drawing the state machine. This skill covers the parts that
actually fail in production:

- **Model** — states, gates, transitions; one current value plus an append-only history;
  never name an automatic state after a human decision.
- **Prove** — do the tests guarding the flow actually catch anything? Harness canaries,
  red proofs, hollow-assertion hunting.
- **Measure** — where does the flow stall, which gate is theater, who is a single point of
  failure — computed from the transition log, with nobody filing a report.
- **Change** — evolving a flow that is full of in-flight items without corrupting history
  or fabricating an audit trail.

## Why trust it

Every rule in this skill came from a workflow that shipped and then failed in a specific,
documented way. The tools apply their own discipline to themselves:

- All four scripts have a `--selftest` that proves the tool is not blind before it judges
  anything else.
- All four are **red-proofed by shipped specs**: each internal guard is mechanically
  removed, the self-test must go red, and the file is restored byte-for-byte
  (`scripts/*.selfproof.spec.json`).
- The red-proof runner found a hollow assertion **in itself** on its first run — the hash
  verification after restore, the tool's only safety net, was the only untested thing in
  it. The metrics tool's first real run exposed a bug that fourteen green assertions had
  missed. Both stories are kept in [references/proving.md](references/proving.md), because
  they are the method working as intended.
- The skill ships with [evaluation scenarios](evals/scenarios.json) — including a negative
  control — and a rule: no release without re-running them on a fresh instance.

## Install

Copy this directory into your skills folder:

```bash
# Claude Code (per-user)
cp -r workflow-design ~/.claude/skills/

# or per-project
cp -r workflow-design your-repo/.claude/skills/
```

No dependencies: the three scripts are single-file, stdlib-only Python (3.10+).

## The tools

| Tool | One line | Try it |
|---|---|---|
| `flowcheck.py` | Lints a flow definition before anyone builds it — unreachable states, dead ends, gates with no rejection destination, automatic transitions wearing a human decision's name | `python scripts/flowcheck.py examples/intake.flow.json` |
| `redproof.py` | Proves each guard in your code is covered: removes it, requires your suite to go **red**, restores byte-for-byte | `python scripts/redproof.py --selftest` |
| `dynamics.py` | Workflow-health metrics plus the top real paths, from a SQLite transition log — and it **refuses** any metric the log cannot support, instead of printing a confident wrong number | `python scripts/dynamics.py --demo /tmp/demo.sqlite` |
| `conformance.py` | Holds the real log against the declared model, both directions: moves the model forbids, and declared edges nobody has ever taken. Reports per-item trace fitness | `python scripts/conformance.py --selftest` |

Together they close a loop no single tool can: `flowcheck` judges the model, `dynamics`
reads reality, and `conformance` makes each accountable to the other.

Verify everything in one line:

```bash
python scripts/flowcheck.py --selftest && python scripts/redproof.py --selftest && python scripts/dynamics.py --selftest && python scripts/conformance.py --selftest
```

## Layout

```text
workflow-design/
├── SKILL.md                      # entry point Claude reads
├── references/
│   ├── modeling.md               # phase 1: tables, states, gates, naming, holds
│   ├── proving.md                # phase 2: canaries, red proofs, hollow tests
│   ├── dynamics.md               # phase 3: the metrics, variants, portable SQL
│   ├── evolving.md               # phase 4: changing a live flow
│   ├── human-flows.md            # flows on spreadsheets, forms, and paper
│   ├── ai-gates.md               # human review over AI/agent output
│   ├── slas.md                   # time promises: deadlines, escalation ladders
│   └── engines.md                # workflow engine vs hand-rolled: decision table
├── scripts/                      # the four tools + their red-proof specs
├── examples/intake.flow.json     # a worked flow that passes flowcheck cleanly
└── evals/                        # evaluation scenarios + fixtures
```

## License

[MIT](LICENSE). Issues and additions welcome — a new rule needs the failure it comes from;
a new metric needs the action its high and low readings each imply.
