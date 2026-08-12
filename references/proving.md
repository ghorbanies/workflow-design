# Phase 2 — Proving the tests are not hollow

## Contents
1. The harness canary (self-test before the first real assertion; blind-spot and encoding canaries)
2. Red proof (remove the guard, require red, restore; neutering patterns; line endings)
3. "Which guard answered?" (two layers, one status code)
4. Fixtures that accidentally pass
5. Coverage tests (enumerate from disk; reasoned allowlist)
6. "I fixed it" ≠ "I proved the fix changed anything"
7. The mid-fix trap
8. Verify in a real client
9. Three test classes workflows almost always lack
10. Never prove a write path against live data
11. Mechanical edits across files
12. Proving checklist

Every pattern here exists because a suite reported "all green" while measuring nothing.
A hollow test is worse than no test: it consumes the attention that would have found the bug.

The unifying question is never *"did the assertion pass?"* but **"could this assertion have
failed?"**

---

## 1. The harness canary — self-test before the first real assertion

Symptom it prevents: the harness cannot see the thing it is asserting on. A response body
that never reaches the shell, an output variable swallowed by a subshell, an element lookup
that always returns empty. Every assertion then passes against an empty string.

Shape (pseudocode — the mechanism transfers to any runner):

```
def canary():
    p0, f0 = passes, fails
    response = do_request("GET", "status")          # the SAME request function
    assert_nonempty(response.code, response.body)   # harness plumbing works at all
    check_equals("canary", 999, response.code)      # must fail
    check_contains("canary", "string_that_cannot_exist")   # must fail
    got_f, got_p = fails - f0, passes - p0
    passes, fails = p0, f0                          # roll back the counters
    if got_f != 2 or got_p != 0:
        die("harness is blind: canary did not go red")
```

Three properties, all required:

1. **It goes through the same functions as the real assertions.** If the canary has one line
   of code that is not on the main path, it is not a self-test. The cheapest way to guarantee
   this is to make the check a function and call it from both places.
2. **It asserts the exact failure count**, not "at least one".
3. **It rolls back the counters** so the canary does not pollute the summary.

### Blind-spot canaries

Where you *know* a layer cannot see something, write it as an executable assertion:

```
result_a = scan_layer_a()        # static: reads text
result_b = scan_layer_b()        # behavioral: sends requests

expect_found(result_a, direct_canary)     # both layers must catch this
expect_found(result_b, direct_canary)
expect_missing(result_a, indirect_canary) # layer A is KNOWN blind here — assert it
expect_found(result_b, indirect_canary)   # …which is the entire reason layer B exists
```

`expect_missing` looks strange and is the most valuable line in the file: the day someone
"optimizes" layer A into seeing it, the suite goes red and demands the assumption be
revisited. A comment saying "layer A can't see indirect writes" protects nothing.

### Encoding canary

If any assertion depends on non-ASCII text, prove the text survives the round trip *inside*
the system and print only ASCII:

```
stored = query_scalar("SELECT name FROM items WHERE id=?", id)
print("utf8-ok" if stored == expected_text else "utf8-lost")
```

Without this, a toolchain that mangles non-ASCII arguments makes every text-dependent
assertion vacuously true — the stored value is garbage and the comparison is garbage-to-garbage.

---

## 2. Red proof — remove the guard, require red, restore

A guard nobody has watched fail is untested. The procedure, per guard:

1. **Commit first.** The tool rewrites source files.
2. Locate the guard by a pattern that matches **exactly once** (count matches; abort on 0 or 2+).
3. Replace it with a neutered version (`if (false)`, dropped filter, widened query).
4. Run the suite. It must exit non-zero **and** the failing assertion must be the one you
   expect — not some unrelated collapse.
5. Restore the file byte-for-byte and verify by hash.
6. Repeat. Report any guard whose removal left the suite green: that assertion is hollow.

Use [`../scripts/redproof.py`](../scripts/redproof.py) rather than hand-rolling it; it
implements the guards above (unique match, line-ending variants, hash-verified restore,
restore-on-crash, baseline-must-be-green).

The runner is itself red-proofed by [`../scripts/selfproof.spec.json`](../scripts/selfproof.spec.json)
— worth reading as a worked example, and worth noting how it was earned: on its first run,
five of the runner's six guards went red and one stayed green. The uncovered one was the
hash verification after restore — the tool's only safety net was the only untested thing in
it. That is the normal result of a first red proof, not an unusual one.

### Neutering patterns that keep the code runnable

| Guard kind | Neutered form |
|---|---|
| Validation throw | `if (false) throw …` |
| Required-field list | drop the field from the list |
| Scope filter in a query | `WHERE (? IS NOT NULL)` — keeps the parameter count valid |
| Stored value | write `null` / a constant instead |
| Recipient filter | call the "all recipients" overload |
| Whole line | replace with empty string |

Keeping arity and syntax valid matters: a neutered file that fails to parse makes *every*
assertion red, which proves nothing about the specific guard.

### Line endings

On mixed-ending files a literal pattern silently fails to match, and the tool reports
"pattern not found" — or worse, an older tool reports success having changed nothing. Try both
`\n` and `\r\n` variants of the pattern and require exactly one to match uniquely.

---

## 3. "Which guard answered?"

The most repeated hollow-assertion class in gated flows.

Two layers protect a route: a session check and a capability check. Both return the same
status code. An assertion that only reads the code passes when either one fires — so removing
the capability check leaves the suite green, and the red proof of that guard fails to go red.

Fixes, in order of preference:

1. **Distinct error identifiers per layer** (`err:no_session` vs `err:no_capability`), asserted
   explicitly. Cheapest and works everywhere.
2. **Assert the side effect**, not the response: after a blocked attempt, assert the row count
   is unchanged and no log row was written.
3. **Set up the state so only one layer can fire**: a fully authenticated actor who lacks only
   the capability isolates the second layer.

The general rule: **an assertion that reads only a value two different code paths produce
carries no information about which path ran.**

---

## 4. Fixtures that accidentally pass

If the fixture's natural insertion order equals the correct sort order, `ORDER BY id` also
passes your ordering test. If every fixture item is in the same tenant, no isolation assertion
can fail. If the only rejected item is also the oldest, "rejections sort last" and "oldest
sorts last" are indistinguishable.

Build fixtures hostile to the trivial implementation:

- Insert in an order that is **wrong** for every dimension you assert on.
- Include at least one item per class, plus one that qualifies under two rules at once.
- Include a neighbor that must **not** appear (other tenant, other actor, terminal state).

Detection: a fixture defect is invisible to the suite and visible to the red proof — the guard
disappears and the assertion stays green. Treat every "removed the guard, still green" result
as *either* a hollow assertion *or* a complicit fixture.

---

## 5. Coverage tests — the only shape resistant to forgetting

Ordinary tests are positive: "this route rejects an anonymous request." None of them ask
"is there a route with no guard at all?" That blind spot ships new unguarded routes forever,
because nobody wrote an assertion about a route that did not exist yet.

A coverage test **builds its list from disk** (or from the router table, the handler registry,
the migration folder — whatever the real enumeration is) and requires each entry to either:

- carry a guard marker, or
- appear in an allowlist **with a one-line reason**.

Three extra assertions keep the allowlist honest:

1. **Staleness:** every allowlist entry still exists. Dead entries turn the list into a
   permanent excuse.
2. **Category integrity:** entries excused as "library, not an entry point" are still required
   by something. The day one becomes reachable on its own, the excuse expires.
3. **Two layers, and prove the second earns its place** (see the blind-spot canary above).
   A static layer reads text and cannot see indirect writes; a behavioral layer sends real
   requests and does not care how the write is spelled. If the behavioral layer also filters
   by the static layer's text pattern, the two layers are one layer wearing a hat.

---

## 6. "I fixed it" ≠ "I proved the fix changed anything"

Green after a fix carries **zero information** — it was green before the fix too.

The proof is a two-run comparison on the *same real input*:

```bash
git show HEAD:path/to/test > /tmp/test_before
run /tmp/test_before   <real input>   # must MISS the defect (exit 0, no mention)
run path/to/test       <real input>   # must CATCH it
```

If the old test also catches it, your fix was not to the test — find out what actually
changed. If the new test also misses it on real input, the fix is theater.

---

## 7. The mid-fix trap

**The highest risk of writing a hollow test is the moment you are fixing another test.**
Attention is on the old defect; the new assertion is written on autopilot and never seen red.
Observed repeatedly: hollow assertions were born, almost without exception, while their author
was correcting something else.

Rule: **any assertion born during a fix gets seen red on its own before commit** — neuter its
guard once, watch it go red, restore. Sixty seconds, at the exact moment you have the least
patience for it.

---

## 8. Verify in a real client

A flow that renders gets opened in a real browser (or the real client, CLI, or device) before
it is called done. Defects that hundreds of server-side assertions missed have been exposed by
one page load: an input the server never receives because the control does not submit, a
keypad emitting digits in a script the server does not parse, a page that renders blank
because an earlier construct broke the parser.

Operational rules:

- **Never start a preview server on a port anything real is using.** Pick a dedicated port and
  check it is free first.
- **Shut it down when the inspection ends**, and restore any launch config byte-for-byte.
- Assert what you saw, not what you expected to see.

---

## 9. Three test classes workflows almost always lack

1. **Actor A on actor B's item** — most suites test *tenant* isolation and never per-actor
   authorization inside a tenant. Approval flows are exactly where that matters.
2. **One number on two screens** — the summary tile and the list it summarizes; the export and
   the dashboard. Each is "correct" alone; only the comparison finds the divergence.
3. **Wrong-typed input to every write endpoint** — strings where numbers are expected, empty
   strings that pass a "field present" check, nulls, oversized payloads. Empty-string-passes-
   presence-check is the single most common one, and it produces junk rows in production.

---

## 10. Never prove a write path against live data

Verification against production is **read-only**: `GET` / `HEAD` and nothing else.

There is no "just one test POST to see whether the guard holds" — if the guard is broken, the
test *is* the breach, and the worst case and the experiment happen simultaneously. Test writes
against a disposable database or a throwaway tenant. If something can only be proven in
production, **write it down as unproven** and say why. An honest unproven line is worth more
than a green check that cost real data.

The same applies to destructive flows in general: password resets, invalidation, bulk state
changes. Run them where a mistake costs nothing.

---

## 11. Mechanical edits across files

Pattern-replacing across multiple files is how red proofs and refactors work, and it is how
source trees get emptied. Rules:

- Write it as a **script file**, never a shell one-liner.
- **Commit before running it.**
- Guard the output: measure length before writing; abort if the result is empty or suddenly
  much smaller than the input.
- A migration whose errors are swallowed by `try/except` **fails silently** — prove it worked
  by running it and reading the result, not by reading the code.

---

## 12. Proving checklist

- [ ] Harness canary runs before the first real assertion, via the same code path.
- [ ] Canary asserts an exact failure count and rolls back counters.
- [ ] Known blind spots asserted with `expect_missing`, not comments.
- [ ] Encoding canary if any assertion depends on non-ASCII text.
- [ ] Every guard red-proofed; files restored and hash-verified.
- [ ] Every "still green after removal" result triaged as hollow assertion or complicit fixture.
- [ ] Assertions distinguish *which* layer answered.
- [ ] Fixtures are hostile to the trivial implementation.
- [ ] Coverage test enumerates from disk, with a reasoned and staleness-checked allowlist.
- [ ] Fixes proven against the previous version of the test on real input.
- [ ] Every assertion born mid-fix seen red separately before commit.
- [ ] Anything visible opened in a real client; preview server on a dedicated port and stopped.
- [ ] Cross-actor, cross-screen, and bad-type tests exist.
- [ ] No write-path verification against live data; unprovable things written down as unproven.
