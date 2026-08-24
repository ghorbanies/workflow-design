# Changelog

## 1.1.0 — 2026-09-01

Both halves of a proof. `redproof.py` used to ask one question — *can this guard fire?* —
and two documented production failures showed that is half an answer.

- **The green side.** A case can now declare a `green` block: the guard, in place, run
  against a **known-good artifact**, required to pass and to print the evidence that it
  actually ran. Reported as `CLEAN`, or `ALWAYS-RED` when the guard rejects a healthy
  artifact. *Why:* a release guard reported all 37 brand assets missing from a **perfect**
  bundle — the names crossed an encoding boundary and arrived as `????`. Its red proof was
  spotless. Wired into the build with `|| exit 1`, merging it would have meant no bundle
  could ever be built again. Red alone cannot separate a working guard from one that
  refuses everything, and the always-red one hides longer because refusing looks careful.
  Spec-level `"require_green": true` makes a missing green side a finding rather than a
  habit.
- **Two-sided agreements.** A case can declare an `agreement` whose `left` and `right`
  must each be read from the built artifact (`"from": "artifact"` plus a command and an
  optional `extract` regex). A side declared assumed, a command that reads nothing, or a
  pattern that matches nothing is rejected as `ONE-SIDED`; disagreeing artifacts are
  `MISMATCH`. *Why:* an app's certificate fingerprint was matched perfectly against the
  association file published on its host — while the app's own declaration was missing the
  required field entirely. One half of the handshake was flawless; the other was not
  speaking. The half you assume is the half that breaks.
- Both features are documented as first-class rules in `SKILL.md` and
  `references/proving.md` (with the proving checklist extended), and each has its own eval
  scenario (`eval-8-always-red-guard`, `eval-9-two-sided-agreement`).
- `redproof.py`'s self-test grew from 8 to 19 assertions. Red-proofing the new code found
  **three hollow assertions in it** — an `expect_output` check whose fixture always printed
  the expected string, an empty-value branch nothing reached, and an exit-code aggregation
  no assertion read. All three are closed and now go red on removal (13/13).
- Documented a real limitation found the same day: a **hard kill** (SIGKILL, closed
  terminal, runner timeout) cannot be trapped and leaves a neutered file on disk. It
  happened once here; nothing was lost because the uncommitted-changes warning and the
  baseline gate both fired on the next run. Recovery is one `git checkout` — which is why
  "commit first" is a rule.

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
