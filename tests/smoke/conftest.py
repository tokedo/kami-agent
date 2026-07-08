"""Smoke-tier configuration: .env loading and harness-mode selection.

The tri-provider live tier (SPEC §11.2) talks to real provider APIs.
Keys come from the environment or from the repo-root ``.env`` (never
committed). Tests skip per provider when the key is absent.

Harness modes (KAMI_SMOKE_HARNESS):
- ``fake`` (default, used in CI): a stand-in serving the *recorded* real
  tool surface (tests/smoke/fixtures/harness_tools.json) with simulated
  execution — real model calls, no chain access.
- ``real``: spawns the pinned kami-harness (KAMI_HARNESS_DIR, python at
  KAMI_HARNESS_PYTHON) with live read-only RPC. Execution is wrapped in a
  read-only allowlist: the model sees the full tool surface, but only
  get_*/list_* tools execute — anything else gets an error result, so a
  stray intent can never sign a transaction.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(REPO_ROOT / ".env")
