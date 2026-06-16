#!/usr/bin/env python3
"""compound-review harness: orchestration-adjacent persistence for multi-reviewer code review.

The session agent (Claude Code) orchestrates the panel; THIS tool owns everything durable:
git/PR context capture, running the Codex CLI reviewers (with precise token capture),
deduplication + cross-reviewer agreement, and the SQLite findings corpus we mine later.

Single-writer rule: `ingest` is the ONLY command that writes reviewer_run/finding rows.
Every reviewer (Codex passes, Claude agents, Gemini agent) drops a "contract" JSON file in
the round's raw/ dir; ingest reads them all, normalizes, dedups, and persists.

Contract file shape (one per reviewer per round), written to <run_dir>/raw/round-<N>/<name>.json:
{
  "reviewer":     "code-review-high",        # unique label for this lens
  "model_family": "claude|gpt|gemini",
  "model":        "opus-4.8",                 # optional
  "effort":       "high",                     # optional
  "ok":           true,                        # false if the reviewer failed
  "error":        null,                        # error string if ok=false
  "duration_ms":  123456,                      # optional
  "tokens":       {"input":0,"cached_input":0,"output":0,"total":0},  # any subset
  "cost_usd":     null,                        # optional; computed from prices.json if absent
  "findings": [
    {"file":"a.ts","line":883,"severity":"blocker","category":"correctness",
     "summary":"...","rationale":"...","suggested_fix":"...","verdict":"confirmed"}
  ]
}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.path.expanduser("~/.claude/compound-review"))
DB_PATH = HOME / "findings.db"
RUNS_DIR = HOME / "runs"
SKILL_DIR = Path(__file__).resolve().parent
PRICES_PATH = SKILL_DIR / "prices.json"
SCHEMA_PATH = SKILL_DIR / "schema" / "finding-schema.json"

# ---------------------------------------------------------------------------
# Normalization vocab — keep small and stable so analytics group cleanly.
# ---------------------------------------------------------------------------
SEVERITY_MAP = {
    "blocker": "blocker", "critical": "blocker", "crit": "blocker",
    "high": "high", "major": "high",
    "medium": "medium", "moderate": "medium", "med": "medium",
    "low": "low", "minor": "low",
    "nit": "nit", "style": "nit", "cosmetic": "nit", "info": "nit", "trivial": "nit",
}
SEVERITY_RANK = {"blocker": 0, "high": 1, "medium": 2, "low": 3, "nit": 4, "unknown": 5}
RANK_SEVERITY = {v: k for k, v in SEVERITY_RANK.items()}
# Categories where a *confirmed* finding is load-bearing enough to gate the loop
# regardless of the finder's drive-by severity label.
IMPACT_CATEGORIES = {"correctness", "security"}


def most_severe(sevs) -> str:
    ranks = [SEVERITY_RANK.get(s, 5) for s in sevs if s]
    return RANK_SEVERITY[min(ranks)] if ranks else "unknown"


def effective_severity(group: list) -> str:
    """Gating severity for a deduped location. Decoupled from the finder's label:
    a verifier's severity wins; else the most-severe finder label; and a confirmed
    correctness/security finding is floored at 'high' (the severity-drift backstop)."""
    verified = [g["verified_severity"] for g in group if g.get("verified_severity")]
    if verified:
        return most_severe(verified)
    base = most_severe([g["severity"] for g in group])
    confirmed = any((g.get("verdict") or "") == "confirmed" for g in group)
    cats = {g.get("category") for g in group}
    if confirmed and (cats & IMPACT_CATEGORIES) and SEVERITY_RANK.get(base, 5) > SEVERITY_RANK["high"]:
        return "high"
    return base
CATEGORY_MAP = {
    "correctness": "correctness", "bug": "correctness", "logic": "correctness",
    "security": "security", "privacy": "security", "auth": "security",
    "performance": "performance", "perf": "performance", "efficiency": "performance",
    "structure": "structure", "maintainability": "structure", "design": "structure",
    "abstraction": "structure", "simplification": "structure", "altitude": "structure",
    "reuse": "structure", "duplication": "structure",
    "style": "style", "naming": "style", "formatting": "style",
    "test": "testing", "testing": "testing", "coverage": "testing",
    "types": "types", "type": "types", "typing": "types",
    "docs": "docs", "documentation": "docs",
}
# Severities we loop on in autonomous mode; everything else is backlog-only.
BLOCKING = {"blocker", "high"}
LINE_BUCKET = 10  # legacy; superseded by proximity clustering
PROXIMITY_LINES = 60  # findings in the same file+category within this line gap = "same location"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_sev(s: str | None) -> str:
    return SEVERITY_MAP.get((s or "").strip().lower(), "unknown")


def norm_cat(c: str | None) -> str:
    c = (c or "").strip().lower()
    return CATEGORY_MAP.get(c, c or "uncategorized")


_HEADING_NAME_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
_HEADING_KW_RE = re.compile(
    r"\b(?:func|function|def|class|interface|type|enum|struct)\s+([A-Za-z_$][\w$]*)")


def _name_from_heading(heading: str) -> str | None:
    """Extract a function/type name from a git diff hunk section heading."""
    h = heading.strip()
    if not h:
        return None
    m = _HEADING_KW_RE.search(h)
    if m:
        return m.group(1)
    m = _HEADING_NAME_RE.search(h)  # identifier immediately before '(' — the callee/decl name
    return m.group(1) if m else None


def build_symbol_index(repo_root: str, base: str) -> dict[str, list[tuple[int, str]]]:
    """Map each changed file to a sorted list of (new_line_start, enclosing_symbol), derived
    from git's own language-aware diff hunk headings (`@@ -a,b +c,d @@ <enclosing fn>`).
    Reliable across TS/Go/Py because git computes the heading, not us."""
    out = git(["diff", f"{base}...HEAD", "--unified=0"], repo_root)
    idx: dict[str, list[tuple[int, str]]] = {}
    cur = None
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")
    for ln in out.splitlines():
        if ln.startswith("+++ b/"):
            cur = ln[6:]
            idx.setdefault(cur, [])
        elif ln.startswith("@@") and cur is not None:
            m = hunk_re.match(ln)
            if m:
                name = _name_from_heading(m.group(2))
                if name:
                    idx[cur].append((int(m.group(1)), name))
    for k in idx:
        idx[k].sort()
    return idx


def enclosing_symbol(index: dict, relpath: str, line: int | None) -> str | None:
    """Symbol whose hunk region contains `line`: the last heading at or above the line."""
    if not relpath or not line:
        return None
    hunks = index.get(relpath) or []
    name = None
    for start, sym in hunks:
        if start <= line:
            name = sym
        else:
            break
    return name


SEM_THRESHOLD = 0.62  # cosine threshold for "same finding" when limbic embeddings are available


def _proximity_keys(pending: list) -> dict:
    """Fallback dedup: cluster findings in the same file+category by line proximity."""
    from collections import defaultdict
    groups: dict[tuple, list[int]] = defaultdict(list)
    for j, p in enumerate(pending):
        groups[(os.path.basename(p["file"]), p["category"])].append(j)
    keys = {}
    for (bn, cat), js in groups.items():
        withline = sorted((j for j in js if pending[j]["line"] is not None), key=lambda j: pending[j]["line"])
        cluster, prev = 0, None
        for j in withline:
            ln = pending[j]["line"]
            if prev is not None and (ln - prev) > PROXIMITY_LINES:
                cluster += 1
            keys[j] = f"{bn}:{cat}:c{cluster}"
            prev = ln
        for j in (j for j in js if pending[j]["line"] is None):
            keys[j] = f"{bn}:{cat}:filelevel:{j}"
    return keys


def _semantic_keys(pending: list) -> dict | None:
    """Preferred dedup: cluster findings within a file by SEMANTIC similarity of their
    summary text (limbic MiniLM embeddings). Merges the same bug described in different
    words / tagged with different categories — which line-proximity can't. Returns None
    if limbic isn't importable, so the caller falls back to proximity."""
    try:
        from limbic.amygdala import EmbeddingModel, greedy_centroid_cluster
    except Exception:
        return None
    from collections import defaultdict
    by_file: dict[str, list[int]] = defaultdict(list)
    for j, p in enumerate(pending):
        by_file[os.path.basename(p["file"])].append(j)
    try:
        model = EmbeddingModel()
        keys = {}
        for fn, js in by_file.items():
            if len(js) == 1:
                keys[js[0]] = f"{fn}:solo{js[0]}"
                continue
            texts = [(pending[j]["summary"] or pending[j].get("rationale") or "") for j in js]
            clusters = greedy_centroid_cluster(model.embed_batch(texts), threshold=SEM_THRESHOLD)
            assigned = {local: f"c{ci}" for ci, members in enumerate(clusters) for local in members}
            for local, j in enumerate(js):
                keys[j] = f"{fn}:sem-{assigned[local]}" if local in assigned else f"{fn}:solo{j}"
        return keys
    except Exception:
        return None


def assign_dedup_keys(pending: list) -> tuple[dict, str]:
    """Return (finding_index -> dedup_key, method_name)."""
    sem = _semantic_keys(pending)
    if sem is not None:
        return sem, "semantic (limbic embeddings)"
    return _proximity_keys(pending), "proximity (line gap)"


def git(args: list[str], cwd: str | None = None) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return ""


def connect() -> sqlite3.Connection:
    HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS review_run (
          run_id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          repo TEXT, branch TEXT, base_ref TEXT, base_sha TEXT, head_sha TEXT,
          pr_number INTEGER, pr_title TEXT, pr_url TEXT,
          diff_files INTEGER, diff_added INTEGER, diff_removed INTEGER,
          effort TEXT, status TEXT DEFAULT 'running', notes TEXT
        );
        CREATE TABLE IF NOT EXISTS reviewer_run (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL REFERENCES review_run(run_id),
          round INTEGER NOT NULL DEFAULT 0,
          reviewer TEXT NOT NULL, model_family TEXT, model TEXT, effort TEXT,
          input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
          cost_usd REAL, duration_ms INTEGER, num_findings INTEGER,
          ok INTEGER DEFAULT 1, error TEXT,
          UNIQUE(run_id, round, reviewer)
        );
        CREATE TABLE IF NOT EXISTS finding (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL REFERENCES review_run(run_id),
          round INTEGER NOT NULL DEFAULT 0,
          reviewer TEXT NOT NULL, model_family TEXT,
          file TEXT, line INTEGER, dedup_key TEXT,
          category TEXT, category_raw TEXT, severity TEXT, severity_raw TEXT,
          summary TEXT, rationale TEXT, suggested_fix TEXT,
          verdict TEXT DEFAULT 'unverified',
          verified_severity TEXT,
          agreement_n INTEGER DEFAULT 1,
          was_fixed INTEGER, fixed_round INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_finding_run ON finding(run_id);
        CREATE INDEX IF NOT EXISTS idx_finding_dedup ON finding(run_id, round, dedup_key);
        CREATE INDEX IF NOT EXISTS idx_finding_cat ON finding(category, severity);
        CREATE INDEX IF NOT EXISTS idx_reviewer_run ON reviewer_run(run_id, round);
        """
    )
    # lightweight migrations for DBs created before a column existed
    cols = {r[1] for r in conn.execute("PRAGMA table_info(finding)").fetchall()}
    if "verified_severity" not in cols:
        conn.execute("ALTER TABLE finding ADD COLUMN verified_severity TEXT")
    conn.commit()


def _mirror_cost_to_limbic(run_id, rnd, reviewer, model, toks, cost, n_findings) -> None:
    """Best-effort: also record this reviewer's spend in limbic's central cost_log so it
    surfaces in the existing cross-project dashboard. No-op if limbic isn't available."""
    try:
        from limbic.cerebellum import cost_log
        cost_log.log(
            project="compound-review", model=model or reviewer,
            prompt_tokens=toks.get("input", 0) or 0,
            completion_tokens=toks.get("output", 0) or 0,
            cached_tokens=toks.get("cached_input", 0) or 0,
            cost_usd=cost, purpose="code-review",
            metadata={"run_id": run_id, "round": rnd, "reviewer": reviewer, "findings": n_findings},
        )
    except Exception:
        pass


def load_prices() -> dict:
    try:
        return json.loads(PRICES_PATH.read_text())
    except Exception:
        return {}


def compute_cost(model: str | None, tokens: dict, prices: dict) -> float | None:
    p = prices.get(model or "", prices.get("default", {}))
    if not p:
        return None
    inp = tokens.get("input", 0) or 0
    cached = tokens.get("cached_input", 0) or 0
    out = tokens.get("output", 0) or 0
    # cached billed at the cached rate; uncached input = input - cached
    uncached = max(inp - cached, 0)
    return round(
        uncached / 1e6 * p.get("input", 0)
        + cached / 1e6 * p.get("cached_input", p.get("input", 0))
        + out / 1e6 * p.get("output", 0),
        6,
    )


# ---------------------------------------------------------------------------
# init — capture context, create the run row, lay out the run dir
# ---------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> None:
    cwd = os.getcwd()
    root = git(["rev-parse", "--show-toplevel"], cwd) or cwd
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or "DETACHED"
    base_ref = args.base
    base_sha = git(["merge-base", base_ref, "HEAD"], cwd) or git(["rev-parse", f"{base_ref}"], cwd)
    head_sha = git(["rev-parse", "HEAD"], cwd)
    repo = Path(root).name

    files = added = removed = 0
    diff_paths: list[str] = []
    numstat = git(["diff", "--numstat", f"{base_sha}...HEAD"], cwd) if base_sha else ""
    for ln in numstat.splitlines():
        parts = ln.split("\t")
        if len(parts) == 3:
            files += 1
            added += int(parts[0]) if parts[0].isdigit() else 0
            removed += int(parts[1]) if parts[1].isdigit() else 0
            diff_paths.append(parts[2])

    pr_number = pr_title = pr_url = None
    try:
        pr = json.loads(
            subprocess.run(
                ["gh", "pr", "view", "--json", "number,title,url"],
                cwd=cwd, capture_output=True, text=True, check=True,
            ).stdout
        )
        pr_number, pr_title, pr_url = pr.get("number"), pr.get("title"), pr.get("url")
    except Exception:
        pass

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", branch).strip("-")[:32]
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}"
    run_dir = RUNS_DIR / run_id
    (run_dir / "raw" / "round-0").mkdir(parents=True, exist_ok=True)

    conn = connect()
    conn.execute(
        """INSERT OR REPLACE INTO review_run
        (run_id, created_at, repo, branch, base_ref, base_sha, head_sha,
         pr_number, pr_title, pr_url, diff_files, diff_added, diff_removed, effort, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'running')""",
        (run_id, now_iso(), repo, branch, base_ref, base_sha, head_sha,
         pr_number, pr_title, pr_url, files, added, removed, args.effort),
    )
    conn.commit()

    # Carryover: open confirmed/plausible findings from prior runs of this repo that touch
    # files in this diff. Surfaced so a follow-up branch doesn't re-discover them — the agent
    # triages each (mark-fixed if this branch resolved it, wontfix if rejected, else plan it).
    carryover = []
    if diff_paths:
        ph = ",".join("?" * len(diff_paths))
        rows = conn.execute(
            f"""SELECT f.run_id, f.dedup_key, f.file, f.line, f.severity, f.verdict,
                       f.summary, r.branch
                FROM finding f JOIN review_run r ON r.run_id = f.run_id
                WHERE r.repo = ? AND f.run_id != ?
                  AND f.verdict IN ('confirmed','plausible')
                  AND COALESCE(f.was_fixed, 0) = 0
                  AND f.file IN ({ph})
                GROUP BY f.run_id, f.dedup_key
                ORDER BY f.run_id""",
            (repo, run_id, *diff_paths),
        ).fetchall()
        carryover = [dict(r) for r in rows]
    conn.close()

    if carryover:
        print(f"### Carryover — {len(carryover)} open finding(s) from prior runs touch this diff", file=sys.stderr)
        for c in carryover:
            print(f"  [{c['severity']}/{c['verdict']}] {c['file']}:{c['line']} ({c['branch']}) "
                  f"run={c['run_id']} key={c['dedup_key']}\n    {c['summary'][:110]}", file=sys.stderr)

    out = {
        "run_id": run_id, "run_dir": str(run_dir),
        "raw_round0": str(run_dir / "raw" / "round-0"),
        "repo": repo, "branch": branch, "base_ref": base_ref,
        "base_sha": base_sha, "head_sha": head_sha,
        "pr_number": pr_number, "pr_url": pr_url,
        "diff_files": files, "diff_added": added, "diff_removed": removed,
        "schema_path": str(SCHEMA_PATH),
        "carryover": carryover,
    }
    print(json.dumps(out, indent=2))


# ---------------------------------------------------------------------------
# run-codex — run one Codex pass, capture findings + tokens, write contract file
# ---------------------------------------------------------------------------
def _extract_json_block(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # last resort: outermost braces
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(text[s : e + 1])
        except Exception:
            pass
    return None


def cmd_run_codex(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    raw_dir = run_dir / "raw" / f"round-{args.round}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    persona = Path(args.prompt_file).read_text()
    repo_root = git(["rev-parse", "--show-toplevel"]) or os.getcwd()

    prompt = (
        f"{persona}\n\n"
        f"Run `git diff {args.base}...HEAD` yourself in the repo to obtain the diff under review, "
        f"then review it. Respond with ONLY a JSON object matching the provided output schema: "
        f'{{"findings": [ ... ]}}. Each finding needs file, line, severity '
        f"(blocker|high|medium|low|nit), category, summary, rationale, suggested_fix. "
        f"Do not include prose outside the JSON."
    )

    last_msg_path = raw_dir / f"{args.name}.lastmsg.txt"

    def invoke(extra: str) -> tuple[dict, str]:
        """Run codex once; return (token-delta, stderr). Writes lastmsg to last_msg_path."""
        cmd = [
            "codex", "exec", "--json", "--skip-git-repo-check",
            "--sandbox", "read-only", "-C", repo_root,
            "-c", f"model_reasoning_effort={args.effort}",
            "--output-schema", str(SCHEMA_PATH),
            "-o", str(last_msg_path),
        ]
        if args.model:
            cmd += ["-m", args.model]
        cmd.append(prompt + extra)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        tok = {"input": 0, "cached_input": 0, "output": 0}
        for ln in proc.stdout.splitlines():
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
                u = ev["usage"]
                tok["input"] += u.get("input_tokens", 0) or 0
                tok["cached_input"] += u.get("cached_input_tokens", 0) or 0
                tok["output"] += u.get("output_tokens", 0) or 0
        return tok, proc.stderr

    t0 = datetime.now()
    tokens = {"input": 0, "cached_input": 0, "output": 0}
    attempts = 0
    parsed = None
    stderr = ""
    # Codex intermittently wraps/prefixes its JSON (~7% of runs). Retry once with a
    # stricter instruction before recording failure; tokens accumulate across attempts.
    for extra in ("", "\n\nIMPORTANT: your previous reply could not be parsed. Reply with ONLY "
                      "the raw JSON object — no markdown fences, no prose before or after."):
        attempts += 1
        tok, stderr = invoke(extra)
        for k in tokens:
            tokens[k] += tok[k]
        parsed = _extract_json_block(last_msg_path.read_text()) if last_msg_path.exists() else None
        if parsed is not None:
            break
    duration_ms = int((datetime.now() - t0).total_seconds() * 1000)
    tokens["total"] = tokens["input"] + tokens["output"]

    findings, ok, error = [], True, None
    if parsed is None:
        ok, error = False, f"could not parse JSON findings from codex output (after {attempts} attempts)"
        (raw_dir / f"{args.name}.stderr.txt").write_text(stderr[-4000:])
    else:
        findings = parsed.get("findings", parsed if isinstance(parsed, list) else [])

    contract = {
        "reviewer": args.name, "model_family": "gpt",
        "model": args.model or "gpt-5.5", "effort": args.effort,
        "ok": ok, "error": error, "duration_ms": duration_ms, "attempts": attempts,
        "tokens": tokens, "findings": findings,
    }
    out_path = raw_dir / f"{args.name}.json"
    out_path.write_text(json.dumps(contract, indent=2))
    print(json.dumps({
        "reviewer": args.name, "ok": ok, "error": error, "attempts": attempts,
        "num_findings": len(findings), "tokens": tokens,
        "contract": str(out_path),
    }, indent=2))


# ---------------------------------------------------------------------------
# run-gemini — symmetric to run-codex; diff piped via stdin, JSON output parsed
# ---------------------------------------------------------------------------
def _find_tokens(d: dict) -> dict:
    """Gemini CLI reports usage at stats.models.<model>.tokens (summed across models).
    Shape: {input, prompt, candidates, total, cached, thoughts, tool}; candidates=output."""
    acc = {"input": 0, "cached_input": 0, "output": 0, "total": 0}
    models = (d.get("stats") or {}).get("models") or {}
    if isinstance(models, dict) and models:
        for m in models.values():
            t = (m or {}).get("tokens") or {}
            acc["input"] += t.get("input", t.get("prompt", 0)) or 0
            acc["output"] += t.get("candidates", t.get("output", 0)) or 0
            acc["cached_input"] += t.get("cached", 0) or 0
            acc["total"] += t.get("total", 0) or 0
        if acc["total"] == 0:
            acc["total"] = acc["input"] + acc["output"]
        return acc
    # fallback for other/older shapes
    for path in (("usage",), ("metadata", "usage"), ("tokens",)):
        node = d
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, dict):
            inp = node.get("input") or node.get("promptTokenCount") or 0
            out = node.get("output") or node.get("candidatesTokenCount") or 0
            if inp or out:
                return {"input": inp, "cached_input": node.get("cached", 0) or 0,
                        "output": out, "total": node.get("total", inp + out)}
    return acc


def _decode_first_object(text: str, want: tuple = ()) -> dict | None:
    """Return the first balanced JSON object in `text` (ignoring leading/trailing junk)
    that contains at least one of the `want` keys. Tolerates warning lines around it."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except Exception:
            continue
        if isinstance(obj, dict) and (not want or any(k in obj for k in want)):
            return obj
    return None


def cmd_run_gemini(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    raw_dir = run_dir / "raw" / f"round-{args.round}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    persona = Path(args.prompt_file).read_text()
    repo_root = git(["rev-parse", "--show-toplevel"]) or os.getcwd()
    diff = git(["diff", f"{args.base}...HEAD"], repo_root)
    schema = SCHEMA_PATH.read_text()

    # NOTE: the gemini CLI hangs when the diff is piped via stdin, and has a ~50s latency
    # floor + ~23k-token overhead per call that blows past the timeout on large prompts.
    # Embed the diff in the prompt but cap it hard so the call actually completes; gemini is
    # only practical as a small/medium-diff lens. Larger diffs get a truncation note.
    # Gemini CLI latency is ~7s/KB of prompt (7KB→50s, 50KB→timeout), so cap low enough to
    # finish inside the timeout. On large PRs this means gemini only sees a slice — it is a
    # small-diff lens; the analytics corpus decides whether it earns its slot.
    MAX_DIFF = 20_000
    diff_blk = diff if len(diff) <= MAX_DIFF else (
        diff[:MAX_DIFF] + f"\n...[diff truncated at {MAX_DIFF} of {len(diff)} bytes — review what is shown]...")
    prompt = (
        f"{persona}\n\nThe unified diff under review follows between the markers. "
        f"Respond with ONLY a JSON object matching this schema (no prose, no code fences):\n{schema}\n"
        f"\n===BEGIN DIFF===\n{diff_blk}\n===END DIFF===\n"
    )
    cmd = ["gemini", "-p", prompt, "--approval-mode", "plan", "-o", "json"]
    if args.model:
        cmd += ["-m", args.model]

    t0 = datetime.now()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        contract = {"reviewer": args.name, "model_family": "gemini", "model": args.model or "gemini-flash-latest",
                    "ok": False, "error": "gemini timed out after 240s", "duration_ms": 240000,
                    "tokens": {"input": 0, "cached_input": 0, "output": 0, "total": 0}, "findings": []}
        (raw_dir / f"{args.name}.json").write_text(json.dumps(contract, indent=2))
        print(json.dumps({"reviewer": args.name, "ok": False, "error": "timeout"}, indent=2))
        return
    duration_ms = int((datetime.now() - t0).total_seconds() * 1000)

    findings, ok, error, tokens = [], True, None, {"input": 0, "cached_input": 0, "output": 0, "total": 0}
    # Always persist both streams for debugging; the JSON envelope can land on either,
    # surrounded by skill-conflict/progress warnings, so search the combined blob.
    (raw_dir / f"{args.name}.stdout.txt").write_text(proc.stdout)
    (raw_dir / f"{args.name}.stderr.txt").write_text(proc.stderr)
    blob = f"{proc.stdout}\n{proc.stderr}"
    env = _decode_first_object(blob, want=("response", "session_id", "error")) or {}
    if not env and proc.returncode != 0:
        error = f"gemini exited {proc.returncode} with no JSON envelope"
    if env.get("error"):
        ok, error = False, str(env["error"].get("message", env["error"]))[:500]
        (raw_dir / f"{args.name}.stderr.txt").write_text(blob[-4000:])
    else:
        tokens = _find_tokens(env)
        resp = env.get("response", env.get("text", ""))
        parsed = _extract_json_block(resp if isinstance(resp, str) else json.dumps(resp))
        if parsed is None:
            ok, error = False, "could not parse findings JSON from gemini response"
            (raw_dir / f"{args.name}.raw.txt").write_text(blob[-8000:])
        else:
            findings = parsed.get("findings", parsed if isinstance(parsed, list) else [])

    contract = {
        "reviewer": args.name, "model_family": "gemini",
        "model": args.model or "gemini-3-pro", "effort": None,
        "ok": ok, "error": error, "duration_ms": duration_ms,
        "tokens": tokens, "findings": findings,
    }
    out_path = raw_dir / f"{args.name}.json"
    out_path.write_text(json.dumps(contract, indent=2))
    print(json.dumps({
        "reviewer": args.name, "ok": ok, "error": error,
        "num_findings": len(findings), "tokens": tokens, "contract": str(out_path),
    }, indent=2))


# ---------------------------------------------------------------------------
# ingest — the single DB writer: read all contract files for a round, persist
# ---------------------------------------------------------------------------
def cmd_ingest(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    raw_dir = run_dir / "raw" / f"round-{args.round}"
    prices = load_prices()
    conn = connect()

    contracts = sorted(raw_dir.glob("*.json"))
    if not contracts:
        print(f"no contract files in {raw_dir}", file=sys.stderr)
        sys.exit(1)
    repo_root = git(["rev-parse", "--show-toplevel"]) or os.getcwd()
    base = conn.execute("SELECT base_sha FROM review_run WHERE run_id=?", (args.run_id,)).fetchone()
    sym_index = build_symbol_index(repo_root, base["base_sha"] if base and base["base_sha"] else "origin/main")

    # collect all findings first so we can compute cross-reviewer agreement
    pending: list[dict] = []
    for cf in contracts:
        try:
            c = json.loads(cf.read_text())
        except Exception as e:
            print(f"skip unparseable {cf.name}: {e}", file=sys.stderr)
            continue
        reviewer = c.get("reviewer", cf.stem)
        family = c.get("model_family")
        toks = c.get("tokens", {}) or {}
        cost = c.get("cost_usd")
        if cost is None:
            cost = compute_cost(c.get("model"), toks, prices)
        fs = c.get("findings", []) or []

        conn.execute(
            """INSERT OR REPLACE INTO reviewer_run
            (run_id, round, reviewer, model_family, model, effort,
             input_tokens, cached_input_tokens, output_tokens, total_tokens,
             cost_usd, duration_ms, num_findings, ok, error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (args.run_id, args.round, reviewer, family, c.get("model"), c.get("effort"),
             toks.get("input"), toks.get("cached_input"), toks.get("output"),
             toks.get("total", (toks.get("input", 0) or 0) + (toks.get("output", 0) or 0)),
             cost, c.get("duration_ms"), len(fs),
             1 if c.get("ok", True) else 0, c.get("error")),
        )
        _mirror_cost_to_limbic(args.run_id, args.round, reviewer, c.get("model"), toks, cost, len(fs))
        for f in fs:
            line = f.get("line")
            line = int(line) if isinstance(line, (int, str)) and str(line).isdigit() else None
            fpath = (f.get("file") or "").strip()
            cat = norm_cat(f.get("category"))
            sym = enclosing_symbol(sym_index, fpath, line)
            pending.append({
                "reviewer": reviewer, "family": family,
                "file": fpath, "line": line, "dedup_key": None, "sym": sym,
                "category": cat, "category_raw": f.get("category"),
                "severity": norm_sev(f.get("severity")), "severity_raw": f.get("severity"),
                "summary": f.get("summary"), "rationale": f.get("rationale"),
                "suggested_fix": f.get("suggested_fix"),
                "verdict": (f.get("verdict") or "unverified").lower(),
            })

    # Assign "same finding" dedup keys: semantic clustering (limbic embeddings) when
    # available — merges the same bug across wording/category — else line-proximity.
    keymap, dedup_method = assign_dedup_keys(pending)
    for j, p in enumerate(pending):
        p["dedup_key"] = keymap[j]
    print(f"dedup method: {dedup_method}")

    # agreement_n = distinct reviewers sharing a dedup_key within this round
    by_key: dict[str, set] = {}
    for p in pending:
        by_key.setdefault(p["dedup_key"], set()).add(p["reviewer"])

    # clear any prior ingest of this round (idempotent re-ingest)
    conn.execute("DELETE FROM finding WHERE run_id=? AND round=?", (args.run_id, args.round))
    for p in pending:
        conn.execute(
            """INSERT INTO finding
            (run_id, round, reviewer, model_family, file, line, dedup_key,
             category, category_raw, severity, severity_raw,
             summary, rationale, suggested_fix, verdict, agreement_n)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (args.run_id, args.round, p["reviewer"], p["family"], p["file"], p["line"],
             p["dedup_key"], p["category"], p["category_raw"], p["severity"], p["severity_raw"],
             p["summary"], p["rationale"], p["suggested_fix"], p["verdict"],
             len(by_key[p["dedup_key"]])),
        )
    conn.commit()
    _print_report(conn, args.run_id, round_filter=args.round)
    conn.close()


# ---------------------------------------------------------------------------
# report — synthesis-ready summary
# ---------------------------------------------------------------------------
def _print_report(conn: sqlite3.Connection, run_id: str, round_filter: int | None = None) -> None:
    where = "WHERE run_id=?"
    params: list = [run_id]
    if round_filter is not None:
        where += " AND round=?"
        params.append(round_filter)

    rr = conn.execute(
        f"""SELECT reviewer, model_family, num_findings,
                  input_tokens, cached_input_tokens, output_tokens, total_tokens,
                  cost_usd, duration_ms, ok, error
            FROM reviewer_run {where} ORDER BY reviewer""", params
    ).fetchall()
    findings = conn.execute(
        f"SELECT * FROM finding {where}", params
    ).fetchall()

    def fresh(r):  # uncached input + output — comparable across providers (codex re-sends cached ctx)
        return max((r["input_tokens"] or 0) - (r["cached_input_tokens"] or 0), 0) + (r["output_tokens"] or 0)

    tok = sum(fresh(r) for r in rr)
    cost = sum((r["cost_usd"] or 0) for r in rr)
    print("\n" + "=" * 72)
    print(f"RUN {run_id}" + (f"  round {round_filter}" if round_filter is not None else ""))
    print("=" * 72)
    print("\nReviewers:  (fresh tok = uncached input + output; cost is cache-aware)")
    for r in rr:
        status = "ok" if r["ok"] else f"FAILED ({r['error']})"
        print(f"  {r['reviewer']:<26} {r['model_family'] or '?':<7} "
              f"{r['num_findings'] or 0:>3} findings  "
              f"{fresh(r):>8} fresh  "
              f"${(r['cost_usd'] or 0):.4f}  {status}")
    print(f"  {'TOTAL':<26} {'':<7} {len(findings):>3} raw      {tok:>8} fresh  ${cost:.4f}")

    # collapse to unique locations; gate on EFFECTIVE severity (verifier/impact-aware),
    # not the finder's raw label, so a confirmed correctness bug can't hide in backlog.
    groups: dict[str, list] = {}
    for f in findings:
        groups.setdefault(f["dedup_key"], []).append(dict(f))
    items = []
    for key, fs in groups.items():
        best = min(fs, key=lambda f: SEVERITY_RANK.get(f["severity"], 5))
        finder_sev = best["severity"]
        eff = effective_severity(fs)
        reviewers = sorted({f["reviewer"] for f in fs})
        verdicts = {f["verdict"] for f in fs}
        items.append({
            "key": key, "severity": eff, "finder_severity": finder_sev,
            "promoted": SEVERITY_RANK.get(eff, 5) < SEVERITY_RANK.get(finder_sev, 5),
            "agreement": len(reviewers), "reviewers": reviewers,
            "file": best["file"], "line": best["line"],
            "category": best["category"], "summary": best["summary"], "verdicts": verdicts,
        })
    items.sort(key=lambda i: (SEVERITY_RANK.get(i["severity"], 5), -i["agreement"]))

    # A location where every reviewer's row is refuted/wontfix is dismissed — it must not
    # gate the loop no matter its severity label.
    dismissed = [i for i in items if i["verdicts"] and i["verdicts"] <= {"refuted", "wontfix"}]
    live = [i for i in items if i not in dismissed]
    blocking = [i for i in live if i["severity"] in BLOCKING]
    backlog = [i for i in live if i["severity"] not in BLOCKING]
    print(f"\nBlocking findings ({len(blocking)}):  [loop on these]")
    for i in blocking:
        agree = f"x{i['agreement']}" if i["agreement"] > 1 else "  "
        v = "/".join(sorted(i["verdicts"]))
        promo = f"  ⬆ promoted from {i['finder_severity']}" if i["promoted"] else ""
        print(f"  [{i['severity']:<7}] {agree} {i['file']}:{i['line']}  ({i['category']}/{v}){promo}")
        print(f"            {i['summary']}")
        print(f"            found by: {', '.join(i['reviewers'])}")
        print(f"            key: {i['key']}")
    print(f"\nBacklog (non-blocking, {len(backlog)}):  [collect, do NOT loop]")
    for i in backlog:
        agree = f"x{i['agreement']}" if i["agreement"] > 1 else "  "
        print(f"  [{i['severity']:<7}] {agree} {i['file']}:{i['line']}  {i['summary'][:66]}")
    if dismissed:
        print(f"\nDismissed (refuted/wontfix, {len(dismissed)}):")
        for i in dismissed:
            print(f"  [{i['severity']:<7}]    {i['file']}:{i['line']}  {i['summary'][:66]}")
    print()


def cmd_report(args: argparse.Namespace) -> None:
    conn = connect()
    _print_report(conn, args.run_id, round_filter=args.round)
    conn.close()


# ---------------------------------------------------------------------------
# set-verdict / mark-fixed / finish — loop bookkeeping
# ---------------------------------------------------------------------------
def cmd_set_verdict(args: argparse.Namespace) -> None:
    conn = connect()
    if args.severity:
        n = conn.execute(
            "UPDATE finding SET verdict=?, verified_severity=? WHERE run_id=? AND round=? AND dedup_key=?",
            (args.verdict.lower(), norm_sev(args.severity), args.run_id, args.round, args.dedup_key),
        ).rowcount
    else:
        n = conn.execute(
            "UPDATE finding SET verdict=? WHERE run_id=? AND round=? AND dedup_key=?",
            (args.verdict.lower(), args.run_id, args.round, args.dedup_key),
        ).rowcount
    conn.commit()
    conn.close()
    extra = f", verified_severity={norm_sev(args.severity)}" if args.severity else ""
    print(f"set verdict={args.verdict}{extra} on {n} finding(s) for {args.dedup_key}")


def cmd_mark_fixed(args: argparse.Namespace) -> None:
    conn = connect()
    n = conn.execute(
        "UPDATE finding SET was_fixed=1, fixed_round=? WHERE run_id=? AND dedup_key=?",
        (args.round, args.run_id, args.dedup_key),
    ).rowcount
    conn.commit()
    conn.close()
    print(f"marked {n} finding(s) fixed for {args.dedup_key}")


def cmd_finish(args: argparse.Namespace) -> None:
    conn = connect()
    # A finish with open blocking findings is usually stale bookkeeping (the fix happened
    # outside the loop) — list them so the agent marks each fixed/wontfix before closing.
    open_blocking = conn.execute(
        """SELECT dedup_key, file, line, severity, verified_severity, verdict, summary
           FROM finding
           WHERE run_id = ?
             AND COALESCE(verified_severity, severity) IN ('blocker','high')
             AND verdict NOT IN ('refuted','wontfix')
             AND COALESCE(was_fixed, 0) = 0
           GROUP BY dedup_key""",
        (args.run_id,),
    ).fetchall()
    if open_blocking:
        print(f"⚠ {len(open_blocking)} blocking finding(s) still open on this run. "
              f"For each: `mark-fixed` if resolved (in-loop or by later commits), "
              f"`set-verdict --verdict wontfix` if deliberately rejected, or note why it stays open:")
        for r in open_blocking:
            sev = r["verified_severity"] or r["severity"]
            print(f"  [{sev}/{r['verdict']}] {r['file']}:{r['line']} key={r['dedup_key']}\n"
                  f"    {r['summary'][:110]}")
    conn.execute("UPDATE review_run SET status='complete', notes=? WHERE run_id=?",
                 (args.notes, args.run_id))
    conn.commit()
    conn.close()
    print(f"run {args.run_id} marked complete")


# ---------------------------------------------------------------------------
# analytics — the payoff: cross-run tuning data
# ---------------------------------------------------------------------------
def cmd_analytics(args: argparse.Namespace) -> None:
    conn = connect()
    print("\n### Categories most often flagged (all runs)")
    for r in conn.execute(
        """SELECT category, severity, COUNT(*) n FROM finding
           GROUP BY category, severity ORDER BY n DESC LIMIT 20"""):
        print(f"  {r['n']:>4}  {r['category']:<16} {r['severity']}")

    print("\n### Per-reviewer productivity & cost")
    print("  (blk✓ = confirmed blocker/high — severity-weighted value; a low-volume reviewer")
    print("   with blk✓ catches is earning its seat. Claude lenses run in-session on the")
    print("   flat-rate Max plan — tokens aren't metered there, so cost shows 'Max'; judge")
    print("   them by finds/confirm/blk✓, not find/Mtok. Only Codex $ is real marginal spend.)")
    print(f"  {'reviewer':<26} {'runs':>4} {'finds':>6} {'confirm':>7} {'refuted':>7} {'blk✓':>5} "
          f"{'tokens':>10} {'cost$':>8} {'find/Mtok':>9}")
    for r in conn.execute(
        """SELECT rr.reviewer,
                  COALESCE(MAX(rr.model_family),'') family,
                  COUNT(DISTINCT rr.run_id) runs,
                  COALESCE(SUM(rr.num_findings),0) finds,
                  COALESCE(SUM(MAX(rr.input_tokens-rr.cached_input_tokens,0)+rr.output_tokens),0) tokens,
                  COALESCE(SUM(rr.cost_usd),0) cost,
                  SUM(CASE WHEN rr.ok=1 THEN 1 ELSE 0 END) ok_rows,
                  SUM(CASE WHEN rr.ok=1 AND COALESCE(rr.input_tokens,0)+COALESCE(rr.output_tokens,0) > 0
                           THEN 1 ELSE 0 END) tok_rows
           FROM reviewer_run rr GROUP BY rr.reviewer ORDER BY finds DESC"""):
        conf = conn.execute(
            "SELECT COUNT(*) c FROM finding WHERE reviewer=? AND verdict='confirmed'",
            (r["reviewer"],)).fetchone()["c"]
        refu = conn.execute(
            "SELECT COUNT(*) c FROM finding WHERE reviewer=? AND verdict='refuted'",
            (r["reviewer"],)).fetchone()["c"]
        blk = conn.execute(
            """SELECT COUNT(*) c FROM finding WHERE reviewer=? AND verdict='confirmed'
               AND COALESCE(verified_severity, severity) IN ('blocker','high')""",
            (r["reviewer"],)).fetchone()["c"]
        if r["family"] == "claude":
            # Claude lenses run in-session on the flat-rate Max plan; tokens unmetered by design.
            tok_s, cost_s, fpm_s = f"{'—':>10}", f"{'Max':>8}", f"{'flat':>9}"
        elif r["tokens"] and r["tok_rows"] == r["ok_rows"]:  # every successful run captured
            tok_s, cost_s = f"{r['tokens']:>10}", f"{r['cost']:>8.3f}"
            fpm_s = f"{r['finds'] / (r['tokens'] / 1e6):>9.1f}"
        elif r["tokens"]:  # partial capture — a per-Mtok rate would be inflated
            tok_s, cost_s = f"{r['tokens']:>9}+", f"{r['cost']:>7.3f}+"
            fpm_s = f"{'partial':>9}"
        else:
            tok_s, cost_s, fpm_s = f"{'n/a':>10}", f"{'n/a':>8}", f"{'n/a':>9}"
        print(f"  {r['reviewer']:<26} {r['runs']:>4} {r['finds']:>6} {conf:>7} {refu:>7} {blk:>5} "
              f"{tok_s} {cost_s} {fpm_s}")

    print("\n### Reviewer precision (of findings that got a verdict)")
    print("  (confirm vs refute among verified findings — low precision + high refute = noisy")
    print("   lens; but weigh against blk✓ above, since a recall lens trades precision for it.)")
    for r in conn.execute(
        """SELECT reviewer,
                  SUM(verdict='confirmed') conf,
                  SUM(verdict='refuted')   refu,
                  SUM(verdict IN ('confirmed','plausible','refuted','wontfix')) verified
           FROM finding GROUP BY reviewer
           HAVING verified > 0 ORDER BY verified DESC"""):
        decided = r["conf"] + r["refu"]
        prec = f"{100*r['conf']/decided:.0f}%" if decided else "—"
        print(f"  {r['reviewer']:<26} {r['conf']:>3} confirmed  {r['refu']:>3} refuted  "
              f"→ {prec:>4} precision  ({r['verified']} verified)")

    print("\n### Cross-reviewer agreement (findings seen by N reviewers)")
    for r in conn.execute(
        """SELECT agreement_n, COUNT(DISTINCT run_id||':'||round||':'||dedup_key) n
           FROM finding GROUP BY agreement_n ORDER BY agreement_n"""):
        print(f"  {r['agreement_n']} reviewer(s): {r['n']} distinct locations")

    print("\n### Unique-catch rate (locations found by exactly one reviewer)")
    total = conn.execute(
        "SELECT COUNT(DISTINCT run_id||':'||round||':'||dedup_key) n FROM finding").fetchone()["n"]
    solo = conn.execute(
        """SELECT COUNT(*) n FROM (
             SELECT run_id, round, dedup_key FROM finding
             GROUP BY run_id, round, dedup_key HAVING COUNT(DISTINCT reviewer)=1)""").fetchone()["n"]
    if total:
        print(f"  {solo}/{total} = {100*solo/total:.1f}% of locations are single-reviewer")

    print("\n### Severity drift — finder label vs verified severity (where verified)")
    rows = conn.execute(
        """SELECT reviewer, severity, verified_severity FROM finding
           WHERE verified_severity IS NOT NULL""").fetchall()
    drift: dict[str, dict] = {}
    for r in rows:
        d = drift.setdefault(r["reviewer"], {"under": 0, "over": 0, "match": 0})
        fr, vr = SEVERITY_RANK.get(r["severity"], 5), SEVERITY_RANK.get(r["verified_severity"], 5)
        d["under" if fr > vr else "over" if fr < vr else "match"] += 1
    if drift:
        print(f"  {'reviewer':<26} {'under-rated':>11} {'over-rated':>11} {'matched':>8}")
        for rev, d in sorted(drift.items()):
            print(f"  {rev:<26} {d['under']:>11} {d['over']:>11} {d['match']:>8}")
    else:
        print("  (no verified severities recorded yet — run set-verdict --severity)")

    # confirmed impact bugs the finder under-rated (the B2 failure mode)
    under = conn.execute(
        """SELECT reviewer, file, line, severity, summary FROM finding
           WHERE verdict='confirmed' AND category IN ('correctness','security')
             AND severity IN ('medium','low','nit')""").fetchall()
    if under:
        print("\n### Under-rated confirmed impact bugs (gated up by promote-on-confirm)")
        for r in under:
            print(f"  {r['reviewer']:<22} [{r['severity']}] {os.path.basename(r['file'])}:{r['line']}  {r['summary'][:50]}")

    print("\n### Token/cost per run")
    for r in conn.execute(
        """SELECT v.run_id, v.branch, v.created_at,
                  COALESCE(SUM(rr.total_tokens),0) tok, COALESCE(SUM(rr.cost_usd),0) cost
           FROM review_run v LEFT JOIN reviewer_run rr ON rr.run_id=v.run_id
           GROUP BY v.run_id ORDER BY v.created_at DESC LIMIT 15"""):
        print(f"  {r['created_at']}  {r['branch']:<24} {r['tok']:>9} tok  ${r['cost']:.3f}")
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="compound_review")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init"); p.add_argument("--base", default="origin/main")
    p.add_argument("--effort", default="full"); p.set_defaults(fn=cmd_init)

    p = sub.add_parser("run-codex")
    for a in ("--run-id", "--run-dir", "--name", "--prompt-file"):
        p.add_argument(a, required=True)
    p.add_argument("--round", type=int, default=0)
    p.add_argument("--base", default="origin/main")
    p.add_argument("--model", default=None)  # None => account default (gpt-5.5 on ChatGPT plan)
    p.add_argument("--effort", default="xhigh")
    p.set_defaults(fn=cmd_run_codex)

    p = sub.add_parser("run-gemini")
    for a in ("--run-id", "--run-dir", "--name", "--prompt-file"):
        p.add_argument(a, required=True)
    p.add_argument("--round", type=int, default=0)
    p.add_argument("--base", default="origin/main")
    # flash, not the default pro model: pro reasoning times out (>600s) on large diffs
    p.add_argument("--model", default="gemini-flash-latest")
    p.set_defaults(fn=cmd_run_gemini)

    p = sub.add_parser("ingest")
    p.add_argument("--run-id", required=True); p.add_argument("--run-dir", required=True)
    p.add_argument("--round", type=int, default=0); p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("report")
    p.add_argument("--run-id", required=True)
    p.add_argument("--round", type=int, default=None); p.set_defaults(fn=cmd_report)

    p = sub.add_parser("set-verdict")
    for a in ("--run-id", "--dedup-key"):
        p.add_argument(a, required=True)
    p.add_argument("--verdict", required=True, type=str.lower,
                   choices=["confirmed", "plausible", "refuted", "wontfix", "unverified"])
    p.add_argument("--severity", default=None,
                   help="verifier's impact assessment; overrides finder labels for gating")
    p.add_argument("--round", type=int, default=0); p.set_defaults(fn=cmd_set_verdict)

    p = sub.add_parser("mark-fixed")
    for a in ("--run-id", "--dedup-key"):
        p.add_argument(a, required=True)
    p.add_argument("--round", type=int, default=0); p.set_defaults(fn=cmd_mark_fixed)

    p = sub.add_parser("finish")
    p.add_argument("--run-id", required=True); p.add_argument("--notes", default=None)
    p.set_defaults(fn=cmd_finish)

    p = sub.add_parser("analytics"); p.set_defaults(fn=cmd_analytics)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
