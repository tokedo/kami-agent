"""CLI: manifest → config builders, init layout + run_start, status (SPEC §10)."""

import json
from pathlib import Path

import pytest
import yaml
from eth_account import Account

from kami_agent import cli
from kami_agent.adapters.anthropic import AnthropicAdapter
from kami_agent.adapters.base import AdapterResponse, StopReason, ToolCall, Usage
from kami_agent.adapters.google import GoogleAdapter
from kami_agent.adapters.openai import OpenAIAdapter
from kami_agent.telemetry import read_events, validate_event

EXAMPLE = Path(__file__).parents[2] / "manifests" / "example.yaml"


@pytest.fixture
def manifest_path(tmp_path):
    manifest = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    manifest.pop("harness", None)  # no MCP child in unit tests
    manifest.pop("chain_rpc_url", None)
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


def test_example_manifest_builds_run_config(tmp_path):
    manifest = cli.load_manifest(EXAMPLE)
    config = cli.build_run_config(manifest, tmp_path)
    assert config.model == "claude-haiku-4-5"
    assert config.prices.input_usd_per_mtok == 1.0
    assert config.caps.session_token_cap == 120_000
    assert config.caps.session_tool_cap == 50
    assert config.params.max_tokens == 4096
    assert config.params.temperature is None
    assert config.budget_usd == 10.0
    assert config.wake_min_minutes == 5
    assert config.wake_max_minutes == 1440
    assert manifest["_manifest_hash"].startswith("sha256:")


def test_build_adapter_per_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert isinstance(cli.build_adapter({"provider": "anthropic", "model": "m"}), AnthropicAdapter)
    assert isinstance(cli.build_adapter({"provider": "openai", "model": "m"}), OpenAIAdapter)
    assert isinstance(cli.build_adapter({"provider": "google", "model": "m"}), GoogleAdapter)
    with pytest.raises(SystemExit):
        cli.build_adapter({"provider": "azure", "model": "m"})


def test_init_creates_run_layout_and_run_start(tmp_path, manifest_path, capsys):
    run_dir = tmp_path / "run"
    rc = cli.main(
        [
            "init",
            "--manifest",
            str(manifest_path),
            "--run-dir",
            str(run_dir),
            "--skip-connectivity",
        ]
    )
    assert rc == 0

    # Layout (SPEC §7): config copy, prompts, workspace, transcripts.
    config = yaml.safe_load((run_dir / "config.yaml").read_text(encoding="utf-8"))
    assert config["run_id"] == "dev-smoke-001"
    assert "_manifest_hash" not in config
    assert (run_dir / "prompts" / "system.txt").exists()
    assert (run_dir / "workspace").is_dir()
    assert (run_dir / "transcripts").is_dir()

    # Wallet: address in config, key only in .env (mode 600).
    env_text = (run_dir / ".env").read_text(encoding="utf-8")
    assert config["wallet_address"].startswith("0x")
    key_line = next(line for line in env_text.splitlines() if "WALLET_PRIVATE_KEY" in line)
    key = key_line.split("=", 1)[1]
    assert Account.from_key(key).address == config["wallet_address"]
    assert (run_dir / ".env").stat().st_mode & 0o777 == 0o600
    assert "WALLET_PRIVATE_KEY" not in (run_dir / "config.yaml").read_text(encoding="utf-8")

    # run_start emitted and schema-valid; manifest_hash matches the file.
    events = list(read_events(run_dir / "telemetry.jsonl"))
    assert [e["event"] for e in events] == ["run_start"]
    validate_event(events[0])
    assert events[0]["manifest_hash"] == cli.load_manifest(manifest_path)["_manifest_hash"]
    assert events[0]["harness_sha"].startswith("352da9b")
    out = capsys.readouterr().out
    assert "initialized" in out


class ScriptedAdapter:
    def complete(self, system, messages, tools, params):
        return AdapterResponse(
            text_blocks=(),
            tool_calls=(ToolCall(id="t-end", name="end_session", args={"reason": "done"}),),
            stop_reason=StopReason.TOOL_USE,
            usage=Usage(input_tokens=100, output_tokens=10),
        )


def test_run_session_command_end_to_end(tmp_path, manifest_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    (tmp_path / "run" / "reference").mkdir(parents=True)
    (tmp_path / "run" / "reference" / "gdd.md").write_text("lore")
    cli.main(
        ["init", "--manifest", str(manifest_path), "--run-dir", str(run_dir), "--skip-connectivity"]
    )
    monkeypatch.setattr(cli, "build_adapter", lambda manifest: ScriptedAdapter())
    rc = cli.main(["run-session", "--run-dir", str(run_dir), "--manual"])
    assert rc == 0
    assert "session_ran" in capsys.readouterr().out
    events = [e["event"] for e in read_events(run_dir / "telemetry.jsonl")]
    assert events == [
        "run_start",
        "session_start",
        "llm_call",
        "tool_call",
        "session_end",
        "schedule_next",
    ]


def test_status_prints_state(tmp_path, manifest_path, capsys):
    run_dir = tmp_path / "run"
    cli.main(
        ["init", "--manifest", str(manifest_path), "--run-dir", str(run_dir), "--skip-connectivity"]
    )
    capsys.readouterr()  # drop init output
    rc = cli.main(["status", "--run-dir", str(run_dir)])
    assert rc == 0
    status = json.loads(capsys.readouterr().out)
    assert status["session_counter"] == 0
    assert status["run_status"] == "active"


def test_load_env_file_existing_env_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("A_KEY=from_file\n# comment\nB_KEY=b\n")
    monkeypatch.setenv("A_KEY", "from_env")
    monkeypatch.delenv("B_KEY", raising=False)
    cli.load_env_file(env)
    import os

    assert os.environ["A_KEY"] == "from_env"
    assert os.environ["B_KEY"] == "b"
    monkeypatch.delenv("B_KEY")
