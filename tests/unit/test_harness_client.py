"""Harness MCP client: stdio child, handshake, tool loading, failure path (SPEC D1)."""

import sys
from pathlib import Path

import pytest

from kami_agent.harness import HarnessClient, HarnessError, tools_hash
from kami_agent.loop import GameTools
from kami_agent.tools.errors import ToolError
from kami_agent.tools.scaffold import SCAFFOLD_TOOL_DEFS

FAKE_SERVER = Path(__file__).parent / "fake_mcp_server.py"


@pytest.fixture(scope="module")
def client():
    with HarnessClient(sys.executable, [str(FAKE_SERVER)], handshake_timeout_s=30) as c:
        yield c


def test_handshake_loads_tools(client):
    assert client.server_name == "fake-kami-harness"
    names = [t.name for t in client.tool_defs]
    assert names == [
        "echo",
        "lens_party",
        "get_gas_balance",
        "multi_hop_tx",
        "batch_rows_tx",
        "error_payload",
        "do_tx",
        "boom",
        "confirmed_tx",
        "revert_tx",
        "unconfirmed_tx",
        "rejected_tx",
        "batch_tx",
    ]
    echo = client.tool_defs[0]
    assert echo.description == "Echo the text back."
    assert echo.input_schema["type"] == "object"
    assert "text" in echo.input_schema["properties"]


def test_satisfies_game_tools_protocol(client):
    assert isinstance(client, GameTools)


def test_execute_returns_text_content(client):
    result = client.execute("echo", {"text": "hello"})
    assert result.content == "echo: hello"
    assert result.tx_hash is None


def test_tx_hash_extracted_from_transaction_results(client):
    result = client.execute("do_tx", {"amount": 5})
    assert result.tx_hash == "0xdeadbeef"
    assert '"amount": 5' in result.content.replace("'", '"') or "5" in result.content


def test_tool_failure_raises_tool_error(client):
    with pytest.raises(ToolError, match="kaboom"):
        client.execute("boom", {})


def test_unknown_tool_raises_tool_error(client):
    with pytest.raises(ToolError):
        client.execute("no_such_tool", {})


def test_raised_terminal_states_reach_the_caller_verbatim(client):
    """A revert and an unconfirmed tx are errors here, with the text untouched.

    The harness raises both rather than returning them, so they arrive as
    isError results. Every fact the harness stated — hash, block, gas,
    revert reason, the do-not-blind-retry warning — must survive to the
    tool result the model reads; the scaffold adds and removes nothing.
    """
    with pytest.raises(ToolError) as revert:
        client.execute("revert_tx", {})
    message = str(revert.value)
    for fact in ("0xbadbeef", "block 77", "REVERTED", "91234 gas", "insufficient stamina"):
        assert fact in message

    with pytest.raises(ToolError) as unconfirmed:
        client.execute("unconfirmed_tx", {})
    message = str(unconfirmed.value)
    for fact in ("0xfeed", "UNCONFIRMED", "120s", "a blind retry can"):
        assert fact in message


def test_confirmed_success_is_classified_on_the_returned_result(client):
    result = client.execute("confirmed_tx", {})
    assert result.terminal_state == "confirmed_success"
    assert result.tx_hash == "0xc0ffee"


def test_reads_carry_no_terminal_state(client):
    """Only transaction outcomes get one; a read is not one."""
    assert client.execute("echo", {"text": "hi"}).terminal_state is None


def test_handshake_failure_aborts(tmp_path):
    # A child that dies before the MCP handshake → HarnessError, which the
    # runner maps to session_end reason=errors with zero model calls (SPEC D1).
    with pytest.raises(HarnessError):
        HarnessClient(sys.executable, ["-c", "import sys; sys.exit(3)"], handshake_timeout_s=10)


def test_handshake_failure_on_nonsense_output():
    with pytest.raises(HarnessError):
        HarnessClient(
            sys.executable,
            ["-c", "print('not an mcp server'); import time; time.sleep(1)"],
            handshake_timeout_s=3,
        )


def test_tools_hash_is_deterministic_and_sensitive():
    h1 = tools_hash(SCAFFOLD_TOOL_DEFS)
    h2 = tools_hash(list(SCAFFOLD_TOOL_DEFS))
    assert h1 == h2
    assert h1.startswith("sha256:")
    assert tools_hash(SCAFFOLD_TOOL_DEFS[:-1]) != h1


def test_close_is_idempotent():
    c = HarnessClient(sys.executable, [str(FAKE_SERVER)], handshake_timeout_s=30)
    c.close()
    c.close()
    with pytest.raises(ToolError, match="not connected"):
        c.execute("echo", {"text": "after close"})


# --- in-band per-transaction receipts (P9 txs, 0.4.0) -------------------------


def test_per_hop_receipts_are_copied_from_a_top_level_array(client):
    """One receipt array for the whole call: the multi-hop shape."""
    result = client.execute("multi_hop_tx", {})
    assert [t.get("tx_hash") for t in result.txs] == ["0xaa", None]
    # Verbatim, including the hop that failed — it is a real transaction
    # whether or not the call as a whole succeeded.
    assert result.txs[0]["status"] == "success"
    assert result.txs[1]["status"] == "error"


def test_per_row_receipts_are_copied_from_inside_a_batch_result_list(client):
    """One receipt array per result row: the batch shape. A single-location
    extractor would silently miss exactly these."""
    result = client.execute("batch_rows_tx", {})
    assert [t["tx_hash"] for t in result.txs] == ["0xb1", "0xb2"]


def test_a_result_with_no_receipts_carries_none(client):
    assert client.execute("confirmed_tx", {}).txs == ()
    assert client.execute("echo", {"text": "hi"}).txs == ()


def test_the_harness_publishes_its_own_registry_hash_in_the_handshake(client):
    """Recorded, never equated with the scaffold's own value (D1).

    The fake server publishes no such hash, which is itself the contract:
    absence is recorded as absence rather than guessed at.
    """
    assert client.harness_tools_hash is None
    scaffold_side = tools_hash(list(client.tool_defs) + list(SCAFFOLD_TOOL_DEFS))
    assert scaffold_side.startswith("sha256:")


def test_the_published_hash_is_parsed_from_the_instructions_field():
    """The harness states it as `tools_hash=<64 hex>` in the handshake."""
    from kami_agent.harness import _HANDSHAKE_TOOLS_HASH

    real = "7fc11fe95b85ebeed4f898e774c50833cd63314d56c3ed18b5afa56989f75262"
    assert _HANDSHAKE_TOOLS_HASH.search(f"tools_hash={real}").group(1) == real
    assert _HANDSHAKE_TOOLS_HASH.search("no hash here") is None
    # Bare hex, no prefix — the scaffold's own value carries `sha256:` and
    # is a different value over different bytes. Never equate.
    assert not real.startswith("sha256:")
