from pathlib import Path

import pytest

from gcae.context import ContextBuilder
from gcae.memory import EventLog, MemoryStore
from gcae.models import AgentState, Event, MemoryRecord, SemanticStep


def state() -> AgentState:
    return AgentState(
        run_id="r",
        source_repo="/s",
        worktree="/w",
        branch="b",
        objective="python",
        original_request="python",
    )


def test_memory_immutable_and_fts(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    record = store.add(
        MemoryRecord(
            kind="failure",
            content="python implementation failed",
            run_id="r",
            immutable=True,
        )
    )
    assert store.search("python", 1)[0].record.id == record.id
    with pytest.raises(ValueError):
        store.update(record.id or 0, "changed")
    store.close()


def test_context_pinned_records_survive_budget(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    record = store.add(
        MemoryRecord(
            kind="failure",
            content="critical failure lesson",
            run_id="r",
            immutable=True,
        )
    )
    context = ContextBuilder(store).build(state(), SemanticStep(id="s", goal="g"), budget=250)
    assert record.id in context.pinned_ids
    assert "critical failure lesson" in context.text


def test_pinned_context_is_not_truncated_when_over_budget(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    content = "critical failure lesson " * 100
    record = store.add(
        MemoryRecord(
            kind="failure",
            content=content,
            run_id="r",
            immutable=True,
        )
    )
    context = ContextBuilder(store).build(state(), budget=32)
    assert record.id in context.pinned_ids
    assert content in context.text


def test_fts_query_handles_punctuation(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    record = store.add(
        MemoryRecord(
            kind="fact",
            content="parser handles request/response JSON safely",
            run_id="r",
        )
    )
    assert store.search("request/response", 1)[0].record.id == record.id


def test_event_log(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    EventLog(path).append(Event(run_id="r", event_type="started"))
    assert path.read_text().count("started") == 1


def test_memory_update_refreshes_fts(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    record = store.add(MemoryRecord(kind="fact", content="alpha failure", run_id="r"))
    store.update(record.id or 0, "beta lesson")
    assert store.search("alpha", 5) == []
    found = store.search("beta", 5)
    assert found and found[0].record.content == "beta lesson"
    store.close()
