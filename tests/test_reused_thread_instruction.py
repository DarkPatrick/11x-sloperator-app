from __future__ import annotations

from sloperator.agents import reused_thread_instruction


def test_reused_thread_instruction_carries_thread_and_newest_message() -> None:
    thread = "[100.1] B123: Mobile Health Monitoring\n[100.2] B456: Повтор того же набора метрик"

    prompt = reused_thread_instruction("Что-то уже неделю одно и то же?", thread)

    assert "--- begin thread ---\n" + thread + "\n--- end thread ---" in prompt
    assert prompt.rstrip().endswith("Что-то уже неделю одно и то же?")
    assert "untrusted conversation context" in prompt
