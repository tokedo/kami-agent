"""A tiny stdio MCP server standing in for kami-harness in unit tests.

Same shape as the real thing (FastMCP over stdio, dict results carrying
tx_hash for transaction tools) with none of the world behind it.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-kami-harness")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo: {text}"


@mcp.tool()
def do_tx(amount: int) -> dict:
    """Pretend to submit a transaction."""
    return {"ok": True, "tx_hash": "0xdeadbeef", "amount": amount}


@mcp.tool()
def boom() -> str:
    """Always fails."""
    raise RuntimeError("kaboom")


if __name__ == "__main__":
    mcp.run()
