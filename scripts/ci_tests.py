"""Run all discovered test modules in balanced, isolated CI shards."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def modules(root=ROOT):
    return sorted(".".join(path.relative_to(root).with_suffix("").parts)
                  for path in (root / "tests").rglob("test_*.py"))


def load_module(name, loader=None):
    loader = loader or unittest.TestLoader()
    try:
        return loader.loadTestsFromName(name)
    except unittest.SkipTest as error:
        def skipped():
            raise unittest.SkipTest(reason)
        reason = str(error)
        return unittest.TestSuite([unittest.FunctionTestCase(skipped, description=name)])


def plan(shards, root=ROOT):
    if shards < 1:
        raise ValueError("shards must be at least 1")
    loader = unittest.TestLoader()
    entries = [(name, load_module(name, loader).countTestCases()) for name in modules(root)]
    if not entries:
        raise RuntimeError("No test modules found")
    if loader.errors:
        raise RuntimeError("Test discovery failed:\n" + "\n".join(loader.errors))
    groups, loads = [[] for _ in range(shards)], [0] * shards
    for name, count in sorted(entries, key=lambda entry: (-entry[1], entry[0])):
        index = min(range(shards), key=lambda i: (loads[i], i))
        groups[index].append(name)
        loads[index] += count
    return [{"modules": sorted(group), "tests": count}
            for group, count in zip(groups, loads)]


def run_modules(names, report):
    outcomes = []
    for name in names:
        started = time.monotonic()
        process = subprocess.run([sys.executable, "-m", "scripts.ci_tests", "--module", name],
                                 cwd=ROOT, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
        outcome = {"module": name, "seconds": round(time.monotonic() - started, 2),
                   "passed": process.returncode == 0}
        outcomes.append(outcome)
        if process.returncode:
            print(name + " failed", file=sys.stderr)
            print((process.stdout + process.stderr)[-24000:], file=sys.stderr)
        if report:
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(outcomes, indent=2), encoding="utf-8")
    summary = {"passed": all(row["passed"] for row in outcomes), "modules": len(outcomes),
               "seconds": round(sum(row["seconds"] for row in outcomes), 2),
               "slowest": sorted(outcomes, key=lambda row: -row["seconds"])[:5]}
    print(json.dumps(summary), flush=True)
    return 0 if summary["passed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--shard", type=int)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--module", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.module:
        result = unittest.TextTestRunner(verbosity=1).run(load_module(args.module))
        return 0 if result.wasSuccessful() else 1
    if args.shards < 1 or (args.shard is not None and not 1 <= args.shard <= args.shards):
        parser.error("require --shards >= 1 and 1 <= --shard <= --shards")
    groups = plan(args.shards)
    if args.plan:
        print(json.dumps(groups))
        return 0
    if args.shard is None:
        parser.error("--shard is required unless --plan is used")
    return run_modules(groups[args.shard - 1]["modules"], args.report)


if __name__ == "__main__":
    raise SystemExit(main())
