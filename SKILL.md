---
name: compound-review
description: "Run a multi-model code-review panel (Claude + Codex + Gemini lenses) over the current branch/PR, dedup and cross-reference findings, log everything to a SQLite findings corpus for later tuning, then autonomously fix blocking findings in a bounded review→fix→re-review loop. Use for 'compound review', 'panel review', 'deep multi-agent review', 'review and fix this branch', or when the user wants several independent reviewers reconciled with token/cost tracking."
---

# Compound Review

A divergence/convergence review harness. It runs several **independent** reviewers across
**disjoint model families** (the bias-reduction lever), reconciles their findings, persists
everything for analysis, then fixes blockers in a **bounded** loop that is designed NOT to
chase nits forever.

The Python harness `compound_review.py` owns everything durable (git/PR context, running the
Codex & Gemini CLIs with token capture, dedup, agreement, the SQLite corpus). You (the agent)
own orchestration, synthesis, and fixing. **The harness's `ingest` is the only DB writer** —
every reviewer just drops a contract JSON file in the round's `raw/` dir.

`HARNESS=~/.claude/skills/compound-review/compound_review.py`
`PROMPTS=~/.claude/skills/compound-review/codex-prompts`

## Design rules (read before running)

- **Two finding classes.** `blocker`/`high` → loop on these. Everything else → backlog, **never gate the loop**. This is what stops the loop from running forever on nits (the documented failure mode of iterative LLM review).
- **Bounded rounds.** Hard cap of **3** fix→re-review rounds. Iterating past ~3 measurably *introduces* new bugs (Toth et al. 2025) — stop and hand back even if findings remain.
- **Scoped re-review, not fresh recall.** After fixing, re-review asks only *"are these specific prior blockers resolved, and did the fix introduce a new blocker?"* — never a fresh full-recall scan (which always invents new nits).
- **Convergence ≠ agreement.** Expect ~90% of findings to come from a single reviewer; the panel is **additive for coverage**, not a consensus machine. Stop on *no-new-blocker*, not on reviewer agreement.
- **Verify by evidence.** Prefer running the new regression test over re-prompting; more LLM reasoning increases false rejections.

## The panel (full lens, 3 families)

| Reviewer | Family | How it runs | Lens |
|----------|--------|-------------|------|
| `code-review-high` | claude | `/code-review high` skill (in-session) | correctness, recall-biased, verified |
| `thermo-nuclear` | claude | `code-reviewer` agent w/ thermo-nuclear prompt | structure / simplification |
| `security-review` | claude | `/security-review` skill or a security agent | security / privacy / contracts |
| `codex-correctness` | gpt | `run-codex` CLI (cheap tokens → use aggressively) | correctness |
| `codex-security` | gpt | `run-codex` CLI | security / privacy |
| `codex-edge-cases` | gpt | `run-codex` CLI | adversarial edge cases |
| `gemini-correctness` | gemini | `run-gemini` CLI — **opt-in, small diffs only** | cross-family correctness |

Codex is the cheapest high-quality lens on the user's plan — run **multiple differentiated
Codex passes** (correctness/security/edge-cases) by default.

**Gemini is opt-in, not default.** The gemini CLI has a ~50s latency floor + ~23k-token
overhead per call and scales ~7s/KB of prompt, so it **times out (>240s) on any diff above
~15-20KB** (measured: 7KB→50s ok, 50KB/192KB→timeout; 4/5 real attempts failed). Only invoke
`run-gemini` when the diff is small (single file / <15KB) and you specifically want the third
model family. On a normal-size PR, skip it — `run-codex` already gives a non-Claude lens.

## Workflow

### 1. Init
```bash
cd <repo>; python3 $HARNESS init --base origin/main --effort full
```
Capture `run_id`, `run_dir`, `raw_round0` from the JSON. (Auto-captures branch, base/head SHA, PR number, diff size.)

### 2. Diverge — run the panel in parallel (round 0)
Launch ALL reviewers concurrently. Three mechanisms:

- **Codex passes** — one Bash call each (run in background; they take 2–5 min):
  ```bash
  python3 $HARNESS run-codex --run-id $RID --run-dir $RD --name codex-correctness  --prompt-file $PROMPTS/correctness.md --base origin/main
  python3 $HARNESS run-codex --run-id $RID --run-dir $RD --name codex-security     --prompt-file $PROMPTS/security.md    --base origin/main
  python3 $HARNESS run-codex --run-id $RID --run-dir $RD --name codex-edge-cases   --prompt-file $PROMPTS/edge-cases.md  --base origin/main
  ```
- **Gemini pass — OPT-IN, only if the diff is small (<15KB)** — needs `GEMINI_API_KEY` exported. Skip on normal PRs (it times out; see panel note):
  ```bash
  python3 $HARNESS run-gemini --run-id $RID --run-dir $RD --name gemini-correctness --prompt-file $PROMPTS/correctness.md --base origin/main
  ```
- **Claude lenses** — spawn via the Agent/Skill tools (`/code-review high`, a `code-reviewer` agent with the thermo-nuclear prompt, `/security-review`). When each returns, **write its findings as a contract file** to `$RD/raw/round-0/<reviewer>.json` using the schema in this skill's `compound_review.py` docstring (reviewer, model_family:"claude", model, tokens from the agent result's usage, findings[]). Map each finding's severity/category to the controlled vocab; include a `verdict` if the reviewer verified it.

Codex/Gemini write their own contract files. You only hand-write the Claude ones.

### 3. Synthesize
```bash
python3 $HARNESS ingest --run-id $RID --run-dir $RD --round 0
```
This dedups by `file:linebucket:category`, computes `agreement_n` (how many reviewers hit each location), persists rows, and prints a report split into **Blocking** (loop targets) and **Backlog**. Present the user a prioritized plan: blockers first (highlight `x2`/`x3` agreement — multi-reviewer findings are the highest-confidence), backlog collected but not gating.

### 4. Verify blockers (optional but recommended)
For each blocking finding not already `confirmed`, spawn one verifier agent (recall-biased: CONFIRMED/PLAUSIBLE/REFUTED). Record verdicts:
```bash
python3 $HARNESS set-verdict --run-id $RID --dedup-key "<key>" --verdict confirmed
```
Drop REFUTED from the fix list.

### 5. Autonomous fix → re-review loop (bounded to 3 rounds)
For round N = 1..3:
1. Fix every confirmed/plausible **blocker** (and obviously-safe backlog items if cheap). Make minimal, targeted edits + add the regression test the reviewer suggested. Prefer the structural root-cause fix when one reviewer identified it (it often deletes a class of findings).
2. Run typecheck/tests for the touched packages. If a fix regresses, revert that fix (revert-on-regression), don't pile on.
3. **Scoped re-review**: re-run only `codex-correctness` (and `codex-edge-cases` if the fix was subtle) on round N, plus a single Claude agent asked *only*: "Are these specific prior blockers now resolved, and did these diffs introduce any new blocker/high issue?" Write contract files to `raw/round-N/`, then `ingest --round N`.
4. `mark-fixed` each resolved finding. **Stop if** the scoped re-review found no new `blocker`/`high`, OR N==3.

### 6. Finish
```bash
python3 $HARNESS mark-fixed --run-id $RID --dedup-key "<key>"   # per resolved finding
python3 $HARNESS finish --run-id $RID --notes "..."
```
Summarize to the user: what was fixed, what's in the backlog, total tokens/cost per reviewer (from the report), and which reviewer uniquely caught each confirmed blocker.

## Analyzing the corpus (the payoff)
```bash
python3 $HARNESS analytics
```
Shows: categories most flagged, per-reviewer productivity (findings + confirmed/refuted + tokens + cost + findings-per-Mtok), cross-reviewer agreement distribution, single-reviewer %, and token/cost per run. Use this over time to drop low-value reviewers, tune prompts toward categories that recur unfixed, and keep the panel inside a token budget.

## Reuse from limbic (auto-detected, optional)

The harness uses two pieces of `~/src/limbic` when importable, and degrades gracefully when not:

- **Semantic finding dedup** (`limbic.amygdala.EmbeddingModel` + `greedy_centroid_cluster`) — findings are clustered by *meaning of their summary*, so the same bug merges across different lines, wording, and even different category labels (proximity clustering can't do this). Falls back to line-proximity clustering when limbic is absent. Tune `SEM_THRESHOLD` (default 0.62) in `compound_review.py`.
- **Central cost mirror** (`limbic.cerebellum.cost_log`) — every reviewer's spend is also logged to limbic's cross-project `llm_costs.db`, so compound-review shows up in the existing dashboard (`python -m limbic.cerebellum.cost_log dashboard`).

(`review-tool` was surveyed too — it's a single-model PR-walkthrough UI; its `public/walkthroughs/*.json` are a possible eval corpus but there's no orchestration/dedup/cost logic worth importing.)

## Tuning notes
- **Budget:** Codex is cheap on the user's plan — bias spend there. Gemini's per-call overhead is high; keep it to one pass unless analytics show it uniquely catches confirmed bugs.
- **Codex customization:** Codex reads `.agents/skills/` and `AGENTS.md`; for stable personas you can later define `.codex/agents/*.toml` with `developer_instructions` + per-agent model/effort instead of the prompt files here.
- **Model pinning:** on a ChatGPT-account Codex login, `*-codex` models 400; default resolves to `gpt-5.5`. Control quality via `--effort xhigh`. Pin specific models only with an API-key login.
- Edit `prices.json` to keep cost estimates accurate; edit the codex prompt files / add new lenses freely.
