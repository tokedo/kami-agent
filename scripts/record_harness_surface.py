"""Record the pinned harness's tool surface into the smoke-tier fixture.

The fixture (``tests/smoke/fixtures/harness_tools.json``) is what the
default smoke tier serves as its tool surface, and what the live tier
asserts a real harness still matches. It was hand-maintained, which is
why it is committed here as a script instead: a fixture nobody can
regenerate is a fixture that silently rots against its pin.

Usage::

    uv run python scripts/record_harness_surface.py \\
        --harness-dir ~/kami-harness \\
        --python ~/kami-harness/.venv-smoke/bin/python

The interpreter is load-bearing, not tidiness. The harness's
agent-visible tool descriptions come from docstrings, and CPython 3.13
strips their common leading indentation at compile time while 3.12
retains it — so the same harness on 3.12 serves the SAME tools with
thousands more description characters, a different hash, and a higher
context floor. Production runs 3.13; a fixture recorded on anything else
describes a surface no run ever sees. This script refuses to record from
an interpreter that is not 3.13.

Two hash fields come out of a recording and they answer DIFFERENT
questions over DIFFERENT bytes (SPEC D1):

- ``harness_only_tools_hash`` — this scaffold's serialization of the
  harness tools ALONE. It is not what any session records: a session's
  ``session_start.tools_hash`` spans harness *and* scaffold tools, so it
  will never equal this value. The field used to be called
  ``tools_hash``, and that name caused a false-alarm class at run
  reconciliation — two different quantities under one name.
- ``harness_published_tools_hash`` — the harness's OWN hash of its OWN
  registry, taken verbatim from the handshake. Different by construction
  from both of the above. Never equate, reconcile, or assert them
  against each other.

The recording also carries the rest of what the handshake states: the
``schema_version`` token, and the **standing text** — everything after the
first newline of the ``instructions`` field, verbatim (kami-harness 4.0.0
and later; empty before). The smoke tier puts that text in the system
prompt exactly as a session does, so the floor it reports includes it, and
the live tier asserts a real harness still sends the same bytes. The text
states the harness's call time box, so it depends on that harness's
``KAMI_CALL_BUDGET_S`` as well as on its commit: record under the value
the runs will use (unset = the harness default).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "smoke" / "fixtures" / "harness_tools.json"

REQUIRED_PYTHON = "3.13"

# The harness refuses to start without a mainnet endpoint. A recording is
# a list_tools call and never dials it; a keyless public node keeps the
# script runnable without a secret.
RPC_FALLBACK = "https://ethereum-rpc.publicnode.com"


def _describe(harness_dir: Path) -> tuple[str, bool]:
    """The checkout's short SHA, and whether anything is modified on top of it.

    A recording taken over uncommitted work describes a surface that no
    pin can reproduce — which is not a hypothetical: the first attempt at
    this fixture picked up an unreleased in-flight tool from a dirty
    checkout, and the recording claimed to be the pinned ref.
    """
    if not (harness_dir / ".git").exists():
        # An extracted archive has no git metadata; the caller chose the
        # ref by extracting it, so there is nothing to be dirty.
        return "extracted", False
    sha = subprocess.run(  # noqa: S603
        ["git", "-C", str(harness_dir), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    status = subprocess.run(  # noqa: S603
        ["git", "-C", str(harness_dir), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return sha, bool(status)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness-dir", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--server", default="executor/server.py")
    parser.add_argument("--sha", help="record this ref name when the source has no git metadata")
    parser.add_argument("--out", default=FIXTURE, type=Path)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="record from a modified working tree (the recording then names no commit)",
    )
    args = parser.parse_args()

    harness_dir = args.harness_dir.expanduser().resolve()
    interpreter = args.python.expanduser().resolve()

    version = subprocess.run(  # noqa: S603
        [str(interpreter), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if version != REQUIRED_PYTHON:
        print(
            f"refusing to record from python {version}: the surface is "
            f"interpreter-dependent and production runs {REQUIRED_PYTHON}",
            file=sys.stderr,
        )
        return 2

    sha, dirty = _describe(harness_dir)
    if dirty and not args.allow_dirty:
        print(
            f"refusing to record from {harness_dir}: the working tree is dirty, "
            "so the recording would not describe any commit. Extract the pinned "
            "ref first (git archive <sha> | tar -x -C <dir>), or pass "
            "--allow-dirty deliberately.",
            file=sys.stderr,
        )
        return 2

    import os

    from kami_agent.harness import HarnessClient, tools_hash

    # The whole environment, as the smoke tier passes it: the MCP SDK's
    # default child env is minimal and the harness needs a real PATH.
    env = {**os.environ, "MAINNET_RPC_URL": os.environ.get("MAINNET_RPC_URL", RPC_FALLBACK)}
    # The handshake happens in the constructor, so a failure raises here.
    client = HarnessClient(
        command=str(interpreter),
        args=[args.server],
        cwd=str(harness_dir),
        env=env,
    )
    try:
        tools = list(client.tool_defs)
        published = client.harness_tools_hash
        name = client.server_name
        server_version = client.server_version
        schema_version = client.harness_schema_version
        standing_text = client.standing_text
    finally:
        client.close()

    surface = {
        "harness": {
            "name": name,
            "version": server_version,
            "sha": args.sha or sha,
            "recorded_under_python": version,
            # The handshake's own schema_version token (None before 3.0.0).
            "schema_version": schema_version,
            # The call box the standing text below states, as configured
            # for this recording (None = the harness default).
            "call_budget_s": os.environ.get("KAMI_CALL_BUDGET_S"),
        },
        # The handshake's standing text, verbatim ("" before 4.0.0). What
        # every session shows the model in its system prompt (SPEC D1).
        "standing_text": standing_text,
        # This scaffold's serialization of the HARNESS tools only — never
        # a session's session_start.tools_hash, which also spans the
        # scaffold surface (SPEC D1).
        "harness_only_tools_hash": tools_hash(tools),
        # The harness's own hash of its own registry, verbatim.
        "harness_published_tools_hash": published,
        "tools": [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ],
    }
    args.out.write_text(json.dumps(surface, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"recorded {len(tools)} tools and {len(standing_text)} chars of standing text "
        f"from {harness_dir} @ {args.sha or sha} (python {version}) → {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
