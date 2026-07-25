from __future__ import annotations

import asyncio

import pytest

from sloperator.agents import (
    ActiveAgentRun,
    AgentSteeringInterrupt,
    SubmitResult,
    _run_process,
)


def test_submit_result_is_a_string_enum() -> None:
    assert SubmitResult.STEERED == "steered"


async def test_claude_control_interrupts_process_for_steering() -> None:
    control = ActiveAgentRun("claude")
    running = asyncio.create_task(
        _run_process(
            ["/bin/sleep", "30"],
            cwd="/tmp",
            timeout_seconds=60,
            control=control,
        )
    )
    for _ in range(100):
        if control.process is not None:
            break
        await asyncio.sleep(0.01)

    assert await control.steer("Use the corrected input")
    with pytest.raises(AgentSteeringInterrupt):
        await running
    assert control.take_claude_steering() == ["Use the corrected input"]
