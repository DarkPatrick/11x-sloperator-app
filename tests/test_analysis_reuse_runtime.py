from __future__ import annotations

from sloperator.store import EventStore


def test_completed_automated_analysis_is_found_by_exact_recent_key(tmp_path) -> None:
    store = EventStore(tmp_path / "store.sqlite3")
    store.initialize()
    store.save_durable_agent_run(
        "CCHANNEL",
        "101.1:web-health-analysis",
        "101.1",
        "prompt",
        {"automated": True, "reuse_key": "web:card+metric"},
    )
    store.set_durable_agent_run_status(
        "CCHANNEL", "101.1:web-health-analysis", "completed"
    )

    assert (
        store.find_recent_completed_analysis("CCHANNEL", "web:card+metric")
        == "101.1"
    )
    assert store.find_recent_completed_analysis("CCHANNEL", "web:other") is None
