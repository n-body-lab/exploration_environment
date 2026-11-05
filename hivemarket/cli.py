"""hivemarket CLI.

  hivemarket observatory up|down|status       long-lived ClickHouse + MinIO + Grafana
  hivemarket run   experiments/baseline.yaml   create run dir, run in Docker, ship live, write report
  hivemarket local experiments/local_smoke.yaml  run in-process (scripted agents / dev)
  hivemarket init  experiments/x.yaml [--mode ...]  only create the run directory
  hivemarket report runs/<run_id>              (re)build timeline.jsonl + summary.md
  hivemarket ingest runs/<run_id> [--full]     (re)ship a run to the observatory
  hivemarket backup --to /path                 ClickHouse backup + mirror of object storage
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from hivemarket.orchestrator.report import write_report
from hivemarket.orchestrator.run import create_run


def make_shipper(run_dir: Path, required: bool = False):
    from hivemarket.observatory.clients import ClickHouse, ObjectStore
    from hivemarket.observatory.manage import available
    from hivemarket.observatory.settings import ObservatorySettings
    from hivemarket.observatory.shipper import Shipper

    s = ObservatorySettings.load()
    if not available(s):
        msg = "observatory not reachable; run data stays in the run directory (ship later with `hivemarket ingest`)"
        if required:
            sys.exit(msg)
        print(f"warning: {msg}", file=sys.stderr)
        return None
    return Shipper(run_dir, ClickHouse.from_settings(s), ObjectStore.from_settings(s), bucket=s.bucket)


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
        else:
            sp.add_argument("--no-ship", action="store_true", help="do not stream to the observatory")
        if name == "run":
            sp.add_argument("--no-build", action="store_true")
        if name == "local":
            sp.add_argument("--unsafe-local-shell", action="store_true",
                            help="allow LLM agents to run shell commands unsandboxed on this machine")
            sp.add_argument("--echo", action="store_true", help="print events as they happen")
    rp = sub.add_parser("report")
    rp.add_argument("run_dir")
    ip = sub.add_parser("ingest")
    ip.add_argument("run_dir")
    ip.add_argument("--full", action="store_true", help="ignore checkpoints and re-ship everything (dedup-safe)")
    op = sub.add_parser("observatory")
    op.add_argument("action", choices=["up", "down", "status"])
    bp = sub.add_parser("backup")
    bp.add_argument("--to", required=True, type=Path, help="destination directory (external drive, NAS, ...)")
    args = p.parse_args()

    if args.cmd == "observatory":
        from hivemarket.observatory import manage
        getattr(manage, args.action)()
        return
    if args.cmd == "backup":
        from hivemarket.observatory.manage import backup
        backup(args.to)
        return
    if args.cmd == "report":
        print(write_report(Path(args.run_dir)).read_text())
        return
    if args.cmd == "ingest":
        run_dir = Path(args.run_dir)
        if not (run_dir / "summary.json").exists():
            write_report(run_dir)
        shipper = make_shipper(run_dir, required=True)
        if args.full:
            shipper.reset()
        print(json.dumps(shipper.finish(status="ingested")))
        return
    if args.cmd == "init":
        print(create_run(args.experiment, args.runs_dir, mode=args.mode, run_id=args.run_id))
        return

    mode = "docker" if args.cmd == "run" else "local"
    run_dir = create_run(args.experiment, args.runs_dir, mode=mode, run_id=args.run_id)
    print(f"run dir: {run_dir}")
    shipper = None if args.no_ship else make_shipper(run_dir)
    if shipper:
        shipper.start()
    status = "finished"
    try:
        if mode == "docker":
            from hivemarket.orchestrator.compose import run_docker
            run_docker(run_dir, build=not args.no_build)
        else:
            from hivemarket.orchestrator.local import run_local
            results = asyncio.run(run_local(run_dir, allow_unsandboxed_shell=args.unsafe_local_shell, echo=args.echo))
            print(json.dumps(results, indent=2))
    except BaseException:
        status = "failed"
        raise
    finally:
        print(write_report(run_dir).read_text())
        if shipper:
            print(f"shipped: {shipper.finish(status)}")


if __name__ == "__main__":
    main()
