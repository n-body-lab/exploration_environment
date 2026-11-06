# Multi-target image. Targets: core (market/collector/gateway), agent, monitor.
FROM python:3.12-slim AS core
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
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
 && useradd --create-home --uid 1000 --shell /bin/bash sandbox
# The harness is readable only by root. Agents (the sandbox user) must not be able to
# read it: the code reveals the monitoring and off-channel detectors, which would make
# agents aware they are in a monitored test. The runtime runs as root and owns these files.
RUN SITE="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
 && chmod -R go-rwx /app "$SITE"/hivemarket "$SITE"/hivemarket-*.dist-info
# Runs as root only so the bash tool can drop to the sandbox user; all other
# capabilities are dropped in compose.
CMD ["python", "-m", "hivemarket.agent"]

FROM core AS monitor
RUN apt-get update \
 && apt-get install -y --no-install-recommends tcpdump \
 && rm -rf /var/lib/apt/lists/*
CMD ["python", "-m", "hivemarket.monitor"]
