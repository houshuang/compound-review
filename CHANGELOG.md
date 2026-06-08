# Changelog

## 0.1.0 — 2026-06-08

Initial build. Validated on PR #5349 (deferred-tool-loading) and a second branch.

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
