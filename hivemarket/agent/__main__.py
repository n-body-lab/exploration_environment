"""Container entrypoint: python -m hivemarket.agent (config in AGENT_CONFIG)."""

import asyncio
import os

from hivemarket.agent.runtime import AgentRuntime
from hivemarket.common.config import AgentRuntimeConfig


def main() -> None:
    cfg = AgentRuntimeConfig.model_validate_json(os.environ.pop("AGENT_CONFIG"))
    asyncio.run(AgentRuntime(cfg).run())


if __name__ == "__main__":
    main()
