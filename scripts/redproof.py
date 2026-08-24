#!/usr/bin/env python3
"""
redproof.py - prove that each guard in your code is actually covered by a test, and that
it can still say yes.

A guard has two sides, and a proof that checks only one is half a proof:

  RED   - remove the guard, run your test command, require the suite to FAIL, restore the
          file byte-for-byte. A guard whose removal leaves the suite green is a guard
          nobody is testing.
  GREEN - with the guard in place, run it against a known-HEALTHY artifact and require it
          to PASS. A guard that rejects a good artifact is exactly as useless as one that
          never fires - and it hides longer, because refusing everything looks careful.

And when a guard checks an AGREEMENT between two sides (certificate vs manifest,
client vs server, package vs host, migration vs schema), both sides must be read from the
built artifact. Reading one side from an assumption proves nothing: the half you assumed
is the half that breaks.

Usage
-----
    python redproof.py spec.json            # run the proof
    python redproof.py --selftest           # prove the tool itself is not blind
    python redproof.py spec.json --only 3   # run one case (index from the listing)
    python redproof.py spec.json --list     # show the cases without running them

Spec format (JSON)
------------------
    {
      "verify":  "bash dev/run_all.sh",     // shell command; exit 0 == green
      "cwd":     ".",                       // optional, relative to the spec file
      "timeout": 300,                       // optional seconds per run
      "require_green": false,               // true = a case with no "green" block fails
      "cases": [
        {
          "label": "scope filter on the queue query",
          "file":  "src/queries.py",
          "from":  "WHERE tenant_id = ? AND state = ?",
          "to":    "WHERE (? IS NOT NULL) AND state = ?",
          "expect_output": "queue leaks across tenants",  // optional substring of the
                                                          // failure output; asserts the
                                                          // RIGHT assertion went red

          // --- the other side: this guard must still pass a good artifact ---
          "green": {
            "command": "python tools/check_bundle.py fixtures/healthy.zip",
            "expect_output": "37/37 present"              // optional
          },

          // --- both halves of a handshake, each read from the artifact ---
          "agreement": {
            "name":  "app fingerprint vs published site association",
            "left":  {"from": "artifact",
                      "command": "keytool -printcert -jarfile build/app.apk",
                      "extract": "SHA256: ([0-9A-F:]+)"}, // optional regex, group 1
            "right": {"from": "artifact",
                      "command": "unzip -p build/app.apk res/values/strings.xml",
                      "extract": "SHA256: ([0-9A-F:]+)"},
            "match": "equal"                              // or "contains"
          }
        }
      ]
    }

Every agreement side must declare `"from": "artifact"`. A side marked "assumed" (or one
whose command produces nothing) fails the case: that is an assumption wearing a proof's
clothes.

Safety
------
* Commit before running. This tool rewrites source files.
* The baseline must be green, or nothing below it means anything.
* Each pattern must match EXACTLY once; 0 or 2+ matches abort that case.
* Files are restored in a finally block, on crash, and on Ctrl-C, then hash-verified.
* Only files listed in the spec are ever touched.
* A HARD kill cannot be trapped (SIGKILL, closing the terminal, a runner timing the
  process out). If that happens mid-case, the neutered file stays on disk. Two tripwires
  catch it: this tool warns about uncommitted changes in target files at the start of the
  next run, and the baseline gate refuses to proceed because the suite is red. Recovery is
  `git checkout -- <file>` - which is why "commit first" is the first rule and not advice.
* Green and agreement checks run with the guard IN PLACE, after the file is restored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

RED, GREEN, DIM, BOLD, OFF = "\033[31m", "\033[32m", "\033[2m", "\033[1m", "\033[0m"
if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
    RED = GREEN = DIM = BOLD = OFF = ""


def _printable(text: str) -> bool:
    """Legacy consoles (cp1252, cp437) raise on box glyphs. Ask before printing them."""
    try:
        text.encode(sys.stdout.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


MARK_OK, MARK_BAD, MARK_WARN, BULLET = (
    ("✓", "✗", "⚠", "·") if _printable("✓✗⚠·")
    else ("[ok]", "[XX]", "[!]", "-")
)


# ── file helpers (bytes throughout, so restore is byte-for-byte) ────────────────

def read_bytes(p: Path) -> bytes:
    return p.read_bytes()


def write_bytes(p: Path, data: bytes) -> None:
    with open(p, "wb") as fh:
        fh.write(data)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def restore_write(p: Path, data: bytes) -> None:
    """Restore a file to its original bytes.

    The fault hook is here so the self-test can prove the hash verification that
    follows every restore actually fires. Without it, removing that verification
    leaves the self-test green - i.e. the safety net is untested. It is inert unless
    REDPROOF_FAULT is set, and nothing but the self-test ever sets it.
    """
    if os.environ.get("REDPROOF_FAULT") == "silent_restore":
        return                                   # a write that does not take effect
    write_bytes(p, data)


def eol_variants(text: str) -> list[bytes]:
    """A literal pattern silently fails to match on mixed line endings. Try both."""
    lf = text.replace("\r\n", "\n")
    crlf = lf.replace("\n", "\r\n")
    out = [lf.encode("utf-8")]
    if crlf != lf:
        out.append(crlf.encode("utf-8"))
    return out


def pick_unique(haystack: bytes, text: str) -> tuple[bytes, str] | tuple[None, str]:
    """Return the variant that matches exactly once, or (None, reason)."""
    counts = []
    for variant in eol_variants(text):
        counts.append((variant, haystack.count(variant)))
    exact = [v for v, n in counts if n == 1]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:                       # both variants match once: ambiguous file
        return None, "pattern matches once in BOTH line-ending forms - ambiguous"
    total = sum(n for _, n in counts)
    if total == 0:
        return None, "pattern not found (checked LF and CRLF)"
    return None, f"pattern matches {total} times - make it unique"


# ── running ────────────────────────────────────────────────────────────────────

def run_verify(cmd: str, cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(cwd), timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        body = (exc.stdout or b"").decode("utf-8", "replace")
        return 124, body + f"\n[redproof] verify timed out after {timeout}s"


def git_dirty(paths: list[Path], root: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--"] + [str(p) for p in paths],
            cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return []
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]


# ── the proof ──────────────────────────────────────────────────────────────────

class Restorer:
    """Holds original bytes and puts them back no matter how we leave."""

    def __init__(self) -> None:
        self.saved: dict[Path, bytes] = {}
        self._installed = False

    def remember(self, path: Path, data: bytes) -> None:
        self.saved.setdefault(path, data)
        if not self._installed:
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    signal.signal(sig, self._on_signal)
                except (ValueError, OSError):
                    pass
            self._installed = True

    def _on_signal(self, *_a: object) -> None:
        self.restore_all()
        sys.stderr.write("\n[redproof] interrupted - files restored\n")
        sys.exit(130)

    def restore_all(self) -> list[str]:
        bad = []
        for path, data in self.saved.items():
            try:
                write_bytes(path, data)
                if sha(read_bytes(path)) != sha(data):
                    bad.append(str(path))
            except OSError as exc:                       # pragma: no cover
                bad.append(f"{path}: {exc}")
        return bad


def load_spec(spec_path: Path) -> tuple[str, Path, int, list[dict]]:
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read spec: {exc}")
    verify = spec.get("verify")
    cases = spec.get("cases")
    if not verify or not isinstance(cases, list) or not cases:
        die("spec needs a non-empty 'verify' string and a non-empty 'cases' list")
    cwd = (spec_path.parent / spec.get("cwd", ".")).resolve()
    timeout = int(spec.get("timeout", 300))
    for i, case in enumerate(cases):
        for key in ("label", "file", "from", "to"):
            if key not in case:
                die(f"case #{i + 1} is missing '{key}'")
        green = case.get("green")
        if green is not None and not str(green.get("command", "")).strip():
            die(f"case #{i + 1}: 'green' needs a 'command' that exercises the guard "
                f"against a healthy artifact")
        agr = case.get("agreement")
        if agr is not None:
            for side in ("left", "right"):
                if side not in agr:
                    die(f"case #{i + 1}: agreement is missing its '{side}' side - "
                        f"an agreement with one side is not an agreement")
                if not str((agr[side] or {}).get("command", "")).strip():
                    die(f"case #{i + 1}: agreement '{side}' needs a 'command' that reads "
                        f"the value out of the artifact")
            if agr.get("match", "equal") not in ("equal", "contains"):
                die(f"case #{i + 1}: agreement 'match' must be 'equal' or 'contains'")
    return verify, cwd, timeout, cases, bool(spec.get("require_green", False))


def die(msg: str, code: int = 2) -> "None":
    sys.stderr.write(f"[redproof] {msg}\n")
    sys.exit(code)


# ── the other side of the proof ────────────────────────────────────────────────

def check_green(case: dict, cwd: Path, timeout: int) -> tuple[str, str]:
    """Guard in place, healthy artifact: it must still say yes.

    A guard that refuses a known-good artifact is as useless as one that never fires,
    and it survives longer because refusing everything reads as caution.
    """
    green = case["green"]
    code, out = run_verify(green["command"], cwd, timeout)
    if code != 0:
        return ("ALWAYS-RED",
                f"rejected a known-good artifact (exit {code}) - this guard cannot say yes")
    want = green.get("expect_output")
    if want and want not in out:
        return ("ALWAYS-RED",
                f"exited 0 on the healthy artifact but never printed {want!r} - "
                f"it may not have run the guard at all")
    return ("CLEAN", f"passed the healthy artifact (exit {code})")


def read_side(side: dict, name: str, cwd: Path, timeout: int) -> tuple[str | None, str]:
    """Read one half of an agreement out of the artifact. Returns (value, error)."""
    origin = str(side.get("from", "")).strip().lower()
    if origin != "artifact":
        return (None, f"{name} side is declared {origin or 'undeclared'!r}, not 'artifact' - "
                      f"an assumed half proves nothing about the half that breaks")
    code, out = run_verify(side["command"], cwd, timeout)
    if code != 0:
        return (None, f"{name} side command failed (exit {code}) - the artifact was not read")
    text = out.strip()
    pattern = side.get("extract")
    if pattern:
        import re
        m = re.search(pattern, out, re.MULTILINE | re.DOTALL)
        if not m:
            return (None, f"{name} side matched nothing for {pattern!r} - "
                          f"the value is not in the artifact")
        text = (m.group(1) if m.groups() else m.group(0)).strip()
    if not text:
        return (None, f"{name} side read an empty value - nothing was actually extracted")
    return (text, "")


def check_agreement(case: dict, cwd: Path, timeout: int) -> tuple[str, str]:
    """Both halves of a handshake, each read from the built artifact - never assumed."""
    agr = case["agreement"]
    name = agr.get("name", "agreement")
    left, err = read_side(agr["left"], "left", cwd, timeout)
    if err:
        return ("ONE-SIDED", f"{name}: {err}")
    right, err = read_side(agr["right"], "right", cwd, timeout)
    if err:
        return ("ONE-SIDED", f"{name}: {err}")
    mode = agr.get("match", "equal")
    ok = (left == right) if mode == "equal" else (left in right or right in left)
    if not ok:
        return ("MISMATCH", f"{name}: the two artifacts disagree "
                            f"({left[:40]!r} vs {right[:40]!r})")
    return ("AGREED", f"{name}: both sides read from the artifact and {mode}")


def proof(spec_path: Path, only: int | None, quiet: bool = False,
          collect: list | None = None) -> int:
    verify, cwd, timeout, cases, require_green = load_spec(spec_path)
    targets = sorted({(cwd / c["file"]).resolve() for c in cases})
    for t in targets:
        if not t.is_file():
            die(f"target file does not exist: {t}")

    say = (lambda *a: None) if quiet else print

    dirty = git_dirty(targets, cwd)
    if dirty:
        say(f"{DIM}{MARK_WARN} uncommitted changes in target files - commit first, so a failed "
            f"restore is recoverable:{OFF}")
        for ln in dirty:
            say(f"{DIM}    {ln}{OFF}")

    say(f"{BOLD}baseline{OFF}  {verify}")
    code, _ = run_verify(verify, cwd, timeout)
    if code != 0:
        die(f"baseline is NOT green (exit {code}). Nothing below it would mean anything.")
    say(f"{GREEN}  {MARK_OK} baseline green{OFF}\n")

    restorer = Restorer()
    results: list[tuple[str, str, str]] = []          # (status, label, detail)

    try:
        for idx, case in enumerate(cases, start=1):
            if only is not None and only != idx:
                continue
            label = case["label"]
            path = (cwd / case["file"]).resolve()
            original = read_bytes(path)
            restorer.remember(path, original)

            def other_sides() -> None:
                """Green side and agreement, with the guard IN PLACE. Run for every case,
                even one whose red side could not run - they are independent proofs."""
                if case.get("green"):
                    st, detail = check_green(case, cwd, timeout)
                    results.append((st, f"{label} [green]", detail))
                    mark, col = ((MARK_OK, GREEN) if st == "CLEAN" else (MARK_BAD, RED))
                    say(f"{col}  {mark} [{idx}] {label} [green] - {detail}{OFF}")
                elif require_green:
                    results.append(("NO-GREEN", f"{label} [green]",
                                    "no green side declared, and require_green is on"))
                    say(f"{RED}  {MARK_BAD} [{idx}] {label} [green] - no green side "
                        f"declared{OFF}")
                if case.get("agreement"):
                    st, detail = check_agreement(case, cwd, timeout)
                    results.append((st, f"{label} [agreement]", detail))
                    mark, col = ((MARK_OK, GREEN) if st == "AGREED" else (MARK_BAD, RED))
                    say(f"{col}  {mark} [{idx}] {label} [agreement] - {detail}{OFF}")

            variant, reason = pick_unique(original, case["from"])
            if variant is None:
                results.append(("SKIP", label, reason))
                say(f"{RED}  {MARK_BAD} [{idx}] {label} - {reason}{OFF}")
                other_sides()
                continue

            # neutered replacement, in the same line-ending form as the match
            to_text = case["to"].replace("\r\n", "\n")
            if b"\r\n" in variant:
                to_text = to_text.replace("\n", "\r\n")
            broken = original.replace(variant, to_text.encode("utf-8"))
            if broken == original:
                results.append(("SKIP", label, "replacement changed nothing"))
                say(f"{RED}  {MARK_BAD} [{idx}] {label} - replacement changed nothing{OFF}")
                other_sides()
                continue

            write_bytes(path, broken)
            try:
                code, out = run_verify(verify, cwd, timeout)
            finally:
                restore_write(path, original)
                if sha(read_bytes(path)) != sha(original):
                    die(f"RESTORE FAILED for {path} - restore it from version control now!")

            if code == 0:
                results.append(("HOLLOW", label, "suite stayed green with the guard removed"))
                say(f"{RED}  {MARK_BAD} [{idx}] {label} - stayed GREEN without the guard{OFF}")
            elif case.get("expect_output") and case["expect_output"] not in out:
                snippet = case["expect_output"]
                results.append(("WRONG", label,
                                f"went red, but not on the expected assertion ({snippet!r})"))
                say(f"{RED}  {MARK_BAD} [{idx}] {label} - red, but expected output not found: "
                    f"{snippet!r}{OFF}")
            else:
                results.append(("RED", label, f"exit {code}"))
                say(f"{GREEN}  {MARK_OK} [{idx}] {label} - went red (exit {code}){OFF}")

            other_sides()
    finally:
        bad = restorer.restore_all()
        if bad:
            die("could not restore: " + ", ".join(bad) + " - restore from version control")

    if collect is not None:
        collect.extend(results)

    GOOD = {"RED", "CLEAN", "AGREED"}
    ran = [r for r in results]
    red_side = [r for r in ran if not r[1].endswith(("[green]", "[agreement]"))]
    green_side = [r for r in ran if r[1].endswith("[green]")]
    agree_side = [r for r in ran if r[1].endswith("[agreement]")]
    bad = [r for r in ran if r[0] not in GOOD]

    parts = [f"{len([r for r in red_side if r[0] == 'RED'])}/{len(red_side)} proven red"]
    if green_side:
        parts.append(f"{len([r for r in green_side if r[0] == 'CLEAN'])}/{len(green_side)} "
                     f"proven green")
    if agree_side:
        parts.append(f"{len([r for r in agree_side if r[0] == 'AGREED'])}/{len(agree_side)} "
                     f"agreements two-sided")
    say(f"\n{BOLD}{'  ·  '.join(parts)}{OFF}")

    for status, label, detail in bad:
        say(f"{RED}  {status:10} {label} - {detail}{OFF}")

    if any(r[0] in ("HOLLOW", "WRONG") for r in bad):
        say(f"\n{DIM}A guard that stays green when removed means one of two things:{OFF}\n"
            f"{DIM}  {BULLET} the assertion is hollow (it cannot distinguish the two outcomes), or{OFF}\n"
            f"{DIM}  {BULLET} the fixture accidentally passes either way.{OFF}")
    if any(r[0] == "ALWAYS-RED" for r in bad):
        say(f"\n{DIM}A guard that rejects a known-good artifact is as useless as one that{OFF}\n"
            f"{DIM}  never fires - and it hides longer, because refusing everything looks{OFF}\n"
            f"{DIM}  careful. Fix the guard, not the fixture.{OFF}")
    if any(r[0] in ("ONE-SIDED", "MISMATCH") for r in bad):
        say(f"\n{DIM}An agreement is only proven when BOTH halves are read from the built{OFF}\n"
            f"{DIM}  artifact. The half you assumed is the half that breaks.{OFF}")
    if not bad and not green_side and not agree_side:
        say(f"{DIM}  {BULLET} no green side declared on any case - you have proven these{OFF}\n"
            f"{DIM}    guards can fire, not that they can still say yes. Add \"green\" blocks.{OFF}")
    return 0 if ran and not bad else 1


# ── self-test: prove the tool is not blind ─────────────────────────────────────

SELFTEST_APP = '''\
STORE = []

def add_item(actor, tenant, text):
    if actor.get("tenant") != tenant:          # GUARD A - covered by the test
        raise PermissionError("cross-tenant write")
    if not text.strip():                       # GUARD B - NOT covered (hollow)
        raise ValueError("empty text")
    STORE.append((tenant, text))
    return len(STORE)
'''

SELFTEST_TEST = '''\
import sys
from app import add_item, STORE

fails = 0

def check(label, cond):
    global fails
    if not cond:
        print("FAIL:", label); fails += 1
    else:
        print("ok:", label)

try:
    add_item({"tenant": 1}, 2, "hello")
    check("cross-tenant write is refused", False)
except PermissionError:
    check("cross-tenant write is refused", True)

check("same-tenant write works", add_item({"tenant": 1}, 1, "hello") == 1)

# NOTE: nothing asserts the empty-text guard. That omission is the point of the self-test.

print("PASS" if fails == 0 else "FAILED")
sys.exit(1 if fails else 0)
'''

# --- fixtures for the OTHER side of the proof --------------------------------
# A guard that inspects a bundle. The healthy bundle has all three entries.
SELFTEST_CHECKER_OK = '''\
import sys
want = ["logo:a", "logo:b", "logo:c"]
text = open(sys.argv[1], encoding="utf-8").read()
found = [w for w in want if w in text]
print(f"{len(found)}/{len(want)} present")
sys.exit(0 if len(found) == len(want) else 1)
'''

# The same guard after the bug this feature exists for: it reads the wanted names
# through a broken boundary, so nothing ever matches - and it rejects a PERFECT
# bundle while looking careful. Removing it still turns a suite red, so the red
# side alone can never tell these two apart.
SELFTEST_CHECKER_ALWAYS_RED = '''\
import sys
want = ["logo:a", "logo:b", "logo:c"]
text = open(sys.argv[1], encoding="utf-8").read()
want = [w.encode("utf-8").decode("latin-1").replace("logo", "????") for w in want]
found = [w for w in want if w in text]
print(f"{len(found)}/{len(want)} present")
sys.exit(0 if len(found) == len(want) else 1)
'''

# Prints a file, so agreement sides can be read without shell-quoting games.
SELFTEST_SHOW = '''\
import sys
sys.stdout.write(open(sys.argv[1], encoding="utf-8").read())
'''


def selftest() -> int:
    print(f"{BOLD}redproof self-test{OFF} - proving the runner reports a hollow assertion\n")
    tmp = Path(tempfile.mkdtemp(prefix="redproof-selftest-"))
    try:
        (tmp / "app.py").write_text(SELFTEST_APP, encoding="utf-8")
        (tmp / "test_app.py").write_text(SELFTEST_TEST, encoding="utf-8")
        before = sha(read_bytes(tmp / "app.py"))

        spec = {
            "verify": f'"{sys.executable}" test_app.py',
            "cases": [
                {"label": "guard A (covered)",
                 "file": "app.py",
                 "from": 'if actor.get("tenant") != tenant:',
                 "to": "if False:"},
                {"label": "guard B (hollow - no assertion covers it)",
                 "file": "app.py",
                 "from": "if not text.strip():",
                 "to": "if False:"},
                {"label": "pattern that does not exist",
                 "file": "app.py",
                 "from": "if nonexistent_guard():",
                 "to": "if False:"},
            ],
        }
        spec_path = tmp / "spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")

        got: list = []
        rc = proof(spec_path, only=None, collect=got)
        by_label = {label: (status, detail) for status, label, detail in got}

        def outcome(prefix: str) -> tuple[str, str]:
            for label, pair in by_label.items():
                if label.startswith(prefix):
                    return pair
            return ("MISSING", "case did not run")

        a_status, _ = outcome("guard A")
        b_status, _ = outcome("guard B")
        c_status, c_detail = outcome("pattern that does not exist")

        after = sha(read_bytes(tmp / "app.py"))
        # Assert on WHICH case produced WHICH outcome. Asserting only on the exit code
        # would pass even if a single case misbehaved - the "which guard answered" trap.
        checks = [
            ("the covered guard is reported RED", a_status == "RED"),
            ("the uncovered guard is reported HOLLOW", b_status == "HOLLOW"),
            ("a missing pattern is reported SKIP, not silently ignored",
             c_status == "SKIP" and "not found" in c_detail),
            ("runner exits non-zero when a guard is not covered", rc != 0),
            ("source restored byte-for-byte", before == after),
        ]
        print()
        bad = 0
        for label, cond in checks:
            print(f"{GREEN}  {MARK_OK} {label}{OFF}" if cond else f"{RED}  {MARK_BAD} {label}{OFF}")
            bad += 0 if cond else 1

        # and the inverse: a spec whose only case is the covered guard must PASS
        spec["cases"] = [spec["cases"][0]]
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        rc_ok = proof(spec_path, only=None, quiet=True)
        cond = rc_ok == 0
        print(f"{GREEN}  {MARK_OK} runner exits 0 when every named guard is covered{OFF}" if cond
              else f"{RED}  {MARK_BAD} runner did not exit 0 on the all-covered spec{OFF}")
        bad += 0 if cond else 1

        # and a red baseline must abort: proving guards against an already-failing suite
        # would report every guard as "red" for the wrong reason.
        spec["verify"] = "exit 1"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        aborted = False
        try:
            proof(spec_path, only=None, quiet=True)
        except SystemExit as exc:
            aborted = exc.code == 2
        print(f"{GREEN}  {MARK_OK} a red baseline aborts the run{OFF}" if aborted
              else f"{RED}  {MARK_BAD} a red baseline did not abort the run{OFF}")
        bad += 0 if aborted else 1

        # a restore that silently does not take effect must be caught by the hash
        # check, not shrugged off. Without this case, removing that check leaves the
        # self-test green - the safety net would be the one untested thing here.
        spec["verify"] = f'"{sys.executable}" test_app.py'
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        os.environ["REDPROOF_FAULT"] = "silent_restore"
        caught = False
        try:
            proof(spec_path, only=None, quiet=True)
        except SystemExit as exc:
            caught = exc.code == 2
        finally:
            os.environ.pop("REDPROOF_FAULT", None)
        restored = sha(read_bytes(tmp / "app.py")) == before
        print(f"{GREEN}  {MARK_OK} a silent restore failure aborts, and the backstop "
              f"restores the file{OFF}" if caught and restored
              else f"{RED}  {MARK_BAD} silent restore failure: aborted={caught} "
                   f"restored={restored}{OFF}")
        bad += 0 if (caught and restored) else 1

        # ── the other side of the proof: a guard must also be able to say yes ──
        py = f'"{sys.executable}"'
        (tmp / "checker_ok.py").write_text(SELFTEST_CHECKER_OK, encoding="utf-8")
        (tmp / "checker_always_red.py").write_text(SELFTEST_CHECKER_ALWAYS_RED,
                                                   encoding="utf-8")
        (tmp / "show.py").write_text(SELFTEST_SHOW, encoding="utf-8")
        (tmp / "healthy_bundle.txt").write_text("logo:a\nlogo:b\nlogo:c\n", encoding="utf-8")

        base_case = {"label": "guard A (covered)", "file": "app.py",
                     "from": 'if actor.get("tenant") != tenant:', "to": "if False:"}

        last_rc = {"code": None}

        def run(cases: list, **extra) -> list:
            s = {"verify": f'{py} test_app.py', "cases": cases, **extra}
            spec_path.write_text(json.dumps(s), encoding="utf-8")
            got: list = []
            last_rc["code"] = proof(spec_path, only=None, quiet=True, collect=got)
            return got

        def status_of(entries: list, suffix: str) -> str:
            for st, lbl, _ in entries:
                if lbl.endswith(suffix):
                    return st
            return "MISSING"

        # ① a guard that passes the healthy bundle is CLEAN; the always-red one is caught
        got = run([{**base_case, "green": {
            "command": f'{py} checker_ok.py healthy_bundle.txt',
            "expect_output": "3/3 present"}}])
        healthy_ok = status_of(got, "[green]") == "CLEAN"

        got = run([{**base_case, "green": {
            "command": f'{py} checker_always_red.py healthy_bundle.txt'}}])
        always_red_caught = status_of(got, "[green]") == "ALWAYS-RED"
        # an ALWAYS-RED finding must also reach the exit code, not just the report
        always_red_fails_run = last_rc["code"] != 0

        # exit 0 alone is not proof the guard ran: demand the evidence it prints
        got = run([{**base_case, "green": {
            "command": f'{py} checker_ok.py healthy_bundle.txt',
            "expect_output": "37/37 present"}}])
        silent_pass_caught = status_of(got, "[green]") == "ALWAYS-RED"

        for label, cond in [
            ("a guard that passes a known-good artifact is reported CLEAN", healthy_ok),
            ("a guard that rejects a known-good artifact is reported ALWAYS-RED",
             always_red_caught),
            ("an ALWAYS-RED guard makes the whole run fail", always_red_fails_run),
            ("exit 0 without the expected evidence is not accepted as a pass",
             silent_pass_caught),
        ]:
            print(f"{GREEN}  {MARK_OK} {label}{OFF}" if cond
                  else f"{RED}  {MARK_BAD} {label}{OFF}")
            bad += 0 if cond else 1

        # a case with no green side is only a failure when require_green is on
        got = run([base_case])
        silent = not any(l.endswith("[green]") for _, l, _ in got)
        got = run([base_case], require_green=True)
        demanded = status_of(got, "[green]") == "NO-GREEN"
        for label, cond in [
            ("a missing green side is silent by default", silent),
            ("require_green turns a missing green side into a finding", demanded),
        ]:
            print(f"{GREEN}  {MARK_OK} {label}{OFF}" if cond
                  else f"{RED}  {MARK_BAD} {label}{OFF}")
            bad += 0 if cond else 1

        # ② both halves of an agreement must come from the artifact
        (tmp / "built_app.txt").write_text("SHA256: AA:BB:CC\n", encoding="utf-8")
        (tmp / "built_site.json").write_text(
            '{"relation":["delegate_permission"],"fingerprints":["AA:BB:CC"]}\n',
            encoding="utf-8")
        (tmp / "other_site.json").write_text('{"fingerprints":["ZZ:YY:XX"]}\n',
                                             encoding="utf-8")
        art = {"from": "artifact", "command": f'{py} show.py built_app.txt',
               "extract": r"SHA256: ([0-9A-F:]+)"}

        got = run([{**base_case, "agreement": {
            "name": "fingerprint handshake", "left": art,
            "right": {"from": "artifact", "command": f'{py} show.py built_site.json',
                      "extract": r"([0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2})"}}}])
        two_sided = status_of(got, "[agreement]") == "AGREED"

        got = run([{**base_case, "agreement": {
            "name": "fingerprint handshake", "left": art,
            "right": {"from": "assumed", "command": f'{py} show.py built_site.json'}}}])
        assumed_caught = status_of(got, "[agreement]") == "ONE-SIDED"

        got = run([{**base_case, "agreement": {
            "name": "fingerprint handshake", "left": art,
            "right": {"from": "artifact", "command": f'{py} show.py other_site.json',
                      "extract": r"([0-9A-Z]{2}:[0-9A-Z]{2}:[0-9A-Z]{2})"}}}])
        mismatch_caught = status_of(got, "[agreement]") == "MISMATCH"

        got = run([{**base_case, "agreement": {
            "name": "fingerprint handshake", "left": art,
            "right": {"from": "artifact", "command": f'{py} show.py built_site.json',
                      "extract": r"NOTHING_MATCHES_THIS_([0-9]+)"}}}])
        no_match_caught = status_of(got, "[agreement]") == "ONE-SIDED"

        # a side whose command succeeds but produces nothing at all: the artifact was
        # opened and had nothing to say, which is still an assumption
        (tmp / "empty_side.txt").write_text("   \n", encoding="utf-8")
        got = run([{**base_case, "agreement": {
            "name": "fingerprint handshake", "left": art,
            "right": {"from": "artifact", "command": f'{py} show.py empty_side.txt'}}}])
        empty_caught = status_of(got, "[agreement]") == "ONE-SIDED"

        for label, cond in [
            ("an agreement read from both artifacts is reported AGREED", two_sided),
            ("a side declared 'assumed' is rejected as ONE-SIDED", assumed_caught),
            ("two artifacts that disagree are reported MISMATCH", mismatch_caught),
            ("a side whose pattern matches nothing is not proven", no_match_caught),
            ("a side that reads an empty value counts as assumed, not proven", empty_caught),
        ]:
            print(f"{GREEN}  {MARK_OK} {label}{OFF}" if cond
                  else f"{RED}  {MARK_BAD} {label}{OFF}")
            bad += 0 if cond else 1

        print(f"\n{'self-test passed' if bad == 0 else 'SELF-TEST FAILED'}")
        return 0 if bad == 0 else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Red-proof each guard: remove it, require red.")
    ap.add_argument("spec", nargs="?", help="path to the JSON spec")
    ap.add_argument("--only", type=int, help="run a single case, 1-based")
    ap.add_argument("--list", action="store_true", help="list cases and exit")
    ap.add_argument("--selftest", action="store_true", help="prove the runner is not blind")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.spec:
        ap.print_help()
        return 2

    spec_path = Path(args.spec).resolve()
    if args.list:
        _, _, _, cases = load_spec(spec_path)
        for i, c in enumerate(cases, start=1):
            print(f"{i:3}  {c['label']}  ({c['file']})")
        return 0
    return proof(spec_path, args.only)


if __name__ == "__main__":
    sys.exit(main())
