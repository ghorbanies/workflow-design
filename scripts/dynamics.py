#!/usr/bin/env python3
"""
dynamics.py - compute a workflow's health metrics from its transition log (SQLite).

Phase 3 of the workflow-design skill, as one command. Every number here is already in the
log if phase 1 was done; nobody has to file a report for the flow to say where it hurts.

Usage
-----
    python dynamics.py --db app.sqlite
    python dynamics.py --db app.sqlite --terminal closed,rejected,cancelled
    python dynamics.py --db app.sqlite --gates completeness,approval   # reversal pair
    python dynamics.py --print-sql                 # the exact SQL, to adapt elsewhere
    python dynamics.py --selftest                  # numbers checked against a known log

Accuracy rules, because a wrong number is worse than no number
--------------------------------------------------------------
* **Preflight first.** Unparsable timestamps, missing actor kinds, or a missing column
  mean the affected metric is REFUSED, not printed. A metric computed over a log that
  cannot support it is a confident lie.
* **No ratio without its volume**, and any ratio whose denominator is below --min-n
  (default 10) is reported as insufficient volume rather than as a percentage.
* **Open spans are counted, not dropped.** Items still sitting in a station are the ones
  you care about; excluding them is survivor bias that deletes the bottleneck.
* **Every ratio prints both readings** - what a high value means AND what a low one
  means - because the "good" value is usually produced by the thing not happening at all.

Schema it expects (override with --map)
---------------------------------------
    transitions(item_id, from_state, to_state, gate, decided_by, actor_kind, reason_code, at)

`at` must be a string SQLite can parse: 'YYYY-MM-DD HH:MM:SS', ISO with 'T', optionally
with 'Z' or an offset. Verified, not assumed: 'Z' and '+03:30' DO parse (the offset is
applied); 'DD/MM/YYYY HH:MM' and epoch integers stored as text do NOT - they yield NULL.

The dangerous case is neither: a log that mixes naive strings with offset-bearing ones
parses fine and computes durations that are silently wrong across the boundary. Preflight
looks for that mix and refuses the time metrics rather than printing a confident number.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

DEFAULT_MAP = {
    "table": "transitions",
    "item": "item_id",
    "from": "from_state",
    "to": "to_state",
    "gate": "gate",
    "actor": "decided_by",
    "kind": "actor_kind",
    "reason": "reason_code",
    "at": "at",
}

READINGS = {
    "auto_pass": ("the gate is theater - remove it or find why humans stopped using it",
                  "the gate is a real decision point"),
    "reversal": ("the FIRST station's instructions do not work",
                 "the first station is good - or the second gate is rubber-stamping"),
    "station": ("this station is the bottleneck",
                "healthy - or items are skipping it, check the volume"),
    "loopback": ("rework: an upstream quality problem, or two stations disagreeing",
                 "healthy - or loop-backs are not being written to the log"),
    "queue_age": ("items are being abandoned, regardless of queue length",
                  "the queue clears"),
    "leak": ("items enter and are never answered; they show up in no dashboard",
             "the flow closes what it opens"),
    "reasons": ("the top reason is a design brief - prevent it upstream",
                "rejections are scattered; no single upstream fix"),
    "concentration": ("a single point of failure, or the queue is visible to only one person",
                      "the decision load is shared"),
}


def die(msg: str) -> "None":
    sys.stderr.write(f"[dynamics] {msg}\n")
    sys.exit(2)


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.refused: list[str] = []
        self.values: dict = {}          # machine-readable, for the self-test

    def head(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(title)
        self.lines.append("-" * len(title))

    def say(self, text: str) -> None:
        self.lines.append("  " + text)

    def refuse(self, metric: str, why: str) -> None:
        self.refused.append(metric)
        self.lines.append("")
        self.lines.append(f"{metric}: REFUSED - {why}")

    def readings(self, key: str) -> None:
        hi, lo = READINGS[key]
        self.say(f"    high -> {hi}")
        self.say(f"    low  -> {lo}")

    def text(self) -> str:
        return "\n".join(self.lines)


# ── SQL, kept in one place so --print-sql shows exactly what ran ───────────────

def sql_set(m: dict) -> dict[str, str]:
    t, item, to, gate = m["table"], m["item"], m["to"], m["gate"]
    kind, actor, reason, at = m["kind"], m["actor"], m["reason"], m["at"]
    return {
        "preflight_bad_time": f"SELECT COUNT(*) FROM {t} WHERE julianday({at}) IS NULL",
        "preflight_no_kind": f"SELECT COUNT(*) FROM {t} WHERE {kind} IS NULL OR {kind}=''",
        "preflight_total": f"SELECT COUNT(*) FROM {t}",
        # Timezone-marked rows: look only at the time part, since the date part has '-'.
        # A log that mixes these with naive rows parses cleanly and computes wrong
        # durations, which is worse than a parse error - see the module docstring.
        "preflight_tz": f"""
            SELECT COUNT(*) FROM {t}
            WHERE julianday({at}) IS NOT NULL
              AND (substr({at}, 11) LIKE '%Z%' OR substr({at}, 11) LIKE '%+%'
                   OR substr({at}, 11) LIKE '%-%')""",

        "auto_pass": f"""
            SELECT {gate} AS gate, COUNT(*) AS decisions,
                   SUM(CASE WHEN {kind} <> 'human' THEN 1 ELSE 0 END) AS automatic
            FROM {t} WHERE {gate} IS NOT NULL AND {gate} <> ''
            GROUP BY {gate} ORDER BY {gate}""",

        "reversal": f"""
            WITH first_pass AS (
              SELECT {item} AS item, MIN({at}) AS at1 FROM {t}
              WHERE {gate} = :g1 AND ({to} NOT LIKE 'reject%%')
              GROUP BY {item}
            )
            SELECT COUNT(*) AS reviewed,
                   SUM(CASE WHEN s.{to} LIKE 'reject%%' THEN 1 ELSE 0 END) AS reversed
            FROM {t} s JOIN first_pass f
              ON f.item = s.{item} AND s.{at} > f.at1
            WHERE s.{gate} = :g2""",

        "spans": f"""
            SELECT {to} AS station, {at} AS entered,
                   (SELECT MIN(n.{at}) FROM {t} n
                     WHERE n.{item} = o.{item} AND n.{at} > o.{at}) AS left_at,
                   (julianday(COALESCE((SELECT MIN(n.{at}) FROM {t} n
                        WHERE n.{item} = o.{item} AND n.{at} > o.{at}),
                        :now)) - julianday({at})) * 24.0 AS hours
            FROM {t} o""",

        "loopback": f"""
            SELECT {to} AS station, COUNT(*) AS revisits
            FROM {t} o
            WHERE EXISTS (SELECT 1 FROM {t} p
                          WHERE p.{item} = o.{item} AND p.{to} = o.{to} AND p.{at} < o.{at})
            GROUP BY {to} ORDER BY revisits DESC""",

        "current": f"""
            SELECT {item} AS item, {to} AS state, MAX({at}) AS at
            FROM {t} GROUP BY {item}""",

        "paths": f"""
            SELECT {item} AS item, {to} AS state
            FROM {t} ORDER BY {item}, {at}, rowid""",

        "reasons": f"""
            SELECT {gate} AS gate, {reason} AS reason, COUNT(*) AS n
            FROM {t}
            WHERE {reason} IS NOT NULL AND {reason} <> ''
            GROUP BY {gate}, {reason} ORDER BY n DESC""",

        "concentration": f"""
            SELECT {gate} AS gate, {actor} AS actor, COUNT(*) AS decisions
            FROM {t} WHERE {gate} IS NOT NULL AND {gate} <> '' AND {kind} = 'human'
            GROUP BY {gate}, {actor} ORDER BY decisions DESC""",
    }


def pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(q * len(ordered)))
    return round(ordered[idx], 2)


# ── the run ───────────────────────────────────────────────────────────────────

def analyse(db: Path, mapping: dict, terminal: list[str], gates: list[str],
            min_n: int, now: str | None = None) -> Report:
    rep = Report()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sql = sql_set(mapping)

    # ── preflight: refuse what the log cannot support ─────────────────────────
    cols = {r["name"] for r in con.execute(
        f"PRAGMA table_info({mapping['table']})").fetchall()}
    if not cols:
        die(f"table {mapping['table']!r} not found in {db}")
    needed = {v for k, v in mapping.items() if k != "table"}
    missing = sorted(needed - cols)

    total = con.execute(sql["preflight_total"]).fetchone()[0]
    bad_time = 0 if mapping["at"] in missing else \
        con.execute(sql["preflight_bad_time"]).fetchone()[0]
    no_kind = 0 if mapping["kind"] in missing else \
        con.execute(sql["preflight_no_kind"]).fetchone()[0]

    tz_rows = 0 if mapping["at"] in missing else \
        con.execute(sql["preflight_tz"]).fetchone()[0]
    naive_rows = total - tz_rows - bad_time
    mixed_tz = tz_rows > 0 and naive_rows > 0

    rep.head("preflight")
    rep.say(f"rows: {total}")
    if missing:
        rep.say(f"MISSING COLUMNS: {', '.join(missing)}")
    rep.say(f"unparsable timestamps: {bad_time}"
            + ("   <- not ISO; 'DD/MM/YYYY' and epoch integers yield NULL here"
               if bad_time else ""))
    rep.say(f"rows with no actor kind: {no_kind}")
    rep.say(f"timezone-marked / naive timestamps: {tz_rows} / {naive_rows}"
            + ("   <- MIXED: durations across the boundary are silently wrong"
               if mixed_tz else ""))
    rep.values["preflight"] = {"rows": total, "bad_time": bad_time,
                               "no_kind": no_kind, "missing": missing,
                               "tz_rows": tz_rows, "mixed_tz": mixed_tz}

    time_ok = not bad_time and not mixed_tz and mapping["at"] not in missing
    kind_ok = not no_kind and mapping["kind"] not in missing

    now = now or "now"

    # ── 1. gate auto-pass rate ────────────────────────────────────────────────
    if not kind_ok:
        rep.refuse("gate auto-pass rate",
                   f"{no_kind} row(s) have no actor kind; the ratio would be a guess")
    else:
        rep.head("1. gate auto-pass rate")
        rows = con.execute(sql["auto_pass"]).fetchall()
        out = {}
        for r in rows:
            out[r["gate"]] = (r["decisions"], r["automatic"])
            if r["decisions"] < min_n:
                rep.say(f"{r['gate']:22} n={r['decisions']:<5} insufficient volume "
                        f"(< {min_n}) - no percentage")
            else:
                rep.say(f"{r['gate']:22} n={r['decisions']:<5} "
                        f"auto={pct(r['automatic'], r['decisions'])}%")
        if rows:
            rep.readings("auto_pass")
        else:
            rep.say("no gate decisions in the log")
        rep.values["auto_pass"] = out

    # ── 2. reversal rate ──────────────────────────────────────────────────────
    if len(gates) != 2:
        rep.refuse("second-approver reversal rate",
                   "needs --gates first,second so the pair is not guessed")
    else:
        rep.head("2. second-approver reversal rate")
        row = con.execute(sql["reversal"], {"g1": gates[0], "g2": gates[1]}).fetchone()
        reviewed, reversed_ = row["reviewed"] or 0, row["reversed"] or 0
        rep.values["reversal"] = (reviewed, reversed_)
        if reviewed < min_n:
            rep.say(f"{gates[0]} -> {gates[1]}: n={reviewed} insufficient volume "
                    f"(< {min_n}) - no percentage")
        else:
            rep.say(f"{gates[0]} -> {gates[1]}: n={reviewed} "
                    f"reversed={pct(reversed_, reviewed)}%")
        rep.readings("reversal")

    # ── 3. time in station ────────────────────────────────────────────────────
    if not time_ok:
        rep.refuse("time in station",
                   f"{bad_time} unparsable timestamp(s)" if bad_time else
                   f"the log mixes {tz_rows} timezone-marked and {naive_rows} naive "
                   f"timestamps; durations across that boundary are wrong by the offset")
    else:
        rep.head("3. time in station (hours; open spans included, terminal states excluded)")
        spans: dict[str, list[float]] = {}
        open_count: dict[str, int] = {}
        for r in con.execute(sql["spans"], {"now": now}).fetchall():
            if r["hours"] is None:
                continue
            # An item that finished is not waiting. Left in, terminal states dominate the
            # p90 with the age of the archive and hide the real bottleneck.
            if r["station"] in terminal:
                continue
            spans.setdefault(r["station"], []).append(float(r["hours"]))
            if r["left_at"] is None:
                open_count[r["station"]] = open_count.get(r["station"], 0) + 1
        ranked = sorted(spans.items(), key=lambda kv: percentile(kv[1], 0.9), reverse=True)
        for station, vals in ranked:
            rep.say(f"{station:22} n={len(vals):<5} p50={percentile(vals, 0.5):<8} "
                    f"p90={percentile(vals, 0.9):<8} still-open={open_count.get(station, 0)}")
        rep.values["stations"] = {k: (len(v), percentile(v, 0.5), percentile(v, 0.9))
                                  for k, v in spans.items()}
        rep.values["open_spans"] = dict(open_count)
        if ranked:
            rep.readings("station")

    # ── 4. queue age ──────────────────────────────────────────────────────────
    current = con.execute(sql["current"]).fetchall()
    waiting: dict[str, list[str]] = {}
    for r in current:
        if r["state"] not in terminal:
            waiting.setdefault(r["state"], []).append(r["at"])
    rep.head("4. queue age (oldest waiting item)")
    for state, times in sorted(waiting.items()):
        rep.say(f"{state:22} waiting={len(times):<5} oldest_entered={min(times)}")
    if not waiting:
        rep.say("nothing waiting - every item is in a terminal state")
    else:
        rep.readings("queue_age")
    rep.values["waiting"] = {k: len(v) for k, v in waiting.items()}

    # ── 5. loop-back ──────────────────────────────────────────────────────────
    rep.head("5. loop-back (rework)")
    loops = con.execute(sql["loopback"]).fetchall()
    for r in loops:
        rep.say(f"{r['station']:22} revisits={r['revisits']}")
    if not loops:
        rep.say("no state is entered twice by the same item")
    else:
        rep.readings("loopback")
    rep.values["loopback"] = {r["station"]: r["revisits"] for r in loops}

    # ── 6. never-terminal ─────────────────────────────────────────────────────
    rep.head("6. never-terminal (the leak)")
    stuck = [r for r in current if r["state"] not in terminal]
    rep.say(f"items whose last state is not terminal: {len(stuck)} of {len(current)}"
            + (f"  ({pct(len(stuck), len(current))}%)" if len(current) >= min_n else
               f"  (n < {min_n}, no percentage)"))
    rep.values["leak"] = (len(stuck), len(current))
    rep.readings("leak")

    # ── 6b. variants: the paths items actually take ───────────────────────────
    rep.head("6b. variants (top real paths)")
    paths: dict = {}
    for r in con.execute(sql["paths"]).fetchall():
        paths.setdefault(r["item"], []).append(r["state"])
    variant_counts: dict[str, int] = {}
    for states_seq in paths.values():
        sig = " > ".join(states_seq)
        variant_counts[sig] = variant_counts.get(sig, 0) + 1
    ranked_v = sorted(variant_counts.items(), key=lambda kv: -kv[1])[:5]
    for sig, n in ranked_v:
        rep.say(f"n={n:<4} ({pct(n, len(paths))}%)  {sig}")
    if len(variant_counts) > 5:
        rest = len(variant_counts) - 5
        rep.say(f"... and {rest} more variant(s)")
    if variant_counts:
        rep.say("    reading -> the top variants are the process as it exists; a long "
                "tail of rare variants is where exceptions (and workarounds) live")
    rep.values["variants"] = dict(variant_counts)

    # ── 7. rejection reasons ──────────────────────────────────────────────────
    rep.head("7. rejection reason distribution")
    reasons = con.execute(sql["reasons"]).fetchall()
    for r in reasons:
        rep.say(f"{str(r['gate']):22} {str(r['reason']):26} n={r['n']}")
    if not reasons:
        rep.say("no enumerated reason codes in the log - see modeling.md, gates section")
    else:
        rep.readings("reasons")
    rep.values["reasons"] = {(r["gate"], r["reason"]): r["n"] for r in reasons}

    # ── 8. decision concentration ─────────────────────────────────────────────
    if not kind_ok:
        rep.refuse("decision concentration",
                   "actor kind is missing, so human decisions cannot be isolated")
    else:
        rep.head("8. decision concentration (human decisions per actor)")
        rows = con.execute(sql["concentration"]).fetchall()
        per_gate: dict[str, int] = {}
        for r in rows:
            per_gate[r["gate"]] = per_gate.get(r["gate"], 0) + r["decisions"]
        for r in rows:
            share = pct(r["decisions"], per_gate[r["gate"]])
            rep.say(f"{str(r['gate']):22} actor={str(r['actor']):<8} "
                    f"n={r['decisions']:<5} share={share}%")
        rep.values["concentration"] = {(r["gate"], r["actor"]): r["decisions"] for r in rows}
        if rows:
            rep.readings("concentration")
        else:
            rep.say("no human gate decisions in the log")

    con.close()
    if rep.refused:
        rep.lines.append("")
        rep.lines.append(f"{len(rep.refused)} metric(s) refused. "
                         f"Fix the log, not the metric.")
    return rep


# ── self-test: a log whose every number is known by construction ──────────────

def build_fixture(path: Path) -> dict:
    """20 items through two gates, with the answers fixed in advance."""
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE transitions (
        id INTEGER PRIMARY KEY, item_id INTEGER, from_state TEXT, to_state TEXT,
        gate TEXT, decided_by INTEGER, actor_kind TEXT, reason_code TEXT, at TEXT)""")

    def add(item, frm, to, gate=None, by=None, kind="human", reason=None, at="2026-01-01 00:00:00"):
        con.execute("INSERT INTO transitions (item_id, from_state, to_state, gate,"
                    " decided_by, actor_kind, reason_code, at) VALUES (?,?,?,?,?,?,?,?)",
                    (item, frm, to, gate, by, kind, reason, at))

    day = lambda d, h=0: f"2026-01-{d:02d} {h:02d}:00:00"

    # Chain integrity holds throughout: every row's from_state equals the item's
    # previous to_state. An earlier version of this fixture broke that in two places,
    # and an eval agent tracing per-item paths correctly flagged both as log-integrity
    # findings - a demo log must not carry accidental defects.

    # gate 'intake': 20 first decisions, 8 automatic -> auto rate 8/22 with re-decisions
    for i in range(1, 21):
        add(i, None, "submitted", at=day(1), kind="human", by=1)
        auto = i <= 8
        add(i, "submitted", "cleared" if auto else "reviewed", gate="intake",
            by=None if auto else (1 if i <= 14 else 2),
            kind="system" if auto else "human", at=day(2))

    # rework: items 1 and 2 are pulled back from auto-clear and re-decided by a human
    # (BEFORE the approval gate, so their chains stay consistent)
    for i in (1, 2):
        add(i, "cleared", "submitted", by=1, at=day(3))
        add(i, "submitted", "reviewed", gate="intake", by=1, at=day(4))

    # gate 'approval': 20 decisions, 4 rejected -> reversal 20%.
    # from_state is whatever the item is actually in: 'cleared' for untouched auto
    # items, 'reviewed' for the rest.
    for i in range(1, 21):
        rejected = i in (17, 18, 19, 20)
        frm = "cleared" if (3 <= i <= 8) else "reviewed"
        add(i, frm, "rejected" if rejected else "approved_by_human",
            gate="approval", by=3, kind="human",
            reason="out_of_policy" if i in (17, 18, 19) else ("wrong_owner" if rejected else None),
            at=day(5))

    # terminal: approved items close, except 9 and 10 (the leak).
    # Rejected items are already terminal - closing them from a state they never
    # reached was exactly the fixture bug described above.
    for i in range(1, 21):
        if i in (9, 10) or i in (17, 18, 19, 20):
            continue
        add(i, "approved_by_human", "closed", kind="system", at=day(6))

    con.commit()
    con.close()
    return {
        "auto_pass_intake": (22, 8),        # 20 + 2 loop-back re-decisions, 8 automatic
        "reversal": (20, 4),
        "leak_items": 2,
        "loopback_submitted": 2,
        "reasons": {("approval", "out_of_policy"): 3, ("approval", "wrong_owner"): 1},
        "concentration_approval_actor3": 20,
    }


def selftest() -> int:
    ok = True

    def show(label: str, cond: bool) -> None:
        nonlocal ok
        print(("  [ok] " if cond else "  [XX] ") + label)
        ok = ok and cond

    print("dynamics self-test\n")
    tmp = Path(tempfile.mkdtemp(prefix="dynamics-selftest-"))
    try:
        db = tmp / "fixture.sqlite"
        want = build_fixture(db)
        rep = analyse(db, dict(DEFAULT_MAP), terminal=["closed", "rejected"],
                      gates=["intake", "approval"], min_n=10, now="2026-01-07 00:00:00")
        v = rep.values

        show("preflight sees no bad timestamps and no missing actor kind",
             v["preflight"]["bad_time"] == 0 and v["preflight"]["no_kind"] == 0)
        show(f"auto-pass rate for 'intake' is {want['auto_pass_intake']}",
             v["auto_pass"].get("intake") == want["auto_pass_intake"])
        show("reversal pair counts 20 reviewed / 4 reversed",
             v["reversal"] == want["reversal"])
        show("the two never-terminal items are found",
             v["leak"][0] == want["leak_items"])
        show("both loop-backs into 'submitted' are counted",
             v["loopback"].get("submitted") == want["loopback_submitted"])
        show("rejection reasons match the fixture",
             all(v["reasons"].get(k) == n for k, n in want["reasons"].items()))
        show("all 20 approval decisions belong to one actor (concentration)",
             v["concentration"].get(("approval", 3)) == want["concentration_approval_actor3"])
        # Items 9 and 10 never leave 'approved_by_human'. If open spans were dropped, this
        # station would report 14 instead of 16 - and the stuck items, the whole reason to
        # look, would vanish from the report. Assert the count, not merely "> 0".
        show("still-open spans are counted, not dropped (16 = 14 closed + 2 open)",
             v["stations"].get("approved_by_human", (0,))[0] == 16
             and v["open_spans"].get("approved_by_human") == 2)
        # found by running the real command and reading the output: terminal states were
        # showing up as stations with the age of the archive as their p90.
        show("terminal states are not reported as stations",
             not ({"closed", "rejected"} & set(v["stations"])))

        # variants: the five path shapes of the fixture, with exact counts
        expected_variants = {
            "submitted > cleared > submitted > reviewed > approved_by_human > closed": 2,
            "submitted > cleared > approved_by_human > closed": 6,
            "submitted > reviewed > approved_by_human": 2,
            "submitted > reviewed > approved_by_human > closed": 6,
            "submitted > reviewed > rejected": 4,
        }
        show("variant analysis finds exactly the fixture's five paths with exact counts",
             v.get("variants") == expected_variants)

        # ordering canary: physical row order in the fixture happens to match time
        # order, so ORDER BY rowid would accidentally pass. This db is hostile to it:
        # rows inserted newest-first.
        rev_db = tmp / "reversed.sqlite"
        con = sqlite3.connect(rev_db)
        con.execute("""CREATE TABLE transitions (
            id INTEGER PRIMARY KEY, item_id INTEGER, from_state TEXT, to_state TEXT,
            gate TEXT, decided_by INTEGER, actor_kind TEXT, reason_code TEXT, at TEXT)""")
        for st, at in [("closed", "2026-01-03 00:00:00"),
                       ("reviewed", "2026-01-02 00:00:00"),
                       ("submitted", "2026-01-01 00:00:00")]:
            con.execute("INSERT INTO transitions (item_id, to_state, actor_kind, at)"
                        " VALUES (1, ?, 'human', ?)", (st, at))
        con.commit()
        con.close()
        rev = analyse(rev_db, dict(DEFAULT_MAP), terminal=["closed"], gates=[],
                      min_n=1, now="2026-01-07 00:00:00")
        show("variant paths follow timestamps, not insertion order",
             list(rev.values["variants"]) == ["submitted > reviewed > closed"])

        # accuracy: a ratio below --min-n must not be printed as a percentage.
        # Assert the specific line, not the phrase anywhere in the report - three
        # sections can emit it, so a report-wide search cannot tell which guard fired.
        rep_small = analyse(db, dict(DEFAULT_MAP), terminal=["closed", "rejected"],
                            gates=["intake", "approval"], min_n=999,
                            now="2026-01-07 00:00:00")
        intake_line = next((ln for ln in rep_small.text().splitlines()
                            if ln.strip().startswith("intake")), "")
        show("below --min-n, the gate's own line says insufficient volume and no percentage",
             "insufficient volume" in intake_line and "auto=" not in intake_line)

        # A mixed-timezone log parses cleanly and is silently wrong. That is the case
        # worth catching, and it is NOT the same as a parse error - so it gets its own
        # fixture. ('Z' and '+03:30' do parse; verified, not assumed.)
        mixed_db = tmp / "mixed.sqlite"
        build_fixture(mixed_db)
        con = sqlite3.connect(mixed_db)
        con.execute("UPDATE transitions SET at = '2026-01-02T03:00:00+03:30' WHERE id = 3")
        con.commit()
        con.close()
        mixed = analyse(mixed_db, dict(DEFAULT_MAP), terminal=["closed", "rejected"],
                        gates=["intake", "approval"], min_n=10, now="2026-01-07 00:00:00")
        show("an offset-bearing timestamp still parses (so a parse check alone is blind)",
             mixed.values["preflight"]["bad_time"] == 0)
        show("mixing naive and offset timestamps refuses time-in-station",
             mixed.values["preflight"]["mixed_tz"] and "time in station" in mixed.refused)

        # canary: corrupt the log and the affected metrics must be REFUSED, not guessed
        con = sqlite3.connect(db)
        con.execute("UPDATE transitions SET actor_kind = NULL WHERE id = 1")
        con.execute("UPDATE transitions SET at = '01/02/2026 03:00' WHERE id = 2")
        con.commit()
        con.close()
        bad = analyse(db, dict(DEFAULT_MAP), terminal=["closed", "rejected"],
                      gates=["intake", "approval"], min_n=10, now="2026-01-07 00:00:00")
        show("a row with no actor kind refuses the auto-pass rate",
             "gate auto-pass rate" in bad.refused)
        show("an unparsable timestamp refuses time-in-station",
             "time in station" in bad.refused)
        show("metrics that do not depend on the damage still run",
             "loopback" in bad.values and bad.values["loopback"])
    finally:
        for f in tmp.glob("*"):
            f.unlink()
        os.rmdir(tmp)

    print("\n" + ("self-test passed" if ok else "SELF-TEST FAILED"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Workflow dynamics from a transition log.")
    ap.add_argument("--db", help="path to the SQLite database")
    ap.add_argument("--map", help="JSON file overriding table/column names")
    ap.add_argument("--terminal", default="closed,rejected,cancelled",
                    help="comma-separated terminal states")
    ap.add_argument("--gates", default="", help="first,second gate for the reversal rate")
    ap.add_argument("--min-n", type=int, default=10,
                    help="below this denominator, ratios are refused as noise")
    ap.add_argument("--print-sql", action="store_true", help="print the SQL that runs")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--demo", metavar="PATH",
                    help="write a small demo transition log (used by evals and for a "
                         "first look at the report format), then exit")
    args = ap.parse_args()

    if args.demo:
        demo = Path(args.demo).resolve()
        if demo.exists():
            die(f"refusing to overwrite existing file: {demo}")
        demo.parent.mkdir(parents=True, exist_ok=True)
        build_fixture(demo)
        print(f"demo log written: {demo}\n"
              f"try: python {Path(sys.argv[0]).name} --db \"{demo}\" "
              f"--terminal closed,rejected --gates intake,approval")
        return 0

    mapping = dict(DEFAULT_MAP)
    if args.map:
        try:
            mapping.update(json.loads(Path(args.map).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            die(f"cannot read --map: {exc}")

    if args.print_sql:
        for name, text in sql_set(mapping).items():
            print(f"-- {name}\n{text.strip()}\n")
        return 0
    if args.selftest:
        return selftest()
    if not args.db:
        ap.print_help()
        return 2
    db = Path(args.db).resolve()
    if not db.is_file():
        die(f"no such database: {db}")

    rep = analyse(db, mapping, [s for s in args.terminal.split(",") if s],
                  [g for g in args.gates.split(",") if g], args.min_n)
    print(rep.text())
    return 1 if rep.refused else 0


if __name__ == "__main__":
    sys.exit(main())
