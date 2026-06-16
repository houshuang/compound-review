# compound-review

A multi-model code-review panel for Claude Code. It runs several **independent** reviewers
across **disjoint model families** (Claude + Codex + optionally Gemini), reconciles their
findings, logs everything to a SQLite corpus for later tuning, and can autonomously fix the
blocking findings in a **bounded** review → fix → re-review loop.

It is a personal Claude Code skill: invoke it in a session with `/compound-review` on the
branch/PR you want reviewed. `SKILL.md` is the agent-facing runbook; this README is the
human-facing "what it is and how it works."

## Install

Clone into your Claude Code skills directory, then invoke `/compound-review` in any repo:

```bash
git clone https://github.com/houshuang/compound-review.git ~/.claude/skills/compound-review
```

The findings corpus and run artifacts are written to `~/.claude/compound-review/` (outside the
skill dir), so your review data never lives in this repo. See **Requirements** below for the
Codex/Gemini CLIs and the optional `limbic` integration.

## Why a panel instead of one reviewer

Two empirical facts (from the LLM-review literature, and reproduced in this tool's own corpus)
drive the design:

1. **Reviewers don't converge on findings — they're additive.** ~85–90% of findings are caught
   by exactly one reviewer. So the value is _coverage_ from diverse lenses/families, not
   consensus. Bias-reduction comes from disjoint model families (Claude vs GPT vs Gemini),
   not the same model with N prompts.
2. **Iterating a fix→re-review loop past ~3 rounds _introduces_ bugs.** So the loop is bounded
   (cap 3), gates only on `blocker`/`high` (nits go to a backlog and never extend the loop),
   and re-reviews are _scoped_ ("are these prior blockers fixed, any new blocker?") rather than
   fresh full-recall scans (which always invent new nits).

## Architecture

A clean determinism split:

- **`compound_review.py` (the harness) owns everything durable** — git/PR context capture,
  running the Codex & Gemini CLIs with precise token capture, finding dedup, cross-reviewer
  agreement, and the SQLite corpus. Its `ingest` command is the **single DB writer**.
- **The session agent (driven by `SKILL.md`) owns judgment** — orchestrating the panel,
  synthesizing the plan, and applying fixes.

Every reviewer — Claude agent, Codex CLI, or Gemini CLI — drops one **contract JSON file**
(reviewer, model_family, tokens, findings[]) into the run's `raw/round-N/` dir. `ingest` reads
them all and reconciles. Adding a new reviewer is just "produce a contract file."

```
init ──► run reviewers (parallel) ──► ingest ──► synthesize plan ──► [fix → scoped re-review]×≤3 ──► finish
         (codex/gemini CLIs +         (dedup,                         (blocking findings only)
          Claude agents)               agreement,
                                        effective severity)
```

## Requirements

- **Python 3.10+** (stdlib only — no pip deps required).
- **Codex CLI** (`codex`) for the GPT lenses. On a ChatGPT-account login the `*-codex` models
  are rejected; the default resolves to `gpt-5.5` — control depth with `--effort xhigh`.
- _Optional:_ **Gemini CLI** (`gemini`) + `GEMINI_API_KEY` for a third family. See the Gemini
  caveat below — it's opt-in.
- _Optional:_ **[limbic](https://github.com/…/limbic)** importable — unlocks semantic finding
  dedup and central cost logging. Falls back gracefully when absent.

## Usage

In a Claude Code session, on the branch to review:

```
/compound-review
```

The skill drives the workflow. Under the hood it calls the harness directly; you can also run
the commands by hand:

```bash
HARNESS=~/.claude/skills/compound-review/compound_review.py
PROMPTS=~/.claude/skills/compound-review/codex-prompts

# 1. capture context + create the run
python3 $HARNESS init --base origin/main --effort full   # → run_id, run_dir

# 2. run reviewers in parallel (each writes a contract file)
python3 $HARNESS run-codex  --run-id $RID --run-dir $RD --name codex-correctness --prompt-file $PROMPTS/correctness.md
python3 $HARNESS run-codex  --run-id $RID --run-dir $RD --name codex-edge-cases  --prompt-file $PROMPTS/edge-cases.md
# (Claude lenses are spawned by the agent and written as contract files)

# 3. reconcile + report (dedup, agreement, effective-severity gating)
python3 $HARNESS ingest --run-id $RID --run-dir $RD --round 0

# 4. record verifier verdicts, mark fixes, finish
python3 $HARNESS set-verdict --run-id $RID --dedup-key "<key>" --verdict confirmed --severity high
python3 $HARNESS mark-fixed  --run-id $RID --dedup-key "<key>"
python3 $HARNESS finish      --run-id $RID --notes "…"

# anytime: cross-run tuning data
python3 $HARNESS analytics
```

## The findings corpus

SQLite at `~/.claude/compound-review/findings.db` (run artifacts under
`~/.claude/compound-review/runs/<run_id>/` — both **outside** this repo, so data is never
committed). Three tables: `review_run` (branch/PR/SHA/diff-size), `reviewer_run` (per-reviewer
tokens/cost/duration/num_findings), `finding` (file/line/category/severity/summary/verdict/
agreement/evidence/was_fixed).

Two ideas make the corpus trustworthy:

- **Dedup → agreement.** Findings are clustered into "same location." With limbic available this
  is **semantic** (embedding clusters of the summary text — merges the same bug across different
  lines, wording, and even different category labels); otherwise it's line-proximity clustering.
  `agreement_n` = how many distinct reviewers hit a cluster — multi-reviewer findings are your
  highest-confidence signal.
- **Effective severity decouples three things** that used to be conflated in one label: _who
  found it_, _is it real_ (verdict), and _how bad is it_ (impact). The loop gates on effective
  severity — a verifier's severity wins, and a deterministic **promote-on-confirm** floors any
  confirmed correctness/security finding at `high`, so a real bug a reviewer lowballed as
  "medium" can't hide in the backlog. The raw finder label is kept so you can _measure_ drift.

`analytics` surfaces: categories most flagged, per-reviewer productivity (findings + confirmed/
refuted + fresh tokens + cost + findings-per-Mtok), cross-reviewer agreement distribution,
single-reviewer %, severity drift per reviewer, and token/cost per run.

## Tuning knobs

- `prices.json` — per-model $/Mtok for cost estimates (cache-aware).
- `codex-prompts/*.md` — the differentiated Codex personas (correctness/security/edge-cases),
  plus `verify.md`, the Codex-side adversarial verifier used to check _Claude_ findings
  cross-family; add your own lenses freely.
- `claude-prompts/*.md` — the stored Claude personas: `thermo-nuclear.md` (PR-level
  organization & logic critique, max 3 findings, no line-level bugs) and `verifier.md`
  (Claude-side adversarial verifier used to check _Codex_ findings — refuting is a first-class
  outcome, recorded to the corpus). Verification is deliberately cross-family.
- `SEM_THRESHOLD` (default 0.62) and `PROXIMITY_LINES` (60) in `compound_review.py` — dedup
  sensitivity.
- Round cap (3) and the blocking/backlog split — encoded in `SKILL.md`'s design rules.

## Gemini caveat

The Gemini CLI hangs when the diff is piped via stdin (the harness embeds it in the prompt
instead) and has a ~50s latency floor + ~23k-token overhead per call, scaling ~7s/KB — so it
**times out (>240s) on any diff above ~15–20KB**. It is therefore **opt-in and small-diff-only**;
on a normal PR, the Codex lenses already provide a non-Claude family. The harness handles a
Gemini timeout gracefully (failed contract, doesn't block the panel).

## limbic integration (optional)

When `~/src/limbic` is importable the harness uses two pieces, and degrades gracefully without
them:

- `amygdala.EmbeddingModel` + `greedy_centroid_cluster` → **semantic finding dedup**.
- `cerebellum.cost_log` → **central cost mirror**, so compound-review spend shows in the
  cross-project cost dashboard alongside everything else.
