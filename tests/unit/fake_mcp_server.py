"""A tiny stdio MCP server standing in for kami-harness in unit tests.

Same shape as the real thing (FastMCP over stdio, dict results carrying
tx_hash for transaction tools) with none of the world behind it.

The four raising tools reproduce the harness's three post-broadcast
terminal states plus its pre-signing rejection, message text included and
raised the same way, so the client is exercised through the real FastMCP
error path — which wraps a raised exception as
``Error executing tool <name>: <message>`` before it reaches the client.
Copied text, not imported: the point is to fail when the harness's
wording drifts away from what the classifier matches.

``lens_party`` carries the pinned harness's signature and its envelope
shape so the session-start brief (SPEC P1.12) runs end to end here and in
the cron-environment smoke. The roster is fixture data, not a recording.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-kami-harness")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo: {text}"


@mcp.tool()
def lens_party(account_index: int = -1) -> dict:
    """Party report for an account: every kami with full vitals.

    Args:
        account_index: Account index (-1: daemon default operator).
    """
    return {
        "data": {
            "account": {"index": 7, "name": "fixture"},
            "kamis": [
                {
                    "id": "0x01",
                    "index": 1,
                    "name": "one",
                    "state": "HARVESTING",
                    "level": 12,
                    "hp": {"current": 41, "total": 90, "percent": 45.6},
                    "hpRatePerHr": "-3.10",
                    "musu": {"accrued": 812, "spotRatePerHr": "44.0", "avgRatePerHr": "41.2"},
                    "cooldownSec": 0,
                    "node": {"index": 53, "name": "fixture node"},
                },
                {
                    "id": "0x02",
                    "index": 2,
                    "name": "two",
                    "state": "RESTING",
                    "level": 9,
                    "hp": {"current": 70, "total": 70, "percent": 100.0},
                    "hpRatePerHr": "0.00",
                    "musu": {"accrued": 0, "spotRatePerHr": "0.0", "avgRatePerHr": "0.0"},
                    "cooldownSec": 118,
                },
            ],
        },
        "untrusted": ["account.name", "kamis[].name", "kamis[].node.name"],
        "meta": {"stale": 3, "block": 41},
    }


@mcp.tool()
def do_tx(amount: int) -> dict:
    """Pretend to submit a transaction."""
    return {"ok": True, "tx_hash": "0xdeadbeef", "amount": amount}


@mcp.tool()
def boom() -> str:
    """Always fails."""
    raise RuntimeError("kaboom")


@mcp.tool()
def confirmed_tx() -> dict:
    """A transaction whose receipt confirmed."""
    return {"tx_hash": "0xc0ffee", "status": "success", "block": 41, "gas_used": 21000}


@mcp.tool()
def revert_tx() -> dict:
    """A transaction that landed and reverted."""
    raise RuntimeError(
        "transaction 0xbadbeef landed on-chain in block 77 and REVERTED: gas "
        "was spent (91234 gas) and no state change was applied. Revert reason "
        "(best-effort eth_call replay at block 77): insufficient stamina"
    )


@mcp.tool()
def unconfirmed_tx() -> dict:
    """A transaction broadcast without a receipt."""
    raise RuntimeError(
        "transaction 0xfeed is UNCONFIRMED: it was broadcast, but no receipt "
        "arrived within 120s. It may still be included and spend gas later. "
        "Check its on-chain status before retrying — a blind retry can "
        "execute the action twice."
    )


@mcp.tool()
def rejected_tx() -> dict:
    """A transaction rejected before signing."""
    raise ValueError("validation failed; no transaction sent: kami 42 is RESTING, not HARVESTING")


@mcp.tool()
def batch_tx() -> dict:
    """A multi-transaction call with a failed item."""
    raise RuntimeError(
        "stop_harvest_batch: 1 of 2 items failed. Items reported successful "
        "below are final on-chain (their gas was spent and their state "
        "changes applied) — do not resubmit them. Per-item outcomes: "
        '[{"kami": 1, "status": "success"}, {"kami": 2, "status": "reverted"}]'
    )


if __name__ == "__main__":
    mcp.run()
