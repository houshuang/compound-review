# Changelog

## 0.4.0 — 2026-06-16

Precision borrows from [diffray](https://github.com/strelov1/diffray), which converged on the
same multi-agent + validation design — three of its prompt-craft ideas, adapted.

- **Intentional-trade-off filtering in both verifiers.** A finding that complains about a
  deliberate decision is a false positive ("the problem is the solution to a different
  problem"). Verifiers now read commit messages (`git log <base>..HEAD`), code comments, and
  AGENTS.md/CLAUDE.md (incl. any "what NOT to flag" list); an acknowledged trade-off is
  refuted. Directly targets the corpus's weakest axis (codex-security precision ~46%).
- **Required `evidence` field** on every finding — the actual code lines (with file:line) that
  prove the issue, quoted not paraphrased. Raises finder precision and makes cross-family
  verification cheap (the verifier starts from the snippet). Persisted to the corpus
  (`finding.evidence`, auto-migrated).
- **Optional `verdict`/`confidence` fields** added to the schema. This also fixes a latent bug:
  the Codex verifier (`verify.md`) returns a `verdict`, which the strict `additionalProperties:
false` schema would previously have rejected. `confidence` (0-100) is available for finders
  but not required — the verdict + agreement system already covers conviction.

## 0.3.0 — 2026-06-16

The two highest-value improvements the 13-run analysis surfaced.

- **Codex JSON-parse retry.** `run-codex` intermittently (~7%, 1/15 rows) emitted unparseable
  output, silently dropping a whole reviewer (≈1 in 3 full runs lost a lens). It now retries
  once with a stricter "raw JSON only" instruction before recording failure; tokens accumulate
  across attempts and the contract records `attempts`.
- **Cross-family verification** (`codex-prompts/verify.md` + SKILL step 4). Findings are now
  verified by a _different_ model family than raised them — Claude findings → Codex verifier,
  Codex findings → Claude verifier. Fixes the in-family leniency the corpus exposed: through
  v0.2.2 all 9 refutes were on Codex findings, 0 on Claude's, because a Claude verifier judged
  Claude findings. This makes the precision numbers trustworthy.

## 0.2.2 — 2026-06-16

- **Failed reviewer rows (`ok=0`) no longer mislabel a reviewer as "partial" token capture.**
  codex-edge-cases failed one run (`could not parse JSON findings from codex output`) and its
  zero-token failure row was dragging the whole reviewer into "partial"; the completeness
  check now counts only successful rows, so find/Mtok is computed from real runs again.

## 0.2.1 — 2026-06-15

Honesty + signal pass after 3 more runs (11 total, ~270 findings). The 0.2.0 changes are
confirmed working in the wild: 12 refuted verdicts now exist (was 0), the severity-drift and
`blk✓` tables have real data, and `was_fixed` is accurate on every post-0.2.0 run.

- **Claude lenses now labeled `Max`/`flat`, not `n/a`/`partial`.** 11 runs proved in-session
  Claude usage is never recoverable, so analytics keys off `model_family='claude'` and shows
  these as flat-rate Max-plan rows (judge by finds/confirm/blk✓). Only Codex carries real
  marginal $. SKILL updated to stop instructing the futile token capture — `null` is correct.
- **New "Reviewer precision" analytics section**: confirm-vs-refute ratio among verified
  findings. First real precision signal — codex-security 50% (5/5), the rest 90–100%.
  (Caveat the corpus surfaces: all 12 refutes are on Codex findings, none on Claude's —
  possible same-family verifier leniency, since the verifier is also Claude.)

## 0.2.0 — 2026-06-11

Tuning pass driven by the first 8 runs of corpus data (~156 findings).

- **Carryover**: `init` now surfaces open confirmed/plausible findings from prior runs that
  touch files in the current diff (and returns them in the JSON), so follow-up branches stop
  re-discovering the same bugs. (Corpus: one hot file was flagged 24× across 4 runs.)
- **`finish` warns on open blocking findings** — fixes routinely landed after the run
  (one run had 3 confirmed highs fixed by later commits but never marked), so `finish`
  now lists them for mark-fixed/wontfix triage instead of silently closing.
- **`wontfix` verdict** (+ `set-verdict` choices validation). Refuted/wontfix locations are
  excluded from the blocking gate and shown in a "Dismissed" report section.
- **Adversarial verifier prompt** (`claude-prompts/verifier.md`) and SKILL now require
  recording a verdict — including REFUTED — for every blocking finding. First 7 runs:
  150 findings, 0 refuted = rubber-stamping; precision can't be measured without refutes.
- **thermo-nuclear retooled** (`claude-prompts/thermo-nuclear.md`): PR-level organization &
  logic critique only, max 3 findings, explicitly no line-level bugs. Corpus: 31 findings,
  5 confirmed — its value was the architectural calls, not the bug-shaped noise.
- **security-review (claude) demoted to conditional** (auth/logging/LLM-payload/tenant diffs
  only): 7 runs, 0 confirmed. codex-security stays unconditional — lowest volume but caught
  the corpus's only two privacy-contract blockers.
- **Scoped re-review lens switched to codex-edge-cases** (16.9 finds/Mtok, 19 confirmed vs
  codex-correctness 6.6/Mtok).
- **Analytics honesty**: per-reviewer table adds `blk✓` (confirmed blocker/high — severity-
  weighted value) and prints `n/a`/`partial` instead of fake find/Mtok when token capture is
  missing or incomplete. SKILL now forbids estimating Claude-lens tokens (null when unknown).
- Codex prompts gained a DO-NOT-FLAG triage-suppression block (mirrors the reviewer's
  global suppression list, which Codex can't see).

## 0.1.0 — 2026-06-08

Initial build. Validated on two real PR branches.

- Multi-model panel: Claude (code-review/thermo-nuclear/security) + Codex
  (correctness/security/edge-cases) + opt-in Gemini.
- Python harness owns durable state; `ingest` is the single DB writer; reviewers communicate
  via contract JSON files.
- SQLite findings corpus with per-reviewer token/cost capture (cache-aware) and `analytics`.
- Cross-reviewer agreement via dedup; **semantic dedup** through limbic embeddings with
  line-proximity fallback.
- **Effective-severity gating** (verifier-override + deterministic promote-on-confirm) so
  confirmed correctness/security bugs can't hide in the backlog when a finder under-rates them.
- Bounded autonomous fix→re-review loop (cap 3 rounds, blocking-only gate, scoped re-review).
- Optional limbic `cost_log` mirror to the central cross-project dashboard.
- Known limitation: Gemini CLI times out on diffs >~15–20KB → kept opt-in, small-diff-only.
