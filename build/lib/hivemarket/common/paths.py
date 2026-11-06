"""Locating the source checkout (prompts/, docker/, observatory/, tests/data/).

Runs need files that live in the repository, not in the installed package:
the default prompt template, the Docker build context, the observatory
compose file. With an editable install (`pip install -e .`) the package sits
inside the checkout; with a regular install it lives in site-packages, so
the checkout is found via $HIVEMARKET_HOME or the current directory.
"""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    candidates: list[Path] = []
    if os.environ.get("HIVEMARKET_HOME"):
        candidates.append(Path(os.environ["HIVEMARKET_HOME"]).expanduser())
    candidates.append(Path(__file__).resolve().parents[2])
    cwd = Path.cwd().resolve()
    candidates += [cwd, *cwd.parents]
    for c in candidates:
        if (c / "docker" / "Dockerfile").is_file() and (c / "prompts" / "system.md").is_file():
            return c
    raise RuntimeError("Cannot find the HiveMarket source checkout (prompts/, docker/). Run from inside the "
                       "repository, install it with `pip install -e .`, or set HIVEMARKET_HOME to its path.")
