#!/usr/bin/env python3
"""
flowcheck.py - lint a workflow model before anyone writes code for it.

Phase 1 of the workflow-design skill is a list of rules people agree with and then skip.
This turns the checkable ones into an exit code: states nobody can reach, states nobody
can leave, gates with no rejection destination, rejections with no enumerated reason, and
automatic transitions wearing a human decision's name.

Usage
-----
    python flowcheck.py my-flow.json
    python flowcheck.py my-flow.json --strict   # warnings count as failures too
    python flowcheck.py --selftest              # prove the checker is not blind
    python flowcheck.py --codes                 # every code, its severity, and why

Exit: 0 clean (or warnings only) - 1 errors found - 2 the spec itself is unusable.

Spec format
-----------
    {
      "name":    "intake pipeline",
      "initial": "submitted",
      "states": {
        "submitted":       {},
        "needs_info":      {},
        "auto_cleared":    {"decided_by": "system"},
        "closed":          {"terminal": true}
      },
      "gates": {
        "completeness": {
          "at":           "submitted",
          "capability":   "review_items",
          "auto":         true,
          "reason_codes": ["missing_field", "illegible"],
          "outcomes": {"pass": "reviewed", "auto_pass": "auto_cleared",
                       "reject": "needs_info"}
        }
      },
      "transitions": [
        {"from": "needs_info", "to": "submitted", "actor": "human",
         "capability": "submit_item"}
      ],
      "allow": [
        {"code": "DELETE-AS-EXIT", "where": "purge_requested",
         "reason": "regulatory erasure request, not a queue exit"}
      ]
    }

Precision rules, so the checker can be trusted rather than argued with:
  * names are matched on whole tokens ("preapproved_draft" does not trigger "approved").
  * a state may declare "decided_by": "system" to say its name is deliberate.
  * anything else is suppressed only through "allow", which needs a written reason -
    and an allow entry that suppresses nothing is itself reported (STALE-ALLOW), so the
    list cannot rot into a permanent excuse.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ERROR, WARN = "error", "warn"

# ── rules: code -> (severity, one-line meaning, why it matters) ─────────────────
RULES: dict[str, tuple[str, str, str]] = {
    "UNKNOWN-STATE-REF": (ERROR,
        "a gate or transition points at a state that does not exist",
        "the flow has a hole nobody sees until an item lands in it"),
    "NO-INITIAL": (ERROR,
        "'initial' is missing or is not a declared state",
        "without an entry point, reachability is undefined"),
    "NO-TERMINAL": (ERROR,
        "no state is marked terminal",
        "items can enter and never finish, and the leak metric has nothing to compare against"),
    "UNREACHABLE-STATE": (ERROR,
        "state cannot be reached from 'initial'",
        "either dead model, or a transition someone assumed existed"),
    "DEAD-END": (ERROR,
        "state has no path to any terminal state",
        "items reaching it are stuck forever and appear in no queue"),
    "TERMINAL-HAS-EXIT": (ERROR,
        "state is marked terminal but has outgoing transitions",
        "then it is not terminal, and every completion count built on it is wrong"),
    "GATE-AT-TERMINAL": (ERROR,
        "gate sits on a state marked terminal",
        "nothing can ever reach the gate; usually the terminal flag is on the wrong state"),
    "GATE-MISSING-CAPABILITY": (ERROR,
        "gate does not say who may decide",
        "a gate without a capability is enforced by whoever remembers to check"),
    "GATE-MISSING-REJECTION": (ERROR,
        "gate has no rejection outcome",
        "a gate that can only pass is not a gate, and rejected items need a destination"),
    "GATE-MISSING-REASON-CODES": (ERROR,
        "gate can reject but has no enumerated reason codes",
        "free-text reasons make the rejection-reason distribution uncomputable"),
    "AUTO-PASS-NAMED-AS-HUMAN": (ERROR,
        "gate can pass automatically but has no distinct auto outcome",
        "sharing a name with the human outcome makes the auto-pass rate uncomputable forever"),
    "HUMAN-NAME-FOR-SYSTEM-TRANSITION": (ERROR,
        "a non-human transition lands in a state whose name claims a human decision",
        "every audit that reads the log will state something that did not happen"),
    "TRANSITION-MISSING-ACTOR": (ERROR,
        "transition does not declare actor: human | system | timeout",
        "without actor kind, not one dynamics metric can be computed"),
    "TRANSITION-MISSING-CAPABILITY": (ERROR,
        "human transition does not say which capability may perform it",
        "authorization that is not written down is not enforced server-side"),
    "STALE-ALLOW": (ERROR,
        "an 'allow' entry suppresses nothing",
        "an allowlist that is never checked becomes a permanent excuse"),
    "ALLOW-MISSING-REASON": (ERROR,
        "an 'allow' entry has no written reason",
        "a suppression nobody can justify later is indistinguishable from a bug"),
    "DELETE-AS-EXIT": (WARN,
        "a state name implies deletion rather than a terminal state",
        "deleting rows destroys the demand record the flow exists to improve"),
    "REJECTION-TO-SELF": (WARN,
        "a rejection outcome lands back on the state the gate sits on",
        "the item never moves, so rework and queue-age metrics cannot see the rejection"),
    "NO-GATES": (WARN,
        "the flow has states but no gate at all",
        "if nothing is ever decided, this is a pipeline, and the gate metrics do not apply"),
}

HUMAN_WORDS = {"approved", "verified", "confirmed", "signed", "accepted",
               "reviewed", "authorized", "cleared", "validated"}
DELETE_WORDS = {"deleted", "purged", "removed", "erased", "dropped", "wiped"}
ACTORS = ("human", "system", "timeout")
_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(name: str) -> set[str]:
    """Whole-token match: 'preapproved_draft' must not trip the 'approved' rule."""
    return set(_TOKEN.findall(str(name).lower()))


class Finding:
    def __init__(self, code: str, where: str, detail: str = "") -> None:
        self.code, self.where, self.detail = code, where, detail

    @property
    def severity(self) -> str:
        return RULES[self.code][0]

    def line(self) -> str:
        meaning = RULES[self.code][1]
        tail = f" - {self.detail}" if self.detail else ""
        mark = "E" if self.severity == ERROR else "w"
        return f"{mark}  {self.code:34} {self.where:26} {meaning}{tail}"


def die(msg: str) -> "None":
    sys.stderr.write(f"[flowcheck] {msg}\n")
    sys.exit(2)


# ── graph helpers ──────────────────────────────────────────────────────────────

def edges(spec: dict) -> list[tuple[str, str, dict]]:
    """Every transition, from the gates' outcomes and from the explicit list."""
    out: list[tuple[str, str, dict]] = []
    for gname, g in (spec.get("gates") or {}).items():
        g = g or {}
        for oname, dest in (g.get("outcomes") or {}).items():
            actor = "system" if oname.startswith("auto") else "human"
            out.append((g.get("at"), dest,
                        {"gate": gname, "outcome": oname, "actor": actor,
                         "capability": g.get("capability")}))
    for t in (spec.get("transitions") or []):
        out.append((t.get("from"), t.get("to"), dict(t)))
    return out


def reachable(start: str, adj: dict[str, set[str]]) -> set[str]:
    seen, stack = {start}, [start]
    while stack:
        for nxt in adj.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


# ── the checks ────────────────────────────────────────────────────────────────

def raw_check(spec: dict) -> list[Finding]:
    found: list[Finding] = []
    states: dict = spec.get("states") or {}
    if not isinstance(states, dict) or not states:
        die("spec needs a non-empty 'states' object")
    gates: dict = spec.get("gates") or {}
    terminals = {n for n, s in states.items() if (s or {}).get("terminal")}

    for name in states:
        if tokens(name) & DELETE_WORDS:
            found.append(Finding("DELETE-AS-EXIT", name))

    initial = spec.get("initial")
    if initial not in states:
        found.append(Finding("NO-INITIAL", str(initial)))
    if not terminals:
        found.append(Finding("NO-TERMINAL", "-"))
    if not gates:
        found.append(Finding("NO-GATES", "-"))

    for gname, g in gates.items():
        g = g or {}
        where = f"gate:{gname}"
        at = g.get("at")
        if at not in states:
            found.append(Finding("UNKNOWN-STATE-REF", where, f"at={at!r}"))
        elif at in terminals:
            found.append(Finding("GATE-AT-TERMINAL", where, f"at={at}"))
        if not g.get("capability"):
            found.append(Finding("GATE-MISSING-CAPABILITY", where))

        outcomes = g.get("outcomes") or {}
        for oname, dest in outcomes.items():
            if dest not in states:
                found.append(Finding("UNKNOWN-STATE-REF", where, f"{oname} -> {dest!r}"))
        rejects = {o: d for o, d in outcomes.items()
                   if o.startswith("reject") or o in ("fail", "deny", "refuse")}
        if not rejects:
            found.append(Finding("GATE-MISSING-REJECTION", where))
        else:
            if not (g.get("reason_codes") or []):
                found.append(Finding("GATE-MISSING-REASON-CODES", where))
            for oname, dest in rejects.items():
                if dest == at:
                    found.append(Finding("REJECTION-TO-SELF", where, f"{oname} -> {dest}"))
        if g.get("auto"):
            autos = {o: d for o, d in outcomes.items() if o.startswith("auto")}
            human_pass = {d for o, d in outcomes.items() if o.startswith("pass")}
            if not autos or (set(autos.values()) & human_pass):
                found.append(Finding("AUTO-PASS-NAMED-AS-HUMAN", where))

    for i, t in enumerate(spec.get("transitions") or [], start=1):
        where = f"transition#{i}"
        for end in ("from", "to"):
            if t.get(end) not in states:
                found.append(Finding("UNKNOWN-STATE-REF", where, f"{end}={t.get(end)!r}"))
        actor = t.get("actor")
        if actor not in ACTORS:
            found.append(Finding("TRANSITION-MISSING-ACTOR", where, f"actor={actor!r}"))
        elif actor == "human" and not t.get("capability"):
            found.append(Finding("TRANSITION-MISSING-CAPABILITY", where))

    # A non-human move may not land in a state whose name claims a human decision -
    # unless the state itself declares that its name is deliberate.
    for src, dst, meta in edges(spec):
        if meta.get("actor") not in ("system", "timeout"):
            continue
        if not isinstance(dst, str) or dst not in states:
            continue
        if (states.get(dst) or {}).get("decided_by") == "system":
            continue
        if tokens(dst) & HUMAN_WORDS:
            found.append(Finding("HUMAN-NAME-FOR-SYSTEM-TRANSITION", f"{src} -> {dst}",
                                 f"actor={meta.get('actor')}"))

    adj: dict[str, set[str]] = {}
    radj: dict[str, set[str]] = {}
    for src, dst, _ in edges(spec):
        if src in states and dst in states:
            adj.setdefault(src, set()).add(dst)
            radj.setdefault(dst, set()).add(src)

    if initial in states:
        seen = reachable(initial, adj)
        for name in states:
            if name not in seen:
                found.append(Finding("UNREACHABLE-STATE", name))

    if terminals:
        co: set[str] = set()
        for term in terminals:
            co |= reachable(term, radj)
        for name in states:
            if name not in co:
                found.append(Finding("DEAD-END", name))

    for name in terminals:
        if adj.get(name):
            found.append(Finding("TERMINAL-HAS-EXIT", name, "-> " + ",".join(sorted(adj[name]))))

    return found


def apply_allowlist(spec: dict, found: list[Finding]) -> list[Finding]:
    """Suppress reasoned exceptions - and report entries that suppress nothing."""
    allows = spec.get("allow") or []
    if not isinstance(allows, list):
        die("'allow' must be a list of {code, where, reason} objects")

    kept, used = [], set()
    for f in found:
        hit = None
        for i, a in enumerate(allows):
            if a.get("code") == f.code and str(a.get("where", "")) in (f.where, ""):
                hit = i
                break
        if hit is None:
            kept.append(f)
        else:
            used.add(hit)

    for i, a in enumerate(allows):
        where = f"allow[{i}]:{a.get('code', '?')}"
        if not str(a.get("reason", "")).strip():
            kept.append(Finding("ALLOW-MISSING-REASON", where))
        if i not in used:
            kept.append(Finding("STALE-ALLOW", where,
                                f"{a.get('code')} on {a.get('where')!r} no longer fires"))
    return kept


def check(spec: dict) -> list[Finding]:
    return apply_allowlist(spec, raw_check(spec))


# ── reporting ─────────────────────────────────────────────────────────────────

def report(spec_path: Path, quiet: bool = False, strict: bool = False
           ) -> tuple[int, list[Finding]]:
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read spec: {exc}")
    findings = check(spec)
    errors = [f for f in findings if f.severity == ERROR]
    warns = [f for f in findings if f.severity == WARN]

    if not quiet:
        print(f"flow: {spec.get('name') or spec_path.name}   "
              f"states={len(spec.get('states') or {})} "
              f"gates={len(spec.get('gates') or {})}")
        for f in sorted(findings, key=lambda x: (x.severity != ERROR, x.code, x.where)):
            print("  " + f.line())
        if not findings:
            print("  clean - every structural rule in modeling.md holds")
        else:
            print(f"\n{len(errors)} error(s), {len(warns)} warning(s). "
                  f"--codes explains why each one matters.")
    return (1 if (errors or (strict and warns)) else 0), findings


# ── self-test ─────────────────────────────────────────────────────────────────

BROKEN = {
    "name": "deliberately broken",
    "initial": "start",
    "states": {
        "start": {},
        "auto_lane": {},
        "approved": {},          # reached by a system transition - naming lie
        "preapproved_draft": {},  # must NOT trip the naming rule (token match)
        "orphan": {},            # unreachable
        "limbo": {},             # reachable, no path to terminal
        "deleted_items": {},     # delete-as-exit (warning)
        "closed": {"terminal": True},
    },
    "gates": {
        "review": {                                  # no capability, no rejection
            "at": "start",
            "auto": True,                            # auto, but no distinct outcome
            "outcomes": {"pass": "auto_lane"},
        },
        "second": {                                  # rejects, but no reason codes,
            "at": "auto_lane",                       # and the rejection goes nowhere
            "capability": "decide",
            "outcomes": {"pass": "closed", "reject": "auto_lane"},
        },
        "ghost": {                                   # points at a state that is not there
            "at": "nowhere",
            "capability": "decide",
            "reason_codes": ["x"],
            "outcomes": {"pass": "closed", "reject": "limbo"},
        },
        "afterlife": {                               # gate on a terminal state
            "at": "closed",
            "capability": "decide",
            "reason_codes": ["x"],
            "outcomes": {"pass": "closed", "reject": "limbo"},
        },
    },
    "transitions": [
        {"from": "start", "to": "approved", "actor": "system"},       # naming lie
        {"from": "start", "to": "preapproved_draft", "actor": "system"},   # legitimate
        {"from": "approved", "to": "closed"},                         # no actor
        {"from": "preapproved_draft", "to": "closed", "actor": "system"},
        {"from": "start", "to": "deleted_items", "actor": "human"},   # no capability
        {"from": "deleted_items", "to": "closed", "actor": "system"},
        {"from": "closed", "to": "start", "actor": "human", "capability": "reopen"},
    ],
}

EXPECTED = {
    "UNREACHABLE-STATE", "DEAD-END", "TERMINAL-HAS-EXIT", "GATE-AT-TERMINAL",
    "GATE-MISSING-CAPABILITY", "GATE-MISSING-REJECTION", "GATE-MISSING-REASON-CODES",
    "AUTO-PASS-NAMED-AS-HUMAN", "HUMAN-NAME-FOR-SYSTEM-TRANSITION",
    "TRANSITION-MISSING-ACTOR", "TRANSITION-MISSING-CAPABILITY", "UNKNOWN-STATE-REF",
    "DELETE-AS-EXIT", "REJECTION-TO-SELF",
}


def selftest() -> int:
    ok = True

    def show(label: str, cond: bool) -> None:
        nonlocal ok
        print(("  [ok] " if cond else "  [XX] ") + label)
        ok = ok and cond

    print("flowcheck self-test\n")
    tmp = Path(tempfile.mkdtemp(prefix="flowcheck-selftest-"))
    try:
        broken = tmp / "broken.json"
        broken.write_text(json.dumps(BROKEN), encoding="utf-8")
        rc, findings = report(broken, quiet=True)
        codes = {f.code for f in findings}

        # Assert WHICH code fired, not just that something did: a checker that emitted
        # one violation for everything would sail through a count-based assertion.
        for code in sorted(EXPECTED):
            show(f"reports {code}", code in codes)
        show("exits non-zero on a broken flow", rc == 1)
        show("every reported code is a documented rule", codes <= set(RULES))

        # precision, not just recall: the token matcher must leave this one alone
        show("'preapproved_draft' does not trip the human-name rule",
             not any(f.code == "HUMAN-NAME-FOR-SYSTEM-TRANSITION"
                     and "preapproved_draft" in f.where for f in findings))

        # an explicit "decided_by": "system" is a legitimate silencer
        spec = json.loads(json.dumps(BROKEN))
        spec["states"]["approved"]["decided_by"] = "system"
        probe = tmp / "declared.json"
        probe.write_text(json.dumps(spec), encoding="utf-8")
        _, declared = report(probe, quiet=True)
        show("a state declaring decided_by=system silences the naming rule",
             not any(f.code == "HUMAN-NAME-FOR-SYSTEM-TRANSITION" for f in declared))

        # the allowlist: suppresses with a reason, and reports itself when it goes stale
        spec = json.loads(json.dumps(BROKEN))
        spec["allow"] = [
            {"code": "DELETE-AS-EXIT", "where": "deleted_items",
             "reason": "regulatory erasure, not a queue exit"},
            {"code": "DEAD-END", "where": "state_that_does_not_exist",
             "reason": "left over from an older model"},
            {"code": "REJECTION-TO-SELF", "where": "gate:second"},   # no reason
        ]
        probe2 = tmp / "allowed.json"
        probe2.write_text(json.dumps(spec), encoding="utf-8")
        _, allowed = report(probe2, quiet=True)
        acodes = {(f.code, f.where) for f in allowed}
        show("a reasoned allow entry suppresses its finding",
             not any(c == "DELETE-AS-EXIT" for c, _ in acodes))
        show("an allow entry that suppresses nothing is reported as STALE-ALLOW",
             any(c == "STALE-ALLOW" for c, _ in acodes))
        show("an allow entry with no reason is reported",
             any(c == "ALLOW-MISSING-REASON" for c, _ in acodes))

        # a gateless flow is a pipeline, and must be told so exactly once
        pipeline = {"name": "no gates", "initial": "a",
                    "states": {"a": {}, "b": {"terminal": True}},
                    "transitions": [{"from": "a", "to": "b", "actor": "system"}]}
        probe0 = tmp / "pipeline.json"
        probe0.write_text(json.dumps(pipeline), encoding="utf-8")
        rc_pipe, pipe_findings = report(probe0, quiet=True)
        show("a flow with no gates is reported as NO-GATES",
             [f.code for f in pipe_findings] == ["NO-GATES"])
        show("a warning alone does not fail the run", rc_pipe == 0)
        rc_strict, _ = report(probe0, quiet=True, strict=True)
        show("--strict makes that same warning fail", rc_strict == 1)

        # the inverse: a clean flow must produce nothing at all
        example = Path(__file__).resolve().parent.parent / "examples" / "intake.flow.json"
        if example.is_file():
            rc_ok, clean = report(example, quiet=True)
            show("the shipped example flow is clean", rc_ok == 0 and not clean)

            # canary: remove one field from the clean example and it must stop being clean
            spec = json.loads(example.read_text(encoding="utf-8"))
            first_gate = sorted(spec["gates"])[0]
            spec["gates"][first_gate].pop("capability", None)
            probe3 = tmp / "probe.json"
            probe3.write_text(json.dumps(spec), encoding="utf-8")
            _, dirty = report(probe3, quiet=True)
            show("removing one field from the clean example makes it dirty",
                 any(f.code == "GATE-MISSING-CAPABILITY" for f in dirty))
        else:
            show(f"shipped example exists at {example}", False)
    finally:
        for leftover in tmp.glob("*"):
            leftover.unlink()
        os.rmdir(tmp)

    print("\n" + ("self-test passed" if ok else "SELF-TEST FAILED"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Lint a workflow model.")
    ap.add_argument("spec", nargs="?", help="path to the flow definition JSON")
    ap.add_argument("--strict", action="store_true", help="warnings fail the run too")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--codes", action="store_true", help="list codes and why they matter")
    args = ap.parse_args()

    if args.codes:
        for code, (sev, meaning, why) in RULES.items():
            print(f"{code}  [{sev}]\n    {meaning}\n    why: {why}\n")
        return 0
    if args.selftest:
        return selftest()
    if not args.spec:
        ap.print_help()
        return 2
    rc, _ = report(Path(args.spec).resolve(), strict=args.strict)
    return rc


if __name__ == "__main__":
    sys.exit(main())
