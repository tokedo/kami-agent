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
shape. It no longer serves the session-start brief — the scaffold reads
the daemon directly for that (SPEC D7) — but it is still the tool the
agent calls for full per-kami detail, so its shape stays pinned here.
The roster is fixture data, not a recording.

The two multi-transaction tools reproduce the two nesting levels the
harness reports per-transaction receipts at: one array for the whole
call, and one per row inside a batch's result list. ``error_payload``
reproduces a tool that reports failure by RETURNING it rather than
raising it, which is the shape ``result_error_shaped`` exists to name.
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
        "meta": {
            "servedAt": "2026-08-07T12:00:00.000Z",
            "blockNumber": 41,
            "stale": False,
            "mode": "daemon",
        },
    }


@mcp.tool()
def get_gas_balance(account: str = "") -> dict:
    """Check native ETH gas balances for the account's wallets on Yominet
    (and the owner's Ethereum mainnet balance when configured).

    The one tool the scaffold depends on by name (SPEC D1): the
    session-start gas-balance injection calls it with no arguments, which
    the real harness reads as "every account". Signature and payload shape
    are copied from the pinned harness, not recorded, so this fails when
    the harness's shape drifts.

    Args:
        account: Account label; empty reports every account.
    """
    return {
        "balances": {
            "main": {
                "operator_address": "0x000000000000000000000000000000000000ope1",
                "operator_eth": "0.019428",
                "owner_address": "0x000000000000000000000000000000000000own1",
                "owner_eth": "0.008112",
                "owner_mainnet_eth": "0",
            }
        }
    }


@mcp.tool()
def multi_hop_tx() -> dict:
    """A multi-hop call reporting one receipt array for the whole call."""
    return {
        "reached_target": False,
        "final_room": 8,
        "error": "item 21201 use failed: out of stamina",
        "txs": [
            {"step": "move", "room": 8, "tx_hash": "0xaa", "status": "success", "gas_used": 1200},
            {"step": "item", "item_id": 21201, "status": "error"},
        ],
    }


@mcp.tool()
def batch_rows_tx() -> dict:
    """A batch reporting one receipt array per result row."""
    return {
        "count": 2,
        "ok": 1,
        "results": [
            {"kami_id": 1, "txs": [{"tx_hash": "0xb1", "status": "success", "block": 9}]},
            {"kami_id": 2, "error": "skill: reverted", "txs": [{"tx_hash": "0xb2"}]},
        ],
    }


@mcp.tool()
def error_payload() -> dict:
    """A tool that RETURNS its failure instead of raising it."""
    return {"error": "could not determine current room from API response"}


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
