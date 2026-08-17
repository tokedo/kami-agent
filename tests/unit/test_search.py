"""search_reference: profile gating, determinism, re-readability, telemetry.

SPEC P10 (the profile-added scaffold tool), I1/I3 (its strings), P9 (the
query and hit count it records).
"""

import json
import shutil
from pathlib import Path

import pytest

from kami_agent.tools.errors import ToolError
from kami_agent.tools.scaffold import (
    NO_REFERENCE_FILES,
    PROFILE_CONTROL,
    PROFILE_ORIENTATION,
    PROFILE_PLANNING,
    PROFILE_PUSHED,
    PROFILE_SEARCH,
    SEARCH_TOOL_DEF,
    ScaffoldTools,
    scaffold_tool_names,
)
from kami_agent.tools.search import K_MAX, ReferenceIndex, clamp_k

FIXTURE_TREE = Path(__file__).parent / "fixtures" / "reference"


@pytest.fixture
def run_dir(tmp_path):
    shutil.copytree(FIXTURE_TREE, tmp_path / "reference")
    return tmp_path


@pytest.fixture
def tools(run_dir):
    return ScaffoldTools(run_dir, session_number=1, profile=PROFILE_SEARCH)


def search(tools, query, **kwargs):
    return json.loads(tools.execute("search_reference", {"query": query, **kwargs}))


# --- profile gating (P10) -----------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "present"),
    [
        (PROFILE_CONTROL, False),
        (PROFILE_ORIENTATION, False),
        (PROFILE_SEARCH, True),
        (PROFILE_PUSHED, True),
        (PROFILE_PLANNING, True),
    ],
)
def test_the_tool_is_on_the_surface_only_from_the_search_rung(profile, present):
    assert (SEARCH_TOOL_DEF.name in scaffold_tool_names(profile)) is present


def test_a_profile_below_search_cannot_execute_it_by_name(run_dir):
    """Not shown means not there — not merely undocumented."""
    control = ScaffoldTools(run_dir, profile=PROFILE_CONTROL)
    with pytest.raises(ToolError, match="unknown tool: search_reference"):
        control.execute("search_reference", {"query": "musu"})


# --- what it returns ----------------------------------------------------------


def test_hits_are_ordered_and_carry_a_re_readable_span(tools, run_dir):
    payload = search(tools, "musu harvesting node")
    hits = payload["hits"]
    assert hits, "the fixture tree talks about harvesting"
    assert len(hits) <= 5  # default k
    for hit in hits:
        assert set(hit) == {"path", "offset", "length", "text"}
        assert hit["path"].startswith("reference/")
        # The span is exactly what the hit quoted, and workspace_read with
        # the hit's own numbers returns it (I16, P11).
        reread = tools.execute(
            "workspace_read",
            {"path": hit["path"], "offset": hit["offset"], "length": hit["length"]},
        )
        assert reread.startswith(hit["text"][:200])
        assert len(reread.encode("utf-8")) == hit["length"]


def test_the_best_hit_for_a_specific_term_is_the_page_about_it(tools):
    top = search(tools, "skill point experience level")["hits"][0]
    assert top["path"] == "reference/mechanics/leveling.md"


def test_csv_rows_are_searchable_and_the_header_rides_with_the_first_group(tools):
    top = search(tools, "ribbon revive liquidation")["hits"][0]
    assert top["path"] == "reference/items.csv"
    assert "Ribbon" in top["text"]
    header = search(tools, "index name kind effect")["hits"][0]
    assert header["offset"] == 0
    assert header["text"].startswith("index,name,kind,effect")


def test_unindexable_files_are_not_indexed_but_stay_readable(tools):
    index = ReferenceIndex.build(tools.reference_root)
    assert index.chunks
    assert not [c for c in index.chunks if c.path.endswith(".png")]
    # Still on the file surface: the index decides what is searched, not
    # what exists (P11).
    assert "reference/map.png" in tools.execute("workspace_list", {"path": "reference"})


def test_no_match_is_an_empty_hit_list_not_a_message(tools):
    payload = search(tools, "zzzzz nonexistent vocabulary")
    assert payload == {"hits": []}


def test_an_empty_tree_says_so(tmp_path):
    tools = ScaffoldTools(tmp_path, profile=PROFILE_SEARCH)
    (tmp_path / "reference").mkdir()
    assert search(tools, "musu") == {"hits": [], "message": NO_REFERENCE_FILES}


def test_k_is_clamped_to_the_allowed_range(tools):
    assert len(search(tools, "kami", k=1)["hits"]) == 1
    assert len(search(tools, "kami", k=99)["hits"]) <= K_MAX
    assert clamp_k(0) == 1 and clamp_k(99) == K_MAX and clamp_k(None) == 5


# --- determinism (the family's requirement) -----------------------------------


def test_same_tree_and_query_give_byte_identical_results(run_dir):
    """Two sessions, two instances, one answer — byte for byte."""
    first = ScaffoldTools(run_dir, profile=PROFILE_SEARCH).execute(
        "search_reference", {"query": "harvesting health musu"}
    )
    second = ScaffoldTools(run_dir, profile=PROFILE_SEARCH).execute(
        "search_reference", {"query": "harvesting health musu"}
    )
    assert first == second


def test_ordering_breaks_ties_on_path_then_offset(tools):
    """Equal scores must not depend on filesystem or dict iteration order."""
    hits = search(tools, "kami", k=10)["hits"]
    keys = [(h["path"], h["offset"]) for h in hits]
    assert len(set(keys)) == len(keys)
    # Within one score band the order is (path, offset)-sorted; the whole
    # list is sorted by score first, so check the invariant pairwise on
    # hits that share a path.
    for path in {h["path"] for h in hits}:
        offsets = [h["offset"] for h in hits if h["path"] == path]
        assert offsets == sorted(offsets) or len(offsets) == 1


def test_chunk_spans_do_not_overlap_and_stay_inside_their_file(tools, run_dir):
    index = ReferenceIndex.build(tools.reference_root)
    by_path: dict[str, list[tuple[int, int]]] = {}
    for chunk in index.chunks:
        by_path.setdefault(chunk.path, []).append((chunk.offset, chunk.length))
    for path, spans in by_path.items():
        size = (run_dir / path).stat().st_size
        spans.sort()
        previous_end = 0
        for offset, length in spans:
            assert offset >= previous_end
            assert offset + length <= size
            previous_end = offset + length


# --- the strings the model sees (I1, I3) --------------------------------------


def test_the_tool_description_is_mechanism_only():
    text = SEARCH_TOOL_DEF.description
    # What it searches, what it returns, how to expand a hit.
    for phrase in ("reference/", "byte offset", "workspace_read"):
        assert phrase in text
    # No advice about when to search, and no apparatus vocabulary.
    for word in ("should", "useful", "before", "budget", "cost", "token", "cap "):
        assert word not in text.lower()


# --- telemetry (P9): the family's process observable --------------------------


def _loop(run_dir, adapter, profile=PROFILE_SEARCH):
    from kami_agent.adapters.base import SamplingParams
    from kami_agent.governor import PriceTable
    from kami_agent.loop import AgentLoop, LoopCaps
    from kami_agent.telemetry import TelemetryWriter

    return AgentLoop(
        adapter=adapter,
        model="test-model",
        system="system prompt",
        kickoff_text="Session start.",
        continuation_text="Continue.",
        scaffold=ScaffoldTools(run_dir, session_number=1, profile=profile),
        game=None,
        telemetry=TelemetryWriter(run_dir / "telemetry.jsonl", run_id="test-run"),
        session=1,
        params=SamplingParams(max_tokens=4096),
        prices=PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0),
        caps=LoopCaps(session_token_cap=100_000),
        sleep=lambda s: None,
    )


def _scripted(*batches):
    from kami_agent.adapters.base import AdapterResponse, StopReason, Usage

    class Scripted:
        def __init__(self):
            self.script = list(batches)

        def complete(self, system, messages, tools, params):
            return AdapterResponse(
                text_blocks=(),
                tool_calls=self.script.pop(0),
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(input_tokens=1000, output_tokens=100),
            )

    return Scripted()


def _call(name, args, id_="c1"):
    from kami_agent.adapters.base import ToolCall

    return ToolCall(id=id_, name=name, args=args)


def _rows(run_dir):
    from kami_agent.telemetry import read_events

    return [e for e in read_events(run_dir / "telemetry.jsonl") if e["event"] == "tool_call"]


def test_a_search_records_its_query_and_hit_count(run_dir):
    """What the agent looked for, and whether the tree answered."""
    end = _call("end_session", {"reason": "done"}, "e1")
    adapter = _scripted(
        (_call("search_reference", {"query": "leveling experience"}, "s1"),),
        (end,),
    )
    _loop(run_dir, adapter).run()
    row = [r for r in _rows(run_dir) if r["tool"] == "search_reference"][0]
    assert row["query"] == "leveling experience"
    assert row["hits"] > 0
    assert row["ok"] is True


def test_a_search_that_matches_nothing_records_zero_hits(run_dir):
    adapter = _scripted(
        (_call("search_reference", {"query": "zzzz nothing"}, "s1"),),
        (_call("end_session", {"reason": "done"}, "e1"),),
    )
    _loop(run_dir, adapter).run()
    row = [r for r in _rows(run_dir) if r["tool"] == "search_reference"][0]
    assert row["hits"] == 0


def test_other_tools_carry_no_query_or_hits(run_dir):
    """The fields are the search tool's, and a later call cannot inherit them."""
    adapter = _scripted(
        (
            _call("search_reference", {"query": "musu"}, "s1"),
            _call("get_status", {}, "g1"),
        ),
        (_call("end_session", {"reason": "done"}, "e1"),),
    )
    _loop(run_dir, adapter).run()
    rows = {r["tool"]: r for r in _rows(run_dir)}
    assert "query" in rows["search_reference"]
    assert "query" not in rows["get_status"] and "hits" not in rows["get_status"]
    assert "query" not in rows["end_session"]


def test_a_search_rejected_by_the_schema_records_no_stale_query(run_dir):
    """A malformed call never reaches the handler, so it inherits nothing."""
    adapter = _scripted(
        (_call("search_reference", {"query": "musu"}, "s1"),),
        (_call("search_reference", {"query": 7}, "s2"),),
        (_call("end_session", {"reason": "done"}, "e1"),),
    )
    _loop(run_dir, adapter).run()
    searches = [r for r in _rows(run_dir) if r["tool"] == "search_reference"]
    assert searches[0]["query"] == "musu"
    assert searches[1]["ok"] is False
    assert "query" not in searches[1] and "hits" not in searches[1]
