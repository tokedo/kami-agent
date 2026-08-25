"""What the provider said: error fields and the on-disk artifact (SPEC P8, P9, P14).

The failure this closes: two runs in a row, the first question after an
incident — "what did the provider say?" — was unanswerable. Fourteen
hours of run-wide provider outage produced zero diagnosable bytes, and
150+ error rows carried a request id and nothing else.
"""

import json

import anthropic
import httpx
import openai
import pytest
from google.genai import errors as genai_errors

from kami_agent.adapters.anthropic import _classify_error as classify_anthropic
from kami_agent.adapters.base import (
    ERROR_TEXT_LOG_CHARS,
    ERROR_TEXT_TELEMETRY_CHARS,
    AdapterError,
    AdapterResponse,
    SamplingParams,
    StopReason,
    ToolCall,
    Usage,
)
from kami_agent.adapters.google import _classify_error as classify_google
from kami_agent.adapters.openai import _classify_error as classify_openai
from kami_agent.errorlog import ERRORS_FILENAME, ErrorLog
from kami_agent.governor import PriceTable
from kami_agent.loop import AgentLoop, LoopCaps
from kami_agent.telemetry import TelemetryWriter, read_events, validate_event
from kami_agent.tools.scaffold import ScaffoldTools

PRICES = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
PARAMS = SamplingParams(max_tokens=4096)


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "workspace").mkdir()
    return tmp_path


# --- adapters: the provider's own words, not the SDK's wrapper ----------------


def _response(status, body):
    request = httpx.Request("POST", "https://api.example/v1/messages")
    return httpx.Response(status, request=request, json=body)


def test_anthropic_carries_the_type_and_the_message_without_the_body_echo():
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    exc = anthropic.RateLimitError(
        "Error code: 429 - {...}", response=_response(429, body), body=body
    )
    error = classify_anthropic(exc)
    assert (error.status_code, error.error_type, error.error_text) == (
        429,
        "rate_limit_error",
        "slow down",
    )
    assert error.retryable is True


def test_openai_carries_the_quota_type_the_incident_needed():
    inner = {
        "message": "You exceeded your current quota.",
        "type": "insufficient_quota",
        "code": "insufficient_quota",
    }
    exc = openai.RateLimitError(
        "Error code: 429 - {...}", response=_response(429, {"error": inner}), body=inner
    )
    error = classify_openai(exc)
    assert (error.status_code, error.error_type, error.error_text) == (
        429,
        "insufficient_quota",
        "You exceeded your current quota.",
    )


def test_google_uses_its_canonical_status_as_the_type():
    exc = genai_errors.APIError(
        429, {"error": {"code": 429, "message": "quota exhausted", "status": "RESOURCE_EXHAUSTED"}}
    )
    error = classify_google(exc)
    assert (error.status_code, error.error_type, error.error_text) == (
        429,
        "RESOURCE_EXHAUSTED",
        "quota exhausted",
    )


@pytest.mark.parametrize(
    ("classify", "exc"),
    [
        (
            classify_anthropic,
            anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")),
        ),
        (classify_openai, openai.APIConnectionError(request=httpx.Request("POST", "https://x"))),
    ],
)
def test_a_transport_failure_has_no_provider_type_and_says_so(classify, exc):
    """Null is the honest answer: no provider answer existed to have a type."""
    error = classify(exc)
    assert error.error_type is None
    assert error.error_status is None if hasattr(error, "error_status") else True
    assert error.status_code is None
    # The transport's own text still survives — it is all there is.
    assert error.error_text and "ConnectionError" in error.error_text
    assert error.retryable is True


def test_the_two_cuts_differ_because_the_two_readers_do():
    long_message = "x" * 10_000
    error = AdapterError("boom", retryable=False, error_text=long_message)
    assert len(error.text_for_telemetry()) == ERROR_TEXT_TELEMETRY_CHARS
    assert len(error.text_for_log()) == ERROR_TEXT_LOG_CHARS
    assert ERROR_TEXT_TELEMETRY_CHARS < ERROR_TEXT_LOG_CHARS


# --- the loop: telemetry row + artifact ---------------------------------------


class FailingAdapter:
    def __init__(self, error):
        self._error = error

    def complete(self, system, messages, tools, params):
        raise self._error


class ExplodingAdapter:
    def complete(self, system, messages, tools, params):
        raise ValueError("a shape the adapter never expected")


def make_loop(run_dir, adapter, *, error_log=None):
    return AgentLoop(
        adapter=adapter,
        model="test-model",
        system="s",
        kickoff_text="Session start.",
        continuation_text="Continue.",
        scaffold=ScaffoldTools(run_dir, session_number=1),
        game=None,
        telemetry=TelemetryWriter(run_dir / "telemetry.jsonl", run_id="r"),
        session=1,
        params=PARAMS,
        prices=PRICES,
        caps=LoopCaps(session_token_cap=100_000, retry_max_attempts=0),
        error_log=error_log,
        sleep=lambda s: None,
    )


def llm_rows(run_dir):
    return [e for e in read_events(run_dir / "telemetry.jsonl") if e["event"] == "llm_call"]


def test_a_failed_call_records_what_the_provider_said(run_dir):
    error = AdapterError(
        "openai API error 429",
        retryable=False,
        status_code=429,
        request_id="req_abc",
        error_type="insufficient_quota",
        error_text="You exceeded your current quota.",
    )
    result = make_loop(run_dir, FailingAdapter(error)).run()
    assert result.reason == "errors"
    row = llm_rows(run_dir)[0]
    validate_event(row)
    assert row["error_status"] == 429
    assert row["error_type"] == "insufficient_quota"
    assert row["error_text"] == "You exceeded your current quota."
    assert row["provider_request_id"] == "req_abc"


def test_the_three_fields_are_present_as_nulls_when_the_provider_served_nothing(run_dir):
    """The deliberate break from omit-when-absent (recorded beside X18).

    During an outage the reader has to tell "the provider sent no status"
    from "this row predates the field". Omission collapses those two.
    """
    error = AdapterError("connection reset", retryable=False, error_text="ConnectionError: reset")
    make_loop(run_dir, FailingAdapter(error)).run()
    row = llm_rows(run_dir)[0]
    validate_event(row)
    assert row["error_status"] is None
    assert row["error_type"] is None
    assert row["error_text"] == "ConnectionError: reset"


def test_a_successful_call_carries_none_of_them(run_dir):
    class Fine:
        def complete(self, system, messages, tools, params):
            return AdapterResponse(
                text_blocks=(),
                tool_calls=(ToolCall(id="e", name="end_session", args={"reason": "done"}),),
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(input_tokens=10, output_tokens=5),
            )

    make_loop(run_dir, Fine()).run()
    row = llm_rows(run_dir)[0]
    assert "error_status" not in row
    assert "error_type" not in row
    assert "error_text" not in row


def test_an_unnormalized_fault_keeps_its_class_out_of_the_type_field(run_dir):
    """One vocabulary per field: error_type holds the PROVIDER's token."""
    make_loop(run_dir, ExplodingAdapter()).run()
    row = llm_rows(run_dir)[0]
    validate_event(row)
    assert row["error_status"] is None
    assert row["error_type"] is None
    assert row["error_text"].startswith("ValueError: a shape the adapter never expected")


def test_the_artifact_is_written_at_the_moment_of_the_error(run_dir):
    log = ErrorLog(run_dir / ERRORS_FILENAME, run_id="r")
    error = AdapterError(
        "boom",
        retryable=True,
        status_code=503,
        request_id="req_9",
        error_type="overloaded_error",
        error_text="upstream is overloaded, " + "detail " * 200,
    )
    make_loop(run_dir, FailingAdapter(error), error_log=log).run()
    log.close()
    lines = (run_dir / ERRORS_FILENAME).read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["error_status"] == 503
    assert record["error_type"] == "overloaded_error"
    assert record["request_id"] == "req_9"
    assert record["retryable"] is True
    assert record["session"] == 1
    assert record["run_id"] == "r"
    assert record["model"] == "test-model"
    # The artifact keeps the LONG cut: it is read one incident at a time.
    assert len(record["error_text"]) > ERROR_TEXT_TELEMETRY_CHARS


def test_every_retry_leaves_its_own_row(run_dir):
    """A retry storm must be countable after the fact, not inferred."""
    log = ErrorLog(run_dir / ERRORS_FILENAME, run_id="r")
    error = AdapterError("429", retryable=True, status_code=429, error_type="rate_limit_error")
    loop = AgentLoop(
        adapter=FailingAdapter(error),
        model="m",
        system="s",
        kickoff_text="Session start.",
        continuation_text="Continue.",
        scaffold=ScaffoldTools(run_dir, session_number=1),
        game=None,
        telemetry=TelemetryWriter(run_dir / "telemetry.jsonl", run_id="r"),
        session=1,
        params=PARAMS,
        prices=PRICES,
        caps=LoopCaps(session_token_cap=100_000, retry_max_attempts=3),
        error_log=log,
        sleep=lambda s: None,
    )
    loop.run()
    log.close()
    records = (run_dir / ERRORS_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(records) == 4  # the initial attempt plus three retries
    assert [json.loads(r)["attempt"] for r in records] == [0, 1, 2, 3]


def test_the_artifact_never_raises_into_the_session(run_dir):
    """A diagnostic that can end a session is not a diagnostic."""
    log = ErrorLog(run_dir / ERRORS_FILENAME, run_id="r")
    log.close()  # the file is gone from under it
    log.record(session=1, error_text="anything")  # must not raise

    unwritable = ErrorLog(run_dir / "no" / "such" / "dir" / "errors.jsonl", run_id="r")
    unwritable.record(session=1, error_text="anything")
    unwritable.close()


def test_the_artifact_is_not_reachable_by_any_agent_path(run_dir):
    """It lives in the run directory, which P11 puts out of reach."""
    from kami_agent.tools.errors import ToolError

    (run_dir / ERRORS_FILENAME).write_text('{"error_text": "secret"}\n', encoding="utf-8")
    tools = ScaffoldTools(run_dir)
    with pytest.raises(ToolError):
        tools.execute("workspace_read", {"path": ERRORS_FILENAME})
    with pytest.raises(ToolError):
        tools.execute("workspace_read", {"path": f"../{ERRORS_FILENAME}"})
    assert ERRORS_FILENAME not in tools.execute("workspace_list", {})


def test_the_file_appears_only_when_something_failed(run_dir):
    """Its existence is the answer to "did anything go wrong?"."""
    log = ErrorLog(run_dir / ERRORS_FILENAME, run_id="r")
    assert not (run_dir / ERRORS_FILENAME).exists()
    log.close()
    assert not (run_dir / ERRORS_FILENAME).exists()

    log = ErrorLog(run_dir / ERRORS_FILENAME, run_id="r")
    log.record(session=1, error_status=500, error_text="upstream failed")
    log.close()
    assert (run_dir / ERRORS_FILENAME).exists()
