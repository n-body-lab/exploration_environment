"""hivemarket CLI.

  hivemarket run   experiments/baseline.yaml        create run dir, run in Docker, write report
  hivemarket local experiments/local_smoke.yaml     run in-process (scripted agents / dev)
  hivemarket init  experiments/x.yaml [--mode ...]  only create the run directory
  hivemarket report runs/<run_id>                   (re)build timeline.jsonl + summary.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from hivemarket.orchestrator.report import write_report
from hivemarket.orchestrator.run import create_run


def main() -> None:
    p = argparse.ArgumentParser(prog="hivemarket")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("run", "local", "init"):
        sp = sub.add_parser(name)
        sp.add_argument("experiment")
        sp.add_argument("--runs-dir", default="runs")
        sp.add_argument("--run-id")
        if name == "init":
            sp.add_argument("--mode", choices=["docker", "local"], default="docker")
        if name == "run":
            sp.add_argument("--no-build", action="store_true")
        if name == "local":
            sp.add_argument("--unsafe-local-shell", action="store_true",
                            help="allow LLM agents to run shell commands unsandboxed on this machine")
            sp.add_argument("--echo", action="store_true", help="print events as they happen")
    rp = sub.add_parser("report")
    rp.add_argument("run_dir")
    args = p.parse_args()

    if args.cmd == "report":
        print(write_report(Path(args.run_dir)).read_text())
        return
    if args.cmd == "init":
        print(create_run(args.experiment, args.runs_dir, mode=args.mode, run_id=args.run_id))
        return
    if args.cmd == "run":
        from hivemarket.orchestrator.compose import run_docker
        run_dir = create_run(args.experiment, args.runs_dir, mode="docker", run_id=args.run_id)
        print(f"run dir: {run_dir}")
        run_docker(run_dir, build=not args.no_build)
    else:
        from hivemarket.orchestrator.local import run_local
        run_dir = create_run(args.experiment, args.runs_dir, mode="local", run_id=args.run_id)
        print(f"run dir: {run_dir}")
        results = asyncio.run(run_local(run_dir, allow_unsandboxed_shell=args.unsafe_local_shell, echo=args.echo))
        print(json.dumps(results, indent=2))
    print(write_report(run_dir).read_text())


if __name__ == "__main__":
    main()
