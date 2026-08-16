from sloperator.automation_controls import AutomationControls


def test_controls_persist_enable_state(tmp_path) -> None:
    path = tmp_path / "controls.json"
    controls = AutomationControls(path)
    controls.set_enabled("triggers", "mobile-health", False)
    assert AutomationControls(path).disabled("triggers", "mobile-health")
    controls.set_enabled("triggers", "mobile-health", True)
    assert not AutomationControls(path).disabled("triggers", "mobile-health")
