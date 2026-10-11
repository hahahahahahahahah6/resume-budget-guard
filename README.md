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
pip install resume-budget-guard

rbg init --cap 5.00

# wrap your normal claude calls -- resume-aware from here on
rbg run -- claude -p --resume <session-id> "keep building the feature"
# spent $4.20 / cap $5.00 -> launching with --max-budget-usd 0.80 (sess-abc)

rbg run -- claude -p --resume <session-id> "one more thing"
# BLOCKED: budget exhausted -- spent $5.00 of $5.00 cap on sess-abc.
# Resume would have reset the cap; refusing to launch.
```

## Why: hard budget caps, please, now

Simon Willison has been making the case that agents need *hard* budget caps,
not advisory ones — when a coding agent can spend your money, "trust me" is
not a control. `claude --max-budget-usd` was the obvious answer, but it has a
hole: the cap is per invocation. Resume a session four times under a $5 cap
and you can silently spend ~$20. This tool closes that hole: the cap is
**cumulative**, enforced from outside the agent, and a resume can never reset
it. Enforcement lives in a wrapper the agent cannot argue with — if the money
is gone, the process never starts.

## Quota-aware auto-wake

Rate limits pause work; budgets shouldn't have to. Point `rbg` at your quota
reset time and `wait` blocks until the window reopens, so a cron job or
orchestrator can pick up exactly where the cap left off:

```bash
rbg init --cap 20.00 --reset-at 2026-10-12T07:00:00Z

# keep a nightly build loop alive across quota resets:
rbg wait --until-reset --timeout 21600 --poll-interval 60 \
  && rbg run -- claude -p --resume <session-id> "continue the nightly build"
# waiting for quota reset at 2026-10-12T07:00:00+00:00 (timeout 21600s)
# quota resets in ~5:59:59; checking again in 60s
# ...
# quota reset reached (2026-10-12T07:00:00+00:00).
# spent $0.00 / cap $20.00 -> launching with --max-budget-usd 20.00 (sess-abc)
```

The reset time can come from `--reset-at` (on `init` or on `wait`), the
`RBG_RESET_AT` environment variable, or config. `wait` exits 0 when the quota
resets, 1 on `--timeout`, 2 when no reset time is configured — plain exit
codes a scheduler can branch on:

```cron
# cron: resume the build loop as soon as the quota window reopens
0 2 * * * cd ~/jobs/nightly && rbg wait --until-reset --timeout 18000 && rbg run -- claude -p --resume $SESSION "continue"
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
| `rbg init --cap 5.00 [--reset-at ISO]` | set the spend cap (+ optional quota reset time) |
| `rbg run [--cap N] [--session-key day] -- claude ...` | guarded invocation |
| `rbg sync --session <id>` | baseline a session from its transcript |
| `rbg status [--session <id>]` | spend vs cap |
| `rbg wait --until-reset [--timeout S] [--poll-interval S]` | block until the quota reset time (exit 0 on reset, 1 on timeout) |
| `rbg reset` | archive the ledger (daily rollover) |

## Interop with subagent-budget

[subagent-budget](https://pypi.org/project/subagent-budget/) (`pip install
subagent-budget`) guards per-agent token and spend budgets; `rbg` guards the
cumulative cap across resumes. The two share one ledger contract: every
`ledger.jsonl` record carries `ts` (ISO-8601 UTC), `kind`, and `cost_usd`
— subagent-budget 0.1.0 was built ledger-compatible with `rbg` on purpose, so
either tool can read the other's history.

To read subagent-budget's ledger from `rbg` (its default home is
`~/.config/subagent-budget/ledger.jsonl`), point the env override at it:

```bash
RBG_LEDGER_PATH=~/.config/subagent-budget/ledger.jsonl rbg status
```

Spend recorded by either tool accumulates under the same `ts`/`kind`/
`cost_usd` fields, so a combined spend picture is one `rbg status` away.
(`rbg`'s own records add `session_key`/`session_id`, which subagent-budget
ignores — both directions stay readable.)

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
- One file, stdlib only, Python 3.9+. 13/13 tests pass (`python3 -m unittest discover -s tests`).
- Install: `pip install resume-budget-guard` — entry point `rbg`.

## License

MIT
