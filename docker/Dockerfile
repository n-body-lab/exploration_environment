# Multi-target image. Targets: core (market/collector/gateway), agent, monitor.
FROM python:3.12-slim AS core
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml ./
COPY hivemarket ./hivemarket
RUN pip install ".[agent,monitor]"

FROM core AS agent
# Tools available to the agent's bash tool. Which tools exist is itself an
# experimental condition; keep this list deliberate.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl netcat-openbsd iproute2 dnsutils procps \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 --shell /bin/bash sandbox \
 && chmod 700 /app
# Runs as root only so the bash tool can drop to the sandbox user; all other
# capabilities are dropped in compose.
CMD ["python", "-m", "hivemarket.agent"]

FROM core AS monitor
RUN apt-get update \
 && apt-get install -y --no-install-recommends tcpdump \
 && rm -rf /var/lib/apt/lists/*
CMD ["python", "-m", "hivemarket.monitor"]
