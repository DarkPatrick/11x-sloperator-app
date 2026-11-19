from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _aix_not_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep finalizer prompts independent of the live admin pause (automation-controls.json)."""
    monkeypatch.setattr("sloperator.experiment_finalizer.aix_paused", lambda settings: False)
