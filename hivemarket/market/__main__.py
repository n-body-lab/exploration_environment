"""Container entrypoint: python -m hivemarket.market"""

import json
import os
from pathlib import Path

import uvicorn

from hivemarket.common.events import EventLog, JsonlSink
from hivemarket.common.manifest import Manifest
from hivemarket.market.app import create_app
from hivemarket.market.state import Market


def build(run_dir: Path, echo: bool = False):
    manifest = Manifest.load(run_dir / "manifest.json")
    world = json.loads((run_dir / "world.json").read_text())
    log = EventLog("market", manifest.run_id, JsonlSink(run_dir / "logs" / "market.jsonl"), echo=echo)
    rules = world.get("rules", {})
    market = Market(
        world,
        log,
        duration_seconds=manifest.duration_seconds,
        allow_price_discrimination=rules.get("allow_price_discrimination", True),
        allow_broadcast=rules.get("allow_broadcast", True),
        max_submissions=rules.get("max_submissions", 3),
    )
    return create_app(market, manifest, log, final_state_path=run_dir / "final_state.json")


def main() -> None:
    run_dir = Path(os.environ.get("RUN_DIR", "/run_data"))
    uvicorn.run(build(run_dir, echo=True), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), log_level="warning")


if __name__ == "__main__":
    main()
