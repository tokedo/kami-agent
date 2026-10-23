"""The OpenAI-compatible path as a tested path (SPEC D2, D3, P7.2; 0.8.0).

`provider: openai` reaches any endpoint that speaks the Chat Completions
API: the manifest's `openai.base_url` (else `OPENAI_BASE_URL`) and
`openai.extra_headers` go to the client as given. A router such as
OpenRouter serves its usage in OpenAI's shape — cached prompt tokens in
`prompt_tokens_details.cached_tokens` — so the same normalization and the
same cache-aware cost apply. Offline, against a recorded OpenRouter-shaped
response.
"""

import json
from pathlib import Path

import httpx
import pytest
from openai.types.chat import ChatCompletion

from kami_agent import cli
from kami_agent.adapters.base import SamplingParams, ToolDef, UserMessage
from kami_agent.adapters.openai import OpenAIAdapter
from kami_agent.governor import PriceTable, cost_usd

FIXTURE = Path(__file__).parent / "fixtures" / "openai" / "openrouter_tool_call_cached.json"
ROUTER = "https://router.example/api/v1"
HEADERS = {"HTTP-Referer": "https://bench.example", "X-Title": "kami-agent"}
TOOLS = [
    ToolDef(
        name="workspace_read",
        description="Read a file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
]


def manifest(**openai_block):
    base = {"provider": "openai", "model": "anthropic/claude-sonnet-4.5"}
    if openai_block:
        base["openai"] = openai_block
    return base


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


def test_the_manifest_names_the_endpoint_and_its_headers():
    adapter = cli.build_adapter(manifest(base_url=ROUTER, extra_headers=HEADERS))
    assert str(adapter._client.base_url) == ROUTER + "/"
    for name, value in HEADERS.items():
        assert adapter._client.default_headers[name] == value


def test_the_environment_names_the_endpoint_when_the_manifest_does_not(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", ROUTER)
    assert str(cli.build_adapter(manifest())._client.base_url) == ROUTER + "/"
    assert cli.openai_endpoint(manifest()) == (ROUTER, None)


def test_without_either_the_sdk_default_endpoint_stands():
    assert cli.openai_endpoint(manifest()) == (None, None)


@pytest.mark.parametrize("bad", [{"X-Title": 7}, ["X-Title"], "X-Title: x"])
def test_headers_must_be_strings(bad):
    with pytest.raises(SystemExit, match="extra_headers"):
        cli.openai_endpoint(manifest(extra_headers=bad))


def test_a_routed_call_end_to_end_offline():
    """The request goes to the router with the headers; the recorded answer
    normalizes to one tool call and cached tokens as the read component."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=json.loads(FIXTURE.read_text(encoding="utf-8")))

    adapter = OpenAIAdapter(
        "anthropic/claude-sonnet-4.5",
        api_key="test-key",
        base_url=ROUTER,
        default_headers=HEADERS,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    response = adapter.complete(
        "system", [UserMessage(text="go")], TOOLS, SamplingParams(max_tokens=512)
    )
    assert seen["url"] == ROUTER + "/chat/completions"
    assert seen["headers"]["x-title"] == "kami-agent"
    assert seen["headers"]["http-referer"] == "https://bench.example"
    assert seen["body"]["model"] == "anthropic/claude-sonnet-4.5"
    (call,) = response.tool_calls
    assert (call.name, call.args) == ("workspace_read", {"path": "plan.md"})
    assert response.usage.input_tokens == 15234
    assert response.usage.cache_read_tokens == 14080
    assert response.usage.cache_write_tokens == 0
    assert response.usage.output_tokens == 96


def test_cached_tokens_are_billed_at_the_cache_rate():
    """P7.2 on the routed path: the manifest's cache_read rate applies to the
    cached component, the input rate to the rest."""
    completion = ChatCompletion.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))

    class Client:
        class chat:  # noqa: N801 - the SDK's attribute shape
            class completions:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    return completion

    usage = (
        OpenAIAdapter("m", client=Client).complete("s", [], [], SamplingParams(max_tokens=8)).usage
    )
    prices = PriceTable(
        input_usd_per_mtok=3.0, output_usd_per_mtok=15.0, cache_read_usd_per_mtok=0.3
    )
    expected = ((15234 - 14080) * 3.0 + 14080 * 0.3 + 96 * 15.0) / 1_000_000
    assert cost_usd(usage, prices) == pytest.approx(expected)
