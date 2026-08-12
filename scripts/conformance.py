#!/usr/bin/env python3
"""
conformance.py - check a workflow's real transition log against its declared model.

flowcheck.py judges the model. dynamics.py reads the log. This tool holds them against
each other - the third leg of process mining (discovery, conformance, enhancement) and
the question neither tool can answer alone: **does the system actually do what the
model says?**

Two directions, both checked:
  * log -> model: every observed transition must be allowed by the model
    (undeclared moves, broken per-item chains, exits from terminal states,
    automatic passes recorded as human, undeclared rejection reasons).
  * model -> log: every declared edge and gate should eventually be observed
    (an edge nobody has ever taken is a warning - untested policy, or dead model).

Usage
-----
    python conformance.py --db app.sqlite --flow my-flow.json
    python conformance.py --db app.sqlite --flow my-flow.json --strict   # warns fail too
    python conformance.py --selftest

Exit: 0 conformant (or warnings only) - 1 violations - 2 unusable input.

The flow JSON is the same format flowcheck.py lints (states/gates/transitions).
The log schema is the same one dynamics.py reads (override with --map):
    transitions(item_id, from_state, to_state, gate, decided_by, actor_kind, reason_code, at)

There is deliberately no allowlist here. A conformance violation means the model file
or the code is wrong, and the model file is cheap to fix - update it, don't suppress
the finding. If the model was *supposed* to allow the move, adding it is the fix.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ERROR, WARN = "error", "warn"

RULES: dict[str, tuple[str, str, str]] = {
    "UNKNOWN-STATE-IN-LOG": (ERROR,
        "the log contains a state the model does not declare",
        "either the model is stale or the code writes states the design never saw"),
    "UNKNOWN-GATE-IN-LOG": (ERROR,
        "the log contains a gate the model does not declare",
        "a decision point exists in code that nobody designed"),
    "OFF-MODEL-TRANSITION": (ERROR,
        "an observed transition is not allowed by any gate outcome or declared transition",
        "the system has a path the model forbids - the exact class of bug approval flows exist to prevent"),
    "BROKEN-CHAIN": (ERROR,
        "a row's from_state is not the item's previous to_state",
        "either transitions are being lost or from_state is fabricated; per-item history cannot be trusted"),
    "INITIAL-MISMATCH": (ERROR,
        "an item's first recorded state is not the model's initial state",
        "items are entering the flow through a side door"),
    "EXIT-FROM-TERMINAL": (ERROR,
        "an observed transition leaves a state the model marks terminal",
        "completion counts and the never-terminal metric are built on that state being final"),
    "ACTOR-KIND-MISMATCH": (ERROR,
        "a gate outcome reserved for humans was recorded with a non-human actor (or vice versa)",
        "the auto-pass rate and every audit answer built on actor_kind are wrong"),
    "UNDECLARED-REASON": (ERROR,
        "a rejection carries a reason_code outside the gate's declared list",
        "the rejection-reason distribution silently fragments"),
    "REJECT-WITHOUT-REASON": (ERROR,
        "a rejection at a gate with declared reason codes carries no reason_code",
        "the one metric that says WHY the flow fails loses rows invisibly"),
    "UNUSED-EDGE": (WARN,
        "a declared edge has never been observed",
        "untested policy or dead model - either way, nobody knows if that path works"),
    "UNUSED-GATE": (WARN,
        "a declared gate has never fired",
        "the gate exists only on paper"),
}

DEFAULT_MAP = {
    "table": "transitions", "item": "item_id", "from": "from_state", "to": "to_state",
    "gate": "gate", "actor": "decided_by", "kind": "actor_kind",
    "reason": "reason_code", "at": "at",
}


def die(msg: str) -> "None":
    sys.stderr.write(f"[conformance] {msg}\n")
    sys.exit(2)


class Finding:
    def __init__(self, code: str, where: str, detail: str = "", count: int = 1) -> None:
        self.code, self.where, self.detail, self.count = code, where, detail, count

    @property
    def severity(self) -> str:
        return RULES[self.code][0]

    def line(self) -> str:
        mark = "E" if self.severity == ERROR else "w"
        n = f" x{self.count}" if self.count > 1 else ""
        tail = f" - {self.detail}" if self.detail else ""
        return f"{mark}  {self.code:24} {self.where:34}{n}{tail}"


# ── the model's allowed behavior ──────────────────────────────────────────────

def load_flow(path: Path) -> dict:
    try:
        flow = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read flow: {exc}")
    if not isinstance(flow.get("states"), dict) or not flow["states"]:
        die("flow needs a non-empty 'states' object (same format flowcheck.py lints)")
    return flow


def allowed_behavior(flow: dict):
    """Derive from the model: allowed edges with actor kinds, gate edges, reasons."""
    states = flow["states"]
    terminals = {n for n, s in states.items() if (s or {}).get("terminal")}
    edges: dict[tuple[str, str], set[str]] = {}      # (from,to) -> allowed actor kinds
    gate_edges: dict[tuple[str, str, str], dict] = {}  # (gate,from,to) -> outcome meta
    gate_reasons: dict[str, set[str]] = {}
    reject_edges: set[tuple[str, str, str]] = set()

    def allow(frm, to, kinds):
        edges.setdefault((frm, to), set()).update(kinds)

    for gname, g in (flow.get("gates") or {}).items():
        g = g or {}
        at = g.get("at")
        gate_reasons[gname] = set(g.get("reason_codes") or [])
        for oname, dest in (g.get("outcomes") or {}).items():
            auto = oname.startswith("auto")
            kinds = {"system", "timeout"} if auto else {"human"}
            allow(at, dest, kinds)
            gate_edges[(gname, at, dest)] = {"outcome": oname, "kinds": kinds}
            if oname.startswith("reject") or oname in ("fail", "deny", "refuse"):
                reject_edges.add((gname, at, dest))
    for t in (flow.get("transitions") or []):
        actor = t.get("actor")
        kinds = {actor} if actor in ("human", "system", "timeout") else \
            {"human", "system", "timeout"}
        allow(t.get("from"), t.get("to"), kinds)
    # creation: entering the initial state from nothing
    allow(None, flow.get("initial"), {"human", "system", "timeout"})
    return states, terminals, edges, gate_edges, gate_reasons, reject_edges


# ── the check ─────────────────────────────────────────────────────────────────

def check(db: Path, flow: dict, mapping: dict, cap: int = 12):
    m = mapping
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = {r["name"] for r in con.execute(f"PRAGMA table_info({m['table']})").fetchall()}
    if not cols:
        die(f"table {m['table']!r} not found in {db}")

    states, terminals, edges, gate_edges, gate_reasons, reject_edges = allowed_behavior(flow)

    rows = con.execute(
        f"SELECT {m['item']} AS item, {m['from']} AS frm, {m['to']} AS dst, "
        f"       {m['gate']} AS gate, {m['kind']} AS kind, {m['reason']} AS reason "
        f"FROM {m['table']} ORDER BY {m['item']}, {m['at']}, rowid").fetchall()
    con.close()

    found: dict[tuple, Finding] = {}

    def hit(code, where, detail=""):
        key = (code, where)
        if key in found:
            found[key].count += 1
        else:
            found[key] = Finding(code, where, detail)

    seen_edges: set[tuple] = set()
    seen_gates: set[str] = set()
    total_rows = 0
    bad_items: set = set()
    prev_by_item: dict = {}
    first_by_item: set = set()

    for r in rows:
        total_rows += 1
        item, frm, dst, gate = r["item"], r["frm"], r["dst"], r["gate"]
        ok = True

        for s in (frm, dst):
            if s is not None and s not in states:
                hit("UNKNOWN-STATE-IN-LOG", str(s)); ok = False
        if gate and gate not in (flow.get("gates") or {}):
            hit("UNKNOWN-GATE-IN-LOG", str(gate)); ok = False

        # chain: first row must be the creation edge; later rows continue the chain
        if item not in first_by_item:
            first_by_item.add(item)
            if frm is not None or dst != flow.get("initial"):
                hit("INITIAL-MISMATCH", f"item {item}",
                    f"first state {dst!r}, expected {flow.get('initial')!r}"); ok = False
        elif frm != prev_by_item.get(item):
            hit("BROKEN-CHAIN", f"item {item}",
                f"from={frm!r} but previous state was {prev_by_item.get(item)!r}"); ok = False
        prev_by_item[item] = dst

        if frm in terminals:
            hit("EXIT-FROM-TERMINAL", f"{frm} -> {dst}"); ok = False

        # is the move allowed at all?
        allowed_kinds = edges.get((frm, dst))
        if allowed_kinds is None and (frm in states or frm is None) and dst in states:
            hit("OFF-MODEL-TRANSITION", f"{frm} -> {dst}"); ok = False
        elif allowed_kinds is not None:
            seen_edges.add((frm, dst))
            if r["kind"] and r["kind"] not in allowed_kinds:
                hit("ACTOR-KIND-MISMATCH", f"{frm} -> {dst}",
                    f"recorded {r['kind']!r}, model allows {sorted(allowed_kinds)}")
                ok = False

        if gate:
            seen_gates.add(gate)
            meta = gate_edges.get((gate, frm, dst))
            if meta is None and gate in (flow.get("gates") or {}):
                hit("OFF-MODEL-TRANSITION", f"{frm} -> {dst}",
                    f"not an outcome of gate {gate!r}"); ok = False
            if (gate, frm, dst) in reject_edges and gate_reasons.get(gate):
                if not r["reason"]:
                    hit("REJECT-WITHOUT-REASON", f"gate:{gate}"); ok = False
                elif r["reason"] not in gate_reasons[gate]:
                    hit("UNDECLARED-REASON", f"gate:{gate}",
                        f"reason {r['reason']!r}"); ok = False

        if not ok:
            bad_items.add(item)

    # model -> log direction
    for (frm, dst) in edges:
        if frm is None:
            continue
        if (frm, dst) not in seen_edges:
            found[("UNUSED-EDGE", f"{frm} -> {dst}")] = Finding(
                "UNUSED-EDGE", f"{frm} -> {dst}")
    for gname in (flow.get("gates") or {}):
        if gname not in seen_gates:
            found[("UNUSED-GATE", f"gate:{gname}")] = Finding("UNUSED-GATE", f"gate:{gname}")

    findings = list(found.values())
    stats = {
        "rows": total_rows,
        "items": len(first_by_item),
        "bad_items": len(bad_items),
        "trace_fitness": (round(100.0 * (len(first_by_item) - len(bad_items))
                                / len(first_by_item), 1) if first_by_item else 0.0),
        "declared_edges": len([e for e in edges if e[0] is not None]),
        "observed_declared_edges": len([e for e in seen_edges if e[0] is not None]),
    }
    return findings, stats


def report(db: Path, flow_path: Path, mapping: dict, strict: bool,
           quiet: bool = False):
    flow = load_flow(flow_path)
    findings, stats = check(db, flow, mapping)
    errors = [f for f in findings if f.severity == ERROR]
    warns = [f for f in findings if f.severity == WARN]

    if not quiet:
        print(f"conformance: {flow.get('name') or flow_path.name}  vs  {db.name}")
        print(f"  rows={stats['rows']} items={stats['items']}  "
              f"trace fitness={stats['trace_fitness']}% "
              f"({stats['items'] - stats['bad_items']}/{stats['items']} items fully conform)  "
              f"edge coverage={stats['observed_declared_edges']}/{stats['declared_edges']}")
        for f in sorted(findings, key=lambda x: (x.severity != ERROR, x.code, x.where)):
            print("  " + f.line())
        if not findings:
            print("  conformant - the log does what the model says, and every declared "
                  "edge has been exercised")
        else:
            print(f"\n{len(errors)} error(s), {len(warns)} warning(s). "
                  "Fix the model file or the code - do not suppress the finding.")
    rc = 1 if (errors or (strict and warns)) else 0
    return rc, findings, stats


# ── self-test ─────────────────────────────────────────────────────────────────

MODEL = {
    "name": "selftest model",
    "initial": "submitted",
    "states": {
        "submitted": {},
        "reviewed": {},
        "auto_cleared": {"decided_by": "system"},
        "rejected": {"terminal": True},
        "closed": {"terminal": True},
    },
    "gates": {
        "intake": {
            "at": "submitted", "capability": "review", "auto": True,
            "reason_codes": ["incomplete"],
            "outcomes": {"pass": "reviewed", "auto_pass": "auto_cleared",
                         "reject": "rejected"},
        },
    },
    "transitions": [
        {"from": "reviewed", "to": "closed", "actor": "human", "capability": "close"},
        {"from": "auto_cleared", "to": "closed", "actor": "system"},
    ],
}


def _mkdb(path: Path, rows: list[tuple]) -> None:
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE transitions (
        id INTEGER PRIMARY KEY, item_id INTEGER, from_state TEXT, to_state TEXT,
        gate TEXT, decided_by INTEGER, actor_kind TEXT, reason_code TEXT, at TEXT)""")
    con.executemany(
        "INSERT INTO transitions (item_id, from_state, to_state, gate, decided_by,"
        " actor_kind, reason_code, at) VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


CLEAN_ROWS = [
    # item 1: submitted -> reviewed (human) -> closed (human)
    (1, None, "submitted", None, 1, "human", None, "2026-01-01 08:00:00"),
    (1, "submitted", "reviewed", "intake", 2, "human", None, "2026-01-02 08:00:00"),
    (1, "reviewed", "closed", None, 2, "human", None, "2026-01-03 08:00:00"),
    # item 2: auto-cleared -> closed (system)
    (2, None, "submitted", None, 1, "human", None, "2026-01-01 09:00:00"),
    (2, "submitted", "auto_cleared", "intake", None, "system", None, "2026-01-02 09:00:00"),
    (2, "auto_cleared", "closed", None, None, "system", None, "2026-01-03 09:00:00"),
    # item 3: rejected with a declared reason
    (3, None, "submitted", None, 1, "human", None, "2026-01-01 10:00:00"),
    (3, "submitted", "rejected", "intake", 2, "human", "incomplete", "2026-01-02 10:00:00"),
]

DIRTY_ROWS = CLEAN_ROWS + [
    # item 4: off-model move, then exit from terminal
    (4, None, "submitted", None, 1, "human", None, "2026-01-01 11:00:00"),
    (4, "submitted", "closed", None, 9, "human", None, "2026-01-02 11:00:00"),
    (4, "closed", "reviewed", None, 9, "human", None, "2026-01-03 11:00:00"),
    # item 5: broken chain (claims to come from reviewed, never was there)
    (5, None, "submitted", None, 1, "human", None, "2026-01-01 12:00:00"),
    (5, "reviewed", "closed", None, 2, "human", None, "2026-01-02 12:00:00"),
    # item 6: enters through a side door, in an unknown state
    (6, None, "imported", None, 1, "human", None, "2026-01-01 13:00:00"),
    # item 7: human-only gate outcome recorded as system
    (7, None, "submitted", None, 1, "human", None, "2026-01-01 14:00:00"),
    (7, "submitted", "reviewed", "intake", None, "system", None, "2026-01-02 14:00:00"),
    # item 8: rejection with an undeclared reason, via an unknown gate
    (8, None, "submitted", None, 1, "human", None, "2026-01-01 15:00:00"),
    (8, "submitted", "rejected", "intake", 2, "human", "vibes", "2026-01-02 15:00:00"),
    (8, "rejected", "reviewed", "appeals", 2, "human", None, "2026-01-03 15:00:00"),
]

EXPECTED_DIRTY = {
    "OFF-MODEL-TRANSITION", "EXIT-FROM-TERMINAL", "BROKEN-CHAIN",
    "INITIAL-MISMATCH", "UNKNOWN-STATE-IN-LOG", "ACTOR-KIND-MISMATCH",
    "UNDECLARED-REASON", "UNKNOWN-GATE-IN-LOG",
}


def selftest() -> int:
    ok = True

    def show(label: str, cond: bool) -> None:
        nonlocal ok
        print(("  [ok] " if cond else "  [XX] ") + label)
        ok = ok and cond

    print("conformance self-test\n")
    tmp = Path(tempfile.mkdtemp(prefix="conformance-selftest-"))
    try:
        flow_path = tmp / "model.json"
        flow_path.write_text(json.dumps(MODEL), encoding="utf-8")

        clean_db = tmp / "clean.sqlite"
        _mkdb(clean_db, CLEAN_ROWS)
        rc, findings, stats = report(clean_db, flow_path, dict(DEFAULT_MAP),
                                     strict=False, quiet=True)
        codes = {f.code for f in findings}
        show("a conforming log yields no errors (exit 0)", rc == 0)
        show("trace fitness is 100% on the clean log", stats["trace_fitness"] == 100.0)
        # the clean log exercises all three gate outcomes and both plain transitions,
        # so the model->log direction must be silent too: zero findings of any kind.
        show("every declared edge was observed (no UNUSED warnings)",
             not codes)

        dirty_db = tmp / "dirty.sqlite"
        _mkdb(dirty_db, DIRTY_ROWS)
        rc, findings, stats = report(dirty_db, flow_path, dict(DEFAULT_MAP),
                                     strict=False, quiet=True)
        codes = {f.code for f in findings}
        # WHICH code fired, not just that something did
        for code in sorted(EXPECTED_DIRTY):
            show(f"detects {code}", code in codes)
        show("exits non-zero on the dirty log", rc == 1)
        show("every reported code is a documented rule", codes <= set(RULES))
        show("trace fitness counts exactly the 5 bad items (3/8 conform)",
             stats["bad_items"] == 5 and stats["trace_fitness"] == 37.5)
        show("the clean items are not blamed for the dirty ones",
             stats["items"] == 8)

        # model->log direction: remove the auto path from the LOG, keep it in the model
        partial_db = tmp / "partial.sqlite"
        _mkdb(partial_db, [r for r in CLEAN_ROWS if r[0] != 2])
        rc, findings, _ = report(partial_db, flow_path, dict(DEFAULT_MAP),
                                 strict=False, quiet=True)
        pcodes = {(f.code, f.where) for f in findings}
        show("a declared-but-never-taken edge is reported as UNUSED-EDGE",
             ("UNUSED-EDGE", "submitted -> auto_cleared") in pcodes
             and ("UNUSED-EDGE", "auto_cleared -> closed") in pcodes)
        show("warnings alone do not fail the run", rc == 0)
        rc_strict, _, _ = report(partial_db, flow_path, dict(DEFAULT_MAP),
                                 strict=True, quiet=True)
        show("--strict makes those warnings fail", rc_strict == 1)

        # and the bridge holds: the shipped example flow accepts dynamics' demo log?
        # No - they model different domains, and pretending otherwise would be a fake
        # integration test. The bridge is proven by format, not by forcing a match:
        # the example flow must load through the same loader used against real logs.
        example = Path(__file__).resolve().parent.parent / "examples" / "intake.flow.json"
        if example.is_file():
            loaded = load_flow(example)
            show("the shipped example flow loads through the same loader",
                 "states" in loaded and loaded["states"])
        else:
            show(f"shipped example exists at {example}", False)
    finally:
        for f in tmp.glob("*"):
            f.unlink()
        os.rmdir(tmp)

    print("\n" + ("self-test passed" if ok else "SELF-TEST FAILED"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Check a transition log against its declared flow model.")
    ap.add_argument("--db", help="SQLite database with the transition log")
    ap.add_argument("--flow", help="flow definition JSON (flowcheck.py format)")
    ap.add_argument("--map", help="JSON file overriding table/column names")
    ap.add_argument("--strict", action="store_true", help="warnings fail the run too")
    ap.add_argument("--codes", action="store_true", help="list codes and why they matter")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.codes:
        for code, (sev, meaning, why) in RULES.items():
            print(f"{code}  [{sev}]\n    {meaning}\n    why: {why}\n")
        return 0
    if args.selftest:
        return selftest()
    if not args.db or not args.flow:
        ap.print_help()
        return 2

    mapping = dict(DEFAULT_MAP)
    if args.map:
        try:
            mapping.update(json.loads(Path(args.map).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            die(f"cannot read --map: {exc}")
    db = Path(args.db).resolve()
    if not db.is_file():
        die(f"no such database: {db}")
    rc, _, _ = report(db, Path(args.flow).resolve(), mapping, strict=args.strict)
    return rc


if __name__ == "__main__":
    sys.exit(main())
