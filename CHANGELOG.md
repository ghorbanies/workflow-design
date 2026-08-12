# Changelog

## 1.0.0 — 2026-08-12

First public release.

**Four phases** — model ([references/modeling.md](references/modeling.md)) ·
prove ([references/proving.md](references/proving.md)) ·
measure ([references/dynamics.md](references/dynamics.md)) ·
change ([references/evolving.md](references/evolving.md)) — plus four context guides:
workflows with no code ([references/human-flows.md](references/human-flows.md)),
human gates over AI/agent output ([references/ai-gates.md](references/ai-gates.md)),
SLAs and escalation ([references/slas.md](references/slas.md)), and the
engine-vs-hand-rolled decision ([references/engines.md](references/engines.md)).

**Four stdlib-only tools** (Python 3.10+), each with `--selftest` and a shipped red-proof
spec that mechanically removes each internal guard and requires the self-test to go red:

- `flowcheck.py` — flow-model linter: 18 rules with severities (unreachable states, dead
  ends, gates with no rejection destination, automatic transitions wearing a human
  decision's name), token-precise matching, reasoned allowlist with staleness detection.
- `redproof.py` — guard-coverage prover: removes each guard you name, runs your suite,
  requires red, restores byte-for-byte with hash verification. On its own first run it
  found a hollow assertion in itself — its restore safety net was the one untested thing.
- `dynamics.py` — transition-log metrics: gate auto-pass rate, reversal rate,
  time-in-station percentiles, queue age, rework, never-terminal leak, rejection reasons,
  decision concentration, and variant analysis (top real paths). Refuses any metric the
  log cannot support (missing actor kinds, mixed-timezone timestamps, small denominators)
  instead of printing a confident wrong number. `--demo` generates a sample log.
- `conformance.py` — holds the real log against the declared model, both directions:
  off-model transitions, broken per-item chains, exits from terminal states, auto passes
  recorded as human, undeclared rejection reasons; plus declared-but-never-taken edges
  and gates. Reports per-item trace fitness.

**Shipped evidence:** a worked example flow (`examples/intake.flow.json`), seven
evaluation scenarios including a negative control (`evals/`), and eval results on fresh
agents with no shared context: **35/36** across all seven scenarios (2026-08-11). The one
failing line was a defect in the eval itself — a scenario that ships no code demanded a
code-tailored spec — and was reworded per the eval's own rule: tighten the line, not the
judgment. Notable moments from the runs: one agent independently rediscovered the need
for chain-integrity checking (now shipped as `conformance.py`), another drafted its
answer as a flow JSON, ran `flowcheck.py` unprompted, got caught by
`AUTO-PASS-NAMED-AS-HUMAN`, fixed it, and re-linted to exit 0.
