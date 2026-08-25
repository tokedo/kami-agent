"""Smoke-tier configuration: .env loading and harness-mode selection.

The tri-provider live tier (README, Verification tier 3) talks to real provider APIs.
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

Sampling parameters (KAMI_SMOKE_MAX_TOKENS, KAMI_SMOKE_TEMPERATURE,
KAMI_SMOKE_REASONING_EFFORT, KAMI_SMOKE_MANIFEST):

    This tier used to send adapter defaults and nothing else, so it
    green-lit a request SHAPE the provider then rejected: a manifest
    pinning a reasoning effort was never exercised here, and the
    rejection was invisible to every pre-launch tier because no tier ever
    sent the manifest's own parameters.

    Unset, every knob leaves this tier byte-identical to what it was — CI
    keeps sending ``max_tokens=4096`` and nothing else. To validate a
    manifest before a launch, point the tier at the manifest itself::

        KAMI_SMOKE_MANIFEST=manifests/006-sonnet5-control.yaml \
        SMOKE_ANTHROPIC_MODEL=claude-sonnet-5 uv run pytest tests/smoke -q -s

    which reads that file's frozen ``params:`` block and sends exactly
    it. The three individual knobs override the manifest per field, so a
    one-off shape can be tried without editing a frozen file. The
    effective parameters are printed in the SMOKE summary line, so the
    log records what was actually sent rather than what was intended.

    Note that the manifest also pins provider and model, which this tier
    does not read: it is parametrized across all three providers, so the
    model comes from SMOKE_<PROVIDER>_MODEL as it always has.
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
