# Publishing & discoverability

How this skill gets found, and the exact steps to publish it. Discovery in the skills
ecosystem has three legs; each has a concrete action here.

## How discovery actually works

1. **`npx skills find <keywords>`** searches the [skills.sh](https://skills.sh/) registry
   by keyword and ranks by **install count**. Skills are indexed from public GitHub repos
   and installed with `npx skills add ghorbanies/workflow-design`.
2. **Agent-side verification**: assistants recommending skills check install count
   (1K+ preferred), **GitHub stars**, and source reputation before suggesting one.
3. **Curated lists** (`travisvn/awesome-claude-skills`, `ComposioHQ/awesome-claude-skills`)
   and marketplaces — many searches start there, and list placement seeds the first
   installs that the ranking then compounds.

Keyword surface is handled in this package (frontmatter description, README first
paragraph, reference filenames). The rest requires publishing.

## Step 1 — publish the repo

- Create a public GitHub repo. Either the skill *is* the repo root, or use a
  `skills/workflow-design/` monorepo layout (both are installable; the flat root gives
  the shorter install command).
- Copy this directory in, keeping the structure (`SKILL.md`, `references/`, `scripts/`,
  `examples/`, `evals/`).
- Before the first push, run the release gate below.
- Set the repo **description** to the first sentence of the README, and add **topics**:
  `claude-skill`, `agent-skills`, `workflow`, `state-machine`, `approval-workflow`,
  `process-mining`, `audit-trail`, `sla`, `human-in-the-loop`, `testing`.
- Install command in README.md points at `ghorbanies/workflow-design` (done).
- Tag `v1.0.0` and collapse the CHANGELOG's unreleased sections into it.

## Step 2 — get listed

Submit to the curated lists (each is a small PR; suggested entry line below) and to
skills.sh if the repo is not picked up automatically.

Suggested entry line:

> **workflow-design** — Design, prove, measure, and safely change workflows (approval
> chains, ticketing, human gates over AI output). Ships four stdlib-only tools: a flow
> linter, a guard red-proof runner, transition-log metrics, and log-vs-model conformance
> checking — each self-tested and red-proofed by shipped specs.

## Step 3 — the trust signals nobody can fake

Install count and stars come from being genuinely useful and provably honest. This
package's differentiators, worth stating wherever it is presented:

- **First mover**: at the time of writing, no skill in the public indexes covers
  workflow/state-machine design, workflow test auditing, or transition-log metrics.
- **Self-applied discipline**: every tool has `--selftest`, and every tool is red-proofed
  by a shipped spec — including the red-proof runner itself, which found a hollow
  assertion in its own safety net on first run (documented in `references/proving.md`).
- **Shipped evals**: `evals/scenarios.json` includes a negative control, and results are
  recorded per release in the CHANGELOG.

## Release gate (run before every push)

```bash
python scripts/flowcheck.py --selftest && \
python scripts/redproof.py --selftest && \
python scripts/dynamics.py --selftest && \
python scripts/conformance.py --selftest && \
python scripts/redproof.py scripts/selfproof.spec.json && \
python scripts/redproof.py scripts/flowcheck.selfproof.spec.json && \
python scripts/redproof.py scripts/dynamics.selfproof.spec.json && \
python scripts/redproof.py scripts/conformance.selfproof.spec.json
```

Plus: re-run `evals/scenarios.json` on a fresh instance (see `evals/EVALS.md`) and record
the result in the CHANGELOG. No green gate, no push.
