# PLAN — resume-budget-guard

## Problem
`claude --print --max-budget-usd N` enforces the cap **per invocation only**.
Totals restored via `--continue` / `--resume` do NOT count toward the cap on the
resumed run (dev.to "11 Claude Code Gotchas", Gotcha #7; official docs confirm
`--max-budget-usd` is a per-`--print`-invocation limit). A $5 cap resumed 4 times
can spend ~$20. The official recommendation is: **track cumulative spend
externally**. No existing tool enforces this:

- `agentcost-cli` (kashifdevfe/agentcost): read-only observability
  (`report`/`watch`/`list`/`outcome`); `hook-session-end` is observability-only;
  `--fail-on-waste` fires on waste alerts, not spend caps. No budget enforcement.
- `yunaremaia/agentcost`: daily/weekly/monthly thresholds are reporting
  comparisons, not blocks.
- Community wrappers (trampollm, maestro-router, 5x-engineer) sum per-run
  `total_cost_usd` for their own loops but don't ship a general-purpose
  resume-aware guard.

## What this builds
`resume-budget-guard` (command: `rbg`): a stdlib-only single-file Python CLI that
maintains an **external cumulative spend ledger** and wraps `claude` invocations
so a resume can never silently reset the budget.

### Commands
- `rbg init --cap 5.00` — create `~/.resume-budget-guard/config.json`.
- `rbg run [--cap N] [--session-key MODE] -- claude -p --resume <sid> "prompt"`
  - Reads ledger, computes `spent` for the session (default mode: per-session;
    `--session-key day` for a global daily cap).
  - `remaining = cap - spent`. If `remaining <= 0`: refuse to launch, exit 3,
    print what was spent and the cap. **This is the enforcement.**
  - Else injects `--max-budget-usd min(user_value, remaining)` into the claude
    args and runs it. Session id comes from `--resume`/`--session-id` args, or
    from the JSON result's `session_id` for fresh sessions.
  - Parses `--output-format json` result's `total_cost_usd`, appends an
    invocation record to the ledger. Passes claude's exit code through.
- `rbg sync --session <sid>` — parse `~/.claude/projects/**/<sid>.jsonl`,
  sum `message.usage` × pricing table, store as the session's **baseline**
  (replaces any prior baseline; invocation records from `run` accumulate on
  top). Covers interactive (non-`-p`) sessions.
- `rbg status [--session <sid>]` — per-session and total spend vs cap.
- `rbg reset` — archive today's ledger (daily rollover).

### Ledger
`~/.resume-budget-guard/ledger.jsonl`, one JSON object per line:
`{"ts", "session_id", "kind": "baseline"|"invocation", "cost_usd"}`.
Cumulative(session) = baseline + Σ invocations. Computed on read; no locking
(MVP: single-user CLI).

### Cost sources (priority order)
1. `total_cost_usd` from `claude --output-format json` (exact, per invocation).
2. Transcript `usage` × hardcoded pricing table (`PRICING_AS_OF` date stamped in
   code; documented as estimates).

## Non-goals (MVP)
- No PyPI release (user decides later).
- No daemon / live watch; no SessionStart hook (wrapper covers `-p`, which is
  where `--max-budget-usd` works anyway).
- No multi-user locking, no cloud sync.

## Tests (must be all green)
`tests/test_rbg.py` (stdlib `unittest`, fake `claude` shim returning canned JSON):
1. Ledger accumulates across two runs (2 + 2 = 4).
2. Resume case: session spent $4 of $5 cap → wrapper passes `--max-budget-usd 1`
   to the shim (assert from shim's recorded argv).
3. Over budget: spent ≥ cap → wrapper exits 3, shim never executed.
4. Transcript sync: synthetic JSONL with usage → correct $ math.
5. Fresh session (no `--resume`) gets the full cap.
6. `status` output shows per-session totals.

## Deliverables
- `resume_budget_guard.py` (single file, stdlib only, `python3 resume_budget_guard.py ...`)
- `tests/test_rbg.py`, `README.md` (English, with "How this differs from
  agentcost-cli" section), `LICENSE` (MIT)
- Public GitHub repo `hahahahahahahahah6/resume-budget-guard` via gh-push
