"""Harness MCP client: stdio child, handshake, tool loading, failure path (brief §3.6)."""

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
    assert names == ["echo", "do_tx", "boom"]
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


def test_handshake_failure_aborts(tmp_path):
    # A child that dies before the MCP handshake → HarnessError, which the
    # runner maps to session_end reason=errors with zero model calls (SPEC §2).
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
