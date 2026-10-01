# resume-budget-guard

Enforce a **cumulative** spend cap across Claude Code resumes.

`claude --print --max-budget-usd N` enforces the cap **per invocation only** —
spend from before a `--resume` / `--continue` does *not* count toward the cap on
the resumed run ([Gotcha #7](https://dev.to/sendtoshailesh/11-claude-code-gotchas-that-quietly-skip-your-guardrails-5c8p);
the official fix is to *"track cumulative spend externally"*). A $5 cap resumed
four times can silently spend ~$20.

`resume-budget-guard` (`rbg`) is that external tracker, plus teeth: it keeps a
cumulative spend ledger and wraps your `claude` invocations so a resume can
never reset the budget. If the cap is already spent, it refuses to launch.

```bash
git clone https://github.com/hahahahahahahahah6/resume-budget-guard
cd resume-budget-guard

rbg init --cap 5.00

# wrap your normal claude calls -- resume-aware from here on
rbg run -- claude -p --resume <session-id> "keep building the feature"
# spent $4.20 / cap $5.00 -> launching with --max-budget-usd 0.80 (sess-abc)

rbg run -- claude -p --resume <session-id> "one more thing"
# BLOCKED: budget exhausted -- spent $5.00 of $5.00 cap on sess-abc.
# Resume would have reset the cap; refusing to launch.
```

## How it works

1. **Ledger** (`~/.resume-budget-guard/ledger.jsonl`): one JSON record per
   invocation — `{ts, session_key, kind, cost_usd}`. Cumulative spend is
   computed on read; no daemon needed.
2. **Wrap**: `rbg run` reads the ledger, computes `remaining = cap - spent`,
   and rewrites `--max-budget-usd` to `min(your value, remaining)` before
   launching `claude`. At zero remaining it exits 3 without launching.
3. **Record**: after the run, it parses `total_cost_usd` from claude's
   `--output-format json` result and appends it to the ledger.
4. **Sync**: `rbg sync --session <id>` parses the session transcript
   (`~/.claude/projects/**/<id>.jsonl`) and records a usage-derived baseline —
   for interactive (non-`-p`) sessions.

Two cap modes: `--session-key session` (default — cap applies per session id)
and `--session-key day` (cap applies to all spend today).

## Commands

| Command | What it does |
|---|---|
| `rbg init --cap 5.00` | set the spend cap |
| `rbg run [--cap N] [--session-key day] -- claude ...` | guarded invocation |
| `rbg sync --session <id>` | baseline a session from its transcript |
| `rbg status [--session <id>]` | spend vs cap |
| `rbg reset` | archive the ledger (daily rollover) |

## How this differs from agentcost-cli

[agentcost-cli](https://github.com/kashifdevfe/agentcost) is excellent
**observability**: it profiles what drives token spend (`report`, `watch`,
`list`, `outcome`). It does not enforce anything — its `hook-session-end`
only observes, and `--fail-on-waste` fires on waste patterns, not spend caps.
`resume-budget-guard` is the opposite side of the coin: it doesn't analyze
*why* you spent, it stops you from spending *more than the cap* — including
across resumes, which is exactly the hole `--max-budget-usd` leaves open.

## Notes

- Cost source priority: claude's own `total_cost_usd` (exact) → transcript
  `usage` × bundled rate table (estimates; `PRICING_AS_OF` stamped in code).
- Single-user CLI; no locking, no network, no auth. Your ledger never leaves
  your machine.
- One file, stdlib only, Python 3.9+. 6/6 tests pass (`python3 -m unittest discover -s tests`).

## License

MIT
