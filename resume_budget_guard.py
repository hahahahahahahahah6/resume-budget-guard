#!/usr/bin/env python3
"""resume-budget-guard (rbg): enforce a cumulative spend cap across Claude Code resumes.

`claude --print --max-budget-usd N` enforces the cap per invocation only --
spend from before a `--resume`/`--continue` does NOT count toward the cap on the
resumed run. This tool keeps an external ledger of cumulative spend and wraps
`claude` so a resume can never silently reset the budget.

Usage:
    rbg init --cap 5.00
    rbg run -- claude -p --resume <session-id> "do the thing"
    rbg run --cap 10 --session-key day -- claude -p "other thing"
    rbg sync --session <session-id>
    rbg status [--session <session-id>]
    rbg reset

Exit codes: 0 ok, 2 usage error, 3 blocked (budget exhausted).
Any other exit code is passed through from the wrapped `claude` process.

Stdlib only. Python 3.9+.
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

APP_DIR = os.path.expanduser("~/.resume-budget-guard")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LEDGER_PATH = os.path.join(APP_DIR, "ledger.jsonl")

EXIT_BLOCKED = 3

# USD per million tokens. Estimates only (used for transcript fallback);
# the primary cost source is claude's own `total_cost_usd`.
PRICING_AS_OF = "2026-10-01"
MODEL_RATES = {
    "sonnet": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "opus": {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
    "haiku": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
}


def _rates_for(model):
    m = (model or "").lower()
    for key in ("opus", "sonnet", "haiku"):
        if key in m:
            return MODEL_RATES[key]
    return MODEL_RATES["sonnet"]


def _ensure_dir():
    os.makedirs(APP_DIR, exist_ok=True)


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg):
    _ensure_dir()
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def read_ledger():
    """Return list of ledger records (dicts). Missing/corrupt lines are skipped."""
    records = []
    try:
        with open(LEDGER_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return records


def append_ledger(record):
    _ensure_dir()
    record = dict(record)
    record.setdefault("ts", datetime.datetime.now(datetime.timezone.utc).isoformat())
    with open(LEDGER_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def cumulative_spent(records, session_key):
    """Cumulative USD for a session key.

    session_key is either a session id, or "day:YYYY-MM-DD" for the daily mode.
    A transcript baseline (kind="baseline") replaces older baselines for the
    same key; invocation records always accumulate.
    """
    baseline = 0.0
    total = 0.0
    for r in records:
        if r.get("session_key") != session_key:
            continue
        try:
            cost = float(r.get("cost_usd", 0))
        except (TypeError, ValueError):
            continue
        if r.get("kind") == "baseline":
            baseline = cost  # latest baseline wins
        elif r.get("kind") == "invocation":
            total += cost
    return baseline + total


def session_key_for(mode, session_id=None):
    if mode == "day":
        day = datetime.date.today().isoformat()
        return "day:" + day
    return session_id or ""


def extract_session_id(argv):
    """Pull --resume/--session-id value out of a claude argv list."""
    for i, a in enumerate(argv):
        if a in ("--resume", "--session-id", "-r") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--resume="):
            return a.split("=", 1)[1]
        if a.startswith("--session-id="):
            return a.split("=", 1)[1]
    return None


def set_budget_arg(argv, budget):
    """Return argv with --max-budget-usd set to min(existing, budget)."""
    out = []
    i = 0
    replaced = False
    while i < len(argv):
        a = argv[i]
        if a == "--max-budget-usd" and i + 1 < len(argv):
            try:
                existing = float(argv[i + 1])
            except ValueError:
                existing = budget
            out += [a, "%.2f" % min(existing, budget)]
            replaced = True
            i += 2
        elif a.startswith("--max-budget-usd="):
            try:
                existing = float(a.split("=", 1)[1])
            except ValueError:
                existing = budget
            out.append("--max-budget-usd=%.2f" % min(existing, budget))
            replaced = True
            i += 1
        else:
            out.append(a)
            i += 1
    if not replaced:
        out += ["--max-budget-usd", "%.2f" % budget]
    return out


def transcript_cost(session_id, projects_dir=None):
    """Sum usage x rates over a Claude Code session transcript. Returns USD float.

    Raises FileNotFoundError if no transcript found for the session.
    """
    base = projects_dir or os.path.expanduser("~/.claude/projects")
    target = None
    for root, _dirs, files in os.walk(base):
        if session_id + ".jsonl" in files:
            target = os.path.join(root, session_id + ".jsonl")
            break
    if target is None:
        raise FileNotFoundError("no transcript for session %s" % session_id)

    total = 0.0
    with open(target) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = entry.get("message") or {}
            usage = msg.get("usage") or {}
            if not usage:
                continue
            rates = _rates_for(msg.get("model", ""))
            total += usage.get("input_tokens", 0) / 1e6 * rates["input"]
            total += usage.get("output_tokens", 0) / 1e6 * rates["output"]
            total += usage.get("cache_read_input_tokens", 0) / 1e6 * rates["cache_read"]
            total += usage.get("cache_creation_input_tokens", 0) / 1e6 * rates["cache_write"]
    return total


def cmd_init(args):
    save_config({"cap_usd": args.cap})
    print("initialized: cap $%.2f -> %s" % (args.cap, CONFIG_PATH))


def cmd_run(args):
    cfg = load_config()
    cap = args.cap if args.cap is not None else cfg.get("cap_usd")
    if cap is None:
        print("error: no cap set (run `rbg init --cap N` or pass --cap)", file=sys.stderr)
        return 2
    cap = float(cap)

    claude_argv = args.command
    if not claude_argv:
        print("error: nothing to run (use `-- claude ...`)", file=sys.stderr)
        return 2

    sid = extract_session_id(claude_argv)
    key = session_key_for(args.session_key, sid)
    spent = cumulative_spent(read_ledger(), key)
    remaining = cap - spent

    label = key if key else "(fresh session)"
    if remaining <= 0:
        print(
            "BLOCKED: budget exhausted -- spent $%.2f of $%.2f cap on %s.\n"
            "Resume would have reset the cap; refusing to launch."
            % (spent, cap, label),
            file=sys.stderr,
        )
        return EXIT_BLOCKED

    final_argv = set_budget_arg(claude_argv, remaining)
    print("spent $%.2f / cap $%.2f -> launching with --max-budget-usd %.2f (%s)"
          % (spent, cap, min(remaining, cap), label))

    # Force JSON output so we can read total_cost_usd afterwards.
    if "--output-format" not in final_argv:
        final_argv += ["--output-format", "json"]

    try:
        proc = subprocess.run(final_argv, capture_output=True, text=True)
    except FileNotFoundError:
        print("error: executable not found: %s" % final_argv[0], file=sys.stderr)
        return 2

    cost = None
    out_sid = sid
    try:
        result = json.loads(proc.stdout)
        if isinstance(result, dict):
            if result.get("total_cost_usd") is not None:
                cost = float(result["total_cost_usd"])
            out_sid = result.get("session_id") or sid
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    if cost is not None:
        rec_key = session_key_for(args.session_key, out_sid)
        append_ledger({
            "session_key": rec_key,
            "session_id": out_sid,
            "kind": "invocation",
            "cost_usd": round(cost, 4),
        })
        print("recorded $%.4f for %s (cumulative now $%.2f)"
              % (cost, rec_key, cumulative_spent(read_ledger(), rec_key)))
    else:
        print("warning: could not read total_cost_usd from output; "
              "nothing recorded. Use `rbg sync --session <id>` later.",
              file=sys.stderr)

    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def cmd_sync(args):
    try:
        cost = transcript_cost(args.session, args.projects_dir)
    except FileNotFoundError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    append_ledger({
        "session_key": args.session,
        "session_id": args.session,
        "kind": "baseline",
        "cost_usd": round(cost, 4),
    })
    print("baseline $%.4f recorded for session %s (rates as of %s; estimates)"
          % (cost, args.session, PRICING_AS_OF))
    return 0


def cmd_status(args):
    records = read_ledger()
    cfg = load_config()
    cap = cfg.get("cap_usd")
    keys = sorted({r.get("session_key", "") for r in records if r.get("session_key")})
    if args.session:
        keys = [k for k in keys if args.session in k]
    if not keys:
        print("ledger empty.")
        return 0
    for k in keys:
        spent = cumulative_spent(records, k)
        if cap:
            print("%s: $%.2f / $%.2f (%.0f%%)" % (k, spent, cap, 100 * spent / cap))
        else:
            print("%s: $%.2f (no cap configured)" % (k, spent))
    return 0


def cmd_reset(args):
    if os.path.exists(LEDGER_PATH):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        archived = LEDGER_PATH + "." + stamp + ".bak"
        shutil.move(LEDGER_PATH, archived)
        print("ledger archived to %s" % archived)
    else:
        print("ledger already empty.")


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="rbg",
        description="Enforce a cumulative spend cap across Claude Code resumes.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="set the spend cap")
    pi.add_argument("--cap", type=float, required=True, help="cap in USD")
    pi.set_defaults(func=cmd_init)

    pr = sub.add_parser("run", help="wrap a claude invocation with the guard")
    pr.add_argument("--cap", type=float, default=None, help="override configured cap")
    pr.add_argument("--session-key", choices=["session", "day"], default="session",
                    help="'session': cap applies per session id; 'day': cap applies to all spend today")
    pr.add_argument("command", nargs=argparse.REMAINDER,
                    help="command to run after `--`, e.g. -- claude -p --resume SID prompt")
    pr.set_defaults(func=cmd_run)

    ps = sub.add_parser("sync", help="record transcript-derived baseline for a session")
    ps.add_argument("--session", required=True)
    ps.add_argument("--projects-dir", default=None)
    ps.set_defaults(func=cmd_sync)

    pt = sub.add_parser("status", help="show spend vs cap")
    pt.add_argument("--session", default=None)
    pt.set_defaults(func=cmd_status)

    pz = sub.add_parser("reset", help="archive the ledger")
    pz.set_defaults(func=cmd_reset)

    args = p.parse_args(argv)
    # argparse REMAINDER keeps the leading `--`; drop it.
    if getattr(args, "command", None) and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
