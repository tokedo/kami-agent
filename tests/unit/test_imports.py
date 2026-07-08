"""Every module in the package imports cleanly."""

import importlib

import pytest

MODULES = [
    "kami_agent",
    "kami_agent.cli",
    "kami_agent.runner",
    "kami_agent.loop",
    "kami_agent.adapters",
    "kami_agent.adapters.base",
    "kami_agent.adapters.anthropic",
    "kami_agent.adapters.openai",
    "kami_agent.adapters.google",
    "kami_agent.harness",
    "kami_agent.tools",
    "kami_agent.tools.errors",
    "kami_agent.tools.sandbox",
    "kami_agent.tools.scaffold",
    "kami_agent.tools.truncation",
    "kami_agent.governor",
    "kami_agent.telemetry",
    "kami_agent.state",
    "kami_agent.supervisor",
]


@pytest.mark.parametrize("module", MODULES)
def test_imports(module: str) -> None:
    importlib.import_module(module)
