#!/usr/bin/env python3
"""Smoke tests for resume-budget-guard. Stdlib unittest only.

Uses a fake `claude` shim (a small python script) that records its argv and
returns canned JSON with total_cost_usd, so no real API spend happens.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import resume_budget_guard as rbg

SHIM_SRC = """\
import json, os, sys
log = os.environ.get("SHIM_ARGV_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
result = {
    "type": "result",
    "session_id": os.environ.get("SHIM_SESSION", "shim-session"),
    "total_cost_usd": float(os.environ.get("SHIM_COST", "0")),
    "is_error": False,
}
print(json.dumps(result))
"""


def make_transcript(path, turns):
    """Write a synthetic Claude Code session transcript."""
    with open(path, "w") as f:
        for model, usage in turns:
            f.write(json.dumps({
                "type": "assistant",
                "message": {"model": model, "usage": usage},
            }) + "\n")


class RbgTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Isolate the app dir per test.
        rbg.APP_DIR = os.path.join(self.tmp, "app")
        rbg.CONFIG_PATH = os.path.join(rbg.APP_DIR, "config.json")
        rbg.LEDGER_PATH = os.path.join(rbg.APP_DIR, "ledger.jsonl")
        # Fake claude shim.
        self.shim = os.path.join(self.tmp, "fake-claude")
        with open(self.shim, "w") as f:
            f.write("#!/usr/bin/env python3\n" + SHIM_SRC)
        os.chmod(self.shim, 0o755)
        self.argv_log = os.path.join(self.tmp, "argv.log")
        self.env = dict(os.environ)
        self.env["SHIM_ARGV_LOG"] = self.argv_log
        self._old_environ = os.environ
        os.environ.clear()
        os.environ.update(self.env)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_environ)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_rbg(self, argv, cost="0", session="shim-session"):
        os.environ["SHIM_COST"] = cost
        os.environ["SHIM_SESSION"] = session
        return rbg.main(argv)

    def shim_argvs(self):
        if not os.path.exists(self.argv_log):
            return []
        with open(self.argv_log) as f:
            return [json.loads(l) for l in f if l.strip()]

    def budget_in(self, argv):
        """Extract --max-budget-usd value from a shim argv list."""
        for i, a in enumerate(argv):
            if a == "--max-budget-usd":
                return float(argv[i + 1])
            if a.startswith("--max-budget-usd="):
                return float(a.split("=", 1)[1])
        return None

    # 1. Ledger accumulates across runs.
    def test_ledger_accumulates(self):
        self.assertEqual(self.run_rbg(
            ["run", "--cap", "10", "--", self.shim, "-p", "one"], cost="2"), 0)
        self.assertEqual(self.run_rbg(
            ["run", "--cap", "10", "--", self.shim, "-p", "two"], cost="2"), 0)
        spent = rbg.cumulative_spent(rbg.read_ledger(), "shim-session")
        self.assertAlmostEqual(spent, 4.0)

    # 2. Resume does NOT reset the cap: $4 of $5 spent -> only $1 allowed.
    def test_resume_cap_not_reset(self):
        rbg.append_ledger({"session_key": "sess-1", "session_id": "sess-1",
                           "kind": "invocation", "cost_usd": 4.0})
        rc = self.run_rbg(
            ["run", "--cap", "5", "--", self.shim, "-p", "--resume", "sess-1", "hi"],
            cost="0.5", session="sess-1")
        self.assertEqual(rc, 0)
        argvs = self.shim_argvs()
        self.assertEqual(len(argvs), 1)
        self.assertAlmostEqual(self.budget_in(argvs[0]), 1.0)

    # 3. Over budget: wrapper refuses, shim never executes.
    def test_over_budget_blocked(self):
        rbg.append_ledger({"session_key": "sess-9", "session_id": "sess-9",
                           "kind": "invocation", "cost_usd": 5.0})
        rc = self.run_rbg(
            ["run", "--cap", "5", "--", self.shim, "-p", "--resume", "sess-9", "hi"],
            cost="1", session="sess-9")
        self.assertEqual(rc, rbg.EXIT_BLOCKED)
        self.assertEqual(self.shim_argvs(), [])

    # 4. Transcript sync math: usage x rates.
    def test_transcript_sync(self):
        proj = os.path.join(self.tmp, "projects", "proj")
        os.makedirs(proj)
        make_transcript(os.path.join(proj, "s1.jsonl"), [
            ("claude-sonnet-4-6", {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
        ])
        rc = rbg.main(["sync", "--session", "s1", "--projects-dir",
                       os.path.join(self.tmp, "projects")])
        self.assertEqual(rc, 0)
        spent = rbg.cumulative_spent(rbg.read_ledger(), "s1")
        # 1M input @ $3 + 1M output @ $15 = $18
        self.assertAlmostEqual(spent, 18.0)

    # 5. Fresh session (no --resume) gets the full cap.
    def test_fresh_session_full_cap(self):
        rc = self.run_rbg(["run", "--cap", "7", "--", self.shim, "-p", "new"],
                          cost="1", session="brand-new")
        self.assertEqual(rc, 0)
        argvs = self.shim_argvs()
        self.assertEqual(len(argvs), 1)
        self.assertAlmostEqual(self.budget_in(argvs[0]), 7.0)

    # 6. status shows per-session totals.
    def test_status_output(self):
        rbg.append_ledger({"session_key": "a", "kind": "invocation", "cost_usd": 1.5})
        rbg.append_ledger({"session_key": "b", "kind": "invocation", "cost_usd": 2.5})
        rbg.save_config({"cap_usd": 5.0})
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = rbg.main(["status"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("a: $1.50 / $5.00", out)
        self.assertIn("b: $2.50 / $5.00", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
